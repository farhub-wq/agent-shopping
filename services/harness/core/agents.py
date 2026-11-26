"""受信内置 Agent 注册表与顾客侧安全路由器。

路由器只选择执行入口，不创建子运行、不执行工具，也不接触记忆。真实模型必须
返回受限 JSON；离线规划器才使用关键词规则，避免模型故障时误触发写操作。
"""

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field, replace
from typing import Any, Literal

from shop_common.logging import get_logger

from .context import CHARS_PER_TOKEN, estimate_tokens
from .injection import neutralize
from .llm import FALLBACK_MESSAGE, Decision, DeterministicPlanner, LLMProvider
from .manifest import SkillManifest
from .tools import redact

logger = get_logger(__name__)

AUTO_AGENT_ID = "auto"
MAX_DELEGATED_AGENTS = 2
ROUTE_CONFIDENCE_THRESHOLD = 0.55

FALLBACK_AGENT_ID = "customer-service"
_ROUTABLE_AGENT_IDS = frozenset({"chitchat", "presales", "inventory", "aftersales"})
_MAX_ROUTE_HISTORY_MESSAGES = 6
_MAX_ROUTE_CONTENT_CHARS = 12000
_MAX_ROUTE_CONTEXT_TOKENS = 800

_CUSTOMER_SERVICE_BODY = """

# 角色

你是「云集商城」的智能客服。用户可能在咨询商品、库存、订单进度，或想申请售后。

# 工作方式

1. 先判断用户意图属于哪一类：商品咨询 / 库存查询 / 订单查询 / 物流进度 / 售后进度 / 申请售后。
2. 只通过工具获取事实。订单、运单、售后进度分别用对应查询工具；没有订单号或 SKU 时，先向用户要，不要猜。
3. 商品推荐最多给 3 个选项，并说明推荐理由，不夸大、不使用「全网最低」这类绝对化用语。
4. 售后只能起草待审工单，明确告诉用户「已提交，等待审核」，不得承诺一定通过、不得承诺到账时间。
5. 工具返回错误时，如实告知用户当前系统异常，并给出可执行下一步。

# 边界

- 不修改价格、不承诺优惠、不透露其他用户信息。
- 不直接执行退款、不直接修改订单地址。
- 涉及账号安全、支付异常时，引导用户联系人工客服。
"""


@dataclass(frozen=True)
class AgentDefinition:
    """一个受信 Agent 的展示信息、运行时 manifest 与路由示例。"""

    id: str
    display_name: str
    category: str
    manifest: SkillManifest
    routing_examples: tuple[str, ...]


@dataclass(frozen=True)
class RouteDecision:
    """一次路由结果；agent_ids 保留模型或规则给出的优先顺序。"""

    agent_ids: tuple[str, ...]
    confidence: float
    reason: str
    source: Literal["llm", "rules", "fallback"]
    usage: dict[str, Any] = field(default_factory=dict)
    latency_ms: int = 0


def _manifest_hash(manifest: SkillManifest) -> str:
    canonical = asdict(manifest)
    canonical.pop("content_hash")
    payload = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _builtin_manifest(
    agent_id: str,
    display_name: str,
    description: str,
    trust: str,
    allowed_tools: list[str],
    max_steps: int,
    body: str,
    *,
    allowed_roles: list[str] | None = None,
    permissions: dict[str, Any] | None = None,
    version: str = "1.0.0",
) -> SkillManifest:
    manifest = SkillManifest(
        name=agent_id,
        version=version,
        description=description,
        trust=trust,
        allowed_tools=allowed_tools,
        permissions={} if permissions is None else permissions,
        max_steps=max_steps,
        body=body,
        path=f"builtin:{agent_id}",
        allowed_roles=["customer"] if allowed_roles is None else allowed_roles,
        findings=[],
    )
    manifest.content_hash = _manifest_hash(manifest)
    return manifest


