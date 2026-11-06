"""MongoDB 客户端封装：承载 Harness 的记忆与 Agent 轨迹这类文档型数据。

懒创建客户端，导入期不连库；服务未部署 Mongo 时由调用方在使用处拿到连接错误，
不会因为 import 顺序导致整个进程起不来。
"""

from __future__ import annotations

from typing import Any

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase

from .config import get_settings

settings = get_settings()

_client: AsyncIOMotorClient[Any] | None = None


def get_mongo_client() -> AsyncIOMotorClient[Any]:
    global _client
    if _client is None:
        # tz_aware：读回的时间统一带 UTC 时区，避免调用方再猜时间基准；
        # serverSelectionTimeoutMS：连不上时快速失败，不让请求长时间挂起。
        _client = AsyncIOMotorClient(
            settings.mongo_url,
            tz_aware=True,
            serverSelectionTimeoutMS=3000,
        )
    return _client


def get_mongo_db() -> AsyncIOMotorDatabase[Any]:
    return get_mongo_client()[settings.mongo_db]


async def close_mongo() -> None:
    global _client
    if _client is not None:
        # 进程退出时释放连接池；close() 本身不是协程，这里保持 async 便于 lifespan 统一 await。
        _client.close()
        _client = None
