import asyncio
import contextlib
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import FastAPI, Query
from pydantic import BaseModel, Field
from redis.asyncio import Redis
from shop_common.auth import Principal, PrincipalDep, signed_identity_headers
from shop_common.config import get_settings
from shop_common.db import get_session_factory, init_models
from shop_common.errors import ApiError, install_error_handlers
from shop_common.events import EventBus, create_redis
from shop_common.http import ServiceClient
from shop_common.logging import configure_logging, get_logger
from shop_common.outbox import OutboxRelay
from sqlalchemy import func, select

from .models import AfterSaleTicket
from .policy import evaluate

settings = get_settings()
logger = get_logger(__name__)

redis: Redis | None = None
bus: EventBus | None = None
order_client = ServiceClient(settings.order_url)
payment_client = ServiceClient(settings.payment_url)


def _forward(principal: Principal) -> dict[str, str]:
    # 调用订单服务时只透传已鉴权身份，不接受客户端自定义角色，防止越权调用；
    # 同时必须带上内网签名，否则订单服务会按未签名身份头拒绝
    return signed_identity_headers(principal.user_id, principal.role, principal.tenant)


def _operator_forward(principal: Principal) -> dict[str, str]:
    # 资金动作以 operator 身份发起：按 operator 重新签名，不能只改头里的角色导致签名失效
    return signed_identity_headers(principal.user_id, "operator", principal.tenant)


@asynccontextmanager
async def lifespan(_: FastAPI):
    global redis, bus
    configure_logging("aftersales")
    await init_models()
    redis = create_redis(settings.redis_url)
    bus = EventBus(redis, namespace="shop", session_factory=get_session_factory())
    relay = OutboxRelay(get_session_factory(), bus)
    tasks = [
        asyncio.create_task(relay.run_forever()),
        # 退款结果由 payment 服务出款后回填，工单不主动轮询
        asyncio.create_task(bus.run_consumer("payment", "aftersales-svc", "aftersales-1", handle_payment_event)),
    ]
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await redis.close()


app = FastAPI(title="aftersales-service", version="0.1.0", lifespan=lifespan)
install_error_handlers(app, "aftersales")


class TicketCreate(BaseModel):
    order_id: str
    type: str = "refund_only"
    reason: str = Field(min_length=2)
    description: str = ""
    amount_cents: int | None = None
    # 行级售后：指定订单行与数量；两者都不传表示整单退款
    order_item_id: str | None = None
    quantity: int | None = Field(default=None, gt=0)


class Proposal(BaseModel):
    recommendation: str
    rationale: str
    confidence: float = Field(ge=0, le=1)


async def _notify_order(ticket: AfterSaleTicket, event: str, extra: dict | None = None) -> None:
    assert bus is not None
    await bus.publish("aftersales", event, {"order_id": ticket.order_id, "ticket_id": ticket.id, **(extra or {})})


async def _approve(ticket: AfterSaleTicket) -> None:
    # 通过动作只落本地决策状态；出款由调用方在事务提交后请求 payment 服务
    ticket.status = "approved"
    ticket.decided_at = datetime.now(UTC)


async def _request_refund(ticket: AfterSaleTicket, principal: Principal) -> None:
    """审批通过后请求 payment 出款；按 ticket_id 幂等，重复调用不会重复打款。"""
    with contextlib.suppress(ApiError):
        await payment_client.request(
            "POST",
            "/payments/refunds",
            json={
                "order_id": ticket.order_id,
                "ticket_id": ticket.id,
                "amount_cents": ticket.amount_cents,
                "order_item_id": ticket.order_item_id,
                "quantity": ticket.quantity,
            },
            headers=_operator_forward(principal),
        )


async def handle_payment_event(event: dict[str, Any]) -> None:
    """payment.refunded 回填工单：退款以支付服务的出款结果为准，不靠人工点完成。"""
    if event.get("type") != "payment.refunded":
        return
    payload = event["payload"]
    ticket_id = payload.get("ticket_id")
    if not ticket_id:
        return
    async with get_session_factory()() as session:
        ticket = await session.get(AfterSaleTicket, str(ticket_id))
        if ticket is None or ticket.status == "refunded":
            return
        ticket.status = "refunded"
        ticket.refund_id = payload.get("refund_id")
        ticket.refunded_at = datetime.now(UTC)
        await session.commit()


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok", "service": "aftersales"}


