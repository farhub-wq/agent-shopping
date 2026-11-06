"""payment 服务：支付单、退款、复式总账与日终对账。

渠道是结算事实，内部总账是业务事实；回调必须验签、去重，资金分录只增不改。
"""

import asyncio
import contextlib
import json
import time
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta
from datetime import time as clock_time
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from redis.asyncio import Redis
from shop_common.auth import PrincipalDep
from shop_common.config import get_settings
from shop_common.db import get_session_factory, init_models
from shop_common.errors import ApiError, install_error_handlers
from shop_common.events import EventBus, create_redis
from shop_common.logging import configure_logging, get_logger
from shop_common.outbox import OutboxRelay, enqueue
from shop_common.tasks import run_task_loop
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from . import models  # noqa: F401  # 注册到 Base.metadata，init_models 才能建表
from .ledger import post_transaction
from .models import LedgerEntry, Payment, ReconciliationDiff, Refund, Settlement
from .provider import callback_signature, get_provider

settings = get_settings()
logger = get_logger(__name__)

redis: Redis | None = None
bus: EventBus | None = None
provider = get_provider(settings.payment_provider)


def _aware(value: datetime) -> datetime:
    # SQLite 回读的 DateTime 不带时区，统一补 UTC 后再比较
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _mock_enabled() -> bool:
    # mock 收银台只用于本地演示：生产或未显式开启时等同接口不存在
    return settings.env == "dev" and settings.payment_mock_mode


async def _expire_payments_once() -> None:
    """单轮超时关单：超时未支付的支付单必须关闭并通知订单，否则库存会被幽灵订单长期占用。"""
    now = datetime.now(UTC)
    async with get_session_factory()() as session:
        rows = (
            (
                await session.execute(
                    select(Payment).where(
                        Payment.status == "pending", Payment.expires_at < now
                    )
                )
            )
            .scalars()
            .all()
        )
        for payment in rows:
            payment.status = "expired"
            # 状态与事件同事务入 outbox，避免「已关单但订单没收到」
            enqueue(
                session,
                "payment",
                "payment.expired",
                {"payment_id": payment.id, "order_id": payment.order_id},
                tenant=payment.tenant_id,
            )
        if rows:
            await session.commit()
            logger.info("关闭超时支付单 %s 笔", len(rows))


@asynccontextmanager
async def lifespan(_: FastAPI):
    global redis, bus
    configure_logging("payment")
    await init_models()
    tasks: list[asyncio.Task] = []
    if settings.background_tasks:
        redis = create_redis(settings.redis_url)
        bus = EventBus(redis, namespace="shop", session_factory=get_session_factory())
        relay = OutboxRelay(get_session_factory(), bus)
        # outbox 投递与超时关单随应用生命周期统一启动
        tasks = [
            asyncio.create_task(relay.run_forever()),
            asyncio.create_task(run_task_loop("payment-expire", redis, get_session_factory(), 30, _expire_payments_once)),
        ]
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


app = FastAPI(title="payment-service", version="0.1.0", lifespan=lifespan)
install_error_handlers(app, "payment")


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok", "service": "payment"}


class CreatePaymentReq(BaseModel):
    order_id: str = Field(min_length=1, max_length=36)
    amount_cents: int = Field(gt=0)
    currency: str = Field(default="CNY", max_length=8)
    tenant: str = Field(default="default", max_length=36)


@app.post("/payments", status_code=201)
async def create_payment(payload: CreatePaymentReq, principal: PrincipalDep) -> dict[str, Any]:
    # 支付单只能由内部运营/系统身份创建，顾客侧通过下单链路间接触发
    principal.require_staff()
    async with get_session_factory()() as session:
        existing = await session.scalar(select(Payment).where(Payment.order_id == payload.order_id))
        if existing is not None:
            # 同一订单重复创建视为幂等：金额不一致说明调用方有问题，直接拒绝
            if existing.amount_cents != payload.amount_cents:
                raise ApiError("CONFLICT", "同一订单的支付金额不一致")
            return {**existing.to_dict(), "idempotent": True}

        payment_id = str(uuid.uuid4())
        expires_at = datetime.now(UTC) + timedelta(seconds=settings.payment_ttl_seconds)
        created = provider.create_payment(
            payment_id=payment_id,
            order_id=payload.order_id,
            amount_cents=payload.amount_cents,
            currency=payload.currency,
            expires_at=expires_at.isoformat(),
            gateway_url=settings.payment_public_base_url,
        )
        payment = Payment(
            id=payment_id,
            tenant_id=payload.tenant,
            order_id=payload.order_id,
            provider=provider.name,
            provider_txn_id=created.provider_txn_id,
            amount_cents=payload.amount_cents,
            currency=payload.currency,
            status="pending",
            pay_url=created.pay_url,
            expires_at=expires_at,
        )
        session.add(payment)
        try:
            await session.commit()
        except IntegrityError:
            # 并发建单撞 order_id 唯一索引：回读已存在记录并返回，不把 500 抛给订单服务
            await session.rollback()
            existing = await session.scalar(select(Payment).where(Payment.order_id == payload.order_id))
            if existing is None:
                raise
            return {**existing.to_dict(), "idempotent": True}
        return payment.to_dict()


