import asyncio
import contextlib
import random
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import FastAPI, Header, Query
from pydantic import BaseModel, Field
from redis.asyncio import Redis
from shop_common.auth import PrincipalDep, signed_identity_headers
from shop_common.config import get_settings
from shop_common.db import get_session_factory, init_models
from shop_common.errors import ApiError, install_error_handlers
from shop_common.events import EventBus, create_redis
from shop_common.http import ServiceClient
from shop_common.idempotency import IdempotencyStore
from shop_common.logging import configure_logging, get_logger
from shop_common.outbox import OutboxRelay, enqueue
from shop_common.tasks import run_task_loop
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from .models import Order, OrderEvent
from .state_machine import STATE_LABELS, resolve_target

settings = get_settings()
logger = get_logger(__name__)

redis: Redis | None = None
bus: EventBus | None = None
idem: IdempotencyStore | None = None
catalog = ServiceClient(settings.catalog_url)
inventory = ServiceClient(settings.inventory_url)
payment_svc = ServiceClient(settings.payment_url)


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
    # 状态变更唯一入口：先由状态机校验并算出目标状态，非法流转直接抛 CONFLICT。
    target = resolve_target(order.status, event, refund_previous=order.refund_previous)
    # 状态与轨迹在同一 session 内写入，随调用方一起 commit，避免出现「状态变了但没轨迹」。
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
    order.updated_at = datetime.now(UTC)
    return target


def _record(
    session: AsyncSession,
    order: Order,
    event: str,
    *,
    actor: str,
    detail: dict[str, Any] | None = None,
) -> None:
    """非状态流转的审计轨迹：部分发货/部分签收不改变订单状态，但仍要留下记录。"""
    session.add(
        OrderEvent(
            order_id=order.id,
            from_status=order.status,
            to_status=order.status,
            event=event,
            actor=actor,
            detail=detail or {},
        )
    )
    order.updated_at = datetime.now(UTC)


def _item_of(order: Order, item_id: str) -> dict[str, Any] | None:
    for item in order.items or []:
        if item.get("item_id") == item_id:
            return item
    return None


def _seen(order: Order, event_id: str | None) -> bool:
    """事件级幂等：同一事件重复投递直接跳过，避免部分发货/退款数量被二次累加。"""
    if not event_id:
        return False
    processed = list(order.processed_event_ids or [])
    if event_id in processed:
        return True
    # 只保留最近 200 个事件 ID，长期运行的订单不无限膨胀 JSON
    order.processed_event_ids = [*processed[-199:], event_id]
    flag_modified(order, "processed_event_ids")
    return False


def _touch_items(order: Order) -> None:
    # JSON 列是整体替换语义，原地修改后必须显式标记，否则 SQLAlchemy 不会写回
    flag_modified(order, "items")


async def _load(session: AsyncSession, order_id: str) -> Order:
    # 路径参数同时兼容内部主键 id 与对用户展示的 order_no。
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
        # Idempotency-Key 固定为订单 id，重复调用由库存服务按预占记录去重。
        await inventory.request(
            "POST",
            "/inventory/reserve",
            json={"order_id": order.id, "items": order.items},
            headers={"Idempotency-Key": order.id, **signed_identity_headers("system", "operator", order.tenant)},
        )
        return True, ""
    except ApiError as exc:
        # 库存不足与库存服务不可用需要区分原因，调用方据此给出不同取消理由。
        if exc.code == "CONFLICT":
            return False, "out_of_stock"
        return False, "inventory_unavailable"


async def _release_inventory(order_id: str, reason: str, *, ensure_closed: bool = True) -> None:
    # 释放库存前必须先关闭支付单（非 pending）：若释放后用户才付款成功，
    # 订单已取消无法履约，只能走迟到支付自动退款。先关单把竞态窗口压到最小，
    # payment 不可用时 fail closed（宁少卖不超卖），由补偿/兜底扫描重试。
    if ensure_closed:
        try:
            await payment_svc.request(
                "POST",
                f"/payments/{order_id}/expire",
                json={},
                headers=signed_identity_headers("system", "operator"),
            )
        except ApiError as exc:
            logger.error("关单失败，暂不释放库存 order_id=%s code=%s", order_id, exc.code)
            return
    # 释放是尽力而为：库存侧还有超时回补兜底，不能因下游失败回滚订单已提交的取消状态。
    with contextlib.suppress(ApiError):
        await inventory.request(
            "POST",
            "/inventory/release",
            json={"order_id": order_id, "reason": reason},
            # 内部释放以系统身份调用，必须带签名头，否则下游会按未签名请求拒绝
            headers=signed_identity_headers("system", "operator"),
        )


