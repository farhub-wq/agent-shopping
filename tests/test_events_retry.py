import json
import uuid

from shop_common.db import get_session_factory, init_models
from shop_common.events import EventBus
from shop_common.tables import DeadEvent, ProcessedEvent
from sqlalchemy import select


def _uid(prefix: str) -> str:
    # 测试库跨次运行持久化，固定 ID 会残留并触发唯一索引冲突，用每次运行唯一的 ID 隔离
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


class FakeRedis:
    """只覆盖 EventBus 用到的流命令；去重与死信已落库，不再走 Redis。"""

    def __init__(self, pending):
        # pending: [{"message_id": str, "fields": dict, "times_delivered": int}]
        self.pending = pending
        self.acked = []
        self.published = []

    async def xreadgroup(self, group, consumer, streams, count=10, block=1000):
        return []

    async def xautoclaim(
        self, name, group, consumer, min_idle_time, start_id="0-0", count=None, justid=False
    ):
        claimed = [(row["message_id"], row["fields"]) for row in self.pending][:count]
        return ["0-0", claimed, []]

    async def xpending_range(self, name, group, min, max, count):
        for row in self.pending:
            if row["message_id"] == min:
                return [{"times_delivered": row["times_delivered"]}]
        return []

    async def xack(self, name, group, message_id):
        self.acked.append(message_id)
        return 1

    async def xadd(self, name, fields, maxlen=None, approximate=True):
        self.published.append((name, fields))
        return "1-1"


def make_message(event_id, deliveries=2, message_id="1000-0"):
    return {
        "message_id": message_id,
        "times_delivered": deliveries,
        "fields": {
            "id": event_id,
            "type": "payment.refunded",
            "tenant": "default",
            "payload": json.dumps({"order_id": "o-1"}),
        },
    }


async def test_pending_message_is_retried_and_acked():
    await init_models()
    evt_id = _uid("evt-retry")
    redis = FakeRedis([make_message(event_id=evt_id, deliveries=2)])
    bus = EventBus(redis, session_factory=get_session_factory(), reclaim_idle_ms=0, max_deliveries=5)
    handled = []

    async def handler(event):
        handled.append(event)

    processed = await bus.poll("payment", "order-svc", "order-3", handler)

    assert processed == 1
    assert handled[0]["id"] == evt_id
    assert redis.acked == ["1000-0"]


async def test_retry_clears_stale_processed_record():
    # 模拟原消费者写完占位后崩溃：不去掉占位，重投的消息会被误判为重复而直接 ack。
    await init_models()
    evt_id = _uid("evt-stale")
    async with get_session_factory()() as session:
        session.add(ProcessedEvent(topic="payment", consumer_group="order-svc", event_id=evt_id))
        await session.commit()
    redis = FakeRedis([make_message(event_id=evt_id, deliveries=2)])
    bus = EventBus(redis, session_factory=get_session_factory(), reclaim_idle_ms=0, max_deliveries=5)
    handled = []

    async def handler(event):
        handled.append(event)

    processed = await bus.poll("payment", "order-svc", "order-3", handler)

    assert processed == 1
    assert handled and redis.acked == ["1000-0"]


async def test_message_over_max_deliveries_moves_to_dead_letter():
    await init_models()
    evt_id = _uid("evt-dead")
    redis = FakeRedis([make_message(event_id=evt_id, deliveries=5)])
    bus = EventBus(redis, session_factory=get_session_factory(), reclaim_idle_ms=0, max_deliveries=5)
    handled = []

    async def handler(event):
        handled.append(event)

    processed = await bus.poll("payment", "order-svc", "order-3", handler)

    assert processed == 0
    assert handled == []
    assert redis.acked == ["1000-0"]
    async with get_session_factory()() as session:
        dead = (
            await session.execute(select(DeadEvent).where(DeadEvent.event_id == evt_id))
        ).scalars().all()
        assert len(dead) == 1
        assert dead[0].source_message_id == "1000-0"
        assert dead[0].deliveries == 5


async def test_failed_retry_keeps_message_pending_and_clears_mark():
    await init_models()
    evt_id = _uid("evt-fail")
    redis = FakeRedis([make_message(event_id=evt_id, deliveries=2)])
    bus = EventBus(redis, session_factory=get_session_factory(), reclaim_idle_ms=0, max_deliveries=5)

    async def handler(event):
        raise RuntimeError("boom")

    processed = await bus.poll("payment", "order-svc", "order-3", handler)

    assert processed == 0
    assert redis.acked == []
    # 失败后占位被清除，下一轮重投仍能抢占处理权
    async with get_session_factory()() as session:
        remaining = (
            await session.execute(
                select(ProcessedEvent).where(ProcessedEvent.event_id == evt_id)
            )
        ).scalars().all()
        assert remaining == []


async def test_replay_dead_republishes_and_removes():
    await init_models()
    evt_id = _uid("evt-replay")
    async with get_session_factory()() as session:
        session.add(
            DeadEvent(
                topic="payment",
                consumer_group="order-svc",
                event_id=evt_id,
                event_type="payment.refunded",
                tenant="default",
                payload={"order_id": "o-9"},
                source_message_id="1000-1",
                deliveries=5,
                reason="超过最大投递次数 5",
            )
        )
        await session.commit()
    redis = FakeRedis([])
    bus = EventBus(redis, session_factory=get_session_factory(), reclaim_idle_ms=0, max_deliveries=5)

    replayed = await bus.replay_dead(topic="payment", event_id=evt_id)

    assert replayed == 1
    assert redis.published[0][0] == "shop:events:payment"
    assert redis.published[0][1]["id"] == evt_id
    async with get_session_factory()() as session:
        remaining = (
            await session.execute(select(DeadEvent).where(DeadEvent.event_id == evt_id))
        ).scalars().all()
        assert remaining == []
