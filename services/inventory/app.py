import asyncio
import uuid
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import FastAPI, Header
from pydantic import BaseModel, Field
from redis.asyncio import Redis
from shop_common.auth import PrincipalDep, signed_identity_headers
from shop_common.config import get_settings
from shop_common.db import get_session_factory, init_models
from shop_common.errors import ApiError, install_error_handlers
from shop_common.events import create_redis
from shop_common.http import ServiceClient
from shop_common.logging import configure_logging, get_logger
from sqlalchemy import select

from .models import Reservation, Restock, Stock
from .scripts import COMMIT_ORDER, RELEASE_ORDER, RESERVE_MULTI, RESTOCK, SEC_KILL, SYNC_STOCK

settings = get_settings()
logger = get_logger(__name__)

NAMESPACE = "shop:inv"
# 预占 TTL = 支付窗口（15 分钟）+ 5 分钟缓冲：payment.expires_at 是唯一权威，这里只做兜底。
RESERVATION_TTL_SECONDS = settings.payment_ttl_seconds + 300
FLASH_LIMIT_PER_USER = 1

# 兜底释放前查支付单用：释放前必须确认支付单已关闭（非 pending），否则 fail closed。
payment_svc = ServiceClient(settings.payment_url)

SEED_SKUS = [f"SKU-100{i}" for i in range(1, 9)]

redis: Redis | None = None
_reserve_script = None
_release_script = None
_commit_script = None
_restock_script = None
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
    """预占超时未支付必须回补库存，否则大促期间库存会被幽灵订单吃光。

    释放前必须确认支付单已关闭（非 pending）：兜底扫描是最后防线，
    支付单仍 pending 或查询失败则 fail closed 等下一轮，宁少卖不超卖。
    """
    while True:
        await asyncio.sleep(30)
        try:
            now = datetime.now(UTC)
            async with get_session_factory()() as session:
                stale = (
                    (
                        await session.execute(
                            select(Reservation).where(
                                Reservation.status == "reserved", Reservation.expires_at < now
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
            by_order: dict[str, list[Reservation]] = {}
            for row in stale:
                by_order.setdefault(row.order_id, []).append(row)
            for order_id in by_order:
                if not await _payment_releasable(order_id):
                    continue
                async with get_session_factory()() as session:
                    # 重新取当前仍为 reserved 的行，避免与并发提交/释放互相踩。
                    rows = (
                        (
                            await session.execute(
                                select(Reservation).where(
                                    Reservation.order_id == order_id,
                                    Reservation.status == "reserved",
                                )
                            )
                        )
                        .scalars()
                        .all()
                    )
                    if rows:
                        await _release_locked(session, rows, reason="expired")
                        await session.commit()
        except asyncio.CancelledError:
            raise
        except Exception:
            # 单轮扫描失败只记日志，不能让它退出后台守护任务。
            logger.exception("超时预占扫描失败")


async def _payment_releasable(order_id: str) -> bool:
    """兜底释放前确认支付单已关闭且未支付：expired/failed/不存在才可释放。

    paid/refunded 走提交与退款链路，pending 尚未关闭，都不满足释放条件；
    查询失败按 fail closed 处理（宁少卖不超卖），等下一轮扫描。
    """
    try:
        data = await payment_svc.request(
            "GET", f"/payments/{order_id}", headers=signed_identity_headers("system", "operator")
        )
    except ApiError as exc:
        if exc.code == "NOT_FOUND":
            # 支付单不存在说明从未成功建单，可安全释放
            return True
        logger.warning("查询支付单失败，跳过兜底释放 order=%s code=%s", order_id, exc.code)
        return False
    status = data.get("status")
    if status in {"expired", "failed"}:
        return True
    logger.info("支付单状态 %s 不满足释放条件 order=%s", status, order_id)
    return False


async def reconcile_stock() -> None:
    """每 5 分钟比对 Redis 与 DB：DB 是真相源，差异以 DB 为准重建并告警。

    对账兜住 Redis 重启、Lua 与事务失败留下的漂移；当前单实例运行，
    Unit C 引入 redis_lock 后改为多实例互斥。
    """
    while True:
        await asyncio.sleep(300)
        try:
            async with get_session_factory()() as session:
                rows = (await session.execute(select(Stock))).scalars().all()
            for row in rows:
                expected = max(row.total - row.reserved, 0)
                key = stock_key(row.sku)
                current = await r().get(key)
                if current is None or int(current) != expected:
                    await _sync_script(keys=[key], args=[expected])
                    logger.warning("库存对账重建 sku=%s redis=%s db=%s", row.sku, current, expected)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("库存对账任务失败")


async def _release_locked(session, rows: list[Reservation], *, reason: str) -> None:
    # DB 的 reserved 计数与 Redis 可用量必须同步回补，否则两边会长期漂移。
    order_id = rows[0].order_id
    for row in rows:
        stock = await session.get(Stock, row.sku)
        if stock:
            stock.reserved = max(stock.reserved - row.qty, 0)
        # 先标记 released，后续重复调用 release 会走幂等短路，不会二次回补。
        row.status = "released"
        row.updated_at = datetime.now(UTC)
    await _release_script(keys=[resv_key(order_id)])
    logger.info("释放预占 order=%s reason=%s", order_id, reason)


@asynccontextmanager
async def lifespan(_: FastAPI):
    global redis, _reserve_script, _release_script, _commit_script, _restock_script, _sec_kill_script, _sync_script
    configure_logging("inventory")
    await init_models()
    redis = create_redis(settings.redis_url)
    # 脚本启动时注册一次，实际调用由 Redis 单线程原子执行，避免检查与扣减之间的竞态。
    _reserve_script = redis.register_script(RESERVE_MULTI)
    _release_script = redis.register_script(RELEASE_ORDER)
    _commit_script = redis.register_script(COMMIT_ORDER)
    _restock_script = redis.register_script(RESTOCK)
    _sec_kill_script = redis.register_script(SEC_KILL)
    _sync_script = redis.register_script(SYNC_STOCK)
    await seed_stocks()
    # 后台任务与 HTTP 服务同生命周期，关闭时统一取消并等待退出。
    tasks = [
        asyncio.create_task(expire_stale_reservations()),
        asyncio.create_task(reconcile_stock()),
    ]
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        for task in tasks:
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
        # 幂等以 (order_id, sku) 行级记录为准：同一订单重复预占直接返回既有结果，不会重复扣减。
        existing = (
            (
                await session.execute(
                    select(Reservation).where(Reservation.order_id == payload.order_id)
                )
            )
            .scalars()
            .all()
        )
        if existing:
            return {
                "order_id": payload.order_id,
                "status": existing[0].status,
                "idempotent": True,
                "items": [{"sku": row.sku, "qty": row.qty} for row in existing],
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

        # 行级预占：每个 SKU 一行，expires_at 统一为支付窗口 + 缓冲，供兜底扫描触发。
        expires_at = datetime.now(UTC) + timedelta(seconds=RESERVATION_TTL_SECONDS)
        reservations = [
            Reservation(
                id=str(uuid.uuid4()),
                order_id=payload.order_id,
                sku=item.sku,
                qty=item.qty,
                status="reserved",
                expires_at=expires_at,
            )
            for item in payload.items
        ]
        try:
            for item in payload.items:
                stock = await session.get(Stock, item.sku)
                if stock is None:
                    stock = Stock(sku=item.sku, total=item.qty, reserved=0)
                    session.add(stock)
                stock.reserved += item.qty
            session.add_all(reservations)
            await session.commit()
        except Exception:
            # 本地落库失败时回滚 DB，并立即释放 Redis 已扣减的额度，保持两侧一致。
            await session.rollback()
            await _release_script(keys=[resv_key(payload.order_id)])
            raise
        return {
            "order_id": payload.order_id,
            "status": "reserved",
            "items": [item.model_dump() for item in payload.items],
        }


@app.post("/inventory/commit")
async def commit(payload: OrderRef, principal: PrincipalDep) -> dict[str, Any]:
    async with get_session_factory()() as session:
        rows = (
            (
                await session.execute(
                    select(Reservation).where(Reservation.order_id == payload.order_id)
                )
            )
            .scalars()
            .all()
        )
        if not rows:
            raise ApiError("NOT_FOUND", "预占记录不存在")
        if all(row.status == "committed" for row in rows):
            # 已提交视为成功幂等返回，不重复扣减 total。
            return {"order_id": payload.order_id, "status": "committed", "idempotent": True}
        if any(row.status != "reserved" for row in rows):
            raise ApiError("CONFLICT", "预占状态不可提交")
        # 提交把「预占」转为「已售」：同时扣减 reserved 与 total，可用量保持不变。
        for row in rows:
            stock = await session.get(Stock, row.sku)
            if stock:
                stock.reserved = max(stock.reserved - row.qty, 0)
                stock.total = max(stock.total - row.qty, 0)
            row.status = "committed"
            row.updated_at = datetime.now(UTC)
        await session.commit()
    await _commit_script(keys=[resv_key(payload.order_id)])
    return {"order_id": payload.order_id, "status": "committed"}


@app.post("/inventory/release")
async def release(payload: OrderRef, principal: PrincipalDep) -> dict[str, Any]:
    async with get_session_factory()() as session:
        rows = (
            (
                await session.execute(
                    select(Reservation).where(Reservation.order_id == payload.order_id)
                )
            )
            .scalars()
            .all()
        )
        if not rows:
            # 订单从未预占（或已清理）时按成功幂等返回，取消流程无需特殊分支。
            return {"order_id": payload.order_id, "status": "not_found", "idempotent": True}
        if all(row.status != "reserved" for row in rows):
            # 已释放/已提交不可回退，直接返回当前状态，避免重复回补库存。
            return {"order_id": payload.order_id, "status": rows[0].status, "idempotent": True}
        await _release_locked(session, rows, reason=payload.reason)
        await session.commit()
    return {"order_id": payload.order_id, "status": "released"}


class RestockItem(BaseModel):
    sku: str
    # 回补目标总量（绝对值）：端点按 (order_id, sku) 已回补量取增量，重复调用幂等
    qty: int = Field(ge=0)


class RestockRequest(BaseModel):
    order_id: str
    items: list[RestockItem] = Field(min_length=1)


@app.post("/inventory/restock")
async def restock(payload: RestockRequest, principal: PrincipalDep) -> dict[str, Any]:
    """退款后回补库存：qty 是目标总量而非增量，由本服务按记录取差值，重试不会重复回补。"""
    deltas: list[dict[str, Any]] = []
    async with get_session_factory()() as session:
        skus = [row.sku for row in payload.items]
        records = {
            rec.sku: rec
            for rec in (
                await session.execute(
                    select(Restock).where(
                        Restock.order_id == payload.order_id, Restock.sku.in_(skus)
                    )
                )
            ).scalars()
        }
        stocks = {
            stock.sku: stock
            for stock in (await session.execute(select(Stock).where(Stock.sku.in_(skus)))).scalars()
        }
        for row in payload.items:
            rec = records.get(row.sku)
            done = rec.qty if rec is not None else 0
            delta = row.qty - done
            if delta < 0:
                # 目标只允许单调增长；变小说明上游计算出错，拒绝而不是悄悄冲减库存
                raise ApiError("CONFLICT", f"SKU {row.sku} 回补目标 {row.qty} 小于已回补量 {done}")
            if delta == 0:
                continue
            stock = stocks.get(row.sku)
            if stock is None:
                raise ApiError("NOT_FOUND", f"SKU {row.sku} 不存在")
            stock.total += delta
            if rec is None:
                session.add(Restock(order_id=payload.order_id, sku=row.sku, qty=row.qty))
            else:
                rec.qty = row.qty
            deltas.append({"sku": row.sku, "qty": delta})
        await session.commit()
    # DB 提交后再同步热点库存；脚本对不存在的键跳过，冷 SKU 以 DB 为准
    for row in deltas:
        await _restock_script(keys=[stock_key(row["sku"])], args=[row["qty"]])
    return {"order_id": payload.order_id, "restocked": deltas}


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
