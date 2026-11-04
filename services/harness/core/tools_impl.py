"""业务工具：Agent 能碰到的所有电商动作都在这里显式登记。

原则：读操作尽量给；写操作只给「提案」权限；不可逆动作保留给人工，且默认不授予任何 Agent。
"""

from typing import Any

from shop_common.auth import Principal
from shop_common.config import get_settings
from shop_common.http import ServiceClient

from .policy import ToolPolicy
from .sandbox import SandboxManager
from .tools import Tool, ToolContext, registry

settings = get_settings()

catalog = ServiceClient(settings.catalog_url)
inventory = ServiceClient(settings.inventory_url)
orders = ServiceClient(settings.order_url)
aftersales = ServiceClient(settings.aftersales_url)
sandbox = SandboxManager()


def _headers(ctx: ToolContext) -> dict[str, str]:
    # 把调用者身份透传给下游服务，让每个服务按自己的规则再做一次授权，
    # Agent 侧放行不等于数据侧放行。
    return {"X-User-Id": ctx.user_id, "X-User-Role": ctx.role, "X-Tenant": ctx.tenant}


def _principal(ctx: ToolContext) -> Principal:
    return Principal(ctx.user_id, ctx.role, ctx.tenant)


async def _catalog_search(args: dict[str, Any], ctx: ToolContext) -> Any:
    params = {key: args[key] for key in ("q", "category") if args.get(key)}
    return await _get(catalog, "/products", params)


async def _get(client: ServiceClient, path: str, params: dict[str, Any]) -> Any:
    # 只拼 query 串，路径仍是固定常量（来自各工具函数），避免模型输入改变请求路径。
    query = "&".join(f"{key}={value}" for key, value in params.items())
    return await client.request("GET", f"{path}?{query}" if query else path)


async def _catalog_detail(args: dict[str, Any], ctx: ToolContext) -> Any:
    return await catalog.request("GET", f"/products/{args['sku']}")


async def _catalog_validate(args: dict[str, Any], ctx: ToolContext) -> Any:
    product = await catalog.request("GET", f"/products/{args['sku']}")
    check = await catalog.request(
        "POST", f"/products/{product['id']}/compliance-check", json={}, headers=_headers(ctx)
    )
    return {"product": product["title"], "sku": product["sku"], **check}


async def _inventory_check(args: dict[str, Any], ctx: ToolContext) -> Any:
    return await inventory.request("GET", f"/inventory/{args['sku']}")


async def _inventory_list(_: dict[str, Any], ctx: ToolContext) -> Any:
    return await inventory.request("GET", "/inventory/stocks", headers=_headers(ctx))


async def _order_lookup(args: dict[str, Any], ctx: ToolContext) -> Any:
    order_id = args.get("order_id") or ""
    if not order_id:
        return {"status": "error", "message": "缺少订单号"}
    return await orders.request("GET", f"/orders/{order_id}", headers=_headers(ctx))


async def _order_list(args: dict[str, Any], ctx: ToolContext) -> Any:
    status = args.get("status")
    path = "/orders" + (f"?status={status}" if status else "")
    return await orders.request("GET", path, headers=_headers(ctx))


async def _after_sales_draft(args: dict[str, Any], ctx: ToolContext) -> Any:
    order_id = args.get("order_id")
    if not order_id:
        return {"status": "error", "message": "起草售后需要订单号"}
    return await aftersales.request(
        "POST",
        "/aftersales/tickets",
        json={
            "order_id": order_id,
            "reason": args.get("reason", "quality_issue"),
            "description": args.get("description", ""),
        },
        headers=_headers(ctx),
    )


async def _after_sales_queue(args: dict[str, Any], ctx: ToolContext) -> Any:
    status = args.get("status", "pending")
    return await aftersales.request("GET", f"/aftersales/tickets?status={status}", headers=_headers(ctx))


async def _after_sales_propose(args: dict[str, Any], ctx: ToolContext) -> Any:
    return await aftersales.request(
        "POST",
        f"/aftersales/tickets/{args['ticket_id']}/proposal",
        json={
            "recommendation": args["recommendation"],
            "rationale": args.get("rationale", ""),
            "confidence": float(args.get("confidence", 0.7)),
        },
        headers=_headers(ctx),
    )


async def _after_sales_approve(args: dict[str, Any], ctx: ToolContext) -> Any:
    """不可逆动作：默认不授予任何 Agent，仅保留给人工审批后重放。"""
    return await aftersales.request(
        "POST",
        f"/aftersales/tickets/{args['ticket_id']}/approve",
        json={"note": args.get("note", "")},
        headers=_headers(ctx),
    )


async def _sandbox_run(args: dict[str, Any], ctx: ToolContext) -> Any:
    result = await sandbox.run(
        args.get("code", ""),
        language=args.get("language", "python"),
        timeout=int(args.get("timeout", settings.sandbox_timeout_seconds)),
    )
    return result.to_dict()


async def _content_draft(args: dict[str, Any], ctx: ToolContext) -> Any:
    product = await catalog.request("GET", f"/products/{args['sku']}")
    angle = args.get("angle", "卖点")
    title = product["title"]
    points = product.get("tags") or []
    draft = (
        f"【{title}】{product.get('subtitle', '')}。"
        f"围绕{angle}，重点讲清楚：{'、'.join(points) or '核心体验'}。"
        "文案仅作为草稿，需运营确认后发布。"
    )
    return {
        "sku": product["sku"],
        "draft": draft,
        "compliance": "需人工审核",
        "claims": [f"围绕{angle}的表达", "未生成绝对化用语"],
    }