@app.get("/payments/{order_id}")
async def get_payment(order_id: str, principal: PrincipalDep) -> dict[str, Any]:
    principal.require_staff()
    async with get_session_factory()() as session:
        payment = await session.scalar(select(Payment).where(Payment.order_id == order_id))
        if payment is None:
            raise ApiError("NOT_FOUND", "支付单不存在")
        return payment.to_dict()


@app.post("/payments/{order_id}/expire")
async def expire_payment(order_id: str, principal: PrincipalDep) -> dict[str, Any]:
    """内部关单：订单取消链路在释放库存前调用，把 pending 支付单原子关闭。

    幂等：已过期/已支付/已退款只返回当前状态，不重复发事件；
    关单成功后订单侧才能释放库存，杜绝「释放后用户付款成功」的迟到支付窗口。
    """
    principal.require_staff()
    async with get_session_factory()() as session:
        payment = await session.scalar(select(Payment).where(Payment.order_id == order_id))
        if payment is None:
            # 支付单不存在说明从未成功建单，视为已关闭，订单侧可继续释放
            return {"order_id": order_id, "status": "not_found", "idempotent": True}
        if payment.status == "pending":
            payment.status = "expired"
            enqueue(
                session,
                "payment",
                "payment.expired",
                {"payment_id": payment.id, "order_id": payment.order_id},
                tenant=payment.tenant_id,
            )
            await session.commit()
        return {"order_id": order_id, "status": payment.status}


@app.post("/payments/callback/{provider_name}")
async def payment_callback(provider_name: str, request: Request) -> dict[str, Any]:
    """渠道回调入口：原始 body + 签名头，重复回调同样返回 2xx。"""
    if provider_name != settings.payment_provider:
        raise ApiError("NOT_FOUND", "未知支付渠道")
    raw_body = await request.body()
    headers = {key.lower(): value for key, value in request.headers.items()}
    event = provider.verify_callback(
        raw_body=raw_body, headers=headers, secret=settings.payment_webhook_secret
    )
    return await _apply_callback(event)


def _event_int(event: dict[str, Any], key: str) -> int:
    try:
        return int(event[key])
    except (KeyError, TypeError, ValueError) as exc:
        raise ApiError("BAD_REQUEST", f"回调缺少合法字段 {key}") from exc


