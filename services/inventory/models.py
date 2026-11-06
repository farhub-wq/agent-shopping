from datetime import UTC, datetime

from shop_common.db import Base, UTCDateTime
from sqlalchemy import Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column


def _now() -> datetime:
    return datetime.now(UTC)


class Stock(Base):
    __tablename__ = "stocks"

    # 库存真相源：total 为总库存，reserved 为已预占未提交部分，可用量 = total - reserved。
    sku: Mapped[str] = mapped_column(String(64), primary_key=True)
    total: Mapped[int] = mapped_column(Integer, default=0)
    reserved: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), default=_now, onupdate=_now
    )


class Reservation(Base):
    __tablename__ = "stock_reservations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    order_id: Mapped[str] = mapped_column(String(36), index=True)
    # 行级预占：一个订单的每个 SKU 一行，(order_id, sku) 唯一既是业务约束也是幂等依据。
    sku: Mapped[str] = mapped_column(String(64))
    qty: Mapped[int] = mapped_column(Integer)
    # 状态机：reserved → committed（已售）或 released（回补），重复请求按当前状态幂等处理。
    status: Mapped[str] = mapped_column(String(16), default="reserved", index=True)
    # 兜底释放的触发点：支付窗口 + 5 分钟缓冲；payment.expires_at 是唯一权威，这里只作兜底。
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), default=_now, onupdate=_now
    )

    __table_args__ = (
        UniqueConstraint("order_id", "sku", name="uq_stock_reservation_order_sku"),
    )


class Restock(Base):
    __tablename__ = "restocks"
    # (order_id, sku) 唯一：记录该订单该 SKU 的累计已回补量，端点按「目标值」语义做幂等
    __table_args__ = (UniqueConstraint("order_id", "sku", name="uq_restock_order_sku"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[str] = mapped_column(String(36), index=True)
    sku: Mapped[str] = mapped_column(String(64))
    qty: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), default=_now, onupdate=_now
    )
