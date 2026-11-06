from datetime import UTC, datetime

from shop_common.db import Base
from sqlalchemy import Boolean, DateTime, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column


def _now() -> datetime:
    return datetime.now(UTC)


class Shipment(Base):
    __tablename__ = "shipments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    # 同一订单允许拆成多个履约单：部分发货时剩余数量会自动拆到新的 pending 单
    order_id: Mapped[str] = mapped_column(String(36), index=True)
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

    # 显式挑字段输出，避免整对象序列化把内部列暴露出去；items 由调用方批量查询后传入
    def to_dict(self, items: list[dict] | None = None) -> dict:
        return {
            "id": self.id,
            "order_id": self.order_id,
            "status": self.status,
            "carrier": self.carrier,
            "tracking_no": self.tracking_no,
            "deadline_at": self.deadline_at.isoformat(),
            "sla_flagged": self.sla_flagged,
            "items": items or [],
            "created_at": self.created_at.isoformat(),
            "shipped_at": self.shipped_at.isoformat() if self.shipped_at else None,
            "delivered_at": self.delivered_at.isoformat() if self.delivered_at else None,
        }


class ShipmentItem(Base):
    __tablename__ = "shipment_items"
    __table_args__ = (
        UniqueConstraint("shipment_id", "order_item_id", name="uq_shipment_item"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    shipment_id: Mapped[str] = mapped_column(String(36), index=True)
    # 关联订单行的稳定 item_id；不依赖 SKU，避免同款多行时无法区分
    order_item_id: Mapped[str] = mapped_column(String(36), index=True)
    sku: Mapped[str] = mapped_column(String(64), default="")
    qty: Mapped[int] = mapped_column(Integer)

    def to_dict(self) -> dict:
        return {
            "order_item_id": self.order_item_id,
            "sku": self.sku,
            "qty": self.qty,
        }
