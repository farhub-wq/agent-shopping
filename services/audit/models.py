"""audit 服务的审计日志模型：只增不改，保留运营动作的可追溯轨迹。"""

from datetime import datetime
from typing import Any

from shop_common.db import Base, UTCDateTime
from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON


class AuditLog(Base):
    __tablename__ = "audit_log"

    # 主键直接复用事件 ID：重复投递撞主键即可幂等，无需额外唯一索引
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime(), index=True)
    service: Mapped[str] = mapped_column(String(32))
    actor_id: Mapped[str] = mapped_column(String(36), index=True)
    actor_role: Mapped[str] = mapped_column(String(16))
    tenant_id: Mapped[str] = mapped_column(String(36), index=True)
    action: Mapped[str] = mapped_column(String(64), index=True)
    target_type: Mapped[str] = mapped_column(String(32), index=True)
    target_id: Mapped[str] = mapped_column(String(64), index=True)
    request_id: Mapped[str] = mapped_column(String(64), index=True)
    before: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    after: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    ip_hash: Mapped[str] = mapped_column(String(64), default="")