async def _apply_callback(event: dict[str, Any]) -> dict[str, Any]:
    """把渠道回调落到支付状态 + 总账 + outbox，重复调用只返回既有结果。"""
    txn_id = str(event.get("provider_txn_id") or "")
    order_id = str(event.get("order_id") or "")
    amount = _event_int(event, "amount_cents")
    currency = str(event.get("currency") or "CNY")
    status = str(event.get("status") or "")
    if not txn_id:
        raise ApiError("BAD_REQUEST", "回调缺少渠道流水号")
    if status not in {"paid", "failed"}:
        raise ApiError("BAD_REQUEST", "不支持的支付状态")

    async with get_session_factory()() as session:
        payment = await session.scalar(select(Payment).where(Payment.provider_txn_id == txn_id))
        if payment is None and order_id:
            payment = await session.scalar(select(Payment).where(Payment.order_id == order_id))
        if payment is None:
            raise ApiError("NOT_FOUND", "支付单不存在")
        # 金额与币种必须与建单一致，伪造金额的回调不落账
        if payment.amount_cents != amount or payment.currency != currency:
            raise ApiError("CONFLICT", "回调金额或币种与支付单不一致")
        if payment.provider_txn_id and payment.provider_txn_id != txn_id:
            raise ApiError("CONFLICT", "支付单已被其他渠道流水号占用")

        if status == "failed":
            if payment.status == "pending":
                payment.status = "failed"
                enqueue(
                    session,
                    "payment",
                    "payment.failed",
                    {"payment_id": payment.id, "order_id": payment.order_id},
                    tenant=payment.tenant_id,
                )
                await session.commit()
            return {"status": payment.status, "idempotent": payment.status != "pending"}

        if payment.status == "paid":
            # 重复回调：不再写第二笔分录、不再发第二个事件
            return {"status": "paid", "idempotent": True}
        if payment.status in {"refunded"}:
            return {"status": payment.status, "idempotent": True}

        payment.provider_txn_id = txn_id
        payment.status = "paid"
        payment.paid_at = datetime.now(UTC)
        # 渠道已收款：借记应收、贷记收入；结算时再从应收转入现金并记手续费
        post_transaction(
            session,
            txn_type="payment",
            ref_type="payment",
            ref_id=payment.id,
            entries=[
                ("PROVIDER_RECEIVABLE", "debit", payment.amount_cents),
                ("REVENUE", "credit", payment.amount_cents),
            ],
            currency=payment.currency,
            tenant=payment.tenant_id,
        )
        enqueue(
            session,
            "payment",
            "payment.succeeded",
            {
                "payment_id": payment.id,
                "order_id": payment.order_id,
                "amount_cents": payment.amount_cents,
                "provider_txn_id": txn_id,
            },
            tenant=payment.tenant_id,
        )
        try:
            await session.commit()
        except IntegrityError:
            # 并发重复回调撞 provider_txn_id 唯一索引：回滚后按幂等成功返回
            await session.rollback()
            return {"status": "paid", "idempotent": True}
        return {"status": "paid", "idempotent": False}


@app.get("/payments/cashier/{payment_id}", response_class=HTMLResponse)
async def mock_cashier(payment_id: str) -> HTMLResponse:
    """本地 mock 收银台：真实渠道会跳转到服务商托管页面。"""
    if not _mock_enabled():
        raise ApiError("NOT_FOUND", "接口不存在")
    async with get_session_factory()() as session:
        payment = await session.get(Payment, payment_id)
        if payment is None:
            raise ApiError("NOT_FOUND", "支付单不存在")
    amount = f"{payment.amount_cents / 100:.2f}"
    if payment.status != "pending":
        body = f"<p>支付单状态：{payment.status}</p>"
    else:
        # 表单只在本服务内提交，生产关闭 mock 后整页不可用
        body = (
            f"<p>订单 {payment.order_id}</p><p>应付 ¥{amount}</p>"
            f'<form method="post" action="/api/payments/cashier/{payment.id}/pay">'
            '<button type="submit">模拟支付成功</button></form>'
        )
    return HTMLResponse(
        f"<!doctype html><meta charset='utf-8'><title>Mock 收银台</title>{body}",
        status_code=200,
    )


@app.post("/payments/cashier/{payment_id}/pay")
async def mock_pay(payment_id: str) -> dict[str, Any]:
    """模拟用户在收银台完成支付：构造与真实渠道一致的回调报文并走完整验签流程。"""
    if not _mock_enabled():
        raise ApiError("NOT_FOUND", "接口不存在")
    async with get_session_factory()() as session:
        payment = await session.get(Payment, payment_id)
        if payment is None:
            raise ApiError("NOT_FOUND", "支付单不存在")
        if payment.status in {"paid", "refunded"}:
            return {"status": payment.status, "idempotent": True}
        if payment.status == "expired":
            raise ApiError("CONFLICT", "支付单已过期，请重新下单")
        txn_id = payment.provider_txn_id or f"mock_{uuid.uuid4().hex}"

    raw_body = json.dumps(
        {
            "provider_txn_id": txn_id,
            "order_id": payment.order_id,
            "amount_cents": payment.amount_cents,
            "currency": payment.currency,
            "status": "paid",
        },
        separators=(",", ":"),
    ).encode()
    ts = str(int(time.time()))
    signature = callback_signature(settings.payment_webhook_secret, ts, raw_body)
    event = provider.verify_callback(
        raw_body=raw_body,
        headers={"x-payment-timestamp": ts, "x-payment-signature": signature},
        secret=settings.payment_webhook_secret,
    )
    return await _apply_callback(event)