async def _commit_inventory(order_id: str) -> bool:
    """支付成功后把预占转为已售；失败返回 False，由补偿扫单持续重试（接口幂等）。"""
    try:
        await inventory.request(
            "POST",
            "/inventory/commit",
            json={"order_id": order_id},
            # 与预占/释放一致：库存服务开启身份头信任后，未签名调用会被 401 拒绝
            headers=signed_identity_headers("system", "operator"),
        )
        return True
    except ApiError as exc:
        # 不再静默吞掉：留下 ERROR 日志供告警，订单侧 inventory_committed 保持 False
        logger.error("库存提交失败 order_id=%s code=%s message=%s", order_id, exc.code, exc.message)
        return False


async def _refund_late_payment(order_id: str, amount_cents: int) -> None:
    """迟到支付自动全额原路退款；ticket_id 固定，payment 侧按 ticket 幂等，可安全重试。"""
    try:
        await payment_svc.request(
            "POST",
            "/payments/refunds",
            json={
                "order_id": order_id,
                "ticket_id": f"latepay-{order_id}",
                "amount_cents": amount_cents,
            },
            headers=signed_identity_headers("system", "operator"),
        )
    except ApiError as exc:
        # 失败不向上抛：补偿扫单按「CANCELLED + paid_at 已落」持续重试，退款按 ticket_id 幂等
        logger.error("迟到支付自动退款失败 order_id=%s code=%s", order_id, exc.code)


def _restock_targets(items: list | None) -> list[dict[str, Any]]:
    """未发货数量的回补目标（绝对值）；已发货部分需退货收货确认流程，不在此回补。"""
    targets: list[dict[str, Any]] = []
    for row in items or []:
        target = min(
            int(row.get("refunded_qty", 0)),
            int(row.get("qty", 0)) - int(row.get("shipped_qty", 0)),
        )
        if target > 0:
            targets.append({"sku": row["sku"], "qty": target})
    return targets


async def _restock_inventory(order_id: str, items: list[dict[str, Any]]) -> None:
    """按目标值回补库存；inventory 按 (order_id, sku) 记录已回补量，重放幂等。"""
    try:
        await inventory.request(
            "POST",
            "/inventory/restock",
            json={"order_id": order_id, "items": items},
            headers=signed_identity_headers("system", "operator"),
        )
    except ApiError as exc:
        # 失败由补偿扫单重放目标值，不阻断退款主流程
        logger.error("库存回补失败 order_id=%s code=%s", order_id, exc.code)


async def _create_payment(order: Order) -> dict[str, Any] | None:
    """创建支付单；失败返回 None，由调用方取消订单并补偿库存。"""
    try:
        return await payment_svc.request(
            "POST",
            "/payments",
            json={
                "order_id": order.id,
                "amount_cents": order.amount_cents,
                "currency": order.currency,
                "tenant": order.tenant,
            },
            headers=signed_identity_headers("system", "operator", order.tenant),
        )
    except ApiError:
        logger.exception("创建支付单失败 order_id=%s", order.id)
        return None


