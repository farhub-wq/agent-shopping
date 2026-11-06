from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import DateTime, TypeDecorator
from sqlalchemy.dialects import mysql
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from .config import get_settings

settings = get_settings()


class Base(DeclarativeBase):
    pass


class UTCDateTime(TypeDecorator):
    """统一按 UTC 存取时间，屏蔽 MySQL 无时区 DATETIME 的差异。

    MySQL 的 DATETIME 不带时区且默认只有秒级精度，这里显式用 DATETIME(6)；
    bind 时把 aware datetime 归一为 UTC naive，读回时补上 UTC，
    调用方始终拿到 aware UTC，与原先 PostgreSQL timestamptz 行为一致。
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "mysql":
            return dialect.type_descriptor(mysql.DATETIME(fsp=6))
        return dialect.type_descriptor(DateTime(timezone=True))

    def process_bind_param(self, value: datetime | None, dialect):
        if value is None:
            return None
        # naive 值按 UTC 处理，避免服务端把本地时区写入数据库
        if value.tzinfo is None:
            return value
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


_engine = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_engine():
    """每个服务一个进程、一个数据库，引擎惰性创建，避免导入期连接外部依赖。"""
    global _engine, _session_factory
    if _engine is None:
        url = settings.resolved_database_url
        # 取连接前先探测存活，避免中间件重启后仍拿到失效连接。
        kwargs: dict = {"pool_pre_ping": True}
        if url.startswith("sqlite"):
            Path("data").mkdir(parents=True, exist_ok=True)
            # aiosqlite 在线程池中执行 SQL，需要放开 SQLite 的同线程检查。
            kwargs["connect_args"] = {"check_same_thread": False}
        elif url.startswith("mysql"):
            kwargs.update(
                {
                    "pool_size": 10,
                    "max_overflow": 20,
                    # MySQL 默认 8 小时空闲断连，提前回收，避免拿到被服务端关掉的连接。
                    "pool_recycle": 3600,
                    # 与原 PostgreSQL 的 READ COMMITTED 语义保持一致，
                    # 避免 REPEATABLE READ 快照让 outbox 轮询读不到新事件。
                    "isolation_level": "READ COMMITTED",
                    # 池满快速失败：拿不到连接的请求立即报错，不排队拖垮整个进程
                    "pool_timeout": 5,
                }
            )
        else:
            kwargs.update({"pool_size": 10, "max_overflow": 20, "pool_timeout": 5})
        _engine = create_async_engine(url, **kwargs)
        # expire_on_commit=False：commit 后仍可读取 ORM 属性，避免响应组装时触发额外查询。
        _session_factory = async_sessionmaker(_engine, expire_on_commit=False)
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    get_engine()
    assert _session_factory is not None
    return _session_factory


async def get_session() -> AsyncIterator[AsyncSession]:
    async with get_session_factory()() as session:
        yield session


async def init_models() -> None:
    engine = get_engine()
    # create_all 只补建缺失的表，不会修改已有表结构，适用于本地与测试环境。
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def dispose_engine() -> None:
    global _engine
    if _engine is not None:
        # 进程退出或测试收尾时释放连接池，避免残留连接占用数据库。
        await _engine.dispose()
        _engine = None
