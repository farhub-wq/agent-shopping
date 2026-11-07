"""模型接入层：默认使用离线确定性规划器，配置了 OpenAI 兼容端点后自动切换。

离线规划器不是玩具——它保证没有 API Key 时整套演示、测试与回归仍可跑通，
也让「模型是否真的在决策」这件事在对比实验中可被分离。
"""

import json
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import httpx
from shop_common.config import get_settings
from shop_common.logging import get_logger
from shop_common.metrics import LLM_CIRCUIT_OPEN, LLM_FALLBACK

from .circuit import CircuitBreaker

logger = get_logger(__name__)

SKU_PATTERN = re.compile(r"SKU-\d{4}")


@dataclass
class Decision:
    kind: str  # final | tool | delta（delta 为 final 答案的 token 分片）
    content: str = ""
    tool: str = ""
    args: dict[str, Any] = field(default_factory=dict)
    usage: dict[str, Any] = field(default_factory=dict)
    model: str = ""  # 产出该决策的模型名，供证据卡审计降级/熔断来源


class LLMProvider:
    name = "base"

    async def decide(
        self, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> Decision:
        raise NotImplementedError

    async def decide_stream(
        self, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> AsyncIterator[Decision]:
        # 默认实现：退回非流式，只产出一帧 terminal；流式序列约定以 0..n 个 delta 开头，
        # 恰好一帧 terminal（final|tool）收尾。
        yield await self.decide(system, messages, tools)


class DeterministicPlanner(LLMProvider):
    """基于关键词与工具结果的确定性编排，用于离线演示与回归测试。"""

    name = "deterministic-v1"
    # 分块步长：离线规划器瞬间出全文，按固定字符数切块模拟逐 token 流式，供默认演示打字机效果。
    CHUNK = 8

    async def decide(
        self, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> Decision:
        available = {item["name"] for item in tools}
        last_user = ""
        for message in reversed(messages):
            if message["role"] == "user":
                last_user = str(message["content"])
                break
        tool_messages = [item for item in messages if item["role"] == "tool"]
        if tool_messages:
            # 只要已经拿到过工具结果就直接收束成最终回答，避免离线规划器反复调用同一工具。
            return Decision(kind="final", content=self._compose(tool_messages))

        sku = SKU_PATTERN.search(last_user)
        text = last_user

        def choose(candidates: list[tuple[str, dict[str, Any]]]) -> Decision:
            # 只从技能实际暴露的工具里挑，候选顺序表达「首选 / 退路」；
            # 全都不可用时明确说明能力缺失，而不是编造一个不存在的工具名。
            for name, args in candidates:
                if name in available:
                    return Decision(kind="tool", tool=name, args=args)
            return Decision(kind="final", content="当前技能没有可用的工具来处理这个请求。")

        if any(word in text for word in ("退款", "退货", "售后", "换货")):
            args = {"reason": "quality_issue" if "质量" in text else "size_issue", "description": text}
            if sku:
                args["sku"] = sku.group(0)
            if "订单" in text:
                match = re.search(r"(SO\d{8}\d{6}|\b[0-9a-f]{8}-[0-9a-f-]{27}\b)", text)
                if match:
                    args["order_id"] = match.group(0)
            return choose([("after_sales_draft", args), ("order_lookup", {})])

        if any(word in text for word in ("订单", "物流", "发货", "到哪")):
            match = re.search(r"(SO\d{8}\d{6}|\b[0-9a-f]{8}-[0-9a-f-]{27}\b)", text)
            return choose([("order_lookup", {"order_id": match.group(0) if match else ""})])

        if any(word in text for word in ("库存", "有货", "还剩", "秒杀")):
            return choose([("inventory_check", {"sku": sku.group(0) if sku else "SKU-1001"})])

        if any(word in text for word in ("上架", "合规", "属性", "标题", "文案")):
            return choose([("catalog_validate", {"sku": sku.group(0) if sku else "SKU-1001"})])

        if any(word in text for word in ("售后", "审核", "工单", "风险")):
            return choose([("after_sales_queue", {"status": "pending"})])

        if any(word in text for word in ("推荐", "买", "选", "耳机", "跑鞋", "精华", "衬衫")):
            keyword = next(
                (word for word in ("耳机", "跑鞋", "精华", "衬衫", "枕头", "收纳") if word in text),
                "",
            )
            return choose([("catalog_search", {"q": keyword})])

        return Decision(
            kind="final",
            content=(
                "我可以帮你查商品库存、追踪订单、检查商品合规或起草售后建议。"
                "请告诉我想处理的商品 SKU 或订单号。"
            ),
        )

    async def decide_stream(
        self, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> AsyncIterator[Decision]:
        terminal = await self.decide(system, messages, tools)
        # 只有 final 且有正文才分块：tool 决策是离散动作，直接单帧 terminal，不产 delta。
        if terminal.kind != "final" or not terminal.content:
            yield terminal
            return
        for start in range(0, len(terminal.content), self.CHUNK):
            yield Decision(kind="delta", content=terminal.content[start : start + self.CHUNK])
        yield terminal

    def _compose(self, tool_messages: list[dict[str, Any]]) -> str:
        payloads: list[Any] = []
        for message in tool_messages:
            content = message.get("content")
            try:
                payloads.append(json.loads(content) if isinstance(content, str) else content)
            except (TypeError, ValueError):
                payloads.append(content)
        if len(payloads) == 1:
            data = payloads[0]
            # 工具返回非 ok 状态时必须如实转述失败，不能把错误包装成正常结论。
            if isinstance(data, dict) and data.get("status") not in {None, "ok"}:
                return f"操作未完成：{data.get('message', '未知原因')}"
            return self._render(data)
        return "已完成以下查询：\n" + "\n".join(f"- {self._render(item)}" for item in payloads)

    def _render(self, data: Any) -> str:
        if isinstance(data, dict):
            if data.get("items") and isinstance(data["items"], list):
                lines = []
                for item in data["items"][:5]:
                    if "title" in item:
                        lines.append(f"{item['title']}（{item.get('sku', '')}）¥{item.get('price_cents', 0) / 100:.2f}")
                    elif "ticket_no" in item:
                        lines.append(f"工单 {item['ticket_no']} {item.get('status')} 风险分 {item.get('risk_score', 0)}")
                    else:
                        lines.append(str(item))
                return "；".join(lines)
            if "available" in data:
                return f"商品 {data.get('sku')} 当前可售 {data['available']} 件"
            if "status" in data and "order_no" in data:
                return f"订单 {data['order_no']} 当前状态：{data['status']}"
            if "result" in data:
                return str(data["result"])[:400]
            return json.dumps(data, ensure_ascii=False)[:500]
        return str(data)[:500]


class OpenAICompatibleProvider(LLMProvider):
    name = "openai-compatible"

    def __init__(self, base_url: str, api_key: str, model: str, timeout: float = 15.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout

    def _payload(
        self, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]], stream: bool
    ) -> dict[str, Any]:
        # 流式与非流式共用同一份 payload，仅 stream 开关不同，避免两处构造漂移。
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, *messages],
            # 工具 schema 直接来自白名单，模型只能在这些名字里选，减少幻觉式调用。
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": item["name"],
                        "description": item["description"],
                        "parameters": item["input_schema"],
                    },
                }
                for item in tools
            ],
            "tool_choice": "auto",
            "temperature": 0.2,
        }
        if stream:
            payload["stream"] = True
        return payload

    async def decide(
        self, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> Decision:
        payload = self._payload(system, messages, tools, stream=False)
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(
                f"{self.base_url}/chat/completions",
                json=payload,
                headers={"Authorization": f"Bearer {self.api_key}"},
            )
            response.raise_for_status()
            body = response.json()
        message = body["choices"][0]["message"]
        usage = body.get("usage", {})
        calls = message.get("tool_calls") or []
        if calls:
            # 一次决策只执行第一个工具调用：运行时按步审计，避免并发副作用难以归因。
            call = calls[0]
            return Decision(
                kind="tool",
                tool=call["function"]["name"],
                args=json.loads(call["function"].get("arguments") or "{}"),
                usage=usage,
            )
        return Decision(kind="final", content=message.get("content") or "", usage=usage)

    async def decide_stream(
        self, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> AsyncIterator[Decision]:
        payload = self._payload(system, messages, tools, stream=True)
        content_parts: list[str] = []
        tool_name = ""
        tool_args = ""
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            async with client.stream(
                "POST",
                f"{self.base_url}/chat/completions",
                json=payload,
                headers={"Authorization": f"Bearer {self.api_key}"},
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[len("data:") :].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except ValueError:
                        # 偶发的非 JSON 心跳行直接跳过，不中断流。
                        continue
                    content_delta, name_delta, args_delta = _openai_stream_delta(chunk)
                    if content_delta:
                        content_parts.append(content_delta)
                        yield Decision(kind="delta", content=content_delta)
                    if name_delta:
                        tool_name = name_delta
                    if args_delta:
                        tool_args += args_delta
        if tool_name:
            # 流式工具调用把参数拆在多帧里，累积后一次性解析为 args。
            try:
                args = json.loads(tool_args or "{}")
            except ValueError:
                args = {}
            yield Decision(kind="tool", tool=tool_name, args=args, model=self.model)
            return
        yield Decision(kind="final", content="".join(content_parts), model=self.model)


def _openai_stream_delta(chunk: dict[str, Any]) -> tuple[str, str, str]:
    # 解析单个 SSE 数据块为 (content_delta, tool_name_delta, tool_args_delta)；
    # 兼容端点对 delta 字段命名与是否出现不保证一致，全部安全取值，缺失返回空串。
    choices = chunk.get("choices") or []
    if not choices:
        return "", "", ""
    delta = choices[0].get("delta") or {}
    content = delta.get("content") or ""
    tool_calls = delta.get("tool_calls") or []
    if tool_calls:
        function = tool_calls[0].get("function") or {}
        return content, function.get("name") or "", function.get("arguments") or ""
    return content, "", ""


# 双模型都不可用时的兜底话术：明确引导转人工，且必须是 final 决策，避免循环里反复试模型。
FALLBACK_MESSAGE = "抱歉，智能客服暂时不可用，请稍后重试或联系人工客服。"


class FailoverProvider(LLMProvider):
    """主模型优先，失败或熔断时降级到备用模型，双模型都失败时返回固定话术。

    主/降级模型调用属网络边界，异常在此被捕获并打点降级，而不是抛成 5xx 阻断客服。
    """

    name = "failover"

    def __init__(
        self,
        primary: LLMProvider,
        fallback: LLMProvider,
        breaker: CircuitBreaker,
        canned_message: str = FALLBACK_MESSAGE,
    ) -> None:
        self.primary = primary
        self.fallback = fallback
        self.breaker = breaker
        self.canned_message = canned_message

    async def decide(
        self, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> Decision:
        if self.breaker.allow_request():
            try:
                decision = await self.primary.decide(system, messages, tools)
            except Exception as exc:
                # 主模型失败：记录失败并降级；日志保留原始异常，便于区分网络故障与代码缺陷。
                self.breaker.record_failure()
                logger.warning("主模型调用失败，降级到备用模型：%s", exc)
            else:
                self.breaker.record_success()
                decision.model = self.primary.name
                return decision
        else:
            # 熔断中：不再对主模型发起请求，直接降级，避免 15s 超时白等。
            LLM_CIRCUIT_OPEN.labels("harness").inc()

        LLM_FALLBACK.labels("harness").inc()
        try:
            decision = await self.fallback.decide(system, messages, tools)
        except Exception as exc:
            logger.error("备用模型也调用失败，返回固定话术：%s", exc)
            return Decision(kind="final", content=self.canned_message, model="unavailable")
        decision.model = self.fallback.name
        return decision

    async def decide_stream(
        self, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> AsyncIterator[Decision]:
        # 流式降级只能在「首 token 前」：已流出内容无法撤回，中途失败只能收尾，
        # 不能再叠降级模型的输出，否则前端会看到两段拼在一起的答案。
        if self.breaker.allow_request():
            emitted = False
            try:
                async for frame in self.primary.decide_stream(system, messages, tools):
                    emitted = True
                    yield frame
            except Exception as exc:
                self.breaker.record_failure()
                logger.warning("主模型流式调用失败：%s", exc)
                if emitted:
                    # 收尾帧：让前端结束 busy，不回退降级模型。
                    yield Decision(kind="final", content="", model="unavailable")
                    return
            else:
                self.breaker.record_success()
                return
        else:
            # 熔断中：不碰主模型，直接走降级模型的流。
            LLM_CIRCUIT_OPEN.labels("harness").inc()

        LLM_FALLBACK.labels("harness").inc()
        try:
            async for frame in self.fallback.decide_stream(system, messages, tools):
                yield frame
        except Exception as exc:
            logger.error("备用模型流式调用也失败，返回固定话术：%s", exc)
            yield Decision(kind="final", content=self.canned_message, model="unavailable")


def build_provider() -> LLMProvider:
    settings = get_settings()
    # 主模型两者齐备才启用；缺任一配置都退回离线规划器，
    # 保证本地演示、CI 与回归测试不依赖外部网络和 API Key。
    if not (settings.llm_base_url and settings.llm_api_key):
        logger.info("未配置模型端点，使用离线确定性规划器")
        return DeterministicPlanner()

    primary = OpenAICompatibleProvider(
        settings.llm_base_url,
        settings.llm_api_key,
        settings.llm_model,
        timeout=settings.llm_timeout_seconds,
    )
    # 降级模型：配置了就用第二个 OpenAI 兼容端点，否则退回离线确定性规划器兜底。
    if settings.llm_fallback_base_url and settings.llm_fallback_api_key:
        fallback: LLMProvider = OpenAICompatibleProvider(
            settings.llm_fallback_base_url,
            settings.llm_fallback_api_key,
            settings.llm_fallback_model or settings.llm_model,
            timeout=settings.llm_timeout_seconds,
        )
    else:
        fallback = DeterministicPlanner()

    breaker = CircuitBreaker(
        failure_threshold=settings.llm_failure_threshold,
        cooldown_seconds=settings.llm_cooldown_seconds,
    )
    logger.info("主模型 %s + 降级模型 %s", settings.llm_model, fallback.name)
    return FailoverProvider(primary, fallback, breaker)