class RefundReq(BaseModel):
    order_id: str
    ticket_id: str
    amount_cents: int = Field(gt=0)
    order_item_id: str | None = None
    quantity: int = Field(default=0, ge=0)


@app.post("/payments/refunds", status_code=201)
async def create_refund(payload: RefundReq, principal: PrincipalDep) -> dict[str, Any]:
    """售后审批通过后的出款入口；按 ticket_id 幂等，累计退款不得超过支付金额。"""
    principal.require_staff()
    async with get_session_factory()() as session:
        existing = await session.scalar(select(Refund).where(Refund.ticket_id == payload.ticket_id))
        if existing is not None:
            return {**existing.to_dict(), "idempotent": True}

        payment = await session.scalar(select(Payment).where(Payment.order_id == payload.order_id))
        if payment is None:
            raise ApiError("NOT_FOUND", "支付单不存在")
        if payment.status not in {"paid", "refunded"}:
            raise ApiError("CONFLICT", "支付单未成功，不能退款")
        refunded = (
            await session.scalar(
                select(func.coalesce(func.sum(Refund.amount_cents), 0)).where(
                    Refund.payment_id == payment.id, Refund.status == "succeeded"
                )
            )
            or 0
        )
        if int(refunded) + payload.amount_cents > payment.amount_cents:
            # 累计退款上限是资金侧的硬约束，不依赖调用方自报的可退额
            raise ApiError("CONFLICT", "累计退款金额超过支付金额")

        provider_refund_id = provider.refund(
            payment_id=payment.id, refund_id=payload.ticket_id, amount_cents=payload.amount_cents
        )
        refund = Refund(
            id=str(uuid.uuid4()),
            payment_id=payment.id,
            order_id=payment.order_id,
            ticket_id=payload.ticket_id,
            order_item_id=payload.order_item_id,
            quantity=payload.quantity,
            amount_cents=payload.amount_cents,
            status="succeeded",
            provider_refund_id=provider_refund_id,
            completed_at=datetime.now(UTC),
        )
        session.add(refund)
        # 退款：借记退款支出、贷记渠道应收；不修改原支付分录
        post_transaction(
            session,
            txn_type="refund",
            ref_type="refund",
            ref_id=refund.id,
            entries=[
                ("REFUND", "debit", refund.amount_cents),
                ("PROVIDER_RECEIVABLE", "credit", refund.amount_cents),
            ],
            currency=payment.currency,
            tenant=payment.tenant_id,
        )
        fully = int(refunded) + refund.amount_cents == payment.amount_cents
        if fully:
            payment.status = "refunded"
        enqueue(
            session,
            "payment",
            "payment.refunded",
            {
                "payment_id": payment.id,
                "order_id": payment.order_id,
                "refund_id": refund.id,
                "ticket_id": refund.ticket_id,
                "order_item_id": refund.order_item_id,
                "quantity": refund.quantity,
                "amount_cents": refund.amount_cents,
                "fully_refunded": fully,
            },
            tenant=payment.tenant_id,
        )
        try:
            await session.commit()
        except IntegrityError:
            # 并发重复审批撞 ticket_id 唯一索引：回读既有退款并幂等返回
            await session.rollback()
            existing = await session.scalar(select(Refund).where(Refund.ticket_id == payload.ticket_id))
            if existing is None:
                raise
            return {**existing.to_dict(), "idempotent": True}
        return refund.to_dict()


async def _local_records(session, biz_date: str) -> list[dict[str, Any]]:
    # 按 UTC 自然日聚合，渠道账单同样以 UTC 业务日为准
    start = datetime.combine(date.fromisoformat(biz_date), clock_time.min, tzinfo=UTC)
    end = start + timedelta(days=1)
    records: list[dict[str, Any]] = []
    payments = (
        (
            await session.execute(
                select(Payment).where(
                    Payment.status.in_({"paid", "refunded"}),
                    Payment.paid_at >= start,
                    Payment.paid_at < end,
                )
            )
        )
        .scalars()
        .all()
    )
    for payment in payments:
        records.append(
            {
                "kind": "payment",
                "provider_txn_id": payment.provider_txn_id,
                "amount_cents": payment.amount_cents,
            }
        )
    refunds = (
        (
            await session.execute(
                select(Refund).where(
                    Refund.status == "succeeded",
                    Refund.completed_at >= start,
                    Refund.completed_at < end,
                )
            )
        )
        .scalars()
        .all()
    )
    for refund in refunds:
        records.append(
            {
                "kind": "refund",
                "provider_txn_id": refund.provider_refund_id,
                "amount_cents": refund.amount_cents,
            }
        )
    return records


