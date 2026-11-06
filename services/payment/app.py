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
from shop_common.audit import enqueue_audit
from shop_common.auth import PrincipalDep
from shop_common.config import get_settings
from shop_common.db import dispose_engine, get_session_factory, init_models
from shop_common.errors import ApiError, install_error_handlers
from shop_common.events import EventBus, create_redis
from shop_common.logging import configure_logging, get_logger
from shop_common.metrics import PAYMENT_CALLBACK_TOTAL, RECONCILIATION_DIFF, install_metrics
from shop_common.outbox import OutboxRelay, enqueue
from shop_common.tasks import run_task_loop
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from . import models  # noqa: F401  # 注册到 Base.metadata，init_models 才能建表
from .ledger import post_transaction
from .models import Chargeback, LedgerEntry, Payment, ReconciliationDiff, Refund, Settlement
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
        await dispose_engine()


app = FastAPI(title="payment-service", version="0.1.0", lifespan=lifespan)
install_error_handlers(app, "payment")
install_metrics(app, "payment")


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok", "service": "payment"}


class CreatePaymentReq(BaseModel):
    order_id: str = Field(min_length=1, max_length=36)
    amount_cents: int = Field(gt=0)
    currency: str = Field(default="CNY", max_length=8)
    tenant: str = Field(default="default", max_length=36)
    user_id: str = Field(min_length=1, max_length=36)


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
            user_id=payload.user_id,
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


@app.get("/payments/chargebacks")
async def list_chargebacks(
    principal: PrincipalDep, status: str | None = None, page: int = 1, size: int = 20
) -> dict[str, Any]:
    """运营查询拒付列表：状态过滤 + 分页，附 order_id 便于关联订单。

    必须注册在 /payments/{order_id} 之前，否则 chargebacks 会被当作订单号吞掉。
    """
    principal.require_staff()
    page = max(1, page)
    size = max(1, min(size, 100))
    async with get_session_factory()() as session:
        query = select(Chargeback)
        count_query = select(func.count(Chargeback.id))
        if status:
            query = query.where(Chargeback.status == status)
            count_query = count_query.where(Chargeback.status == status)
        total = int((await session.scalar(count_query)) or 0)
        rows = (
            (
                await session.execute(
                    query.order_by(Chargeback.opened_at.desc()).offset((page - 1) * size).limit(size)
                )
            )
            .scalars()
            .all()
        )
        # 批量取支付单映射 order_id，避免循环内逐条查库
        payment_ids = [row.payment_id for row in rows]
        payments: dict[str, Payment] = {}
        if payment_ids:
            matched = (
                (await session.execute(select(Payment).where(Payment.id.in_(payment_ids)))).scalars().all()
            )
            payments = {p.id: p for p in matched}
        items = [
            {
                "id": row.id,
                "payment_id": row.payment_id,
                "order_id": payments[row.payment_id].order_id if row.payment_id in payments else "",
                "provider_case_id": row.provider_case_id,
                "amount_cents": row.amount_cents,
                "currency": row.currency,
                "status": row.status,
                "reason": row.reason,
                "opened_at": row.opened_at.isoformat(),
                "closed_at": row.closed_at.isoformat() if row.closed_at else None,
            }
            for row in rows
        ]
    return {"items": items, "page": page, "size": size, "total": total}


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
    try:
        event = provider.verify_callback(
            raw_body=raw_body, headers=headers, secret=settings.payment_webhook_secret
        )
    except ApiError:
        # 验签失败/报文非法是伪造回调的前置信号，计数后告警。
        PAYMENT_CALLBACK_TOTAL.labels("verification_failed").inc()
        raise
    return await _apply_callback(event, request=request)


def _event_int(event: dict[str, Any], key: str) -> int:
    try:
        return int(event[key])
    except (KeyError, TypeError, ValueError) as exc:
        raise ApiError("BAD_REQUEST", f"回调缺少合法字段 {key}") from exc


