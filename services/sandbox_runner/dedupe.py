"""跨 runner 的重复执行防护：Redis 结果缓存 + 分布式执行锁。

目标：同一个 execution_id 在多台 runner 之间最多真正执行一次。
- 先查结果缓存，命中直接复用（故障转移时拿到同一份结果）；
- 抢到执行锁后再查一次缓存，堵住「上一任刚写完结果就释放锁」的竞态；
- 只有真正跑过容器的结果（ok/error/timeout）才缓存；unavailable 表示本实例没执行，
  不能写入缓存，否则其他健康 runner 会被一个假失败挡住；
- 锁带心跳续租，长时间执行不会中途失锁；runner 崩溃后锁按 TTL 过期，
  后续请求可重新执行（宁可在崩溃窗口重跑，也不能永久卡死）。
"""

import asyncio
import json
import time

from redis.asyncio import Redis
from shop_common.logging import get_logger
from shop_common.redis_lock import RedisLock
from shop_common.sandbox import SandboxResult

logger = get_logger(__name__)

# 真正执行过的状态才可缓存；unavailable 不入缓存，避免阻断故障转移。
CACHEABLE_STATUSES = {"ok", "error", "timeout"}


class ExecutionDedupe:
    def __init__(
        self,
        redis: Redis,
        *,
        lock_ttl_seconds: int = 60,
        result_ttl_seconds: int = 300,
    ) -> None:
        self.redis = redis
        # 锁 TTL 必须覆盖单次执行超时；执行期间靠心跳续租，崩溃后才按 TTL 过期。
        self.lock_ttl_seconds = max(lock_ttl_seconds, 30)
        # 结果缓存保留到一次请求的全部故障转移尝试结束即可，5 分钟足够。
        self.result_ttl_seconds = max(result_ttl_seconds, 60)

    @staticmethod
    def result_key(execution_id: str) -> str:
        return f"sandbox:exec:{execution_id}:result"

    @staticmethod
    def lock_name(execution_id: str) -> str:
        return f"sandbox:exec:{execution_id}:lock"

    async def cached(self, execution_id: str) -> SandboxResult | None:
        raw = await self.redis.get(self.result_key(execution_id))
        if not raw:
            return None
        try:
            payload = json.loads(raw)
        except (ValueError, TypeError):
            # 缓存内容损坏时当作未命中，宁可重新执行也不返回错误结果。
            return None
        if not isinstance(payload, dict):
            return None
        return SandboxResult.from_dict(payload)

    async def store(self, execution_id: str, result: SandboxResult) -> None:
        await self.redis.set(
            self.result_key(execution_id),
            json.dumps(result.to_dict()),
            ex=self.result_ttl_seconds,
        )

    def lock(self, execution_id: str) -> RedisLock:
        return RedisLock(self.redis, self.lock_name(execution_id), ttl_seconds=self.lock_ttl_seconds)

    async def wait_for_result(self, execution_id: str, wait_seconds: float) -> SandboxResult | None:
        """另一个 runner 正在执行时短轮询结果：拿到就复用，拿不到交给调用方判定繁忙。"""
        deadline = time.monotonic() + max(wait_seconds, 0)
        while True:
            cached = await self.cached(execution_id)
            if cached is not None:
                return cached
            if time.monotonic() >= deadline:
                return None
            await asyncio.sleep(0.25)
