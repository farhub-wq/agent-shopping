"""基于 Redis 的分布式锁：SET NX 占锁 + TTL 过期 + 心跳续租。

供 relay 与各服务定时任务在多实例部署下互斥：抢不到锁的一方跳过本轮，
避免关单/释放/对账被多个实例重复执行。释放与续租都校验持有者 token，
防止误删或给他人续命。
"""

import asyncio
import contextlib
import uuid

from redis.asyncio import Redis

from .logging import get_logger

logger = get_logger(__name__)

# 释放与续租都必须是「持有者本人」：用 Lua 原子校验 token，避免删掉别人的锁。
_RELEASE_LUA = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("del", KEYS[1])
else
    return 0
end
"""

_RENEW_LUA = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("expire", KEYS[1], ARGV[2])
else
    return 0
end
"""


class RedisLock:
    def __init__(
        self,
        redis: Redis,
        name: str,
        *,
        ttl_seconds: float = 30,
        heartbeat_seconds: float | None = None,
    ) -> None:
        self.redis = redis
        self.name = name
        self.ttl_seconds = max(int(ttl_seconds), 1)
        # 心跳周期取 TTL 的 1/3，保证续租先于过期，长任务不会中途丢锁
        self.heartbeat_seconds = heartbeat_seconds or (self.ttl_seconds / 3)
        self._token: str | None = None
        self._release_script = redis.register_script(_RELEASE_LUA)
        self._renew_script = redis.register_script(_RENEW_LUA)
        self._heartbeat_task: asyncio.Task | None = None

    async def acquire(self) -> bool:
        """尝试一次性占锁；被占用立即返回 False，不阻塞等待。"""
        token = str(uuid.uuid4())
        ok = await self.redis.set(self.name, token, nx=True, ex=self.ttl_seconds)
        if not ok:
            return False
        self._token = token
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        return True

    async def release(self) -> None:
        await self._stop_heartbeat()
        if self._token is None:
            return
        await self._release_script(keys=[self.name], args=[self._token])
        self._token = None

    async def _stop_heartbeat(self) -> None:
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            # 等心跳任务真正退出，避免释放后残留协程继续给已归还的锁续租
            with contextlib.suppress(asyncio.CancelledError):
                await self._heartbeat_task
            self._heartbeat_task = None

    async def _heartbeat_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(self.heartbeat_seconds)
                if self._token is None:
                    return
                # 续租前确认仍是持有者：锁若已过期被他人拿走，就不再续命
                await self._renew_script(keys=[self.name], args=[self._token, self.ttl_seconds])
        except asyncio.CancelledError:
            raise