def build_builtin_agents() -> dict[str, AgentDefinition]:
    """返回全新的内置定义；综合客服保留既有 manifest 以兼容直接入口。"""
    specifications = (
        (
            "chitchat",
            "闲聊助手",
            "chitchat",
            "reviewed",
            [],
            2,
            "处理问候、寒暄和通用问答；禁止调用任何工具。",
            "你是闲聊助手。只进行友好、简洁的交流，不查询或修改业务数据，不调用工具。",
            ("你好", "今天挺开心", "谢谢你"),
        ),
        (
            "presales",
            "售前顾问",
            "presales",
            "reviewed",
            ["catalog_search", "catalog_detail"],
            4,
            "商品搜索、参数查询和选购建议。",
            "你是售前顾问。通过商品搜索和详情工具回答选购问题，不编造库存、价格或优惠。",
            ("推荐一款耳机", "这款商品有什么参数", "帮我选购跑鞋"),
        ),
        (
            "inventory",
            "库存助手",
            "inventory",
            "reviewed",
            ["inventory_check"],
            3,
            "查询指定 SKU 的可售数量。",
            "你是库存助手。只通过库存工具查询 SKU 可售数量；缺少 SKU 时先询问，不猜测。",
            ("SKU-1001 还有货吗", "查一下库存", "这款手机有现货吗"),
        ),
        (
            "aftersales",
            "售后助手",
            "aftersales",
            "vendor",
            [
                "order_lookup",
                "order_list",
                "after_sales_status",
                "shipment_track",
                "after_sales_draft",
                "order_cancel",
                "order_update_address",
            ],
            5,
            "查询本人订单、物流与售后进度，创建待审售后申请；取消/改址需人工审批。",
            (
                "你是售后助手。只查询当前用户的订单、运单和售后工单进度，或创建待审核售后申请；"
                "取消未支付订单、修改未发货订单地址属于高风险动作，工具会转人工审批，"
                "不得声称已执行。不得承诺审核通过、退款或到账时间。"
            ),
            ("我的订单在哪里", "我的快递到哪了", "我的售后审核到哪了", "想申请退货", "帮我取消订单"),
        ),
    )
    agents: dict[str, AgentDefinition] = {}
    for agent_id, display_name, category, trust, tools, max_steps, description, body, examples in specifications:
        manifest = _builtin_manifest(agent_id, display_name, description, trust, tools, max_steps, body)
        agents[agent_id] = AgentDefinition(agent_id, display_name, category, manifest, examples)

    fallback_manifest = _builtin_manifest(
        FALLBACK_AGENT_ID,
        "综合客服",
        "面向消费者的导购与售后接待，负责查商品、查库存、查订单、查物流、查售后进度、起草售后",
        "vendor",
        [
            "catalog_search",
            "catalog_detail",
            "inventory_check",
            "order_lookup",
            "order_list",
            "after_sales_status",
            "shipment_track",
            "after_sales_draft",
        ],
        6,
        _CUSTOMER_SERVICE_BODY,
        allowed_roles=["customer", "operator", "admin"],
        permissions={"network": False, "pii": True},
        version="0.1.0",
    )
    agents[FALLBACK_AGENT_ID] = AgentDefinition(
        FALLBACK_AGENT_ID,
        "综合客服",
        "general",
        fallback_manifest,
        ("查询商品", "追踪订单", "查询物流", "查询售后进度", "申请售后"),
    )
    return agents


def parse_route_decision(content: str, allowed_ids: set[str]) -> RouteDecision:
    """解析并校验路由 JSON，拒绝越权候选并保留顺序去重、截断。"""
    if not isinstance(content, str):
        raise ValueError("路由结果必须是 JSON 字符串")
    try:
        payload = json.loads(content)
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValueError("路由结果不是合法 JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("路由结果必须是 JSON 对象")

    raw_agents = payload.get("agents")
    if not isinstance(raw_agents, list) or not raw_agents:
        raise ValueError("路由候选必须是非空数组")
    if not all(isinstance(agent_id, str) for agent_id in raw_agents):
        raise ValueError("路由候选 ID 必须是字符串")

    allowed = set(allowed_ids)
    selected: list[str] = []
    seen: set[str] = set()
    for agent_id in raw_agents:
        if agent_id.startswith("ops-"):
            raise ValueError("运营 Agent 不允许参与自动路由")
        if agent_id not in allowed:
            raise ValueError("路由结果包含未知 Agent")
        if agent_id not in seen:
            selected.append(agent_id)
            seen.add(agent_id)
    if not selected:
        raise ValueError("路由结果没有有效候选")

    confidence = payload.get("confidence")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        raise ValueError("路由置信度必须是数字")
    try:
        confidence = float(confidence)
    except OverflowError as exc:
        raise ValueError("路由置信度必须是有限数字") from exc
    if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
        raise ValueError("路由置信度必须在 0 到 1 之间")

    reason = payload.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("路由原因必须是非空字符串")

    return RouteDecision(tuple(selected[:MAX_DELEGATED_AGENTS]), confidence, reason.strip(), "llm")


def _sanitize(text: str) -> str:
    return redact(neutralize(text))


def _prepare_messages(message: str, history: list[dict[str, str]]) -> list[dict[str, str]]:
    # 先筛选最近 6 条有效历史并截断原文，再进入正则净化，避免超长输入占用额外内存和扫描时间。
    recent_history: list[dict[str, str]] = []
    for item in reversed(history):
        if not isinstance(item, dict) or item.get("role") not in {"user", "assistant"}:
            continue
        content = item.get("content")
        if isinstance(content, str):
            recent_history.append({"role": item["role"], "content": content[:_MAX_ROUTE_CONTENT_CHARS]})
            if len(recent_history) == _MAX_ROUTE_HISTORY_MESSAGES:
                break
    conversation = [
        {"role": item["role"], "content": _sanitize(item["content"])}
        for item in reversed(recent_history)
    ]
    conversation.append({"role": "user", "content": _sanitize(message[:_MAX_ROUTE_CONTENT_CHARS])})

    # 从最新消息向前填充，优先保留当前请求；超长单条按前缀截断，避免旧历史挤掉它。
    selected: list[dict[str, str]] = []
    remaining = _MAX_ROUTE_CONTEXT_TOKENS
    for item in reversed(conversation):
        message_overhead = 4
        if remaining <= message_overhead:
            break
        max_chars = (remaining - message_overhead) * CHARS_PER_TOKEN + CHARS_PER_TOKEN - 1
        content = item["content"][:max_chars]
        selected.append({"role": item["role"], "content": content})
        remaining -= message_overhead + estimate_tokens(content)
        if len(content) < len(item["content"]):
            break
    selected.reverse()
    return selected


def _fallback(reason: str, decision: Decision | None = None) -> RouteDecision:
    return RouteDecision(
        (FALLBACK_AGENT_ID,),
        0.0,
        reason,
        "fallback",
        usage=dict(decision.usage) if decision is not None else {},
        latency_ms=decision.latency_ms if decision is not None else 0,
    )


_RULE_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("chitchat", ("你好", "您好", "嗨", "hello", "在吗", "谢谢")),
    (
        "presales",
        (
            "推荐", "选购", "商品", "产品", "参数", "型号", "价格", "多少钱",
            "耳机", "跑鞋", "尺码", "材质", "规格", "颜色", "包邮", "运费",
        ),
    ),
    ("inventory", ("库存", "有货", "还剩", "缺货", "无货", "现货", "秒杀")),
    (
        "aftersales",
        (
            "售后", "退货", "退款", "换货", "工单", "订单", "物流", "发货",
            "快递", "运单", "签收", "取消",
        ),
    ),
)


