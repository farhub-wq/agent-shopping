"""audit 服务：消费各业务服务的 audit.* 事件，落库并提供 admin 查询。

审计是纯消费服务：不向外发事件，只落 audit_log 与事件去重/死信共享表；
查询只对 admin 开放，运营也不能越级读取。
"""

import asyncio
import contextlib
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI
from redis.asyncio import Redis
from shop_common.auth import PrincipalDep
from shop_common.config import get_settings, validate_production_settings
from shop_common.db import dispose_engine, get_session_factory, init_models
from shop_common.errors import install_error_handlers
from shop_common.events import EventBus, create_redis
from shop_common.logging import configure_logging, get_logger
from shop_common.metrics import install_metrics
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from .models import AuditLog

settings = get_settings()
logger = get_logger(__name__)

redis: Redis | None = None
bus: EventBus | None = None


def _parse_occurred_at(value: Any) -> datetime:
    # 事件时间由生产者写入 payload；解析失败退化为消费时间，保证落库不中断
    if not value:
        return datetime.now(UTC)
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return datetime.now(UTC)


async def handle_audit_event(event: dict[str, Any]) -> None:
    """消费 audit.* 事件：只处理审计主题，非审计事件直接忽略。"""
    event_type = event.get("type") or ""
    if not event_type.startswith("audit."):
        return
    payload = event.get("payload") or {}
    event_id = str(event.get("id") or "")
    if not event_id:
        return
    async with get_session_factory()() as session:
        session.add(
            AuditLog(
                id=event_id,
                occurred_at=_parse_occurred_at(payload.get("occurred_at")),
                service=str(payload.get("service") or "")[:32],
                actor_id=str(payload.get("actor_id") or ""),
                actor_role=str(payload.get("actor_role") or ""),
                tenant_id=str(event.get("tenant") or "default"),
                action=str(payload.get("action") or ""),
                target_type=str(payload.get("target_type") or ""),
                target_id=str(payload.get("target_id") or ""),
                request_id=str(payload.get("request_id") or ""),
                before=payload.get("before"),
                after=payload.get("after"),
                ip_hash=str(payload.get("ip_hash") or ""),
            )
        )
        try:
            await session.commit()
        except IntegrityError:
            # 重复投递撞主键：回滚后正常返回，EventBus 侧 ProcessedEvent 已保证幂等
            await session.rollback()


@asynccontextmanager
async def lifespan(_: FastAPI):
    global redis, bus
    configure_logging("audit")
    # 生产密钥仍是公开默认值时拒绝启动：否则 JWT 与内网签名身份头都可被任意伪造。
    validate_production_settings(settings)
    await init_models()
    tasks: list[asyncio.Task] = []
    if settings.background_tasks:
        # 单测关闭后台任务，避免测试进程连接 Redis 常驻消费
        redis = create_redis(settings.redis_url)
        bus = EventBus(redis, namespace="shop", session_factory=get_session_factory())
        tasks = [asyncio.create_task(bus.run_consumer("audit", "audit-svc", "audit-1", handle_audit_event))]
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if redis is not None:
            await redis.close()
        await dispose_engine()


app = FastAPI(title="audit-service", version="0.1.0", lifespan=lifespan)
install_error_handlers(app, "audit")
install_metrics(app, "audit")


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok", "service": "audit"}


def _to_dict(row: AuditLog) -> dict[str, Any]:
    # 显式挑字段组装响应，避免整对象序列化泄漏模型内部状态
    return {
        "id": row.id,
        "occurred_at": row.occurred_at.isoformat(),
        "service": row.service,
        "actor_id": row.actor_id,
        "actor_role": row.actor_role,
        "tenant_id": row.tenant_id,
        "action": row.action,
        "target_type": row.target_type,
        "target_id": row.target_id,
        "request_id": row.request_id,
        "before": row.before,
        "after": row.after,
        "ip_hash": row.ip_hash,
    }


@app.get("/audit/logs")
async def list_logs(
    principal: PrincipalDep,
    action: str | None = None,
    target_type: str | None = None,
    target_id: str | None = None,
    actor_id: str | None = None,
    page: int = 1,
    size: int = 20,
) -> dict[str, Any]:
    """审计查询：只对 admin 开放，支持按动作/对象/操作人筛选并倒序分页。"""
    principal.require_admin()
    page = max(page, 1)
    size = min(max(size, 1), 100)
    stmt = select(AuditLog)
    if action:
        stmt = stmt.where(AuditLog.action == action)
    if target_type:
        stmt = stmt.where(AuditLog.target_type == target_type)
    if target_id:
        stmt = stmt.where(AuditLog.target_id == target_id)
    if actor_id:
        stmt = stmt.where(AuditLog.actor_id == actor_id)
    async with get_session_factory()() as session:
        total = await session.scalar(select(func.count()).select_from(stmt.subquery()))
        rows = (
            (
                await session.execute(
                    stmt.order_by(AuditLog.occurred_at.desc())
                    .offset((page - 1) * size)
                    .limit(size)
                )
            )
            .scalars()
            .all()
        )
    return {
        "items": [_to_dict(row) for row in rows],
        "page": page,
        "size": size,
        "total": int(total or 0),
    }
