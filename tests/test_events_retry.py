import json

from shop_common.events import EventBus


class FakeRedis:
    """只覆盖 EventBus 用到的命令，用于验证 pending 重投与死信逻辑。"""

    def __init__(self, pending):
        # pending: [{"message_id": str, "fields": dict, "times_delivered": int}]
        self.pending = pending
        self.acked = []
        self.values = {}
        self.dead = []

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

    async def set(self, key, value, nx=False, ex=None):
        if nx and key in self.values:
            return False
        self.values[key] = value
        return True

    async def delete(self, *keys):
        for key in keys:
            self.values.pop(key, None)
        return 1

    async def xadd(self, name, fields, maxlen=None, approximate=True):
        self.dead.append((name, fields))
        return "1-1"


def make_message(event_id="evt-1", deliveries=2, message_id="1000-0"):
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
    redis = FakeRedis([make_message(deliveries=2)])
    bus = EventBus(redis, reclaim_idle_ms=0, max_deliveries=5)
    handled = []

    async def handler(event):
        handled.append(event)

    processed = await bus.poll("payment", "order-svc", "order-3", handler)

    assert processed == 1
    assert handled[0]["id"] == "evt-1"
    assert redis.acked == ["1000-0"]


async def test_retry_clears_stale_dedupe_placeholder():
    # 模拟原消费者写完占位后崩溃：不去掉占位，重投的消息会被误判为重复而直接 ack。
    redis = FakeRedis([make_message(deliveries=2)])
    redis.values["shop:dedupe:payment:order-svc:evt-1"] = "1"
    bus = EventBus(redis, reclaim_idle_ms=0, max_deliveries=5)
    handled = []

    async def handler(event):
        handled.append(event)

    processed = await bus.poll("payment", "order-svc", "order-3", handler)

    assert processed == 1
    assert handled and redis.acked == ["1000-0"]


async def test_message_over_max_deliveries_moves_to_dead_letter():
    redis = FakeRedis([make_message(deliveries=5)])
    bus = EventBus(redis, reclaim_idle_ms=0, max_deliveries=5)
    handled = []

    async def handler(event):
        handled.append(event)

    processed = await bus.poll("payment", "order-svc", "order-3", handler)

    assert processed == 0
    assert handled == []
    assert redis.acked == ["1000-0"]
    assert redis.dead[0][0] == "shop:events:payment:dead"
    assert redis.dead[0][1]["source_message_id"] == "1000-0"
    assert redis.dead[0][1]["deliveries"] == "5"


async def test_failed_retry_keeps_message_pending():
    redis = FakeRedis([make_message(deliveries=2)])
    bus = EventBus(redis, reclaim_idle_ms=0, max_deliveries=5)

    async def handler(event):
        raise RuntimeError("boom")

    processed = await bus.poll("payment", "order-svc", "order-3", handler)

    assert processed == 0
    assert redis.acked == []