@app.post("/payments/reconciliation")
async def reconcile(
    principal: PrincipalDep, biz_date: str | None = None, currency: str = "CNY"
) -> dict[str, Any]:
    """日终对账：渠道账单与本地支付/退款逐笔比对，差异只记录不自动改资金。"""
    principal.require_staff()
    target_date = biz_date or (date.today() - timedelta(days=1)).isoformat()
    async with get_session_factory()() as session:
        local = await _local_records(session, target_date)
        provider_records = provider.statement(biz_date=target_date, local_records=local)

        local_map = {(row["kind"], row["provider_txn_id"]): row for row in local if row["provider_txn_id"]}
        provider_map = {
            (row["kind"], row["provider_txn_id"]): row for row in provider_records if row["provider_txn_id"]
        }
        diffs: list[dict[str, Any]] = []
        for key, row in local_map.items():
            remote = provider_map.get(key)
            if remote is None:
                diffs.append({"provider_txn_id": key[1], "local_amount": row["amount_cents"], "provider_amount": 0, "kind": "missing_provider"})
            elif int(remote["amount_cents"]) != row["amount_cents"]:
                diffs.append({"provider_txn_id": key[1], "local_amount": row["amount_cents"], "provider_amount": int(remote["amount_cents"]), "kind": "amount_mismatch"})
        for key, row in provider_map.items():
            if key not in local_map:
                diffs.append({"provider_txn_id": key[1], "local_amount": 0, "provider_amount": row["amount_cents"], "kind": "missing_local"})

        # 结算汇总：收入扣除手续费与退款后的净额
        gross = sum(row["amount_cents"] for row in local if row["kind"] == "payment")
        refund_total = sum(row["amount_cents"] for row in local if row["kind"] == "refund")
        fee = gross * settings.payment_fee_bps // 10000
        settlement = await session.scalar(
            select(Settlement).where(
                Settlement.provider == provider.name, Settlement.biz_date == target_date
            )
        )
        if settlement is None:
            settlement = Settlement(id=str(uuid.uuid4()), provider=provider.name, biz_date=target_date)
            session.add(settlement)
        settlement.gross_cents = gross
        settlement.fee_cents = fee
        settlement.net_cents = gross - fee - refund_total
        settlement.currency = currency

        for diff in diffs:
            existing = await session.scalar(
                select(ReconciliationDiff).where(
                    ReconciliationDiff.biz_date == target_date,
                    ReconciliationDiff.provider_txn_id == diff["provider_txn_id"],
                    ReconciliationDiff.kind == diff["kind"],
                )
            )
            if existing is None:
                session.add(
                    ReconciliationDiff(
                        id=str(uuid.uuid4()),
                        biz_date=target_date,
                        provider_txn_id=diff["provider_txn_id"],
                        local_amount=diff["local_amount"],
                        provider_amount=diff["provider_amount"],
                        kind=diff["kind"],
                    )
                )

        # 总账平衡校验：任何一笔交易借方与贷方合计必须相等
        rows = await session.execute(
            select(LedgerEntry.txn_id, LedgerEntry.direction, func.sum(LedgerEntry.amount_cents)).group_by(
                LedgerEntry.txn_id, LedgerEntry.direction
            )
        )
        totals: dict[str, dict[str, int]] = {}
        for txn_id, direction, amount in rows:
            totals.setdefault(txn_id, {"debit": 0, "credit": 0})[direction] = int(amount or 0)
        ledger_balanced = all(item["debit"] == item["credit"] for item in totals.values())

        await session.commit()
        return {
            "biz_date": target_date,
            "payments": len([row for row in local if row["kind"] == "payment"]),
            "refunds": len([row for row in local if row["kind"] == "refund"]),
            "settlement": {
                "gross_cents": gross,
                "fee_cents": fee,
                "net_cents": gross - fee - refund_total,
                "currency": currency,
            },
            "diffs": diffs,
            "ledger_balanced": ledger_balanced,
        }
