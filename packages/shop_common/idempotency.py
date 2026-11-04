"""幂等键：同一 Idempotency-Key 只允许一次副作用，重复请求返回首次结果。"""

import json
from typing import Any

from redis.asyncio import Redis

from .errors import ApiError


class IdempotencyStore:
    def __init__(self, redis: Redis, namespace: str = "shop", ttl_seconds: int = 600) -> None:
        self.redis = redis
        self.namespace = namespace
        self.ttl = ttl_seconds

    def _key(self, scope: str, key: str) -> str:
        return f"{self.namespace}:idem:{scope}:{key}"

    async def begin(self, scope: str, key: str | None) -> bool:
        """返回 True 表示首次执行；False 表示已有请求在途或已完成。"""
        if not key:
            return True
        return bool(await self.redis.set(self._key(scope, key), "{}", nx=True, ex=self.ttl))

    async def get(self, scope: str, key: str | None) -> dict[str, Any] | None:
        if not key:
            return None
        raw = await self.redis.get(self._key(scope, key))
        if not raw or raw == "{}":
            return None
        return json.loads(raw)

    async def finish(self, scope: str, key: str | None, result: dict[str, Any]) -> None:
        if not key:
            return
        await self.redis.set(
            self._key(scope, key), json.dumps(result, ensure_ascii=False), ex=self.ttl
        )

    async def release(self, scope: str, key: str | None) -> None:
        if key:
            await self.redis.delete(self._key(scope, key))

    async def require_ready(self, scope: str, key: str | None) -> dict[str, Any] | None:
        """重复键：已有结果就返回，仍在处理中则拒绝。"""
        cached = await self.get(scope, key)
        if cached is not None:
            return cached
        if key:
            raise ApiError("IDEMPOTENCY_KEY_REUSED", "相同幂等键的请求仍在处理中，请稍后重试")
        return None