def _resolve_refund(
    order: dict[str, Any], payload: TicketCreate
) -> tuple[int, str | None, int]:
    """按订单行计算可退数量与金额；所有上限以订单快照为准，客户端金额只能下调不能突破。"""
    items = order.get("items") or []
    if payload.order_item_id:
        item = next((row for row in items if row.get("item_id") == payload.order_item_id), None)
        if item is None:
            raise ApiError("BAD_REQUEST", "订单行不存在或不属于该订单")
        available_qty = int(item.get("qty", 0)) - int(item.get("refunded_qty", 0))
        if available_qty <= 0:
            raise ApiError("CONFLICT", "该订单行已全部退款")
        qty = payload.quantity or available_qty
        if qty > available_qty:
            raise ApiError("CONFLICT", f"该订单行最多还能退 {available_qty} 件")
        paid = int(item.get("paid_amount_cents") or int(item.get("price_cents", 0)) * int(item.get("qty", 0)))
        remaining = paid - int(item.get("refunded_amount_cents", 0))
        # 退整行剩余数量时直接取剩余金额，避免单价取整导致几分的残留可退额
        default_amount = remaining if qty == available_qty else int(item.get("price_cents", 0)) * qty
        amount = payload.amount_cents or default_amount
        if amount <= 0 or amount > remaining:
            raise ApiError("CONFLICT", f"该订单行最多还能退 {remaining} 分")
        return amount, payload.order_item_id, qty

    if payload.quantity:
        raise ApiError("BAD_REQUEST", "整单退款不能指定数量")
    # 整单退款不落到具体行，无法拆分部分金额；为避免 item 余量与实际退款不一致，
    # 只允许一次退完剩余全部金额，部分金额必须选择订单行
    available = int(order["amount_cents"]) - int(order.get("refund_amount_cents", 0))
    if payload.amount_cents is not None and payload.amount_cents != available:
        raise ApiError("BAD_REQUEST", "整单退款必须退剩余全部金额，部分退款请选择订单行")
    if available <= 0:
        raise ApiError("CONFLICT", "该订单已无可退金额")
    return available, None, 0


@app.post("/aftersales/tickets", status_code=201)
async def create_ticket(payload: TicketCreate, principal: PrincipalDep) -> dict[str, Any]:
    # 售后必须建立在真实且可退的订单上，因此先向订单服务取权威状态，而不是信任请求参数
    order = await order_client.request("GET", f"/orders/{payload.order_id}", headers=_forward(principal))
    if order["status"] not in {"PAID", "SHIPPING", "SHIPPED", "COMPLETED", "REFUNDING"}:
        raise ApiError("CONFLICT", "当前订单状态不支持发起售后")
    created_at = datetime.fromisoformat(order["created_at"])
    amount, order_item_id, quantity = _resolve_refund(order, payload)

    async with get_session_factory()() as session:
        # 只统计近 30 天本人工单数作为频次风控信号，时间窗口固定避免历史数据拖累判定
        cutoff = datetime.now(UTC) - timedelta(days=30)
        recent = await session.scalar(
            select(func.count())
            .select_from(AfterSaleTicket)
            .where(AfterSaleTicket.user_id == principal.user_id, AfterSaleTicket.created_at >= cutoff)
        )
        # REFUNDING 在政策引擎里按 PAID 处理：已进入退款流程的订单仍应允许继续售后判定
        result = evaluate(
            order_status="PAID" if order["status"] == "REFUNDING" else order["status"],
            order_created_at=created_at,
            amount_cents=amount,
            reason=payload.reason,
            description=payload.description,
            recent_ticket_count=int(recent or 0),
            window_days=settings.refund_window_days,
        )
        ticket = AfterSaleTicket(
            id=str(uuid.uuid4()),
            ticket_no=f"AS{datetime.now(UTC):%Y%m%d}{uuid.uuid4().hex[:6].upper()}",
            order_id=payload.order_id,
            order_item_id=order_item_id,
            quantity=quantity,
            user_id=principal.user_id,
            type=payload.type,
            reason=payload.reason,
            description=payload.description,
            amount_cents=amount,
            status="pending",
            risk_score=result["risk_score"],
            risk_reasons=result["risk_reasons"],
            decision_note=result["note"],
        )
        # 政策引擎三档分流：自动驳回/自动通过/转人工；自动档决策人固定记为 policy-engine 便于审计溯源
        if result["decision"] == "auto_reject":
            ticket.status = "rejected"
            ticket.decided_by = "policy-engine"
            ticket.decided_at = datetime.now(UTC)
        elif result["decision"] == "auto_approve":
            ticket.decided_by = "policy-engine"
            await _approve(ticket)
        session.add(ticket)
        # 先提交本地事务再调用外部退款，避免持有数据库事务等待网络 IO
        await session.commit()
        ticket_dict = ticket.to_dict()

    if ticket.status == "approved":
        # 自动通过后触发退款；退款失败不回滚工单结论，保持 approved 等待重试（payment 按 ticket_id 幂等）
        await _request_refund(ticket, principal)
    return {**ticket_dict, "policy": result}