# 工具在模块导入时集中登记（注册副作用），app.py 只要 import 本模块，
# 启动时 registry 就已包含全部业务工具；权限策略与 handler 在这里成对定义。
registry.register(
    Tool(
        "catalog_search",
        "按关键词或类目搜索已上架商品",
        {
            "type": "object",
            "properties": {
                "q": {"type": "string", "description": "关键词"},
                "category": {"type": "string", "description": "类目，如 digital/apparel/beauty/home"},
            },
        },
        _catalog_search,
        ToolPolicy("catalog_search", permission="read", risk="low"),
    )
)
registry.register(
    Tool(
        "catalog_detail",
        "按 SKU 查询商品详情",
        {"type": "object", "properties": {"sku": {"type": "string"}}, "required": ["sku"]},
        _catalog_detail,
        ToolPolicy("catalog_detail", permission="read", risk="low"),
    )
)
registry.register(
    Tool(
        "catalog_validate",
        "检查商品属性完整性与违规词",
        {
            "type": "object",
            "properties": {"sku": {"type": "string"}},
            "required": ["sku"],
        },
        _catalog_validate,
        ToolPolicy("catalog_validate", permission="read", risk="low"),
    )
)
registry.register(
    Tool(
        "inventory_check",
        "查询单个 SKU 的可售库存",
        {"type": "object", "properties": {"sku": {"type": "string"}}, "required": ["sku"]},
        _inventory_check,
        ToolPolicy("inventory_check", permission="read", risk="low"),
    )
)
registry.register(
    Tool(
        "inventory_list",
        "列出全部 SKU 的库存与预占情况",
        {"type": "object", "properties": {}},
        _inventory_list,
        ToolPolicy("inventory_list", permission="read", risk="low", data_class="internal"),
    )
)
registry.register(
    Tool(
        "order_lookup",
        "按订单号查询订单状态与明细（含收货信息）",
        {"type": "object", "properties": {"order_id": {"type": "string"}}, "required": ["order_id"]},
        _order_lookup,
        ToolPolicy("order_lookup", permission="read", risk="low", data_class="pii"),
    )
)
registry.register(
    Tool(
        "order_list",
        "列出当前用户或运营视角的订单",
        {"type": "object", "properties": {"status": {"type": "string"}}},
        _order_list,
        ToolPolicy("order_list", permission="read", risk="low", data_class="pii"),
    )
)
registry.register(
    Tool(
        "after_sales_draft",
        "为指定订单起草售后申请（仅创建待审工单，不直接退款）",
        {
            "type": "object",
            "properties": {
                "order_id": {"type": "string"},
                "reason": {"type": "string"},
                "description": {"type": "string"},
            },
            "required": ["order_id"],
        },
        _after_sales_draft,
        ToolPolicy("after_sales_draft", permission="propose", risk="medium"),
    )
)
registry.register(
    Tool(
        "after_sales_queue",
        "查看售后工单队列",
        {"type": "object", "properties": {"status": {"type": "string"}}},
        _after_sales_queue,
        ToolPolicy("after_sales_queue", permission="read", risk="low", data_class="internal"),
    )
)
registry.register(
    Tool(
        "after_sales_propose",
        "向售后工单写入 Agent 建议（不改变工单状态）",
        {
            "type": "object",
            "properties": {
                "ticket_id": {"type": "string"},
                "recommendation": {"type": "string", "enum": ["approve", "reject", "need_more_info"]},
                "rationale": {"type": "string"},
                "confidence": {"type": "number"},
            },
            "required": ["ticket_id", "recommendation"],
        },
        _after_sales_propose,
        ToolPolicy("after_sales_propose", permission="propose", risk="medium", data_class="internal"),
    )
)
registry.register(
    Tool(
        "after_sales_approve",
        "人工审批通过售后单（不可逆，默认不授予任何 Agent）",
        {"type": "object", "properties": {"ticket_id": {"type": "string"}, "note": {"type": "string"}}},
        _after_sales_approve,
        # requires_approval=True 是唯一的执行入口条件：注册表会先返回 needs_approval，
        # 只有人工审批后经 approve_and_replay 才会真正跑到 handler。
        ToolPolicy(
            "after_sales_approve",
            permission="write",
            risk="irreversible",
            data_class="internal",
            requires_approval=True,
        ),
    )
)
registry.register(
    Tool(
        "sandbox_run",
        "在隔离沙箱中执行 Python 代码并返回受限输出",
        {
            "type": "object",
            "properties": {
                "code": {"type": "string"},
                "language": {"type": "string", "enum": ["python"]},
                "timeout": {"type": "integer"},
            },
            "required": ["code"],
        },
        _sandbox_run,
        # execute 权限意味着只有 vendor 及以上信任等级（且 exec=True）能用；
        # reviewed/community 技能即使声明了 sandbox_run 也会被 policy 拦下。
        ToolPolicy("sandbox_run", permission="execute", risk="medium"),
    )
)
registry.register(
    Tool(
        "content_draft",
        "生成商品文案草稿（不发布，需人工确认）",
        {
            "type": "object",
            "properties": {"sku": {"type": "string"}, "angle": {"type": "string"}},
            "required": ["sku"],
        },
        _content_draft,
        ToolPolicy("content_draft", permission="read", risk="low"),
    )
)

BUSINESS_TOOLS = [tool for tool in registry.names()]
