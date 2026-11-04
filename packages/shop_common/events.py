"""基于 Redis Streams 的最小事件总线。

选它而不是 Kafka 的原因：垂直切片只需要「至少一次投递 + 消费者组 + 幂等」三件事，
Redis 已在库存热点路径上，少一个中间件就少一类部署故障。生产环境替换为 Kafka 时，
只需保持 `publish/poll` 两个方法的行为契约。
"""

import asyncio
import json
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import ResponseError

from .logging import get_logger

logger = get_logger(__name__)

Handler = Callable[[dict[str, Any]], Awaitable[None]]


class EventBus:
    def __init__(self, redis: Redis, namespace: str = "shop") -> None:
        self.redis = redis
        self.namespace = namespace

    def _stream(self, topic: str) -> str:
        return f"{self.namespace}:events:{topic}"

    def _dedupe_key(self, topic: str, group: str, event_id: str) -> str:
        return f"{self.namespace}:dedupe:{topic}:{group}:{event_id}"

    async def publish(
        self,
        topic: str,
        event_type: str,
        payload: dict[str, Any],
        *,
        event_id: str | None = None,
        tenant: str = "default",
        maxlen: int = 5000,
    ) -> str:
        event_id = event_id or str(uuid.uuid4())
        await self.redis.xadd(
            self._stream(topic),
            {
                "id": event_id,
                "type": event_type,
                "tenant": tenant,
                "payload": json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            },
            maxlen=maxlen,
            approximate=True,
        )
        return event_id

    async def ensure_group(self, topic: str, group: str) -> None:
        try:
            await self.redis.xgroup_create(self._stream(topic), group, id="0", mkstream=True)
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    async def poll(
        self,
        topic: str,
        group: str,
        consumer: str,
        handler: Handler,
        *,
        count: int = 10,
        block_ms: int = 1000,
    ) -> int:
        """拉取一批事件并把它们交给 handler；处理成功后才 ack，失败则留在 pending。"""
        entries = await self.redis.xreadgroup(
            group, consumer, {self._stream(topic): ">"}, count=count, block=block_ms
        )
        processed = 0
        for _, messages in entries:
            for message_id, fields in messages:
                event_id = fields.get("id", message_id)
                dedupe = self._dedupe_key(topic, group, event_id)
                if not await self.redis.set(dedupe, "1", nx=True, ex=86400):
                    await self.redis.xack(self._stream(topic), group, message_id)
                    continue
                event = {
                    "id": event_id,
                    "type": fields.get("type"),
                    "tenant": fields.get("tenant", "default"),
                    "payload": json.loads(fields.get("payload", "{}")),
                }
                try:
                    await handler(event)
                except Exception:
                    await self.redis.delete(dedupe)
                    logger.exception("事件处理失败 topic=%s id=%s", topic, event_id)
                    continue
                await self.redis.xack(self._stream(topic), group, message_id)
                processed += 1
        return processed

    async def run_consumer(
        self,
        topic: str,
        group: str,
        consumer: str,
        handler: Handler,
    ) -> None:
        await self.ensure_group(topic, group)
        logger.info("事件消费者启动 topic=%s group=%s", topic, group)
        while True:
            try:
                await self.poll(topic, group, consumer, handler)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("事件循环异常 topic=%s，2 秒后重试", topic)
                await asyncio.sleep(2)


def create_redis(url: str) -> Redis:
    return Redis.from_url(url, decode_responses=True)
