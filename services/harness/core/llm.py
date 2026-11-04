"""模型接入层：默认使用离线确定性规划器，配置了 OpenAI 兼容端点后自动切换。

离线规划器不是玩具——它保证没有 API Key 时整套演示、测试与回归仍可跑通，
也让「模型是否真的在决策」这件事在对比实验中可被分离。
"""

import json
import re
from dataclasses import dataclass, field
from typing import Any

import httpx
from shop_common.config import get_settings
from shop_common.logging import get_logger

logger = get_logger(__name__)

SKU_PATTERN = re.compile(r"SKU-\d{4}")


@dataclass
class Decision:
    kind: str  # final | tool
    content: str = ""
    tool: str = ""
    args: dict[str, Any] = field(default_factory=dict)
    usage: dict[str, Any] = field(default_factory=dict)


class LLMProvider:
    name = "base"

    async def decide(
        self, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> Decision:
        raise NotImplementedError


class DeterministicPlanner(LLMProvider):
    """基于关键词与工具结果的确定性编排，用于离线演示与回归测试。"""

    name = "deterministic-v1"

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

    def __init__(self, base_url: str, api_key: str, model: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model

    async def decide(
        self, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> Decision:
        payload = {
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
        async with httpx.AsyncClient(timeout=60) as client:
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


def build_provider() -> LLMProvider:
    settings = get_settings()
    # 两者齐备才切到真实模型；缺任一配置都退回离线规划器，
    # 保证本地演示、CI 与回归测试不依赖外部网络和 API Key。
    if settings.llm_base_url and settings.llm_api_key:
        logger.info("使用 OpenAI 兼容模型 %s", settings.llm_model)
        return OpenAICompatibleProvider(settings.llm_base_url, settings.llm_api_key, settings.llm_model)
    logger.info("未配置模型端点，使用离线确定性规划器")
    return DeterministicPlanner()
