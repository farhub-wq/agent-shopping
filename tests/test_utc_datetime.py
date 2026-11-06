from datetime import UTC, datetime

from shop_common.db import UTCDateTime
from sqlalchemy import String
from sqlalchemy.dialects import mysql
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class _Base(DeclarativeBase):
    pass


class _Row(_Base):
    __tablename__ = "utc_rows"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    at: Mapped[datetime] = mapped_column(UTCDateTime())


async def test_utc_datetime_roundtrip_sqlite_keeps_aware_utc():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(_Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    aware = datetime(2026, 1, 2, 3, 4, 5, 123456, tzinfo=UTC)

    async with factory() as session:
        session.add(_Row(id="a", at=aware))
        await session.commit()
    async with factory() as session:
        stored = await session.get(_Row, "a")
        assert stored is not None
        # 读回必须带 UTC 时区，且微秒精度不能丢
        assert stored.at == aware
        assert stored.at.tzinfo is UTC

    await engine.dispose()


async def test_utc_datetime_treats_naive_input_as_utc():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(_Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    naive = datetime(2026, 1, 2, 3, 4, 5, 123456)

    async with factory() as session:
        session.add(_Row(id="b", at=naive))
        await session.commit()
    async with factory() as session:
        stored = await session.get(_Row, "b")
        assert stored is not None
        assert stored.at == naive.replace(tzinfo=UTC)

    await engine.dispose()


def test_utc_datetime_uses_microsecond_precision_on_mysql():
    impl = UTCDateTime().dialect_impl(mysql.dialect())
    # MySQL 默认 DATETIME 只有秒级精度，这里必须是 DATETIME(6)
    assert impl.fsp == 6
