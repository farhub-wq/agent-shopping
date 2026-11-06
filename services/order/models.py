from datetime import UTC, datetime
from typing import Any

from shop_common.db import Base
from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON


def _now() -> datetime:
    return datetime.now(UTC)


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    order_no: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    tenant: Mapped[str] = mapped_column(String(64), default="default", index=True)
    # 订单状态只能由状态机变更，这里默认 CREATED，路由不得直接赋值 status。
    status: Mapped[str] = mapped_column(String(16), default="CREATED", index=True)
    amount_cents: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(8), default="CNY")
    items: Mapped[list] = mapped_column(JSON, default=list)
    address: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    cancel_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 支付单由 payment 服务创建；订单只持有引用与展示用收银台地址
    payment_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    pay_url: Mapped[str] = mapped_column(Text, default="")
    payment_status: Mapped[str] = mapped_column(String(16), default="unpaid")
    payment_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 退款进入 REFUNDING 前的状态快照：退款驳回/部分退款完成时据此回退到原状态。
    refund_previous: Mapped[str | None] = mapped_column(String(16), nullable=True)
    refund_amount_cents: Mapped[int] = mapped_column(Integer, default=0)
    # 已处理事件 ID：outbox 去重窗口只有 24h，业务侧再留一层，防止部分发货/部分退款被重复累加
    processed_event_ids: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    shipped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def to_dict(self) -> dict[str, Any]:
        # 显式挑选对外字段，不直接序列化 ORM 实例；时间统一转 ISO 字符串。
        return {
            "id": self.id,
            "order_no": self.order_no,
            "user_id": self.user_id,
            "status": self.status,
            "amount_cents": self.amount_cents,
            "currency": self.currency,
            "items": self.items or [],
            "address": self.address or {},
            "cancel_reason": self.cancel_reason,
            "payment_id": self.payment_id,
            "pay_url": self.pay_url,
            "payment_status": self.payment_status,
            "payment_expires_at": (
                self.payment_expires_at.isoformat() if self.payment_expires_at else None
            ),
            "refund_amount_cents": self.refund_amount_cents,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "paid_at": self.paid_at.isoformat() if self.paid_at else None,
            "shipped_at": self.shipped_at.isoformat() if self.shipped_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
        }


class OrderEvent(Base):
    __tablename__ = "order_events"

    # 只追加的事件轨迹：每次状态流转写一条 from→to 记录，用于审计与时间线展示。
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[str] = mapped_column(String(36), index=True)
    from_status: Mapped[str] = mapped_column(String(16))
    to_status: Mapped[str] = mapped_column(String(16))
    event: Mapped[str] = mapped_column(String(32))
    actor: Mapped[str] = mapped_column(String(64), default="system")
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
