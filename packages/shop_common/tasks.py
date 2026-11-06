"""定时任务的统一外壳：分布式锁互斥 + task_runs 轨迹。

多个服务实例同时启动时，同一任务只允许一个实例执行（锁），
执行结果与耗时写入 task_runs 供运维观测，失败不退出循环。
"""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .logging import get_logger
from .redis_lock import RedisLock
from .tables import TaskRun

logger = get_logger(__name__)

TaskFunc = Callable[[], Awaitable[None]]


async def run_task_loop(
    name: str,
    redis: Redis,
    session_factory: async_sessionmaker[AsyncSession],
    interval_seconds: float,
    func: TaskFunc,
    *,
    lock_ttl_seconds: float = 60,
) -> None:
    """带锁与轨迹的定时任务循环。

    - 抢不到锁说明其他实例正在执行，直接跳过本轮；
    - 每轮执行前后写 task_runs（succeeded/failed），失败只记日志不抛出。
    """
    lock = RedisLock(redis, f"shop:task:{name}", ttl_seconds=lock_ttl_seconds)
    while True:
        await asyncio.sleep(interval_seconds)
        if not await lock.acquire():
            continue
        started_at = datetime.now(UTC)
        try:
            await func()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("定时任务 %s 执行失败", name)
            await _record(
                session_factory, name, "failed", started_at, {"error": f"{type(exc).__name__}: {exc}"}
            )
        else:
            await _record(session_factory, name, "succeeded", started_at)
        finally:
            await lock.release()


async def _record(
    session_factory: async_sessionmaker[AsyncSession],
    name: str,
    status: str,
    started_at: datetime,
    detail: dict[str, Any] | None = None,
) -> None:
    try:
        async with session_factory() as session:
            session.add(
                TaskRun(
                    task_name=name,
                    status=status,
                    started_at=started_at,
                    finished_at=datetime.now(UTC),
                    detail=detail or {},
                )
            )
            await session.commit()
    except Exception:
        # 轨迹写入失败不能反过来拖垮任务循环
        logger.exception("task_runs 轨迹写入失败 task=%s", name)
