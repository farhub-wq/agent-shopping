import asyncio
import uuid
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any

from fastapi import FastAPI, Header
from pydantic import BaseModel, Field
from redis.asyncio import Redis
from sqlalchemy import select

from shop_common.auth import PrincipalDep
from shop_common.config import get_settings
from shop_common.db import get_session_factory, init_models
from shop_common.errors import ApiError, install_error_handlers
from shop_common.events import create_redis
from shop_common.logging import configure_logging, get_logger

from .models import Reservation, Stock
from .scripts import COMMIT_ORDER, RELEASE_ORDER, RESERVE_MULTI, SEC_KILL, SYNC_STOCK

settings = get_settings()
logger = get_logger(__name__)

NAMESPACE = "shop:inv"
# 预占 TTL（秒）：超时未支付由后台任务回补，避免幽灵订单长期占用库存。
RESERVATION_TTL_SECONDS = 900
FLASH_LIMIT_PER_USER = 1

SEED_SKUS = [f"SKU-100{i}" for i in range(1, 9)]

redis: Redis | None = None
_reserve_script = None
_release_script = None
_commit_script = None
_sec_kill_script = None
_sync_script = None


def r() -> Redis:
    assert redis is not None
    return redis


def stock_key(sku: str) -> str:
    return f"{NAMESPACE}:stock:{sku}"


def resv_key(order_id: str) -> str:
    return f"{NAMESPACE}:resv:{order_id}"


def flash_key(sku: str) -> str:
    return f"{NAMESPACE}:flash:{sku}"


def flash_users_key(sku: str) -> str:
    return f"{NAMESPACE}:flash:{sku}:users"


async def sync_hot_stock() -> None:
    """以数据库为真相源重建 Redis 热点库存；已存在的键不覆盖，避免重启清零在途预占。"""
    async with get_session_factory()() as session:
        rows = (await session.execute(select(Stock))).scalars().all()
    for row in rows:
        key = stock_key(row.sku)
        # 已存在说明存在在途预占（Redis 值已扣除），不能覆盖成全量可用，否则会超卖。
        if not await r().exists(key):
            await _sync_script(keys=[key], args=[max(row.total - row.reserved, 0)])


async def seed_stocks() -> None:
    async with get_session_factory()() as session:
        for sku in SEED_SKUS:
            if await session.get(Stock, sku) is None:
                session.add(Stock(sku=sku, total=120, reserved=0))
        await session.commit()
    # nx=True 保证重启不重置秒杀额度，只做首次初始化。
    await r().set(flash_key("SKU-1001"), settings.flash_sale_stock, nx=True)
    await sync_hot_stock()


