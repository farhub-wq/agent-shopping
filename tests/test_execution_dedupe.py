"""跨 runner 去重存储的读写契约测试（不依赖真实 Redis）。"""

import pytest
from sandbox_runner.dedupe import ExecutionDedupe, owner_scope
from shop_common.sandbox import SandboxResult

OWNER = "owner-a"


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

    await dedupe.store(OWNER, "exec-1", result)

    cached = await dedupe.cached(OWNER, "exec-1")
    assert cached == result


@pytest.mark.asyncio
async def test_cached_ignores_corrupted_payload():
    redis = _FakeRedis()
    dedupe = ExecutionDedupe(redis)
    await redis.set(dedupe.result_key(OWNER, "exec-1"), b"not-json", ex=300)

    # 缓存损坏时按未命中处理，宁可重新执行也不返回错误结果。
    assert await dedupe.cached(OWNER, "exec-1") is None


@pytest.mark.asyncio
async def test_store_sets_ttl():
    redis = _FakeRedis()
    dedupe = ExecutionDedupe(redis, result_ttl_seconds=300)

    await dedupe.store(OWNER, "exec-1", SandboxResult("ok"))

    assert redis.expires[dedupe.result_key(OWNER, "exec-1")] == 300


@pytest.mark.asyncio
async def test_result_is_scoped_to_owner():
    redis = _FakeRedis()
    dedupe = ExecutionDedupe(redis)

    await dedupe.store(OWNER, "exec-1", SandboxResult("ok", stdout="owner-a"))

    # 同一个 execution_id 换主体必须查不到：键里绑定主体摘要，内网他人无法串读。
    assert await dedupe.cached("owner-b", "exec-1") is None


def test_owner_scope_is_stable_and_opaque():
    # 同一主体得到同一摘要（故障转移可共享缓存），但摘要不含明文身份字段。
    first = owner_scope("acme", "u-1")
    assert first == owner_scope("acme", "u-1")
    assert first != owner_scope("acme", "u-2")
    assert "u-1" not in first
