"""审计事件入口：业务服务在写操作事务内调用，与业务数据同事务写入 outbox。

审计是旁路能力：audit 服务异步消费落库，只增不改、保留 180 天；
写操作回滚则审计事件一并消失，保证「有审计必有对应业务变更」。
"""

import hashlib
from datetime import UTC, datetime
from typing import Any

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from .config import get_settings
from .events import EventBus
from .outbox import enqueue


def hash_ip(ip: str) -> str:
    """客户端 IP 只存 SHA-256 哈希：审计不落明文 IP，避免 PII 扩散。"""
    return hashlib.sha256(ip.encode()).hexdigest()


def _payload(
    *,
    actor_id: str,
    actor_role: str,
    action: str,
    target_type: str,
    target_id: str,
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
    request_id: str,
    ip: str,
    request: Request | None,
) -> dict[str, Any]:
    if request is not None:
        # 显式传值优先；缺省从请求里取贯穿链路的请求号与客户端 IP
        request_id = request_id or request.headers.get("x-request-id", "")
        ip = ip or (request.client.host if request.client else "")
    return {
        "service": get_settings().service_name,
        "actor_id": actor_id,
        "actor_role": actor_role,
        "action": action,
        "target_type": target_type,
        "target_id": target_id,
        "before": before,
        "after": after,
        "request_id": request_id,
        "ip_hash": hash_ip(ip) if ip else "",
        "occurred_at": datetime.now(UTC).isoformat(),
    }


def enqueue_audit(
    session: AsyncSession,
    *,
    actor_id: str,
    actor_role: str,
    action: str,
    target_type: str,
    target_id: str,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    request_id: str = "",
    ip: str = "",
    request: Request | None = None,
    tenant: str = "default",
) -> None:
    """在业务写事务内入队一条 `audit.{action}` 事件；调用方负责 commit。"""
    enqueue(
        session,
        "audit",
        f"audit.{action}",
        _payload(
            actor_id=actor_id,
            actor_role=actor_role,
            action=action,
            target_type=target_type,
            target_id=target_id,
            before=before,
            after=after,
            request_id=request_id,
            ip=ip,
            request=request,
        ),
        tenant=tenant,
    )


async def publish_audit(
    bus: EventBus,
    *,
    actor_id: str,
    actor_role: str,
    action: str,
    target_type: str,
    target_id: str,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    request_id: str = "",
    ip: str = "",
    request: Request | None = None,
    tenant: str = "default",
) -> None:
    """无关系库的服务（harness）直接投递审计事件，不经 outbox 落库。"""
    await bus.publish(
        "audit",
        f"audit.{action}",
        _payload(
            actor_id=actor_id,
            actor_role=actor_role,
            action=action,
            target_type=target_type,
            target_id=target_id,
            before=before,
            after=after,
            request_id=request_id,
            ip=ip,
            request=request,
        ),
        tenant=tenant,
    )
