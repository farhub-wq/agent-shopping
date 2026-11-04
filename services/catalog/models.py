from datetime import datetime, timezone

from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from shop_common.db import Base


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Product(Base):
    __tablename__ = "products"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    sku: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    title: Mapped[str] = mapped_column(String(200), index=True)
    subtitle: Mapped[str] = mapped_column(String(200), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    category: Mapped[str] = mapped_column(String(64), index=True)
    price_cents: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(8), default="CNY")
    image_url: Mapped[str] = mapped_column(String(500), default="")
    status: Mapped[str] = mapped_column(String(16), default="draft", index=True)
    tags: Mapped[list] = mapped_column(JSON, default=list)
    attributes: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "sku": self.sku,
            "title": self.title,
            "subtitle": self.subtitle,
            "description": self.description,
            "category": self.category,
            "price_cents": self.price_cents,
            "currency": self.currency,
            "image_url": self.image_url,
            "status": self.status,
            "tags": self.tags or [],
            "attributes": self.attributes or {},
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }
