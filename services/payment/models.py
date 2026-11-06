"""payment_db 的 ORM 模型：支付单、退款、清结算、复式总账与对账差异。

资金表只增不改：已入账分录不允许更新或删除，冲正必须新增反向分录。
"""

from datetime import UTC, datetime
from typing import Any

from shop_common.db import Base, UTCDateTime
from sqlalchemy import BigInteger, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column


def _now() -> datetime:
    return datetime.now(UTC)


class Payment(Base):
    __tablename__ = "payments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(36), default="default", index=True)
    # 支付单归属用户：mock 收银台与支付确认按此校验，防止跨用户代付/窥探他人支付单
    user_id: Mapped[str] = mapped_column(String(36), default="", index=True)
    # 一单一个支付单：order_id 唯一索引同时承担业务约束与创建幂等兜底
    order_id: Mapped[str] = mapped_column(String(36), unique=True, index=True)
    provider: Mapped[str] = mapped_column(String(32), default="mock")
    # 渠道流水号唯一，重复回调靠它识别；未支付前允许为空
    provider_txn_id: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True)
    amount_cents: Mapped[int] = mapped_column(BigInteger)
    currency: Mapped[str] = mapped_column(String(8), default="CNY")
    # pending → paid / expired / refunded；失败回调不覆盖已支付状态
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    pay_url: Mapped[str] = mapped_column(Text, default="")
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), index=True)
    paid_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=_now, onupdate=_now)

    def to_dict(self) -> dict[str, Any]:
        # 显式挑字段，内部租户、渠道签名等不对外暴露
        return {
            "id": self.id,
            "order_id": self.order_id,
            "provider": self.provider,
            "provider_txn_id": self.provider_txn_id,
            "amount_cents": self.amount_cents,
            "currency": self.currency,
            "status": self.status,
            "pay_url": self.pay_url,
            "expires_at": self.expires_at.isoformat(),
            "paid_at": self.paid_at.isoformat() if self.paid_at else None,
        }


class Refund(Base):
    __tablename__ = "refunds"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    payment_id: Mapped[str] = mapped_column(String(36), index=True)
    order_id: Mapped[str] = mapped_column(String(36), index=True)
    # 售后工单号唯一：同一工单重复审批不会重复出款。
    # 除售后工单 UUID 外，迟到支付用 latepay-<order_id>（44 字符），列宽需覆盖两种来源。
    ticket_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    order_item_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    quantity: Mapped[int] = mapped_column(Integer, default=0)
    amount_cents: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    provider_refund_id: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True)
    failure_reason: Mapped[str] = mapped_column(Text, default="")
    requested_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=_now)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "payment_id": self.payment_id,
            "order_id": self.order_id,
            "ticket_id": self.ticket_id,
            "order_item_id": self.order_item_id,
            "quantity": self.quantity,
            "amount_cents": self.amount_cents,
            "status": self.status,
            "provider_refund_id": self.provider_refund_id,
            "requested_at": self.requested_at.isoformat(),
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
        }


class Chargeback(Base):
    __tablename__ = "chargebacks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(36), default="default", index=True)
    payment_id: Mapped[str] = mapped_column(String(36), index=True)
    # 渠道争议单号唯一：重复拒付通知靠它识别，是拒付幂等的唯一键
    provider_case_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    amount_cents: Mapped[int] = mapped_column(BigInteger)
    currency: Mapped[str] = mapped_column(String(8), default="CNY")
    # opened → won（申诉成功，渠道退回资金）/ lost（败诉，损失确认）
    status: Mapped[str] = mapped_column(String(16), default="opened", index=True)
    reason: Mapped[str] = mapped_column(Text, default="")
    opened_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=_now)
    closed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=_now)


class Settlement(Base):
    __tablename__ = "settlements"
    __table_args__ = (UniqueConstraint("provider", "biz_date", name="uq_settlement_provider_date"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    provider: Mapped[str] = mapped_column(String(32))
    biz_date: Mapped[str] = mapped_column(String(10), index=True)
    gross_cents: Mapped[int] = mapped_column(BigInteger, default=0)
    fee_cents: Mapped[int] = mapped_column(BigInteger, default=0)
    net_cents: Mapped[int] = mapped_column(BigInteger, default=0)
    currency: Mapped[str] = mapped_column(String(8), default="CNY")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=_now)


class LedgerTransaction(Base):
    __tablename__ = "ledger_transactions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(36), default="default", index=True)
    # 交易类型：payment / refund / settlement / chargeback
    type: Mapped[str] = mapped_column(String(16), index=True)
    ref_type: Mapped[str] = mapped_column(String(16))
    ref_id: Mapped[str] = mapped_column(String(36), index=True)
    currency: Mapped[str] = mapped_column(String(8), default="CNY")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=_now, index=True)


class LedgerEntry(Base):
    __tablename__ = "ledger_entries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    txn_id: Mapped[str] = mapped_column(String(36), index=True)
    account_code: Mapped[str] = mapped_column(String(32))
    # direction 只用 debit / credit；金额始终为正，方向决定借贷
    direction: Mapped[str] = mapped_column(String(8))
    amount_cents: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=_now)


class ReconciliationDiff(Base):
    __tablename__ = "reconciliation_diffs"
    __table_args__ = (
        UniqueConstraint("biz_date", "provider_txn_id", "kind", name="uq_recon_diff"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    biz_date: Mapped[str] = mapped_column(String(10), index=True)
    provider_txn_id: Mapped[str] = mapped_column(String(64), index=True)
    local_amount: Mapped[int] = mapped_column(BigInteger, default=0)
    provider_amount: Mapped[int] = mapped_column(BigInteger, default=0)
    # missing_local / missing_provider / amount_mismatch
    kind: Mapped[str] = mapped_column(String(24))
    status: Mapped[str] = mapped_column(String(16), default="open", index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=_now)
