import asyncio
import contextlib
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import FastAPI, Query, Request
from pydantic import BaseModel, Field
from redis.asyncio import Redis
from shop_common.audit import enqueue_audit
from shop_common.auth import PrincipalDep, signed_identity_headers
from shop_common.config import get_settings, validate_production_settings
from shop_common.db import dispose_engine, get_session_factory, init_models
from shop_common.errors import ApiError, install_error_handlers
from shop_common.events import EventBus, create_redis
from shop_common.http import ServiceClient
from shop_common.logging import configure_logging, get_logger
from shop_common.metrics import install_metrics
from shop_common.outbox import OutboxRelay, enqueue
from shop_common.tasks import run_task_loop
from sqlalchemy import select

from .models import Shipment, ShipmentItem

settings = get_settings()
logger = get_logger(__name__)

redis: Redis | None = None
bus: EventBus | None = None
order_client = ServiceClient(settings.order_url)


async def _items_map(session, shipment_ids: list[str]) -> dict[str, list[dict]]:
    """批量查询履约行，避免列表页 N+1 查询。"""
    if not shipment_ids:
        return {}
    rows = (
        await session.execute(select(ShipmentItem).where(ShipmentItem.shipment_id.in_(shipment_ids)))
    ).scalars().all()
    result: dict[str, list[dict]] = {}
    for row in rows:
        result.setdefault(row.shipment_id, []).append(row.to_dict())
    return result


async def handle_order_paid(event: dict[str, Any]) -> None:
    # 同一消费组会收到多种订单事件，必须先按 type 过滤，否则会把非支付消息当成付款处理
    if event.get("type") != "order.paid":
        return
    order_id = event["payload"]["order_id"]
    async with get_session_factory()() as session:
        # outbox 重投可能重复投递同一事件：订单只要已有履约单就跳过，避免重复拆单
        existing = await session.scalar(select(Shipment).where(Shipment.order_id == order_id))
        if existing is not None:
            return
        # 履约行必须来自订单快照，不能信任客户端传的 SKU 与数量
        order = await order_client.request(
            "GET", f"/orders/{order_id}", headers=signed_identity_headers("system", "operator")
        )
        items = order.get("items") or []
        if not items:
            logger.warning("订单 %s 没有可履约行，跳过建单", order_id)
            return
        # 建单时即写入 SLA 截止时间，让超时判定有确定性依据，而不是依赖扫描时的相对计算
        shipment = Shipment(
            id=str(uuid.uuid4()),
            order_id=order_id,
            deadline_at=datetime.now(UTC) + timedelta(hours=settings.fulfillment_sla_hours),
        )
        session.add(shipment)
        for item in items:
            session.add(
                ShipmentItem(
                    shipment_id=shipment.id,
                    order_item_id=str(item.get("item_id") or item.get("sku")),
                    sku=str(item.get("sku") or ""),
                    qty=int(item.get("qty") or 0),
                )
            )
        await session.commit()
        logger.info("已为订单 %s 创建履约单", order_id)


async def _sla_scan_once() -> None:
    """单轮履约超时扫描：超时不是等用户投诉才发现，而是主动触发告警与升级。"""
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


