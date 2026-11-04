import asyncio
import contextlib
import random
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import FastAPI, Header, Query
from pydantic import BaseModel, Field
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shop_common.auth import PrincipalDep
from shop_common.config import get_settings
from shop_common.db import get_session_factory, init_models
from shop_common.errors import ApiError, install_error_handlers
from shop_common.events import EventBus, create_redis
from shop_common.http import ServiceClient
from shop_common.idempotency import IdempotencyStore
from shop_common.logging import configure_logging, get_logger
from shop_common.outbox import OutboxRelay, enqueue

from .models import Order, OrderEvent
from .state_machine import STATE_LABELS, resolve_target

settings = get_settings()
logger = get_logger(__name__)

redis: Redis | None = None
bus: EventBus | None = None
idem: IdempotencyStore | None = None
catalog = ServiceClient(settings.catalog_url)
inventory = ServiceClient(settings.inventory_url)


def _r() -> Redis:
    assert redis is not None
    return redis


def _apply(
    session: AsyncSession,
    order: Order,
    event: str,
    *,
    actor: str = "system",
    detail: dict[str, Any] | None = None,
) -> str:
    target = resolve_target(order.status, event, refund_previous=order.refund_previous)
    session.add(
        OrderEvent(
            order_id=order.id,
            from_status=order.status,
            to_status=target,
            event=event,
            actor=actor,
            detail=detail or {},
        )
    )
    order.status = target
    order.updated_at = datetime.now(timezone.utc)
    return target


async def _load(session: AsyncSession, order_id: str) -> Order:
    order = await session.get(Order, order_id)
    if order is None:
        order = await session.scalar(select(Order).where(Order.order_no == order_id))
    if order is None:
        raise ApiError("NOT_FOUND", "订单不存在")
    return order


def _cancel(order: Order, session: AsyncSession, reason: str) -> None:
    _apply(session, order, "cancel", actor="system", detail={"reason": reason})
    order.cancel_reason = reason


async def _reserve_inventory(order: Order) -> tuple[bool, str]:
    try:
        await inventory.request(
            "POST",
            "/inventory/reserve",
            json={"order_id": order.id, "items": order.items},
            headers={"Idempotency-Key": order.id, "X-User-Id": "system", "X-User-Role": "operator"},
        )
        return True, ""
    except ApiError as exc:
        if exc.code == "CONFLICT":
            return False, "out_of_stock"
        return False, "inventory_unavailable"


async def _release_inventory(order_id: str, reason: str) -> None:
    with contextlib.suppress(ApiError):
        await inventory.request(
            "POST",
            "/inventory/release",
            json={"order_id": order_id, "reason": reason},
            headers={"X-User-Id": "system", "X-User-Role": "operator"},
        )


async def _commit_inventory(order_id: str) -> None:
    with contextlib.suppress(ApiError):
        await inventory.request(
            "POST",
            "/inventory/commit",
            json={"order_id": order_id},
            headers={"X-User-Id": "system", "X-User-Role": "operator"},
        )


async def handle_event(event: dict[str, Any]) -> None:
    topic = event["type"]
    payload = event["payload"]
    async with get_session_factory()() as session:
        if topic == "fulfillment.shipped":
            order = await session.scalar(select(Order).where(Order.id == payload["order_id"]))
            if order and order.status == "PAID":
                _apply(session, order, "ship", actor="fulfillment")
                order.shipped_at = datetime.now(timezone.utc)
                await session.commit()
        elif topic == "fulfillment.delivered":
            order = await session.scalar(select(Order).where(Order.id == payload["order_id"]))
            if order and order.status == "SHIPPED":
                _apply(session, order, "deliver", actor="fulfillment")
                order.completed_at = datetime.now(timezone.utc)
                await session.commit()
        elif topic == "after-sales.approved":
            order = await session.scalar(select(Order).where(Order.id == payload["order_id"]))
            if order and order.status in {"PAID", "SHIPPED", "COMPLETED"}:
                order.refund_previous = order.status
                _apply(session, order, "refund_request", actor="aftersales")
                order.refund_amount_cents = int(payload.get("amount_cents", order.amount_cents))
                await session.commit()
        elif topic == "after-sales.rejected":
            order = await session.scalar(select(Order).where(Order.id == payload["order_id"]))
            if order and order.status == "REFUNDING":
                _apply(session, order, "refund_reject", actor="aftersales")
                await session.commit()
        elif topic == "after-sales.refunded":
            order = await session.scalar(select(Order).where(Order.id == payload["order_id"]))
            if order and order.status == "REFUNDING":
                _apply(session, order, "refund_complete", actor="aftersales")
                await session.commit()


