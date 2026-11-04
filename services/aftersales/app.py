import asyncio
import contextlib
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import FastAPI, Query
from pydantic import BaseModel, Field
from redis.asyncio import Redis
from sqlalchemy import func, select

from shop_common.auth import Principal, PrincipalDep
from shop_common.config import get_settings
from shop_common.db import get_session_factory, init_models
from shop_common.errors import ApiError, install_error_handlers
from shop_common.events import EventBus, create_redis
from shop_common.http import ServiceClient
from shop_common.logging import configure_logging, get_logger
from shop_common.outbox import OutboxRelay

from .models import AfterSaleTicket
from .policy import evaluate

settings = get_settings()
logger = get_logger(__name__)

redis: Redis | None = None
bus: EventBus | None = None
order_client = ServiceClient(settings.order_url)


def _forward(principal: Principal) -> dict[str, str]:
    return {
        "X-User-Id": principal.user_id,
        "X-User-Role": principal.role,
        "X-Tenant": principal.tenant,
    }


@asynccontextmanager
async def lifespan(_: FastAPI):
    global redis, bus
    configure_logging("aftersales")
    await init_models()
    redis = create_redis(settings.redis_url)
    bus = EventBus(redis, namespace="shop")
    relay = OutboxRelay(get_session_factory(), bus)
    task = asyncio.create_task(relay.run_forever())
    try:
        yield
    finally:
        task.cancel()
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


class Proposal(BaseModel):
    recommendation: str
    rationale: str
    confidence: float = Field(ge=0, le=1)


async def _notify_order(ticket: AfterSaleTicket, event: str, extra: dict | None = None) -> None:
    assert bus is not None
    await bus.publish("aftersales", event, {"order_id": ticket.order_id, "ticket_id": ticket.id, **(extra or {})})


async def _approve(ticket: AfterSaleTicket) -> None:
    ticket.status = "approved"
    ticket.decided_at = datetime.now(timezone.utc)
    await _notify_order(ticket, "after-sales.approved", {"amount_cents": ticket.amount_cents})


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok", "service": "aftersales"}


@app.post("/aftersales/tickets", status_code=201)
async def create_ticket(payload: TicketCreate, principal: PrincipalDep) -> dict[str, Any]:
    order = await order_client.request("GET", f"/orders/{payload.order_id}", headers=_forward(principal))
    if order["status"] not in {"PAID", "SHIPPED", "COMPLETED", "REFUNDING"}:
        raise ApiError("CONFLICT", "当前订单状态不支持发起售后")
    created_at = datetime.fromisoformat(order["created_at"])
    amount = payload.amount_cents or order["amount_cents"]

    async with get_session_factory()() as session:
        cutoff = datetime.now(timezone.utc) - timedelta(days=30)
        recent = await session.scalar(
            select(func.count())
            .select_from(AfterSaleTicket)
            .where(AfterSaleTicket.user_id == principal.user_id, AfterSaleTicket.created_at >= cutoff)
        )
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
            ticket_no=f"AS{datetime.now(timezone.utc):%Y%m%d}{uuid.uuid4().hex[:6].upper()}",
            order_id=payload.order_id,
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
        if result["decision"] == "auto_reject":
            ticket.status = "rejected"
            ticket.decided_by = "policy-engine"
            ticket.decided_at = datetime.now(timezone.utc)
        elif result["decision"] == "auto_approve":
            ticket.decided_by = "policy-engine"
            await _approve(ticket)
        session.add(ticket)
        await session.commit()
        ticket_dict = ticket.to_dict()

    if ticket.status == "approved":
        with contextlib.suppress(ApiError):
            await order_client.request(
                "POST",
                f"/orders/{ticket.order_id}/refund",
                json={"amount_cents": ticket.amount_cents},
                headers={**_forward(principal), "X-User-Role": "operator"},
            )
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
        ticket.agent_recommendation = payload.model_dump()
        await session.commit()
        return ticket.to_dict()


class Decision(BaseModel):
    note: str = ""


@app.post("/aftersales/tickets/{ticket_id}/approve")
async def approve(ticket_id: str, principal: PrincipalDep, payload: Decision | None = None) -> dict[str, Any]:
    principal.require_staff()
    note = payload.note if payload else ""
    async with get_session_factory()() as session:
        ticket = await session.get(AfterSaleTicket, ticket_id)
        if ticket is None:
            raise ApiError("NOT_FOUND", "售后单不存在")
        if ticket.status not in {"pending", "approved"}:
            raise ApiError("CONFLICT", f"售后单状态 {ticket.status} 不可通过")
        await _approve(ticket)
        ticket.decided_by = principal.user_id
        ticket.decision_note = note or ticket.decision_note
        await session.commit()
        result = ticket.to_dict()

    with contextlib.suppress(ApiError):
        await order_client.request(
            "POST",
            f"/orders/{ticket.order_id}/refund",
            json={"amount_cents": ticket.amount_cents},
            headers={**_forward(principal), "X-User-Role": "operator"},
        )
    return result


@app.post("/aftersales/tickets/{ticket_id}/reject")
async def reject(ticket_id: str, principal: PrincipalDep, payload: Decision | None = None) -> dict[str, Any]:
    principal.require_staff()
    note = payload.note if payload else ""
    async with get_session_factory()() as session:
        ticket = await session.get(AfterSaleTicket, ticket_id)
        if ticket is None:
            raise ApiError("NOT_FOUND", "售后单不存在")
        if ticket.status != "pending":
            raise ApiError("CONFLICT", f"售后单状态 {ticket.status} 不可驳回")
        ticket.status = "rejected"
        ticket.decided_by = principal.user_id
        ticket.decision_note = note or "人工驳回"
        ticket.decided_at = datetime.now(timezone.utc)
        await session.commit()
        result = ticket.to_dict()
    await _notify_order(ticket, "after-sales.rejected")
    return result


@app.post("/aftersales/tickets/{ticket_id}/refund-complete")
async def refund_complete(ticket_id: str, principal: PrincipalDep) -> dict[str, Any]:
    principal.require_staff()
    async with get_session_factory()() as session:
        ticket = await session.get(AfterSaleTicket, ticket_id)
        if ticket is None:
            raise ApiError("NOT_FOUND", "售后单不存在")
        if ticket.status != "approved":
            raise ApiError("CONFLICT", "只有已通过的售后单可完成退款")
        ticket.status = "refunded"
        ticket.decided_at = datetime.now(timezone.utc)
        await session.commit()
        result = ticket.to_dict()

    with contextlib.suppress(ApiError):
        await order_client.request(
            "POST",
            f"/orders/{ticket.order_id}/refund-complete",
            json={},
            headers={**_forward(principal), "X-User-Role": "operator"},
        )
    await _notify_order(ticket, "after-sales.refunded", {"amount_cents": ticket.amount_cents})
    return result
