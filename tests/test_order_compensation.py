import uuid

import order.app as order_app
import pytest
from order.app import handle_event
from order.models import Order, OrderEvent
from shop_common.db import get_session_factory, init_models
from shop_common.errors import ApiError
from sqlalchemy import select


def _items(shipped_a: int = 0) -> list[dict]:
    return [
        {
            "item_id": "item-a",
            "sku": "SKU-A",
            "title": "A",
            "qty": 2,
            "price_cents": 300,
            "paid_amount_cents": 600,
            "shipped_qty": shipped_a,
            "refunded_qty": 0,
            "refunded_amount_cents": 0,
        }
    ]


async def _create_order(**overrides) -> str:
    await init_models()
    order_id = str(uuid.uuid4())
    async with get_session_factory()() as session:
        session.add(
            Order(
                id=order_id,
                order_no=f"T{uuid.uuid4().hex[:12]}",
                user_id="u1",
                status=overrides.get("status", "RESERVED"),
                amount_cents=600,
                cancel_reason=overrides.get("cancel_reason"),
                payment_status=overrides.get("payment_status", "unpaid"),
                inventory_committed=overrides.get("inventory_committed", False),
                items=_items(overrides.get("shipped_a", 0)),
            )
        )
        await session.commit()
    return order_id


def _event(event_id: str, topic: str, payload: dict) -> dict:
    return {"id": event_id, "type": topic, "payload": payload}


@pytest.mark.asyncio
async def test_late_payment_auto_refunds_full_amount(monkeypatch):
    # 订单已超时关单，支付回调迟到：落 paid 资金事实并自动全额退款
    calls: list[dict] = []

    async def fake_request(method, path, *, json=None, headers=None):
        calls.append({"method": method, "path": path, "json": json})
        return {}

    monkeypatch.setattr(order_app.payment_svc, "request", fake_request)
    order_id = await _create_order(
        status="CANCELLED", cancel_reason="payment_timeout", payment_status="expired"
    )
    await handle_event(
        _event("evt-late-1", "payment.succeeded", {"order_id": order_id, "amount_cents": 600})
    )

    async with get_session_factory()() as session:
        order = await session.get(Order, order_id)
        assert order is not None
        # 迟到支付不改变终态，只落资金事实
        assert order.status == "CANCELLED"
        assert order.payment_status == "paid"
        events = (
            (await session.execute(select(OrderEvent).where(OrderEvent.order_id == order_id)))
            .scalars()
            .all()
        )
        assert any(row.event == "late_payment" for row in events)

    expected = {
        "method": "POST",
        "path": "/payments/refunds",
        "json": {
            "order_id": order_id,
            "ticket_id": f"latepay-{order_id}",
            "amount_cents": 600,
        },
    }
    assert calls == [expected]

    # 第二个支付成功事件到达时 payment_status 已是 paid，不会重复发起退款
    await handle_event(
        _event("evt-late-2", "payment.succeeded", {"order_id": order_id, "amount_cents": 600})
    )
    assert calls == [expected]


@pytest.mark.asyncio
async def test_inventory_commit_failure_leaves_flag_for_sweeper(monkeypatch):
    # 库存服务不可用：订单照常 PAID，但标志保持 False 等补偿扫单重试
    async def failing_request(method, path, *, json=None, headers=None):
        raise ApiError("DEPENDENCY_FAILED", "inventory down")

    monkeypatch.setattr(order_app.inventory, "request", failing_request)
    order_id = await _create_order()
    await handle_event(
        _event("evt-pay-1", "payment.succeeded", {"order_id": order_id, "amount_cents": 600})
    )
    async with get_session_factory()() as session:
        order = await session.get(Order, order_id)
        assert order is not None
        assert order.status == "PAID"
        assert order.inventory_committed is False


@pytest.mark.asyncio
async def test_inventory_commit_success_sets_flag(monkeypatch):
    async def ok_request(method, path, *, json=None, headers=None):
        return {}

    monkeypatch.setattr(order_app.inventory, "request", ok_request)
    order_id = await _create_order()
    await handle_event(
        _event("evt-pay-2", "payment.succeeded", {"order_id": order_id, "amount_cents": 600})
    )
    async with get_session_factory()() as session:
        order = await session.get(Order, order_id)
        assert order is not None
        assert order.status == "PAID"
        assert order.inventory_committed is True


@pytest.mark.asyncio
async def test_refund_restocks_unshipped_quantity(monkeypatch):
    calls: list[dict] = []

    async def spy_request(method, path, *, json=None, headers=None):
        calls.append({"method": method, "path": path, "json": json})
        return {}

    monkeypatch.setattr(order_app.inventory, "request", spy_request)
    # 已发 1 件：退款 2 件时只有未发货的 1 件允许回补
    order_id = await _create_order(status="PAID", inventory_committed=True, shipped_a=1)
    await handle_event(
        _event(
            "evt-ref-1",
            "payment.refunded",
            {
                "order_id": order_id,
                "refund_id": "r1",
                "order_item_id": "item-a",
                "quantity": 2,
                "amount_cents": 600,
                "fully_refunded": False,
            },
        )
    )
    assert calls == [
        {
            "method": "POST",
            "path": "/inventory/restock",
            "json": {"order_id": order_id, "items": [{"sku": "SKU-A", "qty": 1}]},
        }
    ]


@pytest.mark.asyncio
async def test_refund_on_cancelled_order_skips_restock(monkeypatch):
    # CANCELLED 订单的库存已由预占释放回补，退款事件不得二次回补
    calls: list[dict] = []

    async def spy_request(method, path, *, json=None, headers=None):
        calls.append({"method": method, "path": path, "json": json})
        return {}

    monkeypatch.setattr(order_app.inventory, "request", spy_request)
    order_id = await _create_order(
        status="CANCELLED",
        cancel_reason="payment_timeout",
        payment_status="paid",
        inventory_committed=True,
    )
    await handle_event(
        _event(
            "evt-ref-2",
            "payment.refunded",
            {
                "order_id": order_id,
                "refund_id": "r2",
                "order_item_id": None,
                "quantity": 0,
                "amount_cents": 600,
                "fully_refunded": True,
            },
        )
    )
    assert calls == []
    async with get_session_factory()() as session:
        order = await session.get(Order, order_id)
        assert order is not None
        assert order.status == "CANCELLED"
        assert order.payment_status == "refunded"
