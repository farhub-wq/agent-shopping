"""模型接入层：默认使用离线确定性规划器，配置了 OpenAI 兼容端点后自动切换。

离线规划器不是玩具——它保证没有 API Key 时整套演示、测试与回归仍可跑通，
也让「模型是否真的在决策」这件事在对比实验中可被分离。
"""

import json
import re
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import httpx
from shop_common.config import get_settings
from shop_common.errors import ApiError
from shop_common.logging import get_logger
from shop_common.metrics import LLM_CIRCUIT_OPEN, LLM_FALLBACK

from .circuit import CircuitBreaker

logger = get_logger(__name__)

SKU_PATTERN = re.compile(r"SKU-\d{4}")
# 订单号兼容业务单号（SO+数字）与内部 UUID 两种形态。
_ORDER_PATTERN = re.compile(r"(SO\d{8}\d{6}|\b[0-9a-f]{8}-[0-9a-f-]{27}\b)")
_ORDER_WORDS = ("订单", "物流", "发货", "到哪", "快递", "运单", "签收")
_SHIPMENT_WORDS = ("物流", "快递", "运单", "签收", "到哪")

# 单次 plan 的步骤上限：超出截断，避免一次决策无限执行工具（NFR-10 工具调用有上限）。
MAX_PLAN_STEPS = 6


@dataclass
class Decision:
    kind: str  # final | tool | delta | plan（delta 为 final 答案的 token 分片）
    content: str = ""
    tool: str = ""
    args: dict[str, Any] = field(default_factory=dict)
    steps: list[dict[str, Any]] = field(default_factory=list)  # plan 的有序工具步骤 [{tool, args}]
    usage: dict[str, Any] = field(default_factory=dict)
    model: str = ""  # 产出该决策的模型名，供证据卡审计降级/熔断来源
    latency_ms: int = 0  # 本次模型调用的耗时，供用量记录汇总