@app.get("/aftersales/tickets")
async def list_tickets(
    principal: PrincipalDep,
    status: str | None = None,
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
) -> dict[str, Any]:
    async with get_session_factory()() as session:
        stmt = select(AfterSaleTicket).order_by(AfterSaleTicket.created_at.desc())
        # 非员工只能看到自己名下的工单，数据范围在 SQL 层收敛，避免越权读
        if not principal.is_staff():
            stmt = stmt.where(AfterSaleTicket.user_id == principal.user_id)
        if status:
            stmt = stmt.where(AfterSaleTicket.status == status)
        rows = (await session.execute(stmt.offset((page - 1) * size).limit(size))).scalars().all()
        return {"items": [row.to_dict() for row in rows], "page": page, "size": size}


@app.get("/aftersales/tickets/{ticket_id}")
async def get_ticket(ticket_id: str, principal: PrincipalDep) -> dict[str, Any]:
    async with get_session_factory()() as session:
        ticket = await session.get(AfterSaleTicket, ticket_id)
        if ticket is None:
            raise ApiError("NOT_FOUND", "售后单不存在")
        # 单条查询同样校验归属：非员工且非本人一律拒绝，不能靠列表过滤兜底
        if not principal.is_staff() and ticket.user_id != principal.user_id:
            raise ApiError("FORBIDDEN", "无权查看该售后单")
        return ticket.to_dict()


@app.post("/aftersales/tickets/{ticket_id}/proposal")
async def submit_proposal(ticket_id: str, payload: Proposal, principal: PrincipalDep) -> dict[str, Any]:
    """供运营 Agent 写入建议；不改变工单状态，避免模型直接放款。"""
    principal.require_staff()
    async with get_session_factory()() as session:
        ticket = await session.get(AfterSaleTicket, ticket_id)
        if ticket is None:
            raise ApiError("NOT_FOUND", "售后单不存在")
        # 只记录 Agent 建议到独立字段，工单状态不变：模型输出永远不能直接触发放款
        ticket.agent_recommendation = payload.model_dump()
        await session.commit()
        return ticket.to_dict()


class Decision(BaseModel):
    note: str = ""


@app.post("/aftersales/tickets/{ticket_id}/approve")
async def approve(ticket_id: str, principal: PrincipalDep, payload: Decision | None = None) -> dict[str, Any]:
    # 通过即意味着放款，属于不可逆资金动作，只允许员工人工触发
    principal.require_staff()
    note = payload.note if payload else ""
    async with get_session_factory()() as session:
        ticket = await session.get(AfterSaleTicket, ticket_id)
        if ticket is None:
            raise ApiError("NOT_FOUND", "售后单不存在")
        # 仅 pending 可首次通过；已 approved 允许补充复核，其余状态一律拒绝以免重复放款
        if ticket.status not in {"pending", "approved"}:
            raise ApiError("CONFLICT", f"售后单状态 {ticket.status} 不可通过")
        await _approve(ticket)
        ticket.decided_by = principal.user_id
        ticket.decision_note = note or ticket.decision_note
        await session.commit()
        result = ticket.to_dict()

    # 本地已落库后再请求 payment 出款；失败不阻断响应，重复审批会按 ticket_id 幂等
    await _request_refund(ticket, principal)
    return result


@app.post("/aftersales/tickets/{ticket_id}/reject")
async def reject(ticket_id: str, principal: PrincipalDep, payload: Decision | None = None) -> dict[str, Any]:
    principal.require_staff()
    note = payload.note if payload else ""
    async with get_session_factory()() as session:
        ticket = await session.get(AfterSaleTicket, ticket_id)
        if ticket is None:
            raise ApiError("NOT_FOUND", "售后单不存在")
        # 驳回只对未决策工单生效，避免把已通过（可能已放款）的工单改回拒绝造成状态歧义
        if ticket.status != "pending":
            raise ApiError("CONFLICT", f"售后单状态 {ticket.status} 不可驳回")
        ticket.status = "rejected"
        ticket.decided_by = principal.user_id
        ticket.decision_note = note or "人工驳回"
        ticket.decided_at = datetime.now(UTC)
        await session.commit()
        result = ticket.to_dict()
    # 状态提交后再通知订单侧；驳回发生在出款前，订单通常尚未进入 REFUNDING，事件为安全兜底
    await _notify_order(ticket, "after-sales.rejected")
    return result
