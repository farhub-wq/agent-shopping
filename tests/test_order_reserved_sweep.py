import uuid
from datetime import UTC, datetime, timedelta

import order.app as order_app
from order.app import _expire_unpaid_once
from order.models import Order
from shop_common.db import get_session_factory, init_models


async def _create_reserved_order(minutes_ago: int) -> str:
    await init_models()
    order_id = str(uuid.uuid4())
    async with get_session_factory()() as session:
        session.add(
            Order(
                id=order_id,
                order_no=f"S{uuid.uuid4().hex[:12]}",
                user_id="u1",
                status="RESERVED",
                amount_cents=100,
                created_at=datetime.now(UTC) - timedelta(minutes=minutes_ago),
                items=[],
            )
        )
        await session.commit()
    return order_id


async def test_reserved_stuck_over_5_minutes_is_cancelled(monkeypatch):
    # RESERVED 超过 5 分钟未推进到 PAYING（如预占后进程崩溃）：按超时取消，先关支付单再释放库存
    calls: list[dict] = []

    async def spy_request(method, path, *, json=None, headers=None):
        calls.append({"method": method, "path": path})
        return {}

    monkeypatch.setattr(order_app.payment_svc, "request", spy_request)
    monkeypatch.setattr(order_app.inventory, "request", spy_request)
    order_id = await _create_reserved_order(minutes_ago=6)

    await _expire_unpaid_once()

    async with get_session_factory()() as session:
        order = await session.get(Order, order_id)
        assert order is not None
        assert order.status == "CANCELLED"
        assert order.cancel_reason == "payment_timeout"

    assert calls == [
        {"method": "POST", "path": f"/payments/{order_id}/expire"},
        {"method": "POST", "path": "/inventory/release"},
    ]


async def test_reserved_fresh_order_is_untouched(monkeypatch):
    calls: list[dict] = []

    async def spy_request(method, path, *, json=None, headers=None):
        calls.append({"method": method, "path": path})
        return {}

    monkeypatch.setattr(order_app.payment_svc, "request", spy_request)
    monkeypatch.setattr(order_app.inventory, "request", spy_request)
    order_id = await _create_reserved_order(minutes_ago=1)

    await _expire_unpaid_once()

    async with get_session_factory()() as session:
        order = await session.get(Order, order_id)
        assert order is not None
        assert order.status == "RESERVED"
    assert calls == []
