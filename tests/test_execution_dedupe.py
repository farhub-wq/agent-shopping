"""跨 runner 去重存储的读写契约测试（不依赖真实 Redis）。"""

import pytest
from sandbox_runner.dedupe import ExecutionDedupe
from shop_common.sandbox import SandboxResult


class _FakeRedis:
    def __init__(self) -> None:
        self.data: dict[str, object] = {}
        self.expires: dict[str, int | None] = {}

    async def get(self, key: str) -> object:
        return self.data.get(key)

    async def set(self, key: str, value: object, ex: int | None = None) -> bool:
        self.data[key] = value
        self.expires[key] = ex
        return True


@pytest.mark.asyncio
async def test_store_and_cached_roundtrip():
    dedupe = ExecutionDedupe(_FakeRedis())
    result = SandboxResult("ok", stdout="42", backend="docker", exit_code=0)

    await dedupe.store("exec-1", result)

    cached = await dedupe.cached("exec-1")
    assert cached == result


@pytest.mark.asyncio
async def test_cached_ignores_corrupted_payload():
    redis = _FakeRedis()
    dedupe = ExecutionDedupe(redis)
    await redis.set(dedupe.result_key("exec-1"), b"not-json", ex=300)

    # 缓存损坏时按未命中处理，宁可重新执行也不返回错误结果。
    assert await dedupe.cached("exec-1") is None


@pytest.mark.asyncio
async def test_store_sets_ttl():
    redis = _FakeRedis()
    dedupe = ExecutionDedupe(redis, result_ttl_seconds=300)

    await dedupe.store("exec-1", SandboxResult("ok"))

    assert redis.expires[dedupe.result_key("exec-1")] == 300