def parse_decision(message: dict[str, Any], tools: list[dict[str, Any]]) -> Decision:
    """把 OpenAI 响应 message 校验成决策：0 个 tool_calls → final，1 个 → tool，≥2 个 → plan。

    校验工具名在 tools 白名单内、arguments 是合法 JSON 对象；不合法抛 ApiError，
    让坏输出在降级层被捕获而不是静默变成空决策。
    """
    allowed = {item["name"] for item in tools}
    calls = message.get("tool_calls") or []
    if not calls:
        return Decision(kind="final", content=message.get("content") or "")
    steps: list[dict[str, Any]] = []
    for call in calls:
        function = call.get("function") or {}
        name = function.get("name") or ""
        if name not in allowed:
            raise ApiError("BAD_DECISION", f"模型返回了未知工具 {name}")
        raw_args = function.get("arguments") or "{}"
        try:
            args = json.loads(raw_args)
        except ValueError as exc:
            raise ApiError("BAD_DECISION", f"工具 {name} 的参数不是合法 JSON") from exc
        if not isinstance(args, dict):
            raise ApiError("BAD_DECISION", f"工具 {name} 的参数必须是 JSON 对象")
        steps.append({"tool": name, "args": args})
    if len(steps) == 1:
        return Decision(kind="tool", tool=steps[0]["tool"], args=steps[0]["args"])
    return Decision(kind="plan", steps=steps)


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
        order_match = _ORDER_PATTERN.search(text)
        order_id = order_match.group(0) if order_match else ""

        def choose(candidates: list[tuple[str, dict[str, Any]]]) -> Decision:
            # 只从技能实际暴露的工具里挑，候选顺序表达「首选 / 退路」；
            # 全都不可用时明确说明能力缺失，而不是编造一个不存在的工具名。
            for name, args in candidates:
                if name in available:
                    return Decision(kind="tool", tool=name, args=args)
            return Decision(kind="final", content="当前技能没有可用的工具来处理这个请求。")

        # 多意图查询：同时命中 ≥2 个独立查询类意图时产出 plan，而不是被首个关键词截断。
        # 售后/退款属「提议类」动作，保留下面的「首选/退路」语义，不参与 plan 组合。
        plan_steps: list[dict[str, Any]] = []
        if sku and any(word in text for word in ("库存", "有货", "还剩", "秒杀")):
            plan_steps.append({"tool": "inventory_check", "args": {"sku": sku.group(0)}})
        if any(word in text for word in ("上架", "合规", "属性", "标题", "文案")):
            plan_steps.append(
                {"tool": "catalog_validate", "args": {"sku": sku.group(0) if sku else "SKU-1001"}}
            )
        if any(word in text for word in _ORDER_WORDS):
            if "shipment_track" in available and any(word in text for word in _SHIPMENT_WORDS) and order_id:
                plan_steps.append({"tool": "shipment_track", "args": {"order_id": order_id}})
            else:
                plan_steps.append({"tool": "order_lookup", "args": {"order_id": order_id}})
        if any(word in text for word in ("推荐", "买", "选", "耳机", "跑鞋", "精华", "衬衫")):
            keyword = next(
                (word for word in ("耳机", "跑鞋", "精华", "衬衫", "枕头", "收纳") if word in text),
                "",
            )
            plan_steps.append({"tool": "catalog_search", "args": {"q": keyword}})
        plan_steps = [step for step in plan_steps if step["tool"] in available]
        if len(plan_steps) >= 2:
            return Decision(kind="plan", steps=plan_steps)

        # 售后进度优先于建单：用户问「审核到哪了」时不应再起草一张新工单。
        if (
            any(word in text for word in ("售后", "退款", "退货", "工单"))
            and any(word in text for word in ("进度", "审核", "状态", "到哪", "结果", "通过", "拒绝"))
            and "after_sales_status" in available
        ):
            return Decision(kind="tool", tool="after_sales_status", args={})

        if any(word in text for word in ("退款", "退货", "售后", "换货")):
            args = {"reason": "quality_issue" if "质量" in text else "size_issue", "description": text}
            if sku:
                args["sku"] = sku.group(0)
            if "订单" in text and order_id:
                args["order_id"] = order_id
            return choose([("after_sales_draft", args), ("order_lookup", {})])

        if any(word in text for word in _ORDER_WORDS):
            if "shipment_track" in available and any(word in text for word in _SHIPMENT_WORDS):
                if not order_id:
                    return Decision(kind="final", content="请提供订单号，我帮你查询物流进度。")
                return Decision(kind="tool", tool="shipment_track", args={"order_id": order_id})
            return choose([("order_lookup", {"order_id": order_id})])

        if any(word in text for word in ("库存", "有货", "还剩", "秒杀")):
            # 缺 SKU 时必须追问，默认某个 SKU 会把「有货吗」答成别的商品库存。
            if not sku:
                return Decision(kind="final", content="请提供商品 SKU，我再帮你查询可售库存。")
            return choose([("inventory_check", {"sku": sku.group(0)})])

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
        # 连接池复用：减少连接建立开销，避免高并发下 TIME_WAIT 连接堆积
        self._client: httpx.AsyncClient | None = None

    def _get_client(self) -> httpx.AsyncClient:
        """惰性创建并复用 HTTP 客户端，避免每次请求都新建连接。"""
        if self._client is None:
            limits = httpx.Limits(max_connections=100, max_keepalive_connections=20)
            self._client = httpx.AsyncClient(timeout=self.timeout, limits=limits)
        return self._client

    async def close(self) -> None:
        """关闭 HTTP 客户端，释放连接池资源。"""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

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
            # 流式下默认不返回 usage，显式声明才在收尾块带上真实 token 数，供用量落库。
            payload["stream_options"] = {"include_usage": True}
        return payload

    async def decide(
        self, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> Decision:
        payload = self._payload(system, messages, tools, stream=False)
        started = time.perf_counter()
        # 复用客户端连接池
        client = self._get_client()
        response = await client.post(
            f"{self.base_url}/chat/completions",
            json=payload,
            headers={"Authorization": f"Bearer {self.api_key}"},
        )
        response.raise_for_status()
        body = response.json()
        message = body["choices"][0]["message"]
        usage = body.get("usage", {})
        decision = parse_decision(message, tools)
        decision.usage = usage
        decision.model = self.model
        decision.latency_ms = int((time.perf_counter() - started) * 1000)
        return decision

    async def decide_stream(
        self, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> AsyncIterator[Decision]:
        payload = self._payload(system, messages, tools, stream=True)
        content_parts: list[str] = []
        # 并行 tool_calls 的参数拆在多帧里，按 index 归位累积，避免串到同一工具的 args。
        tool_parts: dict[int, list[str]] = {}
        usage: dict[str, Any] = {}
        started = time.perf_counter()
        # 复用客户端连接池
        client = self._get_client()
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
                    # 声明 include_usage 后，最后一块带 usage 且无 choices，单独收下。
                    if chunk.get("usage"):
                        usage = chunk["usage"]
                    content_delta, tool_deltas = _openai_stream_delta(chunk)
                    if content_delta:
                        content_parts.append(content_delta)
                        yield Decision(kind="delta", content=content_delta)
                    for index, name_delta, args_delta in tool_deltas:
                        entry = tool_parts.setdefault(index, ["", ""])
                        if name_delta:
                            entry[0] = name_delta
                        if args_delta:
                            entry[1] += args_delta
        latency_ms = int((time.perf_counter() - started) * 1000)
        if tool_parts:
            # 组装成伪 message 交给 parse_decision 统一校验（单个 → tool，多个 → plan）。
            tool_calls = [
                {"function": {"name": entry[0], "arguments": entry[1]}}
                for entry in (tool_parts[index] for index in sorted(tool_parts))
            ]
            decision = parse_decision({"tool_calls": tool_calls}, tools)
            decision.model = self.model
            decision.usage = usage
            decision.latency_ms = latency_ms
            yield decision
            return
        yield Decision(
            kind="final",
            content="".join(content_parts),
            model=self.model,
            usage=usage,
            latency_ms=latency_ms,
        )


def _openai_stream_delta(chunk: dict[str, Any]) -> tuple[str, list[tuple[int, str, str]]]:
    # 解析单个 SSE 数据块为 (content_delta, [(index, name_delta, args_delta), ...])；
    # 并行 tool_calls 的每个 delta 带 index，缺失字段安全返回空，绝不抛 KeyError。
    choices = chunk.get("choices") or []
    if not choices:
        return "", []
    delta = choices[0].get("delta") or {}
    content = delta.get("content") or ""
    tool_calls = delta.get("tool_calls") or []
    deltas: list[tuple[int, str, str]] = []
    for position, tool_call in enumerate(tool_calls):
        function = tool_call.get("function") or {}
        name = function.get("name") or ""
        args = function.get("arguments") or ""
        index = tool_call.get("index", position)
        deltas.append((index, name, args))
    return content, deltas


class AnthropicProvider(LLMProvider):
    """Anthropic Messages API 适配器（POST {base_url}/messages）。

    与 OpenAI 兼容端点最大的差异在消息格式：工具调用不是独立的 tool_calls 字段，
    而是 assistant 内容块里的 tool_use，工具结果以 user 消息里的 tool_result 回填。
    这里做一层转换，让运行时 ReAct 循环继续用统一的 OpenAI 形状消息，转换后的
    tool_calls 仍经 parse_decision 统一校验。
    """

    name = "anthropic"

    def __init__(self, base_url: str, api_key: str, model: str, timeout: float = 15.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        # 连接池复用
        self._client: httpx.AsyncClient | None = None

    def _get_client(self) -> httpx.AsyncClient:
        """惰性创建并复用 HTTP 客户端。"""
        if self._client is None:
            limits = httpx.Limits(max_connections=100, max_keepalive_connections=20)
            self._client = httpx.AsyncClient(timeout=self.timeout, limits=limits)
        return self._client

    async def close(self) -> None:
        """关闭 HTTP 客户端，释放连接池资源。"""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _payload(
        self, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]], stream: bool
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "max_tokens": 4096,
            "system": system,
            "messages": _to_anthropic_messages(messages),
            # Anthropic 工具 schema 直接用 input_schema 字段，不需要 OpenAI 的 function 包装。
            "tools": [
                {"name": item["name"], "description": item["description"], "input_schema": item["input_schema"]}
                for item in tools
            ],
        }
        if stream:
            payload["stream"] = True
        return payload

    def _headers(self) -> dict[str, str]:
        return {"x-api-key": self.api_key, "anthropic-version": "2023-06-01"}

    async def decide(
        self, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> Decision:
        payload = self._payload(system, messages, tools, stream=False)
        started = time.perf_counter()
        # 复用客户端连接池
        client = self._get_client()
        response = await client.post(f"{self.base_url}/messages", json=payload, headers=self._headers())
        response.raise_for_status()
        body = response.json()
        content, tool_calls, usage = _anthropic_response_parts(body)
        decision = parse_decision({"content": content, "tool_calls": tool_calls}, tools)
        decision.usage = usage
        decision.model = self.model
        decision.latency_ms = int((time.perf_counter() - started) * 1000)
        return decision

    async def decide_stream(
        self, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> AsyncIterator[Decision]:
        payload = self._payload(system, messages, tools, stream=True)
        text_parts: list[str] = []
        # tool_use 的 name 在 content_block_start 给出，参数拆在后续 input_json_delta 分帧累积。
        tool_blocks: dict[int, dict[str, str]] = {}
        usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        started = time.perf_counter()
        # 复用客户端连接池
        client = self._get_client()
        async with client.stream(
            "POST", f"{self.base_url}/messages", json=payload, headers=self._headers()
        ) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    try:
                        event = json.loads(line[len("data:") :].strip())
                    except ValueError:
                        continue
                    event_type = event.get("type")
                    if event_type == "content_block_start":
                        block = event.get("content_block") or {}
                        if block.get("type") == "text":
                            text_parts.append(block.get("text") or "")
                        elif block.get("type") == "tool_use":
                            tool_blocks[event.get("index", 0)] = {
                                "name": block.get("name") or "",
                                "args": "",
                            }
                    elif event_type == "content_block_delta":
                        delta = event.get("delta") or {}
                        if delta.get("type") == "text_delta":
                            part = delta.get("text") or ""
                            text_parts.append(part)
                            yield Decision(kind="delta", content=part)
                        elif delta.get("type") == "input_json_delta":
                            index = event.get("index", 0)
                            tool_blocks.setdefault(index, {"name": "", "args": ""})
                            tool_blocks[index]["args"] += delta.get("partial_json") or ""
                    elif event_type == "message_start":
                        _merge_anthropic_usage(usage, (event.get("message") or {}).get("usage") or {})
                    elif event_type == "message_delta":
                        _merge_anthropic_usage(usage, event.get("usage") or {})
        latency_ms = int((time.perf_counter() - started) * 1000)
        if tool_blocks:
            tool_calls = [
                {"function": {"name": block["name"], "arguments": block["args"] or "{}"}}
                for _, block in sorted(tool_blocks.items())
            ]
            decision = parse_decision({"tool_calls": tool_calls}, tools)
            decision.model = self.model
            decision.usage = usage
            decision.latency_ms = latency_ms
            yield decision
            return
        yield Decision(
            kind="final",
            content="".join(text_parts),
            model=self.model,
            usage=usage,
            latency_ms=latency_ms,
        )


def _to_anthropic_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # 连续多条 tool 消息合并成一条 user 消息（多个 tool_result 块），符合 Anthropic 推荐格式；
    # assistant 的 tool_calls 转成 tool_use 内容块，其余文本消息原样透传。
    out: list[dict[str, Any]] = []
    i = 0
    while i < len(messages):
        message = messages[i]
        role = message.get("role")
        if role == "tool":
            blocks: list[dict[str, Any]] = []
            while i < len(messages) and messages[i].get("role") == "tool":
                blocks.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": messages[i].get("tool_call_id") or "",
                        "content": str(messages[i].get("content") or ""),
                    }
                )
                i += 1
            out.append({"role": "user", "content": blocks})
            continue
        if role == "assistant":
            tool_calls = message.get("tool_calls")
            if tool_calls:
                blocks = []
                for call in tool_calls:
                    function = call.get("function") or {}
                    try:
                        args = json.loads(function.get("arguments") or "{}")
                    except ValueError:
                        args = {}
                    blocks.append(
                        {
                            "type": "tool_use",
                            "id": call.get("id") or "",
                            "name": function.get("name") or "",
                            "input": args,
                        }
                    )
                out.append({"role": "assistant", "content": blocks})
            else:
                out.append({"role": "assistant", "content": str(message.get("content") or "")})
        else:
            out.append({"role": "user", "content": str(message.get("content") or "")})
        i += 1
    return out


def _anthropic_response_parts(body: dict[str, Any]) -> tuple[str, list[dict[str, Any]], dict[str, Any]]:
    # 把 Anthropic 非流式响应拆成 OpenAI 形状的 (content, tool_calls, usage)，复用 parse_decision。
    content = ""
    tool_calls: list[dict[str, Any]] = []
    for block in body.get("content") or []:
        if block.get("type") == "text":
            content += block.get("text") or ""
        elif block.get("type") == "tool_use":
            tool_calls.append(
                {
                    "function": {
                        "name": block.get("name") or "",
                        "arguments": json.dumps(block.get("input") or {}, ensure_ascii=False),
                    }
                }
            )
    return content, tool_calls, _anthropic_usage_to_openai(body.get("usage") or {})


def _anthropic_usage_to_openai(usage: dict[str, Any]) -> dict[str, Any]:
    prompt = int(usage.get("input_tokens") or 0)
    completion = int(usage.get("output_tokens") or 0)
    return {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": prompt + completion}


def _merge_anthropic_usage(acc: dict[str, Any], usage: dict[str, Any]) -> None:
    # 流式下 input_tokens 在 message_start、output_tokens 在 message_delta，分两次合并。
    acc["prompt_tokens"] += int(usage.get("input_tokens") or 0)
    acc["completion_tokens"] += int(usage.get("output_tokens") or 0)
    acc["total_tokens"] = acc["prompt_tokens"] + acc["completion_tokens"]


# 双模型都不可用时的兜底话术：明确引导转人工，且必须是 final 决策，避免循环里反复试模型。
FALLBACK_MESSAGE = "抱歉，智能客服暂时不可用，请稍后重试或联系人工客服。"


class CannedProvider(LLMProvider):
    """固定话术提供者：生产降级链的终点。

    不用 DeterministicPlanner 兜底是有意的——关键词规划器会在模型宕机时错选工具
    （包括 after_sales_draft 这类写操作），把不可靠结果包装成正常答案。
    生产环境降级宁可如实告知并转人工；model 记为 unavailable，
    证据卡可审计「本次答案非模型产出」。
    """

    name = "canned"

    async def decide(
        self, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> Decision:
        return Decision(kind="final", content=FALLBACK_MESSAGE, model="unavailable")

    async def decide_stream(
        self, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> AsyncIterator[Decision]:
        # 契约与离线规划器一致：恰好一帧 terminal，不产 delta。
        yield await self.decide(system, messages, tools)


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

    async def close(self) -> None:
        """关闭主备模型的 HTTP 客户端。"""
        if hasattr(self.primary, 'close'):
            await self.primary.close()
        if hasattr(self.fallback, 'close'):
            await self.fallback.close()

    async def decide(
        self, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> Decision:
        if await self.breaker.allow_request():
            try:
                decision = await self.primary.decide(system, messages, tools)
            except Exception as exc:
                # 主模型失败：记录失败并降级；日志保留原始异常，便于区分网络故障与代码缺陷。
                await self.breaker.record_failure()
                logger.warning("主模型调用失败，降级到备用模型：%s", exc)
            else:
                await self.breaker.record_success()
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
        if await self.breaker.allow_request():
            emitted = False
            try:
                async for frame in self.primary.decide_stream(system, messages, tools):
                    emitted = True
                    yield frame
            except Exception as exc:
                await self.breaker.record_failure()
                logger.warning("主模型流式调用失败：%s", exc)
                if emitted:
                    # 收尾帧：让前端结束 busy，不回退降级模型。
                    yield Decision(kind="final", content="", model="unavailable")
                    return
            else:
                await self.breaker.record_success()
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


def build_provider(redis: Any) -> LLMProvider:
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
    # 降级模型：配置了就用第二个 OpenAI 兼容端点；没配置就用固定话术，
    # 生产环境绝不让关键词规划器冒充模型产出（错选工具比明确不可用更危险）。
    if settings.llm_fallback_base_url and settings.llm_fallback_api_key:
        fallback: LLMProvider = OpenAICompatibleProvider(
            settings.llm_fallback_base_url,
            settings.llm_fallback_api_key,
            settings.llm_fallback_model or settings.llm_model,
            timeout=settings.llm_timeout_seconds,
        )
    else:
        fallback = CannedProvider()

    # 熔断状态跨 worker 共享（Redis）：主模型宕机时全集群一起开路，
    # 不再各 worker 各自失败满阈值才熔断。
    breaker = CircuitBreaker(
        redis,
        "primary",
        failure_threshold=settings.llm_failure_threshold,
        cooldown_seconds=settings.llm_cooldown_seconds,
    )
    logger.info("主模型 %s + 降级模型 %s", settings.llm_model, fallback.name)
    return FailoverProvider(primary, fallback, breaker)


def build_provider_from_record(
    redis: Any,
    *,
    base_url: str,
    api_key: str,
    model: str,
    provider_type: str,
    provider_id: str,
) -> LLMProvider:
    """按数据库里的用户/组织模型记录构建 provider（含降级与熔断）。

    与全局 build_provider 的区别：降级统一用固定话术（用户自配的第三方 key 挂了
    不应悄悄切到别的模型产生额外费用），熔断器按 provider_id 命名，让每个自配
    模型独立开路，互不拖累。
    """
    settings = get_settings()
    timeout = settings.llm_timeout_seconds
    if provider_type == "anthropic":
        primary: LLMProvider = AnthropicProvider(base_url, api_key, model, timeout=timeout)
    else:
        primary = OpenAICompatibleProvider(base_url, api_key, model, timeout=timeout)

    breaker = CircuitBreaker(
        redis,
        f"provider-{provider_id}",
        failure_threshold=settings.llm_failure_threshold,
        cooldown_seconds=settings.llm_cooldown_seconds,
    )
    return FailoverProvider(primary, CannedProvider(), breaker)