@asynccontextmanager
async def lifespan(_: FastAPI):
    global redis, bus
    configure_logging("fulfillment")
    # 生产密钥仍是公开默认值时拒绝启动：否则 JWT 与内网签名身份头都可被任意伪造。
    validate_production_settings(settings)
    await init_models()
    redis = create_redis(settings.redis_url)
    bus = EventBus(redis, namespace="shop", session_factory=get_session_factory())
    relay = OutboxRelay(get_session_factory(), bus)
    tasks = [
        # 三组长驻任务：outbox 投递、订单事件消费、SLA 扫描，随应用生命周期统一启动与取消
        asyncio.create_task(relay.run_forever()),
        asyncio.create_task(bus.run_consumer("order", "fulfillment-svc", "fulfillment-1", handle_order_paid)),
        asyncio.create_task(run_task_loop("fulfillment-sla", redis, get_session_factory(), 30, _sla_scan_once)),
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
        # 排空在途请求后释放连接池，避免停机时残留连接占住数据库
        await dispose_engine()


app = FastAPI(title="fulfillment-service", version="0.1.0", lifespan=lifespan)
install_error_handlers(app, "fulfillment")
install_metrics(app, "fulfillment")


class ShipItemInput(BaseModel):
    order_item_id: str = Field(min_length=1)
    qty: int = Field(gt=0)


class ShipRequest(BaseModel):
    carrier: str = "SF"
    tracking_no: str = ""
    # 不传 items 表示整单发货；传了则只发指定行与数量，剩余自动拆到新的 pending 单
    items: list[ShipItemInput] | None = None


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
        item_map = await _items_map(session, [row.id for row in rows])
        return {
            "items": [row.to_dict(item_map.get(row.id, [])) for row in rows],
            "page": page,
            "size": size,
        }


@app.get("/shipments/by-order/{order_id}")
async def shipment_by_order(order_id: str, principal: PrincipalDep) -> dict[str, Any]:
    # 按订单查履约单是用户侧高频入口：非员工必须先确认订单归属，
    # 否则拿到 order_id 就能枚举他人物流状态。
    if not principal.is_staff():
        if principal.user_id == "guest":
            raise ApiError("FORBIDDEN", "请先登录后查看履约信息")
        try:
            order = await order_client.request(
                "GET",
                f"/orders/{order_id}",
                headers=signed_identity_headers(principal.user_id, principal.role, principal.tenant),
            )
        except ApiError as exc:
            # 订单不存在与无权查看统一返回 403，避免通过状态码探测他人订单
            if exc.code in {"FORBIDDEN", "NOT_FOUND"}:
                raise ApiError("FORBIDDEN", "无权查看该订单的履约信息") from exc
            raise
        if order.get("user_id") != principal.user_id:
            raise ApiError("FORBIDDEN", "无权查看该订单的履约信息")
    async with get_session_factory()() as session:
        rows = (
            (
                await session.execute(
                    select(Shipment)
                    .where(Shipment.order_id == order_id)
                    .order_by(Shipment.created_at)
                )
            )
            .scalars()
            .all()
        )
        if not rows:
            raise ApiError("NOT_FOUND", "该订单尚未进入履约")
        item_map = await _items_map(session, [row.id for row in rows])
        return {"order_id": order_id, "items": [row.to_dict(item_map.get(row.id, [])) for row in rows]}


@app.post("/shipments/{shipment_id}/ship")
async def ship(shipment_id: str, payload: ShipRequest, principal: PrincipalDep, request: Request) -> dict[str, Any]:
    # 发货是不可逆的状态推进，只允许内部员工操作
    principal.require_staff()
    async with get_session_factory()() as session:
        shipment = await session.get(Shipment, shipment_id)
        if shipment is None:
            raise ApiError("NOT_FOUND", "履约单不存在")
        # 用状态前置判断拦截重复发货：并发重复请求会在这里被判冲突，而不是互相覆盖数据
        if shipment.status != "pending":
            raise ApiError("CONFLICT", f"履约单状态 {shipment.status} 不可发货")
        rows = (
            (await session.execute(select(ShipmentItem).where(ShipmentItem.shipment_id == shipment.id)))
            .scalars()
            .all()
        )
        if not rows:
            raise ApiError("CONFLICT", "履约单没有可发货行")

        selected: dict[str, int] = {}
        if payload.items is not None:
            # 显式传空 items 表示「什么都不发」，不能退化成整单发货
            if not payload.items:
                raise ApiError("BAD_REQUEST", "发货明细不能为空")
            for row in payload.items:
                if row.order_item_id in selected:
                    raise ApiError("BAD_REQUEST", "同一订单行不能重复发货")
                selected[row.order_item_id] = row.qty
            known = {item.order_item_id: item for item in rows}
            for item_id, qty in selected.items():
                if item_id not in known:
                    raise ApiError("CONFLICT", "发货行不属于该履约单")
                if qty > known[item_id].qty:
                    raise ApiError("CONFLICT", "发货数量超过履约数量")
        else:
            selected = {item.order_item_id: item.qty for item in rows}

        # 未选中的剩余数量拆到新的 pending 单，保证「一张待发货单」始终只覆盖未发部分
        shipped_items = [
            {"item_id": item.order_item_id, "qty": selected.get(item.order_item_id, 0)}
            for item in rows
            if selected.get(item.order_item_id, 0) > 0
        ]
        leftover = [item for item in rows if item.qty - selected.get(item.order_item_id, 0) > 0]
        if leftover:
            child = Shipment(
                id=str(uuid.uuid4()),
                order_id=shipment.order_id,
                # 子单继承父单 SLA 截止时间，避免部分发货把整体履约时限无限顺延
                deadline_at=shipment.deadline_at,
            )
            session.add(child)
            for item in leftover:
                session.add(
                    ShipmentItem(
                        shipment_id=child.id,
                        order_item_id=item.order_item_id,
                        sku=item.sku,
                        qty=item.qty - selected.get(item.order_item_id, 0),
                    )
                )
                item.qty = selected.get(item.order_item_id, 0)
                if item.qty == 0:
                    await session.delete(item)

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
                # 订单侧按 item_id 累加已发数量，all_shipped 决定是否整体转 SHIPPED
                "items": shipped_items,
                "all_shipped": not leftover,
            },
        )
        enqueue_audit(
            session,
            actor_id=principal.user_id,
            actor_role=principal.role,
            action="fulfillment.ship",
            target_type="shipment",
            target_id=shipment.id,
            before={"status": "pending"},
            after={"status": "shipped", "tracking_no": shipment.tracking_no, "items": shipped_items},
            request=request,
            tenant=principal.tenant,
        )
        await session.commit()
        return shipment.to_dict(shipped_items)


@app.post("/shipments/{shipment_id}/deliver")
async def deliver(shipment_id: str, principal: PrincipalDep, request: Request) -> dict[str, Any]:
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
        # 只有该订单所有履约单都签收后，订单才允许整体转 COMPLETED
        remaining = await session.scalar(
            select(Shipment.id).where(
                Shipment.order_id == shipment.order_id,
                Shipment.id != shipment.id,
                Shipment.status.in_({"pending", "shipped"}),
            )
        )
        # 与发货同理：签收状态和事件必须原子落库，才能驱动订单侧转为已完成
        enqueue(
            session,
            "fulfillment",
            "fulfillment.delivered",
            {
                "shipment_id": shipment.id,
                "order_id": shipment.order_id,
                "all_delivered": remaining is None,
            },
        )
        enqueue_audit(
            session,
            actor_id=principal.user_id,
            actor_role=principal.role,
            action="fulfillment.deliver",
            target_type="shipment",
            target_id=shipment.id,
            before={"status": "shipped"},
            after={"status": "delivered"},
            request=request,
            tenant=principal.tenant,
        )
        await session.commit()
        return shipment.to_dict()