async def expire_stale_reservations() -> None:
    """预占超时未支付必须回补库存，否则大促期间库存会被幽灵订单吃光。"""
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=RESERVATION_TTL_SECONDS)
    while True:
        await asyncio.sleep(30)
        try:
            async with get_session_factory()() as session:
                # 超过 TTL 仍为 reserved 的记录视为未支付幽灵订单，释放后统一提交。
                stale = (
                    (
                        await session.execute(
                            select(Reservation).where(
                                Reservation.status == "reserved", Reservation.created_at < cutoff
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                for reservation in stale:
                    await _release_locked(session, reservation, reason="expired")
                if stale:
                    await session.commit()
                    logger.info("已回补 %s 条超时预占", len(stale))
        except asyncio.CancelledError:
            raise
        except Exception:
            # 单轮扫描失败只记日志，不能让它退出后台守护任务。
            logger.exception("超时预占扫描失败")


async def _release_locked(session, reservation: Reservation, *, reason: str) -> None:
    # DB 的 reserved 计数与 Redis 可用量必须同步回补，否则两边会长期漂移。
    for item in reservation.items:
        stock = await session.get(Stock, item["sku"])
        if stock:
            stock.reserved = max(stock.reserved - item["qty"], 0)
    # 先标记 released，后续重复调用 release 会走幂等短路，不会二次回补。
    reservation.status = "released"
    reservation.updated_at = datetime.now(timezone.utc)
    await _release_script(keys=[resv_key(reservation.order_id)])
    logger.info("释放预占 order=%s reason=%s", reservation.order_id, reason)


@asynccontextmanager
async def lifespan(_: FastAPI):
    global redis, _reserve_script, _release_script, _commit_script, _sec_kill_script, _sync_script
    configure_logging("inventory")
    await init_models()
    redis = create_redis(settings.redis_url)
    # 脚本启动时注册一次，实际调用由 Redis 单线程原子执行，避免检查与扣减之间的竞态。
    _reserve_script = redis.register_script(RESERVE_MULTI)
    _release_script = redis.register_script(RELEASE_ORDER)
    _commit_script = redis.register_script(COMMIT_ORDER)
    _sec_kill_script = redis.register_script(SEC_KILL)
    _sync_script = redis.register_script(SYNC_STOCK)
    await seed_stocks()
    # 超时回补任务与 HTTP 服务同生命周期，关闭时取消并等待退出。
    task = asyncio.create_task(expire_stale_reservations())
    try:
        yield
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        await redis.close()


app = FastAPI(title="inventory-service", version="0.1.0", lifespan=lifespan)
install_error_handlers(app, "inventory")


class ReserveItem(BaseModel):
    sku: str
    qty: int = Field(gt=0, le=99)


class ReserveRequest(BaseModel):
    order_id: str
    items: list[ReserveItem] = Field(min_length=1)


class OrderRef(BaseModel):
    order_id: str
    reason: str = "cancelled"


class StockUpsert(BaseModel):
    sku: str
    total: int = Field(ge=0)


class SecKillRequest(BaseModel):
    user_id: str


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok", "service": "inventory"}


@app.get("/inventory/stocks")
async def list_stocks(principal: PrincipalDep) -> dict[str, Any]:
    principal.require_staff()
    async with get_session_factory()() as session:
        rows = (await session.execute(select(Stock))).scalars().all()
        items = []
        for row in rows:
            # 优先取 Redis 实时可用量（预占/秒杀即时变化），缺失时退化到 DB 的 total-reserved。
            available = await r().get(stock_key(row.sku))
            items.append(
                {
                    "sku": row.sku,
                    "total": row.total,
                    "reserved": row.reserved,
                    "available": int(available) if available is not None else row.total - row.reserved,
                }
            )
        return {"items": items}


@app.post("/inventory/stocks")
async def upsert_stock(payload: StockUpsert, principal: PrincipalDep) -> dict[str, Any]:
    principal.require_staff()
    async with get_session_factory()() as session:
        stock = await session.get(Stock, payload.sku)
        if stock is None:
            stock = Stock(sku=payload.sku, total=payload.total, reserved=0)
            session.add(stock)
        else:
            stock.total = payload.total
        await session.commit()
    # DB 是真相源，先提交再回填 Redis；已预占部分不计入可用量。
    await _sync_script(keys=[stock_key(payload.sku)], args=[max(payload.total - (stock.reserved or 0), 0)])
    return {"sku": payload.sku, "total": payload.total, "reserved": stock.reserved}


@app.get("/inventory/{sku}")
async def get_stock(sku: str) -> dict[str, Any]:
    available = await r().get(stock_key(sku))
    async with get_session_factory()() as session:
        stock = await session.get(Stock, sku)
    if stock is None and available is None:
        # 两处都没有记录才算 SKU 不存在，任一侧有数据都可正常返回。
        raise ApiError("NOT_FOUND", "SKU 不存在")
    return {
        "sku": sku,
        "available": int(available) if available is not None else max((stock.total - stock.reserved), 0),
        "total": stock.total if stock else None,
        "reserved": stock.reserved if stock else None,
    }


@app.post("/inventory/reserve")
async def reserve(
    payload: ReserveRequest,
    principal: PrincipalDep,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> dict[str, Any]:
    async with get_session_factory()() as session:
        # 幂等以 order_id 唯一行为准：同一订单重复预占直接返回既有结果，不会重复扣减。
        existing = await session.scalar(
            select(Reservation).where(Reservation.order_id == payload.order_id)
        )
        if existing is not None:
            return {
                "order_id": payload.order_id,
                "status": existing.status,
                "idempotent": True,
                "items": existing.items,
            }

        keys = [resv_key(payload.order_id)] + [stock_key(item.sku) for item in payload.items]
        args = [RESERVATION_TTL_SECONDS, len(payload.items)] + [item.qty for item in payload.items]
        # 检查可用量、扣减、写预占哈希、设置 TTL 全部在一次 Lua 调用内完成，避免超卖窗口。
        result = await _reserve_script(keys=keys, args=args)
        if int(result[0]) != 1:
            failed_index = int(result[1])
            failed_sku = payload.items[max(failed_index - 1, 0)].sku
            raise ApiError(
                "CONFLICT",
                "库存不足，请调整购买数量",
                details={"sku": failed_sku, "available": int(result[2])},
            )

        reservation = Reservation(
            id=str(uuid.uuid4()),
            order_id=payload.order_id,
            items=[item.model_dump() for item in payload.items],
            status="reserved",
        )
        try:
            for item in payload.items:
                stock = await session.get(Stock, item.sku)
                if stock is None:
                    stock = Stock(sku=item.sku, total=item.qty, reserved=0)
                    session.add(stock)
                stock.reserved += item.qty
            session.add(reservation)
            await session.commit()
        except Exception:
            # 本地落库失败时回滚 DB，并立即释放 Redis 已扣减的额度，保持两侧一致。
            await session.rollback()
            await _release_script(keys=[resv_key(payload.order_id)])
            raise
        return {"order_id": payload.order_id, "status": "reserved", "items": reservation.items}


@app.post("/inventory/commit")
async def commit(payload: OrderRef, principal: PrincipalDep) -> dict[str, Any]:
    async with get_session_factory()() as session:
        reservation = await session.scalar(
            select(Reservation).where(Reservation.order_id == payload.order_id)
        )
        if reservation is None:
            raise ApiError("NOT_FOUND", "预占记录不存在")
        if reservation.status == "committed":
            # 已提交视为成功幂等返回，不重复扣减 total。
            return {"order_id": payload.order_id, "status": "committed", "idempotent": True}
        if reservation.status != "reserved":
            raise ApiError("CONFLICT", f"预占状态 {reservation.status} 不可提交")
        # 提交把「预占」转为「已售」：同时扣减 reserved 与 total，可用量保持不变。
        for item in reservation.items:
            stock = await session.get(Stock, item["sku"])
            if stock:
                stock.reserved = max(stock.reserved - item["qty"], 0)
                stock.total = max(stock.total - item["qty"], 0)
        reservation.status = "committed"
        reservation.updated_at = datetime.now(timezone.utc)
        await session.commit()
    await _commit_script(keys=[resv_key(payload.order_id)])
    return {"order_id": payload.order_id, "status": "committed"}


@app.post("/inventory/release")
async def release(payload: OrderRef, principal: PrincipalDep) -> dict[str, Any]:
    async with get_session_factory()() as session:
        reservation = await session.scalar(
            select(Reservation).where(Reservation.order_id == payload.order_id)
        )
        if reservation is None:
            # 订单从未预占（或已清理）时按成功幂等返回，取消流程无需特殊分支。
            return {"order_id": payload.order_id, "status": "not_found", "idempotent": True}
        if reservation.status in {"released", "committed"}:
            # 已释放/已提交不可回退，直接返回当前状态，避免重复回补库存。
            return {"order_id": payload.order_id, "status": reservation.status, "idempotent": True}
        await _release_locked(session, reservation, reason=payload.reason)
        await session.commit()
    return {"order_id": payload.order_id, "status": "released"}


@app.post("/inventory/flash-sale/{sku}/seckill")
async def seckill(sku: str, payload: SecKillRequest, principal: PrincipalDep) -> dict[str, Any]:
    # 限购校验与库存扣减在同一 Lua 脚本内，避免并发请求都通过校验后超卖或重复购买。
    result = await _sec_kill_script(
        keys=[flash_key(sku), flash_users_key(sku)],
        args=[payload.user_id, FLASH_LIMIT_PER_USER],
    )
    if int(result[0]) != 1:
        # Lua 返回的失败码映射为对用户友好的错误信息。
        raise ApiError("CONFLICT", {"DUPLICATE": "每人限购一件", "SOLD_OUT": "已售罄"}[result[1]])
    return {"sku": sku, "user_id": payload.user_id, "remaining": int(result[2]), "status": "reserved"}


@app.get("/inventory/flash-sale/{sku}")
async def flash_status(sku: str) -> dict[str, Any]:
    # 键不存在说明该 SKU 没有配置秒杀活动，而不是库存为 0。
    remaining = await r().get(flash_key(sku))
    if remaining is None:
        raise ApiError("NOT_FOUND", "该商品未配置秒杀")
    return {"sku": sku, "remaining": int(remaining)}
