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
    def __init__(
        self,
        redis: Redis,
        namespace: str = "shop",
        *,
        reclaim_idle_ms: int = 30_000,
        max_deliveries: int = 5,
        reclaim_batch: int = 10,
    ) -> None:
        self.redis = redis
        self.namespace = namespace
        # pending 回收参数：消费失败或进程崩溃的事件不会被 XREADGROUP ">" 读到，
        # 必须按空闲时长认领重投；超过最大投递次数转入死信，避免无限重试拖住消费组。
        self.reclaim_idle_ms = reclaim_idle_ms
        self.max_deliveries = max_deliveries
        self.reclaim_batch = reclaim_batch

    def _stream(self, topic: str) -> str:
        # key 统一带命名空间，方便同一 Redis 实例上多个环境/应用互不干扰。
        return f"{self.namespace}:events:{topic}"

    def _dead_stream(self, topic: str) -> str:
        return f"{self.namespace}:events:{topic}:dead"

    def _dedupe_key(self, topic: str, group: str, event_id: str) -> str:
        # 去重键按 topic + 消费组 + 事件 ID 隔离，不同消费组各自消费同一事件不会被互相吞掉。
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
            # 近似裁剪限制流长度，防止长期运行后事件无上限堆积撑爆 Redis。
            maxlen=maxlen,
            approximate=True,
        )
        return event_id

    async def ensure_group(self, topic: str, group: str) -> None:
        # 组已存在时 Redis 返回 BUSYGROUP，按幂等成功处理，保证消费者重启不报错。
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
        """拉取一批事件并把它们交给 handler；处理成功后才 ack，失败则留在 pending。

        先回收超时未 ack 的 pending 消息再读新消息：否则失败的消费永远等不到重试。
        """
        processed = await self._retry_pending(topic, group, consumer, handler)
        entries = await self.redis.xreadgroup(
            group, consumer, {self._stream(topic): ">"}, count=count, block=block_ms
        )
        for _, messages in entries:
            for message_id, fields in messages:
                if await self._handle_message(topic, group, message_id, fields, handler):
                    processed += 1
        return processed

    async def _handle_message(
        self,
        topic: str,
        group: str,
        message_id: str,
        fields: dict[str, Any],
        handler: Handler,
        *,
        redelivered: bool = False,
    ) -> bool:
        """处理单条事件；返回是否真正执行了 handler（重复事件 ack 跳过返回 False）。"""
        event_id = fields.get("id", message_id)
        dedupe = self._dedupe_key(topic, group, event_id)
        if redelivered:
            # 原消费者可能在写占位后崩溃，残留的占位会让重投的同一事件被当成重复直接 ack，
            # 因此重投递前先清掉占位（handler 侧仍需幂等，认领存在短暂的并发窗口）。
            await self.redis.delete(dedupe)
        # SET NX 占位做消费去重：已处理的事件直接 ack 跳过，占位 24 小时后自动过期。
        if not await self.redis.set(dedupe, "1", nx=True, ex=86400):
            await self.redis.xack(self._stream(topic), group, message_id)
            return False
        event = {
            "id": event_id,
            "type": fields.get("type"),
            "tenant": fields.get("tenant", "default"),
            "payload": json.loads(fields.get("payload", "{}")),
        }
        try:
            await handler(event)
        except Exception:
            # 处理失败时删除占位且不 ack，事件留在 pending，由 _retry_pending 重投。
            await self.redis.delete(dedupe)
            logger.exception("事件处理失败 topic=%s group=%s id=%s", topic, group, event_id)
            return False
        await self.redis.xack(self._stream(topic), group, message_id)
        return True

    async def _retry_pending(
        self, topic: str, group: str, consumer: str, handler: Handler
    ) -> int:
        """认领空闲超时的 pending 消息并重投；超过最大投递次数转死信。"""
        stream = self._stream(topic)
        try:
            _next_id, messages, _deleted = await self.redis.xautoclaim(
                stream,
                group,
                consumer,
                min_idle_time=self.reclaim_idle_ms,
                start_id="0-0",
                count=self.reclaim_batch,
            )
        except ResponseError as exc:
            # 兼容不支持 XAUTOCLAIM 的旧 Redis（<6.2）：只记录告警，不影响正常消费。
            logger.warning("pending 回收不可用 topic=%s error=%s", topic, exc)
            return 0
        processed = 0
        for message_id, fields in messages:
            pending = await self.redis.xpending_range(stream, group, message_id, message_id, 1)
            deliveries = pending[0]["times_delivered"] if pending else 0
            if deliveries >= self.max_deliveries:
                try:
                    await self._move_to_dead_letter(topic, group, message_id, fields, deliveries)
                except Exception:
                    # 死信写入失败时不 ack，下一轮继续尝试，避免事件直接丢失。
                    logger.exception("死信写入失败 topic=%s id=%s", topic, fields.get("id", message_id))
                    continue
                await self.redis.xack(stream, group, message_id)
                logger.error(
                    "事件超过最大重试次数，已转死信 topic=%s group=%s id=%s deliveries=%s",
                    topic,
                    group,
                    fields.get("id", message_id),
                    deliveries,
                )
                continue
            if await self._handle_message(topic, group, message_id, fields, handler, redelivered=True):
                processed += 1
        return processed

    async def _move_to_dead_letter(
        self,
        topic: str,
        group: str,
        message_id: str,
        fields: dict[str, Any],
        deliveries: int,
    ) -> None:
        await self.redis.xadd(
            self._dead_stream(topic),
            {
                "id": fields.get("id", message_id),
                "type": fields.get("type", ""),
                "tenant": fields.get("tenant", "default"),
                "payload": fields.get("payload", "{}"),
                "source_message_id": message_id,
                "group": group,
                "deliveries": str(deliveries),
            },
            maxlen=1000,
            approximate=True,
        )
        # 死信后允许人工重放：清掉占位，否则重放会被去重逻辑吞掉。
        await self.redis.delete(self._dedupe_key(topic, group, fields.get("id", message_id)))

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
                # 循环异常（如 Redis 短暂不可用）不退出消费者，退避后继续。
                logger.exception("事件循环异常 topic=%s，2 秒后重试", topic)
                await asyncio.sleep(2)


def create_redis(url: str) -> Redis:
    return Redis.from_url(url, decode_responses=True)
