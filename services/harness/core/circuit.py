"""模型熔断器：跨 worker 共享状态，失败计数与开路截止时间放 Redis。

只负责「该不该放行」这一件事，不感知模型与业务；降级编排在 llm.FailoverProvider。

多 worker 语义：计数 INCRBY 原子累加；冷却期结束后先悲观续写开路时间再放行
本进程这一次试探，实现跨进程「单试探」——其余 worker 看到新时间戳继续 fail-fast。

Redis 自身故障时 fail-open：熔断是护栏不是关键路径，护栏基础设施挂了
不能把模型调用一并掐死。
"""

import time
from collections.abc import Callable
from typing import Any

from shop_common.logging import get_logger

logger = get_logger(__name__)


class CircuitBreaker:
    def __init__(
        self,
        redis: Any,
        name: str,
        failure_threshold: int = 3,
        cooldown_seconds: float = 300.0,
        now_fn: Callable[[], float] = time.time,
    ) -> None:
        self._redis = redis
        self.failure_threshold = failure_threshold
        self.cooldown_seconds = cooldown_seconds
        self._now = now_fn
        self._failures_key = f"shop:harness:circuit:{name}:failures"
        self._open_until_key = f"shop:harness:circuit:{name}:open_until"

    async def allow_request(self) -> bool:
        """闭合放行；开路未到期拒绝；冷却期结束悲观续写后放行一次试探。"""
        try:
            raw = await self._redis.get(self._open_until_key)
        except Exception as exc:  # Redis 故障：护栏 fail-open，热路径只记 debug
            logger.debug("熔断器状态读取失败，按放行处理：%s", exc)
            return True
        if raw is None:
            return True
        now = self._now()
        if float(raw) > now:
            return False
        # 半开试探：先续写开路时间，让其他 worker 继续 fail-fast；
        # 试探成功由 record_success 真正闭合，失败由 record_failure 维持开路。
        try:
            await self._redis.set(
                self._open_until_key, str(now + self.cooldown_seconds), ex=int(self.cooldown_seconds * 2)
            )
        except Exception as exc:
            logger.debug("熔断器试探标记写入失败，仍放行本次试探：%s", exc)
        return True

    async def record_success(self) -> None:
        """一次成功清零连续失败计数并闭合电路。"""
        try:
            await self._redis.delete(self._open_until_key)
            await self._redis.delete(self._failures_key)
        except Exception as exc:
            logger.warning("熔断器闭合状态写入失败：%s", exc)

    async def record_failure(self) -> None:
        """只对「连续」失败计数：达到阈值即开路，截止时间写墙钟（跨进程可读）。"""
        try:
            failures = await self._redis.incrby(self._failures_key, 1)
            if failures == 1:
                # 计数键随两倍冷却期自然过期：无需后台清理，过期即视同闭合重计。
                await self._redis.expire(self._failures_key, int(self.cooldown_seconds * 2))
            if failures >= self.failure_threshold:
                await self._redis.set(
                    self._open_until_key, str(self._now() + self.cooldown_seconds), ex=int(self.cooldown_seconds * 2)
                )
        except Exception as exc:
            logger.warning("熔断器失败计数写入失败：%s", exc)
