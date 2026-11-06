"""事务性 Outbox：业务写入与事件发布在同一事务内落库，再由中继异步投递。

这解决的是「订单已创建但事件丢失」和「事件已发但事务回滚」两类不一致，
也是让微服务之间不靠双写碰运气的唯一低成本办法。
"""

import asyncio
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Integer, String, Text, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from .db import Base, UTCDateTime
from .events import EventBus
from .logging import get_logger
from .redis_lock import RedisLock

logger = get_logger(__name__)


class OutboxEvent(Base):
    __tablename__ = "outbox_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    topic: Mapped[str] = mapped_column(String(64), index=True)
    event_type: Mapped[str] = mapped_column(String(64))
    tenant: Mapped[str] = mapped_column(String(64), default="default")
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), default=lambda: datetime.now(UTC), index=True
    )
    # published_at 为空即待投递；attempts 与 last_error 用于重试与故障观测。
    published_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime(), nullable=True, index=True
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)


def enqueue(
    session: AsyncSession,
    topic: str,
    event_type: str,
    payload: dict[str, Any],
    *,
    tenant: str = "default",
) -> OutboxEvent:
    """在业务事务里调用；调用方负责 commit。"""
    event = OutboxEvent(topic=topic, event_type=event_type, payload=payload, tenant=tenant)
    session.add(event)
    return event


class OutboxRelay:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        bus: EventBus,
        *,
        batch_size: int = 200,
    ) -> None:
        self.session_factory = session_factory
        self.bus = bus
        self.batch_size = batch_size
        # 每个 topic 一把中继锁：多实例部署时同一 topic 只允许一个 relay 投递，避免双发。
        self._locks: dict[str, RedisLock] = {}

    def _lock(self, topic: str) -> RedisLock:
        if topic not in self._locks:
            self._locks[topic] = RedisLock(
                self.bus.redis, f"{self.bus.namespace}:relay:{topic}", ttl_seconds=60
            )
        return self._locks[topic]

    async def flush_once(self) -> int:
        async with self.session_factory() as session:
            pending = (
                (
                    await session.execute(
                        # 只取未投递事件并按创建时间升序，尽量保持同一聚合根事件的先后顺序。
                        select(OutboxEvent)
                        .where(OutboxEvent.published_at.is_(None))
                        .order_by(OutboxEvent.created_at)
                        .limit(self.batch_size)
                    )
                )
                .scalars()
                .all()
            )
            if not pending:
                return 0
            # 快照取出，避免在并行投递任务里跨会话持有 ORM 实例。
            snapshot = [
                {
                    "id": row.id,
                    "topic": row.topic,
                    "event_type": row.event_type,
                    "payload": row.payload,
                    "tenant": row.tenant,
                }
                for row in pending
            ]
        by_topic: dict[str, list[dict[str, Any]]] = {}
        for item in snapshot:
            by_topic.setdefault(item["topic"], []).append(item)
        # 按 topic 分组并行投递：不同 topic 互不阻塞，吞吐不再被单 topic 串行拖慢。
        results = await asyncio.gather(
            *(self._relay_topic(topic, items) for topic, items in by_topic.items())
        )
        succeeded: list[str] = []
        failed: list[tuple[str, str]] = []
        sent = 0
        for ok_ids, err_ids in results:
            succeeded.extend(ok_ids)
            failed.extend(err_ids)
            sent += len(ok_ids)
        # 成功标记与失败记录共用一次提交，保证投递状态和错误信息不丢。
        async with self.session_factory() as session:
            if succeeded:
                rows = (
                    (
                        await session.execute(
                            select(OutboxEvent).where(OutboxEvent.id.in_(succeeded))
                        )
                    )
                    .scalars()
                    .all()
                )
                for row in rows:
                    row.published_at = datetime.now(UTC)
                    row.attempts += 1
            if failed:
                err_map = dict(failed)
                rows = (
                    (
                        await session.execute(
                            select(OutboxEvent).where(OutboxEvent.id.in_([i for i, _ in failed]))
                        )
                    )
                    .scalars()
                    .all()
                )
                for row in rows:
                    row.attempts += 1
                    row.last_error = err_map[row.id]
            await session.commit()
            return sent

    async def _relay_topic(
        self, topic: str, items: list[dict[str, Any]]
    ) -> tuple[list[str], list[tuple[str, str]]]:
        lock = self._lock(topic)
        if not await lock.acquire():
            # 其他实例正在投递该 topic，本轮让出，事件留在下一轮扫描。
            logger.info("relay topic=%s 锁被占用，跳过本轮", topic)
            return [], []
        succeeded: list[str] = []
        failed: list[tuple[str, str]] = []
        try:
            for item in items:
                try:
                    await self.bus.publish(
                        item["topic"],
                        item["event_type"],
                        item["payload"],
                        event_id=item["id"],
                        tenant=item["tenant"],
                    )
                    succeeded.append(item["id"])
                except Exception as exc:  # 投递失败不阻塞后续事件，交给下一轮重试
                    failed.append((item["id"], f"{type(exc).__name__}: {exc}"))
                    logger.warning("outbox 投递失败 id=%s", item["id"])
        finally:
            await lock.release()
        return succeeded, failed

    async def run_forever(self, interval_seconds: float = 0.5) -> None:
        logger.info("outbox 中继启动 interval=%.1fs", interval_seconds)
        while True:
            try:
                await self.flush_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                # 中继异常不退出循环，记录后继续下一轮扫描。
                logger.exception("outbox 中继异常，2 秒后重试")
            await asyncio.sleep(interval_seconds)
