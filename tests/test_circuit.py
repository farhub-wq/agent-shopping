import pytest
from harness.core.circuit import CircuitBreaker


class _FakeRedis:
    # 熔断器依赖：get/set/delete/incrby/expire。值按 str 存，贴近真实 Redis。
    def __init__(self):
        self.store: dict[str, str] = {}
        self.expires: dict[str, int] = {}
        self.broken = False  # 置 True 模拟 Redis 故障，验证 fail-open

    async def get(self, key):
        if self.broken:
            raise ConnectionError("redis down")
        return self.store.get(key)

    async def set(self, key, value, ex=None):
        if self.broken:
            raise ConnectionError("redis down")
        self.store[key] = str(value)
        if ex:
            self.expires[key] = ex

    async def delete(self, key):
        if self.broken:
            raise ConnectionError("redis down")
        self.store.pop(key, None)

    async def incrby(self, key, amount):
        if self.broken:
            raise ConnectionError("redis down")
        self.store[key] = str(int(self.store.get(key) or 0) + amount)
        return int(self.store[key])

    async def expire(self, key, seconds):
        if self.broken:
            raise ConnectionError("redis down")
        self.expires[key] = seconds


def _breaker(redis, threshold=3, cooldown=60.0, now=1000.0):
    # 注入固定时钟：测试不依赖真实时间流逝，时间推进改 now_fn 返回值即可。
    clock = {"t": now}
    b = CircuitBreaker(redis, "primary", failure_threshold=threshold, cooldown_seconds=cooldown, now_fn=lambda: clock["t"])
    return b, clock


@pytest.mark.asyncio
async def test_breaker_opens_after_threshold_failures():
    redis = _FakeRedis()
    b, _ = _breaker(redis, threshold=2)
    assert await b.allow_request() is True
    await b.record_failure()
    assert await b.allow_request() is True
    await b.record_failure()
    assert await b.allow_request() is False  # 连续 2 次失败后熔断


@pytest.mark.asyncio
async def test_state_shared_across_workers():
    # 两个 breaker 实例共用一份 Redis = 两个 worker：A 失败计数，B 立即看到开路。
    redis = _FakeRedis()
    worker_a, _ = _breaker(redis, threshold=2)
    worker_b, _ = _breaker(redis, threshold=2)
    await worker_a.record_failure()
    await worker_a.record_failure()
    assert await worker_b.allow_request() is False


@pytest.mark.asyncio
async def test_half_open_allows_single_probe_only():
    # 冷却期结束：第一个 allow_request 放行试探并悲观续写开路，
    # 第二个（其他 worker）在试探结果落地前继续 fail-fast。
    redis = _FakeRedis()
    b, clock = _breaker(redis, threshold=1, cooldown=60.0)
    await b.record_failure()
    clock["t"] += 61  # 冷却期已过
    assert await b.allow_request() is True  # 放行试探
    assert await b.allow_request() is False  # 悲观续写生效：并发请求不跟风
    # 试探失败：维持开路；时钟再推进，下一轮才可再试探。
    await b.record_failure()
    assert await b.allow_request() is False
    clock["t"] += 61
    assert await b.allow_request() is True
    # 试探成功：真正闭合。
    await b.record_success()
    assert await b.allow_request() is True
    assert await b.allow_request() is True  # 不再受悲观续写影响


@pytest.mark.asyncio
async def test_breaker_reset_on_success():
    redis = _FakeRedis()
    b, _ = _breaker(redis, threshold=2)
    await b.record_failure()
    await b.record_success()  # 一次成功清零
    await b.record_failure()
    assert await b.allow_request() is True  # 只有 1 次连续失败，不应熔断


@pytest.mark.asyncio
async def test_fail_open_when_redis_down():
    # Redis 故障：放行判断 fail-open；记录动作吞错不抛出，不打断模型调用链。
    redis = _FakeRedis()
    b, _ = _breaker(redis)
    redis.broken = True
    assert await b.allow_request() is True
    await b.record_failure()
    await b.record_success()
