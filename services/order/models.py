from datetime import datetime, timezone
from typing import Any

from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from shop_common.db import Base


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    order_no: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    tenant: Mapped[str] = mapped_column(String(64), default="default", index=True)
    status: Mapped[str] = mapped_column(String(16), default="CREATED", index=True)
    amount_cents: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(8), default="CNY")
    items: Mapped[list] = mapped_column(JSON, default=list)
    address: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    cancel_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    refund_previous: Mapped[str | None] = mapped_column(String(16), nullable=True)
    refund_amount_cents: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    shipped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def to_dict(self) -> dict[str, Any]:
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
            "refund_amount_cents": self.refund_amount_cents,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "paid_at": self.paid_at.isoformat() if self.paid_at else None,
            "shipped_at": self.shipped_at.isoformat() if self.shipped_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
        }


class OrderEvent(Base):
    __tablename__ = "order_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[str] = mapped_column(String(36), index=True)
    from_status: Mapped[str] = mapped_column(String(16))
    to_status: Mapped[str] = mapped_column(String(16))
    event: Mapped[str] = mapped_column(String(32))
    actor: Mapped[str] = mapped_column(String(64), default="system")
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
