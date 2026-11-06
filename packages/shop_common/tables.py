"""事件去重、死信与定时任务轨迹的通用表。

这些表落在各自服务的库里（事件消费与任务都在服务进程内运行），
跨服务不共享，靠 DB 唯一索引保证消费幂等与任务互斥的最终兜底。
"""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from .db import Base, UTCDateTime


def _now() -> datetime:
    return datetime.now(UTC)


class ProcessedEvent(Base):
    __tablename__ = "processed_events"
    __table_args__ = (
        UniqueConstraint("topic", "consumer_group", "event_id", name="uq_processed_event"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    topic: Mapped[str] = mapped_column(String(64))
    consumer_group: Mapped[str] = mapped_column(String(64))
    event_id: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=_now, index=True)


class DeadEvent(Base):
    __tablename__ = "dead_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    topic: Mapped[str] = mapped_column(String(64), index=True)
    consumer_group: Mapped[str] = mapped_column(String(64))
    event_id: Mapped[str] = mapped_column(String(64))
    event_type: Mapped[str] = mapped_column(String(64))
    tenant: Mapped[str] = mapped_column(String(64), default="default")
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    source_message_id: Mapped[str] = mapped_column(String(64), default="")
    deliveries: Mapped[int] = mapped_column(Integer, default=0)
    reason: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=_now, index=True)


class TaskRun(Base):
    __tablename__ = "task_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    task_name: Mapped[str] = mapped_column(String(64), index=True)
    # started / succeeded / failed
    status: Mapped[str] = mapped_column(String(16), default="started", index=True)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=_now)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
