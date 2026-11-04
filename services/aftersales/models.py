from datetime import datetime, timezone

from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from shop_common.db import Base


def _now() -> datetime:
    return datetime.now(timezone.utc)


class AfterSaleTicket(Base):
    __tablename__ = "after_sale_tickets"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    ticket_no: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    order_id: Mapped[str] = mapped_column(String(36), index=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    type: Mapped[str] = mapped_column(String(24), default="refund_only")
    reason: Mapped[str] = mapped_column(String(64))
    description: Mapped[str] = mapped_column(Text, default="")
    amount_cents: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    risk_score: Mapped[int] = mapped_column(Integer, default=0)
    risk_reasons: Mapped[list] = mapped_column(JSON, default=list)
    decision_note: Mapped[str] = mapped_column(Text, default="")
    agent_recommendation: Mapped[dict] = mapped_column(JSON, default=dict)
    decided_by: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "ticket_no": self.ticket_no,
            "order_id": self.order_id,
            "user_id": self.user_id,
            "type": self.type,
            "reason": self.reason,
            "description": self.description,
            "amount_cents": self.amount_cents,
            "status": self.status,
            "risk_score": self.risk_score,
            "risk_reasons": self.risk_reasons or [],
            "decision_note": self.decision_note,
            "agent_recommendation": self.agent_recommendation or {},
            "decided_by": self.decided_by,
            "created_at": self.created_at.isoformat(),
            "decided_at": self.decided_at.isoformat() if self.decided_at else None,
        }