async def _startup() -> list[asyncio.Task]:
    global redis, bus, idem
    await init_models()
    redis = create_redis(settings.redis_url)
    bus = EventBus(redis, namespace="shop")
    idem = IdempotencyStore(redis, namespace="shop")
    relay = OutboxRelay(get_session_factory(), bus)
    tasks = [
        asyncio.create_task(relay.run_forever()),
        asyncio.create_task(bus.run_consumer("fulfillment", "order-svc", "order-1", handle_event)),
        asyncio.create_task(bus.run_consumer("aftersales", "order-svc", "order-2", handle_event)),
    ]
    return tasks


@asynccontextmanager
async def lifespan(_: FastAPI):
    configure_logging("order")
    tasks = await _startup()
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if redis is not None:
            await redis.close()


app = FastAPI(title="order-service", version="0.1.0", lifespan=lifespan)
install_error_handlers(app, "order")


class OrderItemInput(BaseModel):
    sku: str
    qty: int = Field(gt=0, le=99)


class Address(BaseModel):
    receiver: str = Field(min_length=1)
    phone: str = Field(min_length=6)
    province: str = ""
    city: str = ""
    detail: str = Field(min_length=3)


class CreateOrderRequest(BaseModel):
    items: list[OrderItemInput] = Field(min_length=1)
    address: Address


class PayRequest(BaseModel):
    simulate_failure: bool = False
    method: str = "mock"


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok", "service": "order"}