async def _expire_unpaid_once() -> None:
    """单轮超时关单：覆盖「库存已锁但支付单未建成」和支付事件丢失的窗口。"""
    now = datetime.now(UTC)
    expired_ids: list[str] = []
    async with get_session_factory()() as session:
        rows = (
            (
                await session.execute(
                    select(Order).where(
                        Order.status.in_({"RESERVED", "PAYING"}),
                        (
                            (Order.payment_expires_at.is_not(None) & (Order.payment_expires_at < now))
                            | (
                                Order.payment_id.is_(None)
                                & (
                                    Order.created_at
                                    < now - timedelta(seconds=settings.payment_ttl_seconds)
                                )
                            )
                        ),
                    )
                )
            )
            .scalars()
            .all()
        )
        for order in rows:
            _cancel(order, session, "payment_timeout")
            enqueue(
                session,
                "order",
                "order.cancelled",
                {"order_id": order.id, "reason": "payment_timeout"},
                tenant=order.tenant,
            )
            expired_ids.append(order.id)
        if expired_ids:
            await session.commit()
    # 事务提交后再释放库存；释放失败可重试，不会把订单状态回滚
    for order_id in expired_ids:
        await _release_inventory(order_id, "payment_timeout")
    if expired_ids:
        logger.info("兜底关闭超时未支付订单 %s 笔", len(expired_ids))


# 补偿扫单覆盖的三类失步窗口，所有补偿操作都幂等，可无限重放
_LATE_REFUND_REASONS = {"payment_timeout", "payment_expired"}
# 库存提交/回补只在「钱已收」的状态上做；CANCELLED 订单走预占释放，不在补偿范围内
_COMMITTED_STATUSES = {"PAID", "SHIPPING", "SHIPPED", "COMPLETED", "REFUNDING", "REFUNDED"}


async def _compensation_once() -> None:
    """单轮补偿：迟到支付退款重试、库存提交重试、库存回补重放。"""
    async with get_session_factory()() as session:
        late = (
            (
                await session.execute(
                    select(Order).where(
                        Order.status == "CANCELLED",
                        Order.paid_at.is_not(None),
                        Order.cancel_reason.in_(_LATE_REFUND_REASONS),
                    )
                )
            )
            .scalars()
            .all()
        )
        pending_commit = (
            (
                await session.execute(
                    select(Order).where(
                        Order.status.in_(_COMMITTED_STATUSES),
                        Order.inventory_committed.is_(False),
                    )
                )
            )
            .scalars()
            .all()
        )
        pending_restock = (
            (
                await session.execute(
                    select(Order).where(
                        Order.status.in_(_COMMITTED_STATUSES),
                        Order.inventory_committed.is_(True),
                        Order.refund_amount_cents > 0,
                    )
                )
            )
            .scalars()
            .all()
        )
        late_refs = [(row.id, row.amount_cents) for row in late]
        commit_ids = [row.id for row in pending_commit]
        restock_refs = [(row.id, _restock_targets(row.items)) for row in pending_restock]
    # 外部调用全部放在会话外，避免长事务占连接
    for order_id, amount in late_refs:
        await _refund_late_payment(order_id, amount)
    for order_id in commit_ids:
        # 永久失败（预占已被 TTL 释放）会持续返回 False 并打 ERROR 日志，按决策告警后人工处理
        if await _commit_inventory(order_id):
            async with get_session_factory()() as session:
                row = await session.get(Order, order_id)
                if row is not None:
                    row.inventory_committed = True
                    await session.commit()
    for order_id, targets in restock_refs:
        if targets:
            await _restock_inventory(order_id, targets)


