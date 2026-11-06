from datetime import UTC, datetime

from shop_common.db import Base, UTCDateTime
from sqlalchemy import Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON


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
    __tablename__ = "reservations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    # order_id 唯一索引既是业务约束，也是预占接口的幂等依据：一个订单只允许一条预占记录。
    order_id: Mapped[str] = mapped_column(String(36), unique=True, index=True)
    items: Mapped[list] = mapped_column(JSON, default=list)
    # 状态机：reserved → committed（已售）或 released（回补），重复请求按当前状态幂等处理。
    status: Mapped[str] = mapped_column(String(16), default="reserved", index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), default=_now, onupdate=_now
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
