from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from shop_common.db import Base


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Shipment(Base):
    __tablename__ = "shipments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    # 一单一履约：order_id 唯一索引同时承担业务约束与事件重投时的幂等兜底
    order_id: Mapped[str] = mapped_column(String(36), unique=True, index=True)
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    carrier: Mapped[str] = mapped_column(String(32), default="")
    tracking_no: Mapped[str] = mapped_column(String(64), default="")
    # SLA 截止时间在创建履约单时确定，扫描器据此判定超时
    deadline_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # 超时只告警一次的开关，避免同一履约单在每轮扫描中重复产生事件
    sla_flagged: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    shipped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 发货重试次数，供后续接入承运方失败重试时统计使用
    attempts: Mapped[int] = mapped_column(Integer, default=0)

    # 显式挑字段输出，避免整对象序列化把内部列暴露出去
    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "order_id": self.order_id,
            "status": self.status,
            "carrier": self.carrier,
            "tracking_no": self.tracking_no,
            "deadline_at": self.deadline_at.isoformat(),
            "sla_flagged": self.sla_flagged,
            "created_at": self.created_at.isoformat(),
            "shipped_at": self.shipped_at.isoformat() if self.shipped_at else None,
            "delivered_at": self.delivered_at.isoformat() if self.delivered_at else None,
        }