async def _apply_callback(event: dict[str, Any], *, request: Request | None = None) -> dict[str, Any]:
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
            PAYMENT_CALLBACK_TOTAL.labels("not_found").inc()
            raise ApiError("NOT_FOUND", "支付单不存在")
        # 金额与币种必须与建单一致，伪造金额的回调不落账
        if payment.amount_cents != amount or payment.currency != currency:
            PAYMENT_CALLBACK_TOTAL.labels("conflict").inc()
            raise ApiError("CONFLICT", "回调金额或币种与支付单不一致")
        if payment.provider_txn_id and payment.provider_txn_id != txn_id:
            PAYMENT_CALLBACK_TOTAL.labels("conflict").inc()
            raise ApiError("CONFLICT", "支付单已被其他渠道流水号占用")

        if status == "failed":
            PAYMENT_CALLBACK_TOTAL.labels("failed").inc()
            if payment.status == "pending":
                payment.status = "failed"
                enqueue(
                    session,
                    "payment",
                    "payment.failed",
                    {"payment_id": payment.id, "order_id": payment.order_id},
                    tenant=payment.tenant_id,
                )
                enqueue_audit(
                    session,
                    actor_id="channel",
                    actor_role="system",
                    action="payment.callback",
                    target_type="payment",
                    target_id=payment.id,
                    before={"status": "pending"},
                    after={"status": "failed"},
                    request=request,
                    tenant=payment.tenant_id,
                )
                await session.commit()
            return {"status": payment.status, "idempotent": payment.status != "pending"}

        if payment.status == "paid":
            # 重复回调：不再写第二笔分录、不再发第二个事件
            PAYMENT_CALLBACK_TOTAL.labels("paid").inc()
            return {"status": "paid", "idempotent": True}
        if payment.status in {"refunded"}:
            return {"status": payment.status, "idempotent": True}

        payment.provider_txn_id = txn_id
        payment.status = "paid"
        payment.paid_at = datetime.now(UTC)
        PAYMENT_CALLBACK_TOTAL.labels("paid").inc()
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
        enqueue_audit(
            session,
            actor_id="channel",
            actor_role="system",
            action="payment.callback",
            target_type="payment",
            target_id=payment.id,
            before={"status": "pending"},
            after={"status": "paid", "amount_cents": payment.amount_cents},
            request=request,
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
async def mock_cashier(payment_id: str, principal: PrincipalDep) -> HTMLResponse:
    """本地 mock 收银台：真实渠道会跳转到服务商托管页面。"""
    if not _mock_enabled():
        raise ApiError("NOT_FOUND", "接口不存在")
    async with get_session_factory()() as session:
        payment = await session.get(Payment, payment_id)
        if payment is None:
            raise ApiError("NOT_FOUND", "支付单不存在")
        # 收银台页面含他人订单号与金额，校验归属防止 IDOR
        if not principal.is_staff() and payment.user_id != principal.user_id:
            raise ApiError("FORBIDDEN", "无权查看该支付单")
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
async def mock_pay(payment_id: str, principal: PrincipalDep) -> dict[str, Any]:
    """模拟用户在收银台完成支付：构造与真实渠道一致的回调报文并走完整验签流程。"""
    if not _mock_enabled():
        raise ApiError("NOT_FOUND", "接口不存在")
    async with get_session_factory()() as session:
        payment = await session.get(Payment, payment_id)
        if payment is None:
            raise ApiError("NOT_FOUND", "支付单不存在")
        # 支付确认是用户动作：只允许本人或运营代付，防止拿到他人 payment_id 替别人完成支付
        if not principal.is_staff() and payment.user_id != principal.user_id:
            raise ApiError("FORBIDDEN", "无权支付该订单")
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
async def create_refund(payload: RefundReq, principal: PrincipalDep, request: Request) -> dict[str, Any]:
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
        enqueue_audit(
            session,
            actor_id=principal.user_id,
            actor_role=principal.role,
            action="payment.refund",
            target_type="refund",
            target_id=refund.id,
            before={"refunded_cents": int(refunded)},
            after={"amount_cents": refund.amount_cents, "fully_refunded": fully},
            request=request,
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


@app.post("/payments/chargebacks/callback/{provider_name}")
async def chargeback_callback(provider_name: str, request: Request) -> dict[str, Any]:
    """渠道拒付回调入口：验签与支付回调一致，字段按拒付语义解析。"""
    if provider_name != settings.payment_provider:
        raise ApiError("NOT_FOUND", "未知支付渠道")
    raw_body = await request.body()
    headers = {key.lower(): value for key, value in request.headers.items()}
    event = provider.verify_callback(
        raw_body=raw_body, headers=headers, secret=settings.payment_webhook_secret
    )
    return await _apply_chargeback(event, request=request)


async def _apply_chargeback(event: dict[str, Any], *, request: Request | None = None) -> dict[str, Any]:
    """落拒付单 + 总账冲正 + 事件 + 审计；provider_case_id 唯一索引保证重复通知幂等。"""
    case_id = str(event.get("provider_case_id") or "")
    txn_id = str(event.get("provider_txn_id") or "")
    payment_id = str(event.get("payment_id") or "")
    amount = _event_int(event, "amount_cents")
    reason = str(event.get("reason") or "")
    if not case_id:
        raise ApiError("BAD_REQUEST", "回调缺少渠道争议单号")
    if not txn_id and not payment_id:
        raise ApiError("BAD_REQUEST", "回调缺少定位支付单的字段")
    if amount <= 0:
        raise ApiError("BAD_REQUEST", "拒付金额必须大于 0")

    async with get_session_factory()() as session:
        existing = await session.scalar(select(Chargeback).where(Chargeback.provider_case_id == case_id))
        if existing is not None:
            return {"status": existing.status, "idempotent": True}

        payment = (
            await session.scalar(select(Payment).where(Payment.provider_txn_id == txn_id)) if txn_id else None
        )
        if payment is None and payment_id:
            payment = await session.get(Payment, payment_id)
        if payment is None:
            raise ApiError("NOT_FOUND", "支付单不存在")
        # 金额与币种必须与支付单一致；伪造金额的拒付通知不落账
        if payment.amount_cents != amount or payment.currency != str(event.get("currency") or "CNY"):
            raise ApiError("CONFLICT", "拒付金额或币种与支付单不一致")
        if payment.status not in {"paid", "refunded"}:
            raise ApiError("CONFLICT", "支付单未成功，不可拒付")
        if amount > payment.amount_cents:
            raise ApiError("CONFLICT", "拒付金额超过支付金额")

        chargeback = Chargeback(
            id=str(uuid.uuid4()),
            tenant_id=payment.tenant_id,
            payment_id=payment.id,
            provider_case_id=case_id,
            amount_cents=amount,
            currency=payment.currency,
            status="opened",
            reason=reason,
            opened_at=datetime.now(UTC),
        )
        session.add(chargeback)
        # 拒付发生：借记拒付损失、贷记渠道应收，应收减少损失增加
        post_transaction(
            session,
            txn_type="chargeback",
            ref_type="chargeback",
            ref_id=chargeback.id,
            entries=[
                ("CHARGEBACK", "debit", amount),
                ("PROVIDER_RECEIVABLE", "credit", amount),
            ],
            currency=payment.currency,
            tenant=payment.tenant_id,
        )
        enqueue(
            session,
            "payment",
            "payment.chargeback_opened",
            {
                "chargeback_id": chargeback.id,
                "payment_id": payment.id,
                "order_id": payment.order_id,
                "amount_cents": amount,
                "provider_case_id": case_id,
            },
            tenant=payment.tenant_id,
        )
        enqueue_audit(
            session,
            actor_id="channel",
            actor_role="system",
            action="payment.chargeback_opened",
            target_type="chargeback",
            target_id=chargeback.id,
            before={"status": "opened"},
            after={"amount_cents": amount, "payment_id": payment.id},
            request=request,
            tenant=payment.tenant_id,
        )
        try:
            await session.commit()
        except IntegrityError:
            # 并发重复通知撞 provider_case_id 唯一索引：回滚后按幂等返回
            await session.rollback()
            return {"status": "opened", "idempotent": True}
        return {"status": "opened", "idempotent": False}


class ResolveChargebackReq(BaseModel):
    outcome: str = Field(pattern="^(won|lost)$")


@app.post("/payments/chargebacks/{chargeback_id}/resolve")
async def resolve_chargeback(
    chargeback_id: str, payload: ResolveChargebackReq, principal: PrincipalDep, request: Request
) -> dict[str, Any]:
    """运营处置拒付：won 反向冲正（渠道退回资金），lost 保持损失不额外记账。"""
    principal.require_staff()
    async with get_session_factory()() as session:
        chargeback = await session.get(Chargeback, chargeback_id)
        if chargeback is None:
            raise ApiError("NOT_FOUND", "拒付单不存在")
        if chargeback.status != "opened":
            return {"status": chargeback.status, "idempotent": True}
        if payload.outcome == "won":
            # 申诉成功：借记渠道应收、贷记拒付损失，反向冲正 opened 时的分录
            post_transaction(
                session,
                txn_type="chargeback",
                ref_type="chargeback",
                ref_id=chargeback.id,
                entries=[
                    ("PROVIDER_RECEIVABLE", "debit", chargeback.amount_cents),
                    ("CHARGEBACK", "credit", chargeback.amount_cents),
                ],
                currency=chargeback.currency,
                tenant=chargeback.tenant_id,
            )
        chargeback.status = payload.outcome
        chargeback.closed_at = datetime.now(UTC)
        enqueue(
            session,
            "payment",
            "payment.chargeback_resolved",
            {
                "chargeback_id": chargeback.id,
                "payment_id": chargeback.payment_id,
                "outcome": payload.outcome,
            },
            tenant=chargeback.tenant_id,
        )
        enqueue_audit(
            session,
            actor_id=principal.user_id,
            actor_role=principal.role,
            action="payment.chargeback_resolved",
            target_type="chargeback",
            target_id=chargeback.id,
            before={"status": "opened"},
            after={"status": payload.outcome},
            request=request,
            tenant=chargeback.tenant_id,
        )
        await session.commit()
    return {"status": payload.outcome, "idempotent": False}


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
        # 对账差异数是观测对账健康度的核心仪表：本轮检测到的开放差异量，供告警阈值参考。
        RECONCILIATION_DIFF.labels("payment").set(len(diffs))
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