async def handle_event(event: dict[str, Any]) -> None:
    # 消费入口按事件 type 过滤；业务分支再校验当前状态与事件 ID，
    # 使重复投递成为无操作，从而保证消费者幂等。
    topic = event["type"]
    payload = event["payload"]
    event_id = event.get("id")
    release_reason: str | None = None
    commit_inventory = False
    late_refund_amount: int | None = None
    restock_items: list[dict[str, Any]] = []
    order_id = str(payload.get("order_id") or "")
    async with get_session_factory()() as session:
        order = await session.scalar(select(Order).where(Order.id == order_id)) if order_id else None
        if order is None:
            return
        if _seen(order, event_id):
            return

        if topic == "payment.succeeded":
            # 支付成功是资金事实：RESERVED 直接支付时先补 pay_start，再进入 PAID
            if order.status in {"RESERVED", "PAYING"}:
                if order.status == "RESERVED":
                    _apply(session, order, "pay_start", actor="payment")
                _apply(session, order, "pay_success", actor="payment")
                order.paid_at = datetime.now(UTC)
                enqueue(
                    session,
                    "order",
                    "order.paid",
                    {"order_id": order.id, "amount_cents": order.amount_cents},
                    tenant=order.tenant,
                )
                await session.commit()
                commit_inventory = True
            elif (
                order.status == "CANCELLED"
                and order.cancel_reason in {"payment_timeout", "payment_expired"}
                and order.paid_at is None
                and order.refund_amount_cents == 0
            ):
                # 迟到支付：订单已超时关单但用户实际付款成功，落资金事实并自动全额退款
                order.paid_at = datetime.now(UTC)
                _record(session, order, "late_payment", actor="payment")
                await session.commit()
                late_refund_amount = order.amount_cents
        elif topic == "payment.failed":
            # 支付失败允许顾客换渠道重试，退回 RESERVED，不取消订单、不动库存
            if order.status == "PAYING":
                _apply(session, order, "pay_fail", actor="payment")
                await session.commit()
        elif topic == "payment.expired":
            # 超时未支付：取消订单并释放库存，支付单状态由 payment 服务维护
            if order.status in {"RESERVED", "PAYING"}:
                _cancel(order, session, "payment_expired")
                enqueue(
                    session,
                    "order",
                    "order.cancelled",
                    {"order_id": order.id, "reason": "payment_expired"},
                    tenant=order.tenant,
                )
                await session.commit()
                release_reason = "payment_expired"
        elif topic == "payment.refunded":
            # 退款金额已由 payment 出款；订单只负责回写 item 可退余量与状态
            amount = int(payload.get("amount_cents") or 0)
            item_id = payload.get("order_item_id")
            if item_id:
                item = _item_of(order, str(item_id))
                if item is None:
                    raise ApiError("CONFLICT", "退款对应的订单行不存在")
                item["refunded_qty"] = int(item.get("refunded_qty", 0)) + int(payload.get("quantity") or 0)
                item["refunded_amount_cents"] = int(item.get("refunded_amount_cents", 0)) + amount
                if payload.get("fully_refunded"):
                    # 全额退款时以整行金额收口，避免四舍五入造成残留可退额
                    item["refunded_qty"] = int(item.get("qty", 0))
                    item["refunded_amount_cents"] = int(item.get("paid_amount_cents", 0))
            else:
                for item in order.items or []:
                    item["refunded_qty"] = int(item.get("qty", 0))
                    item["refunded_amount_cents"] = int(item.get("paid_amount_cents", 0))
            _touch_items(order)
            order.refund_amount_cents = int(order.refund_amount_cents or 0) + amount
            if order.status in {"PAID", "SHIPPING", "SHIPPED", "COMPLETED"}:
                order.refund_previous = order.status
                _apply(session, order, "refund_request", actor="payment")
            if order.status == "REFUNDING":
                if payload.get("fully_refunded"):
                    _apply(session, order, "refund_complete", actor="payment")
                else:
                    # 部分退款完成后退回退款前状态，订单不因一笔行级退款整体变成 REFUNDED
                    _apply(session, order, "refund_partial_complete", actor="payment")
            # 退款出款成功后回补未发货数量的库存；CANCELLED 订单的库存已由预占释放回补，跳过防双重回补
            if order.inventory_committed and order.status in {
                "PAID",
                "SHIPPING",
                "SHIPPED",
                "COMPLETED",
                "REFUNDING",
                "REFUNDED",
            }:
                restock_items = _restock_targets(order.items)
            await session.commit()
        elif topic == "fulfillment.shipped":
            items = payload.get("items") or []
            for row in items:
                item = _item_of(order, str(row.get("item_id", "")))
                if item is None:
                    continue
                item["shipped_qty"] = int(item.get("shipped_qty", 0)) + int(row.get("qty") or 0)
                # 防御性收口：发货总量不允许超过下单数量
                item["shipped_qty"] = min(item["shipped_qty"], int(item.get("qty", 0)))
            _touch_items(order)
            if payload.get("all_shipped"):
                # 全部发完：无论 PAID 还是 SHIPPING，都收敛到 SHIPPED
                if order.status in {"PAID", "SHIPPING"}:
                    _apply(session, order, "ship", actor="fulfillment")
                    order.shipped_at = datetime.now(UTC)
            elif order.status == "PAID":
                # 首次部分发货进入 SHIPPING；后续部分发货只记轨迹不迁移
                _apply(session, order, "ship_partial", actor="fulfillment")
            else:
                _record(session, order, "partial_ship", actor="fulfillment", detail={"items": items})
            await session.commit()
        elif topic == "fulfillment.delivered":
            if payload.get("all_delivered") and order.status == "SHIPPED":
                _apply(session, order, "deliver", actor="fulfillment")
                order.completed_at = datetime.now(UTC)
            else:
                _record(session, order, "partial_delivery", actor="fulfillment")
            await session.commit()
        elif topic == "after-sales.rejected":
            # 退款未出款即被驳回：把 REFUNDING 回退到退款前状态
            if order.status == "REFUNDING":
                _apply(session, order, "refund_reject", actor="aftersales")
                await session.commit()

    # 跨服务副作用放到事务提交之后，避免下游失败回滚已落库的状态
    if commit_inventory and await _commit_inventory(order_id):
        async with get_session_factory()() as session:
            # 提交成功立刻固化标志，补偿扫单据此跳过该订单
            row = await session.get(Order, order_id)
            if row is not None:
                row.inventory_committed = True
                await session.commit()
    if release_reason:
        # payment.expired 事件意味着支付单已关闭，无需再关一次，直接释放。
        await _release_inventory(order_id, release_reason, ensure_closed=False)
    if late_refund_amount is not None:
        await _refund_late_payment(order_id, late_refund_amount)
    if restock_items:
        await _restock_inventory(order_id, restock_items)


