import asyncio
import contextlib
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import FastAPI, Query
from pydantic import BaseModel
from redis.asyncio import Redis
from shop_common.auth import PrincipalDep
from shop_common.config import get_settings
from shop_common.db import get_session_factory, init_models
from shop_common.errors import ApiError, install_error_handlers
from shop_common.events import EventBus, create_redis
from shop_common.logging import configure_logging, get_logger
from shop_common.outbox import OutboxRelay, enqueue
from sqlalchemy import select

from .models import Shipment

settings = get_settings()
logger = get_logger(__name__)

redis: Redis | None = None
bus: EventBus | None = None


async def handle_order_paid(event: dict[str, Any]) -> None:
    # 同一消费组会收到多种订单事件，必须先按 type 过滤，否则会把非支付消息当成付款处理
    if event.get("type") != "order.paid":
        return
    order_id = event["payload"]["order_id"]
    async with get_session_factory()() as session:
        # outbox 重投可能重复投递同一事件，用 order_id 唯一性做幂等兜底：已建单则直接跳过
        existing = await session.scalar(select(Shipment).where(Shipment.order_id == order_id))
        if existing is not None:
            return
        # 建单时即写入 SLA 截止时间，让超时判定有确定性依据，而不是依赖扫描时的相对计算
        shipment = Shipment(
            id=str(uuid.uuid4()),
            order_id=order_id,
            deadline_at=datetime.now(UTC) + timedelta(hours=settings.fulfillment_sla_hours),
        )
        session.add(shipment)
        await session.commit()
        logger.info("已为订单 %s 创建履约单", order_id)


async def sla_scanner() -> None:
    """履约超时扫描：超时不是等用户投诉才发现，而是主动触发告警与升级。"""
    while True:
        # 固定 30 秒轮询：超时告警允许分钟级延迟，换取低频扫描，避免频繁压库
        await asyncio.sleep(30)
        try:
            async with get_session_factory()() as session:
                # 只扫「未发货 + 未标记 + 已过截止时间」的履约单；sla_flagged 作为去重标记，防止同一单反复告警
                overdue = (
                    (
                        await session.execute(
                            select(Shipment).where(
                                Shipment.status == "pending",
                                Shipment.sla_flagged.is_(False),
                                Shipment.deadline_at < datetime.now(UTC),
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                for shipment in overdue:
                    shipment.sla_flagged = True
                    # 超时事件与标记位在同一事务内入 outbox，保证「已告警」与「已发事件」不会各自丢失
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
            # 服务关闭时取消任务属于正常路径，向上抛出交由 lifespan 收敛
            raise
        except Exception:
            # 单轮扫描失败不能杀死常驻任务，记录堆栈后继续下一轮
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
        # 三组长驻任务：outbox 投递、订单事件消费、SLA 扫描，随应用生命周期统一启动与取消
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
    # 列表是运营视图，需员工身份；size 上限由参数校验兜住，避免一次拉全表
    principal.require_staff()
    async with get_session_factory()() as session:
        stmt = select(Shipment).order_by(Shipment.created_at.desc())
        if status:
            stmt = stmt.where(Shipment.status == status)
        rows = (await session.execute(stmt.offset((page - 1) * size).limit(size))).scalars().all()
        return {"items": [row.to_dict() for row in rows], "page": page, "size": size}


@app.get("/shipments/by-order/{order_id}")
async def shipment_by_order(order_id: str, principal: PrincipalDep) -> dict[str, Any]:
    # 按订单查履约单是用户侧高频入口，不做员工限制，但只返回本服务自有字段
    async with get_session_factory()() as session:
        shipment = await session.scalar(select(Shipment).where(Shipment.order_id == order_id))
        if shipment is None:
            raise ApiError("NOT_FOUND", "该订单尚未进入履约")
        return shipment.to_dict()


@app.post("/shipments/{shipment_id}/ship")
async def ship(shipment_id: str, payload: ShipRequest, principal: PrincipalDep) -> dict[str, Any]:
    # 发货是不可逆的状态推进，只允许内部员工操作
    principal.require_staff()
    async with get_session_factory()() as session:
        shipment = await session.get(Shipment, shipment_id)
        if shipment is None:
            raise ApiError("NOT_FOUND", "履约单不存在")
        # 用状态前置判断拦截重复发货：并发重复请求会在这里被判冲突，而不是互相覆盖数据
        if shipment.status != "pending":
            raise ApiError("CONFLICT", f"履约单状态 {shipment.status} 不可发货")
        shipment.status = "shipped"
        shipment.carrier = payload.carrier
        # 承运方未给单号时本地生成占位单号，保证下游事件始终有可追踪标识
        shipment.tracking_no = payload.tracking_no or f"{payload.carrier}{uuid.uuid4().hex[:12].upper()}"
        shipment.shipped_at = datetime.now(UTC)
        # 状态变更与发货事件同事务入 outbox，避免出现「已发货但订单服务没收到事件」的不一致
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
        # 签收只能在「已发货」之后，防止跳过发货直接完成履约
        if shipment.status != "shipped":
            raise ApiError("CONFLICT", "只有已发货的履约单可签收")
        shipment.status = "delivered"
        shipment.delivered_at = datetime.now(UTC)
        # 与发货同理：签收状态和事件必须原子落库，才能驱动订单侧转为已完成
        enqueue(
            session,
            "fulfillment",
            "fulfillment.delivered",
            {"shipment_id": shipment.id, "order_id": shipment.order_id},
        )
        await session.commit()
        return shipment.to_dict()
