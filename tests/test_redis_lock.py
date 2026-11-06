import asyncio

from shop_common.redis_lock import RedisLock


class _Script:
    def __init__(self, func):
        self._func = func

    async def __call__(self, keys, args):
        return await self._func(keys, args)


class FakeRedis:
    """只覆盖 RedisLock 用到的命令：set + register_script（release/renew 两条 Lua）。"""

    def __init__(self):
        self.values = {}
        self.renewed = []

    async def set(self, name, value, nx=False, ex=None):
        if nx and name in self.values:
            return False
        self.values[name] = value
        return True

    def register_script(self, script):
        async def run(keys, args):
            key = keys[0]
            # 两条脚本都先校验持有者 token，不匹配直接返回 0（不删别人的锁、不给别人续租）
            if self.values.get(key) != args[0]:
                return 0
            if len(args) == 2:  # renew 脚本带 token + ttl
                self.renewed.append(key)
                return 1
            self.values.pop(key, None)  # release 脚本
            return 1

        return _Script(run)


async def test_lock_acquire_and_release_allows_reacquire():
    redis = FakeRedis()
    lock = RedisLock(redis, "shop:task:x", ttl_seconds=10)
    assert await lock.acquire() is True
    assert "shop:task:x" in redis.values
    await lock.release()
    assert "shop:task:x" not in redis.values
    assert await lock.acquire() is True
    await lock.release()


async def test_lock_is_mutually_exclusive():
    redis = FakeRedis()
    first = RedisLock(redis, "shop:task:x", ttl_seconds=10)
    second = RedisLock(redis, "shop:task:x", ttl_seconds=10)
    assert await first.acquire() is True
    assert await second.acquire() is False
    await first.release()
    assert await second.acquire() is True
    await second.release()


async def test_release_does_not_delete_foreign_lock():
    redis = FakeRedis()
    lock = RedisLock(redis, "shop:task:x", ttl_seconds=10)
    assert await lock.acquire() is True
    # 模拟锁已被他人抢占（原 token 失效），原持有者释放不得误删新锁
    redis.values["shop:task:x"] = "other-token"
    await lock.release()
    assert redis.values["shop:task:x"] == "other-token"


async def test_heartbeat_renews_lock():
    redis = FakeRedis()
    lock = RedisLock(redis, "shop:task:x", ttl_seconds=1, heartbeat_seconds=0.01)
    assert await lock.acquire() is True
    await asyncio.sleep(0.05)
    assert redis.renewed, "心跳应在持有期间续租"
    await lock.release()
    # 释放后心跳任务已退出，不再续租
    renewed_after_release = len(redis.renewed)
    await asyncio.sleep(0.05)
    assert len(redis.renewed) == renewed_after_release