async def _startup() -> list[asyncio.Task]:
    global redis, bus, idem
    await init_models()
    redis = create_redis(settings.redis_url)
    bus = EventBus(redis, namespace="shop", session_factory=get_session_factory())
    idem = IdempotencyStore(redis, namespace="shop")
    relay = OutboxRelay(get_session_factory(), bus)
    # 同进程内并行跑 outbox 投递和三个消费组；消费组各自的游标保证不重复消费同一条消息。
    # 两个补偿/兜底扫描用 run_task_loop 加分布式锁，多实例下只有一个会执行。
    tasks = [
        asyncio.create_task(relay.run_forever()),
        asyncio.create_task(bus.run_consumer("fulfillment", "order-svc", "order-1", handle_event)),
        asyncio.create_task(bus.run_consumer("aftersales", "order-svc", "order-2", handle_event)),
        asyncio.create_task(bus.run_consumer("payment", "order-svc", "order-3", handle_event)),
        asyncio.create_task(run_task_loop("order-expire-unpaid", redis, get_session_factory(), 60, _expire_unpaid_once)),
        asyncio.create_task(run_task_loop("order-compensation", redis, get_session_factory(), 60, _compensation_once)),
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
    # 幂等三层：先查已完成结果直接返回；再用 begin 抢占执行权；并发重复请求拿不到锁时再查一次结果。
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
        # 价格与标题以 catalog 为准回填，绝不信任客户端传入的金额，防止篡改下单价格。
        product = await catalog.request("GET", f"/products/{item.sku}")
        items.append(
            {
                # item_id 是后续部分发货与部分退款的稳定业务键，不能依赖 SKU（同款可能多行）
                "item_id": uuid.uuid4().hex,
                "sku": product["sku"],
                "product_id": product["id"],
                "title": product["title"],
                "qty": item.qty,
                "price_cents": product["price_cents"],
                "paid_amount_cents": product["price_cents"] * item.qty,
                "shipped_qty": 0,
                "refunded_qty": 0,
                "refunded_amount_cents": 0,
            }
        )
        amount += product["price_cents"] * item.qty

    order = Order(
        id=str(uuid.uuid4()),
        order_no=f"SO{datetime.now(UTC):%Y%m%d}{random.randint(0, 999999):06d}",
        user_id=principal.user_id,
        tenant=principal.tenant,
        status="CREATED",
        amount_cents=amount,
        items=items,
        address=payload.address.model_dump(),
    )
    async with get_session_factory()() as session:
        session.add(order)
        # 事件随订单写入同库 outbox，与业务数据同事务提交，避免消息发出但事务回滚。
        enqueue(session, "order", "order.created", {"order_id": order.id, "amount_cents": amount}, tenant=principal.tenant)
        await session.commit()

    # 库存是跨服务调用，不能塞进本地事务；先落订单再预占，失败则走取消补偿。
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
        # 库存不足也把已取消订单缓存为幂等结果，重复提交同一幂等键得到一致响应。
        with contextlib.suppress(Exception):
            await idem.finish(scope, idempotency_key, result)
        raise ApiError("CONFLICT", "库存不足，订单已取消", details={"order": result, "reason": reason})

    # 库存锁定后再创建支付单；支付不可用则取消订单并释放库存，避免长期占用库存。
    payment_data = await _create_payment(order)
    if payment_data is None:
        async with get_session_factory()() as session:
            order = await session.get(Order, order.id)
            assert order is not None
            _cancel(order, session, "payment_unavailable")
            enqueue(
                session,
                "order",
                "order.cancelled",
                {"order_id": order.id, "reason": "payment_unavailable"},
                tenant=principal.tenant,
            )
            await session.commit()
            result = order.to_dict()
        await _release_inventory(order.id, "payment_unavailable", ensure_closed=False)
        with contextlib.suppress(Exception):
            await idem.finish(scope, idempotency_key, result)
        raise ApiError("CONFLICT", "支付服务不可用，订单已取消", details={"order": result})

    async with get_session_factory()() as session:
        order = await session.get(Order, order.id)
        assert order is not None
        order.payment_id = payment_data["id"]
        order.pay_url = payment_data["pay_url"]
        order.payment_expires_at = datetime.fromisoformat(payment_data["expires_at"])
        # 订单进入 PAYING 表示已下支付单；真正 PAID 由 payment.succeeded 事件驱动
        _apply(session, order, "pay_start", actor="system")
        enqueue(session, "order", "order.paying", {"order_id": order.id}, tenant=principal.tenant)
        await session.commit()
        result = order.to_dict()

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
        # 权限收口：staff 可按 user_id 过滤；普通用户强制只看自己，忽略传入的 user_id。
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
        # 越权校验放在查询之后：非 staff 只能访问自己的订单，避免按 id 遍历他人订单。
        if not principal.is_staff() and order.user_id != principal.user_id:
            raise ApiError("FORBIDDEN", "无权查看该订单")
        return order.to_dict()


@app.get("/orders/{order_id}/timeline")
async def order_timeline(order_id: str, principal: PrincipalDep) -> dict[str, Any]:
    async with get_session_factory()() as session:
        order = await _load(session, order_id)
        if not principal.is_staff() and order.user_id != principal.user_id:
            raise ApiError("FORBIDDEN", "无权查看该订单")
        # 轨迹按时间正序返回，前端据此还原状态流转过程；状态码统一翻译成中文标签。
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
    # 取消落库成功后释放库存；释放接口幂等，重复取消不会二次回补。
    await _release_inventory(order_id, reason)
    return result


@app.post("/orders/{order_id}/ship")
async def mark_shipped(order_id: str, principal: PrincipalDep) -> dict[str, Any]:
    # 人工整单发货：把订单行标记为全部发出，用于履约系统异常时的人工兜底
    principal.require_staff()
    async with get_session_factory()() as session:
        order = await _load(session, order_id)
        _apply(session, order, "ship", actor=principal.user_id)
        for item in order.items or []:
            item["shipped_qty"] = int(item.get("qty", 0))
        _touch_items(order)
        order.shipped_at = datetime.now(UTC)
        await session.commit()
        return order.to_dict()


@app.post("/orders/{order_id}/complete")
async def mark_completed(order_id: str, principal: PrincipalDep) -> dict[str, Any]:
    # 完成订单属于后台操作，必须 staff；状态机只允许 SHIPPED → COMPLETED。
    principal.require_staff()
    async with get_session_factory()() as session:
        order = await _load(session, order_id)
        _apply(session, order, "deliver", actor=principal.user_id)
        order.completed_at = datetime.now(UTC)
        await session.commit()
        return order.to_dict()