@app.post("/orders", status_code=201)
async def create_order(
    payload: CreateOrderRequest,
    principal: PrincipalDep,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> dict[str, Any]:
    assert idem is not None
    assert bus is not None
    scope = "order-create"
    cached = await idem.get(scope, idempotency_key)
    if cached is not None:
        return cached
    if idempotency_key and not await idem.begin(scope, idempotency_key):
        cached = await idem.get(scope, idempotency_key)
        if cached is not None:
            return cached
        raise ApiError("IDEMPOTENCY_KEY_REUSED", "相同幂等键的订单正在创建中，请稍后重试")

    items: list[dict[str, Any]] = []
    amount = 0
    for item in payload.items:
        product = await catalog.request("GET", f"/products/{item.sku}")
        items.append(
            {
                "sku": product["sku"],
                "product_id": product["id"],
                "title": product["title"],
                "qty": item.qty,
                "price_cents": product["price_cents"],
            }
        )
        amount += product["price_cents"] * item.qty

    order = Order(
        id=str(uuid.uuid4()),
        order_no=f"SO{datetime.now(timezone.utc):%Y%m%d}{random.randint(0, 999999):06d}",
        user_id=principal.user_id,
        tenant=principal.tenant,
        status="CREATED",
        amount_cents=amount,
        items=items,
        address=payload.address.model_dump(),
    )
    async with get_session_factory()() as session:
        session.add(order)
        enqueue(session, "order", "order.created", {"order_id": order.id, "amount_cents": amount}, tenant=principal.tenant)
        await session.commit()

    reserved, reason = await _reserve_inventory(order)
    async with get_session_factory()() as session:
        order = await session.get(Order, order.id)
        assert order is not None
        if reserved:
            _apply(session, order, "reserve", actor="system")
            enqueue(session, "order", "order.reserved", {"order_id": order.id}, tenant=principal.tenant)
        else:
            _cancel(order, session, reason)
            enqueue(
                session,
                "order",
                "order.cancelled",
                {"order_id": order.id, "reason": reason},
                tenant=principal.tenant,
            )
        await session.commit()
        result = order.to_dict()

    if not reserved:
        with contextlib.suppress(Exception):
            await idem.finish(scope, idempotency_key, result)
        raise ApiError("CONFLICT", "库存不足，订单已取消", details={"order": result, "reason": reason})

    await idem.finish(scope, idempotency_key, result)
    return result


@app.get("/orders")
async def list_orders(
    principal: PrincipalDep,
    status: str | None = None,
    user_id: str | None = None,
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
) -> dict[str, Any]:
    async with get_session_factory()() as session:
        stmt = select(Order).order_by(Order.created_at.desc())
        if principal.is_staff() and user_id:
            stmt = stmt.where(Order.user_id == user_id)
        elif not principal.is_staff():
            stmt = stmt.where(Order.user_id == principal.user_id)
        if status:
            stmt = stmt.where(Order.status == status)
        rows = (
            (await session.execute(stmt.offset((page - 1) * size).limit(size)))
            .scalars()
            .all()
        )
        return {"items": [row.to_dict() for row in rows], "page": page, "size": size}


@app.get("/orders/{order_id}")
async def get_order(order_id: str, principal: PrincipalDep) -> dict[str, Any]:
    async with get_session_factory()() as session:
        order = await _load(session, order_id)
        if not principal.is_staff() and order.user_id != principal.user_id:
            raise ApiError("FORBIDDEN", "无权查看该订单")
        return order.to_dict()


@app.get("/orders/{order_id}/timeline")
async def order_timeline(order_id: str, principal: PrincipalDep) -> dict[str, Any]:
    async with get_session_factory()() as session:
        order = await _load(session, order_id)
        if not principal.is_staff() and order.user_id != principal.user_id:
            raise ApiError("FORBIDDEN", "无权查看该订单")
        events = (
            (
                await session.execute(
                    select(OrderEvent)
                    .where(OrderEvent.order_id == order.id)
                    .order_by(OrderEvent.created_at)
                )
            )
            .scalars()
            .all()
        )
        return {
            "order_id": order.id,
            "status": order.status,
            "status_label": STATE_LABELS.get(order.status, order.status),
            "events": [
                {
                    "event": item.event,
                    "from": STATE_LABELS.get(item.from_status, item.from_status),
                    "to": STATE_LABELS.get(item.to_status, item.to_status),
                    "actor": item.actor,
                    "detail": item.detail,
                    "at": item.created_at.isoformat(),
                }
                for item in events
            ],
        }


@app.post("/orders/{order_id}/pay")
async def pay_order(order_id: str, payload: PayRequest, principal: PrincipalDep) -> dict[str, Any]:
    """模拟支付网关。真实系统在此处接第三方支付并处理异步回调与对账。"""
    assert bus is not None
    async with get_session_factory()() as session:
        order = await _load(session, order_id)
        if not principal.is_staff() and order.user_id != principal.user_id:
            raise ApiError("FORBIDDEN", "无权支付该订单")
        if order.status == "PAID":
            return {**order.to_dict(), "idempotent": True}
        _apply(session, order, "pay_start", actor=principal.user_id, detail={"method": payload.method})
        await session.commit()
        await session.refresh(order)

        if payload.simulate_failure:
            _apply(session, order, "pay_fail", actor=principal.user_id, detail={"reason": "mock_decline"})
            await session.commit()
            raise ApiError("CONFLICT", "支付失败，请更换支付方式", details={"order": order.to_dict()})

        _apply(session, order, "pay_success", actor=principal.user_id, detail={"method": payload.method})
        order.paid_at = datetime.now(timezone.utc)
        enqueue(session, "order", "order.paid", {"order_id": order.id, "amount_cents": order.amount_cents})
        await session.commit()
        result = order.to_dict()

    await _commit_inventory(order_id)
    return result


@app.post("/orders/{order_id}/cancel")
async def cancel_order(order_id: str, principal: PrincipalDep, reason: str = "user_cancelled") -> dict[str, Any]:
    async with get_session_factory()() as session:
        order = await _load(session, order_id)
        if not principal.is_staff() and order.user_id != principal.user_id:
            raise ApiError("FORBIDDEN", "无权取消该订单")
        _cancel(order, session, reason)
        enqueue(session, "order", "order.cancelled", {"order_id": order.id, "reason": reason})
        await session.commit()
        result = order.to_dict()
    await _release_inventory(order_id, reason)
    return result


@app.post("/orders/{order_id}/ship")
async def mark_shipped(order_id: str, principal: PrincipalDep) -> dict[str, Any]:
    principal.require_staff()
    async with get_session_factory()() as session:
        order = await _load(session, order_id)
        _apply(session, order, "ship", actor=principal.user_id)
        order.shipped_at = datetime.now(timezone.utc)
        await session.commit()
        return order.to_dict()


@app.post("/orders/{order_id}/complete")
async def mark_completed(order_id: str, principal: PrincipalDep) -> dict[str, Any]:
    principal.require_staff()
    async with get_session_factory()() as session:
        order = await _load(session, order_id)
        _apply(session, order, "deliver", actor=principal.user_id)
        order.completed_at = datetime.now(timezone.utc)
        await session.commit()
        return order.to_dict()


@app.post("/orders/{order_id}/refund")
async def request_refund(order_id: str, principal: PrincipalDep, amount_cents: int | None = None) -> dict[str, Any]:
    """售后审批通过后调用：进入退款处理中。"""
    assert bus is not None
    async with get_session_factory()() as session:
        order = await _load(session, order_id)
        if order.status == "REFUNDING":
            return order.to_dict()
        order.refund_previous = order.status
        _apply(session, order, "refund_request", actor=principal.user_id)
        order.refund_amount_cents = amount_cents or order.amount_cents
        await session.commit()
        result = order.to_dict()
    await bus.publish("order", "order.refunding", {"order_id": order_id})
    return result


@app.post("/orders/{order_id}/refund-complete")
async def complete_refund(order_id: str, principal: PrincipalDep) -> dict[str, Any]:
    principal.require_staff()
    async with get_session_factory()() as session:
        order = await _load(session, order_id)
        _apply(session, order, "refund_complete", actor=principal.user_id)
        await session.commit()
        return order.to_dict()
