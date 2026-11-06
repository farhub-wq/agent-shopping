import uuid
from datetime import UTC, datetime

import pytest
from audit import app as audit_app
from audit.models import AuditLog
from fastapi.testclient import TestClient
from shop_common.auth import create_token
from shop_common.config import get_settings
from shop_common.db import get_session_factory, init_models
from sqlalchemy import select

settings = get_settings()


def _admin_headers() -> dict[str, str]:
    token = create_token(user_id="admin-1", role="admin")
    return {"Authorization": f"Bearer {token}"}


def _customer_headers() -> dict[str, str]:
    token = create_token(user_id="buyer-1", role="customer")
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="module")
def client():
    # 单测关闭后台消费，避免测试进程连接 Redis 常驻
    settings.background_tasks = False
    with TestClient(audit_app.app) as test_client:
        yield test_client


def _event(event_type: str, **payload: object) -> dict:
    return {
        "id": str(uuid.uuid4()),
        "type": event_type,
        "tenant": "default",
        "payload": {
            "service": "order",
            "actor_id": "op-1",
            "actor_role": "operator",
            "action": "order.ship",
            "target_type": "order",
            "target_id": "o-1",
            "request_id": "r-1",
            "ip_hash": "abc",
            "occurred_at": datetime.now(UTC).isoformat(),
            **payload,
        },
    }


@pytest.mark.asyncio
async def test_audit_event_landing_is_idempotent():
    # 同一事件投递两次：第二次撞主键，应只落一条
    await init_models()
    event = _event("audit.order.ship", before={"status": "PAID"}, after={"status": "SHIPPED"})
    await audit_app.handle_audit_event(event)
    await audit_app.handle_audit_event(event)
    async with get_session_factory()() as session:
        rows = (
            (await session.execute(select(AuditLog).where(AuditLog.id == event["id"])))
            .scalars()
            .all()
        )
        assert len(rows) == 1
        row = rows[0]
        assert row.action == "order.ship"
        assert row.service == "order"
        assert row.target_id == "o-1"
        assert row.after == {"status": "SHIPPED"}


@pytest.mark.asyncio
async def test_audit_ignores_non_audit_events():
    event = _event("payment.succeeded")
    await audit_app.handle_audit_event(event)
    async with get_session_factory()() as session:
        assert await session.get(AuditLog, event["id"]) is None


def test_query_requires_admin(client):
    # 顾客与运营都不可读审计；admin 可查（空列表也返回 200）
    assert client.get("/audit/logs", headers=_customer_headers()).status_code == 403
    assert client.get("/audit/logs", headers=_admin_headers()).status_code == 200
