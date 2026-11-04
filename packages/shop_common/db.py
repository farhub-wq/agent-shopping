from collections.abc import AsyncIterator
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from .config import get_settings

settings = get_settings()


class Base(DeclarativeBase):
    pass


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
        else:
            kwargs.update({"pool_size": 10, "max_overflow": 20})
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