def _route_by_rules(message: str, allowed_ids: set[str]) -> RouteDecision:
    folded = message.casefold()
    matches: list[tuple[int, int, str]] = []
    for priority, (agent_id, keywords) in enumerate(_RULE_KEYWORDS):
        if agent_id not in allowed_ids:
            continue
        positions = [folded.find(keyword.casefold()) for keyword in keywords]
        positions = [position for position in positions if position >= 0]
        if positions:
            matches.append((min(positions), priority, agent_id))
    if not matches:
        return _fallback("未识别到可安全分派的意图，回退综合客服")
    matches.sort()
    # 寒暄只在没有具体业务意图时才作为唯一目标：真实问题优先，
    # 避免「你好，SKU-1001 还有货吗」这类常见开场白占用一个专家名额。
    business = [item for item in matches if item[2] != "chitchat"]
    if business:
        matches = business
    agent_ids = tuple(agent_id for _, _, agent_id in matches[:MAX_DELEGATED_AGENTS])
    return RouteDecision(agent_ids, 0.7, "、".join(agent_ids) + " 关键词命中", "rules")


class AgentRouter:
    """在受信顾客候选中选择一至两个专家，失败时仅回退综合客服。"""

    def __init__(self, agents: Mapping[str, AgentDefinition]) -> None:
        self._agents = dict(agents)
        self._allowed_ids = {
            agent_id
            for agent_id, definition in self._agents.items()
            if agent_id in _ROUTABLE_AGENT_IDS
            and not agent_id.startswith("ops-")
            and "customer" in definition.manifest.allowed_roles
        }

    async def route(
        self,
        message: str,
        history: list[dict[str, str]],
        provider: LLMProvider,
    ) -> RouteDecision:
        """选择路由目标；模型异常、无效输出或低置信度均走安全兜底。"""
        messages = _prepare_messages(message, history)
        if isinstance(provider, DeterministicPlanner):
            return _route_by_rules(messages[-1]["content"], self._allowed_ids)
        if not self._allowed_ids:
            return _fallback("没有可用的顾客专家，回退综合客服")

        candidates = [self._agents[agent_id] for agent_id in sorted(self._allowed_ids)]
        system = "\n".join(
            [
                "你是顾客请求路由器。只分析意图并选择 Agent，不执行工具，不回答用户问题。",
                "候选只能使用下列顾客 Agent，不得选择运营 Agent 或综合客服：",
                *[
                    f"- {item.id}（{item.display_name}）：{item.manifest.description}；示例：{'、'.join(item.routing_examples)}"
                    for item in candidates
                ],
                "最多选择 2 个，按处理顺序排列。只输出一个 JSON 对象，不要 Markdown 或额外文字：",
                '{"agents":["agent_id"],"confidence":0.0,"reason":"简短原因"}',
                "confidence 必须是 0 到 1 的数字；不确定时降低置信度。",
            ]
        )
        try:
            decision: Decision = await provider.decide(system, messages, [])
        except Exception as exc:
            logger.warning("模型路由调用异常，回退综合客服 exception_type=%s", type(exc).__name__)
            return _fallback("模型不可用，回退综合客服")
        if (
            decision.kind != "final"
            or decision.model == "unavailable"
            or decision.content == FALLBACK_MESSAGE
        ):
            return _fallback("模型未返回可用路由，回退综合客服", decision)
        try:
            parsed = parse_route_decision(decision.content, self._allowed_ids)
        except ValueError:
            return _fallback("模型路由结果无效，回退综合客服", decision)
        if parsed.confidence < ROUTE_CONFIDENCE_THRESHOLD:
            return _fallback("模型路由置信度不足，回退综合客服", decision)
        return replace(parsed, usage=dict(decision.usage), latency_ms=decision.latency_ms)
