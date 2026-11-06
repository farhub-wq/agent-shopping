from datetime import UTC, datetime

from shop_common.db import Base, UTCDateTime
from sqlalchemy import Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON


def _now() -> datetime:
    return datetime.now(UTC)


class AfterSaleTicket(Base):
    __tablename__ = "after_sale_tickets"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    # 工单号对用户可见，唯一索引避免重复生成造成客服对不上号
    ticket_no: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    order_id: Mapped[str] = mapped_column(String(36), index=True)
    # 行级售后：order_item_id + quantity 为空表示整单退款
    order_item_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    quantity: Mapped[int] = mapped_column(Integer, default=0)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    type: Mapped[str] = mapped_column(String(24), default="refund_only")
    reason: Mapped[str] = mapped_column(String(64))
    description: Mapped[str] = mapped_column(Text, default="")
    amount_cents: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    # 风险分与命中原因由政策引擎写入，保留评分依据便于人工复核时解释结论
    risk_score: Mapped[int] = mapped_column(Integer, default=0)
    risk_reasons: Mapped[list] = mapped_column(JSON, default=list)
    # 决策说明：自动档写政策理由，人工档写客服备注
    decision_note: Mapped[str] = mapped_column(Text, default="")
    # Agent 只能写建议，不参与状态流转；单独存字段以便审计模型输出与最终决策的差异
    agent_recommendation: Mapped[dict] = mapped_column(JSON, default=dict)
    # 记录决策人：policy-engine 表示自动档，其余为人工操作者的用户 ID
    decided_by: Mapped[str] = mapped_column(String(64), default="")
    # 支付服务的退款单号与完成时间，由 payment.refunded 事件回填
    refund_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    refunded_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=_now)
    decided_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)

    # 只挑对外字段返回，不把 ORM 对象整体序列化
    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "ticket_no": self.ticket_no,
            "order_id": self.order_id,
            "order_item_id": self.order_item_id,
            "quantity": self.quantity,
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
            "refund_id": self.refund_id,
            "refunded_at": self.refunded_at.isoformat() if self.refunded_at else None,
            "created_at": self.created_at.isoformat(),
            "decided_at": self.decided_at.isoformat() if self.decided_at else None,
        }
