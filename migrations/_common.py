"""Alembic 共享运行逻辑：按 SERVICE_NAME 定位服务模型与数据库。

每个有库服务一个独立迁移目录（独立版本序列），但运行逻辑完全一致，
收敛成一份，避免 7 份 env.py 复制粘贴漂移。
"""

import asyncio
import importlib

from alembic import context
from shop_common.config import get_settings
from shop_common.db import Base, UTCDateTime
from sqlalchemy.ext.asyncio import create_async_engine

settings = get_settings()
target_metadata = Base.metadata

# 有数据库的服务白名单；gateway/harness 无关系库，不应走到迁移
DB_SERVICES = {"catalog", "inventory", "order", "fulfillment", "aftersales", "identity", "payment"}


def _load_service_models() -> None:
    svc = settings.service_name
    if svc not in DB_SERVICES:
        raise RuntimeError(f"服务 {svc} 无迁移目录，不应执行 Alembic 迁移")
    # 必须 import 服务 app（而非仅 models）：共享表（outbox_events / processed_events /
    # dead_events / task_runs）是否落在本服务库，取决于 app 的 import 链，
    # 只有 import app 才能精确还原该服务库的实际表集合。
    importlib.import_module(f"{svc}.app")


_load_service_models()


def _render_item(type_, obj, autogen_context):
    # alembic 1.20 签名：type_ 是种类字符串（"type"/"column"/"primary_key"…），
    # 实际对象在第二个参数 obj。只有渲染列类型且是 UTCDateTime 时才接管，
    # 保证迁移 DDL 与 create_all 一致（MySQL 侧为 DATETIME(6)）。
    if type_ == "type" and isinstance(obj, UTCDateTime):
        autogen_context.imports.add("from shop_common.db import UTCDateTime")
        return "UTCDateTime()"
    # 返回 False 才表示交给默认渲染；返回 None 会被当作「渲染结果为空」
    return False


def run_migrations_offline() -> None:
    context.configure(
        url=settings.resolved_database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_item=_render_item,
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, render_item=_render_item)
    with context.begin_transaction():
        context.run_migrations()


async def _run_async() -> None:
    engine = create_async_engine(settings.resolved_database_url)
    async with engine.connect() as connection:
        await connection.run_sync(_do_run_migrations)
    await engine.dispose()


def run_migrations_online() -> None:
    asyncio.run(_run_async())


def run_migrations() -> None:
    if context.is_offline_mode():
        run_migrations_offline()
    else:
        run_migrations_online()
