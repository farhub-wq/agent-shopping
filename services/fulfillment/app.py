import asyncio
import contextlib
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import FastAPI, Query
from pydantic import BaseModel
from redis.asyncio import Redis
from sqlalchemy import select

from shop_common.auth import PrincipalDep
from shop_common.config import get_settings
from shop_common.db import get_session_factory, init_models
from shop_common.errors import ApiError, install_error_handlers
from shop_common.events import EventBus, create_redis
from shop_common.logging import configure_logging, get_logger
from shop_common.outbox import OutboxRelay, enqueue

from .models import Shipment

settings = get_settings()
logger = get_logger(__name__)

redis: Redis | None = None
bus: EventBus | None = None


async def handle_order_paid(event: dict[str, Any]) -> None:
    if event.get("type") != "order.paid":
        return
    order_id = event["payload"]["order_id"]
    async with get_session_factory()() as session:
        existing = await session.scalar(select(Shipment).where(Shipment.order_id == order_id))
        if existing is not None:
            return
        shipment = Shipment(
            id=str(uuid.uuid4()),
            order_id=order_id,
            deadline_at=datetime.now(timezone.utc) + timedelta(hours=settings.fulfillment_sla_hours),
        )
        session.add(shipment)
        await session.commit()
        logger.info("已为订单 %s 创建履约单", order_id)


async def sla_scanner() -> None:
    """履约超时扫描：超时不是等用户投诉才发现，而是主动触发告警与升级。"""
    while True:
        await asyncio.sleep(30)
        try:
            async with get_session_factory()() as session:
                overdue = (
                    (
                        await session.execute(
                            select(Shipment).where(
                                Shipment.status == "pending",
                                Shipment.sla_flagged.is_(False),
                                Shipment.deadline_at < datetime.now(timezone.utc),
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                for shipment in overdue:
                    shipment.sla_flagged = True
                    enqueue(
                        session,
                        "fulfillment",
                        "fulfillment.sla_breached",
                        {"shipment_id": shipment.id, "order_id": shipment.order_id},
                    )
                if overdue:
                    await session.commit()
                    logger.warning("检测到 %s 条履约超时", len(overdue))
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("履约超时扫描失败")


@asynccontextmanager
async def lifespan(_: FastAPI):
    global redis, bus
    configure_logging("fulfillment")
    await init_models()
    redis = create_redis(settings.redis_url)
    bus = EventBus(redis, namespace="shop")
    relay = OutboxRelay(get_session_factory(), bus)
    tasks = [
        asyncio.create_task(relay.run_forever()),
        asyncio.create_task(bus.run_consumer("order", "fulfillment-svc", "fulfillment-1", handle_order_paid)),
        asyncio.create_task(sla_scanner()),
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


app = FastAPI(title="fulfillment-service", version="0.1.0", lifespan=lifespan)
install_error_handlers(app, "fulfillment")


class ShipRequest(BaseModel):
    carrier: str = "SF"
    tracking_no: str = ""


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok", "service": "fulfillment"}


@app.get("/shipments")
async def list_shipments(
    principal: PrincipalDep,
    status: str | None = None,
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
) -> dict[str, Any]:
    principal.require_staff()
    async with get_session_factory()() as session:
        stmt = select(Shipment).order_by(Shipment.created_at.desc())
        if status:
            stmt = stmt.where(Shipment.status == status)
        rows = (await session.execute(stmt.offset((page - 1) * size).limit(size))).scalars().all()
        return {"items": [row.to_dict() for row in rows], "page": page, "size": size}


@app.get("/shipments/by-order/{order_id}")
async def shipment_by_order(order_id: str, principal: PrincipalDep) -> dict[str, Any]:
    async with get_session_factory()() as session:
        shipment = await session.scalar(select(Shipment).where(Shipment.order_id == order_id))
        if shipment is None:
            raise ApiError("NOT_FOUND", "该订单尚未进入履约")
        return shipment.to_dict()


@app.post("/shipments/{shipment_id}/ship")
async def ship(shipment_id: str, payload: ShipRequest, principal: PrincipalDep) -> dict[str, Any]:
    principal.require_staff()
    async with get_session_factory()() as session:
        shipment = await session.get(Shipment, shipment_id)
        if shipment is None:
            raise ApiError("NOT_FOUND", "履约单不存在")
        if shipment.status != "pending":
            raise ApiError("CONFLICT", f"履约单状态 {shipment.status} 不可发货")
        shipment.status = "shipped"
        shipment.carrier = payload.carrier
        shipment.tracking_no = payload.tracking_no or f"{payload.carrier}{uuid.uuid4().hex[:12].upper()}"
        shipment.shipped_at = datetime.now(timezone.utc)
        enqueue(
            session,
            "fulfillment",
            "fulfillment.shipped",
            {
                "shipment_id": shipment.id,
                "order_id": shipment.order_id,
                "tracking_no": shipment.tracking_no,
            },
        )
        await session.commit()
        return shipment.to_dict()


@app.post("/shipments/{shipment_id}/deliver")
async def deliver(shipment_id: str, principal: PrincipalDep) -> dict[str, Any]:
    principal.require_staff()
    async with get_session_factory()() as session:
        shipment = await session.get(Shipment, shipment_id)
        if shipment is None:
            raise ApiError("NOT_FOUND", "履约单不存在")
        if shipment.status != "shipped":
            raise ApiError("CONFLICT", "只有已发货的履约单可签收")
        shipment.status = "delivered"
        shipment.delivered_at = datetime.now(timezone.utc)
        enqueue(
            session,
            "fulfillment",
            "fulfillment.delivered",
            {"shipment_id": shipment.id, "order_id": shipment.order_id},
        )
        await session.commit()
        return shipment.to_dict()
