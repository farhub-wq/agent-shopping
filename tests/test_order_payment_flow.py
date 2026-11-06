import uuid

import pytest
from order.app import handle_event
from order.models import Order
from order.state_machine import resolve_target
from shop_common.db import get_session_factory, init_models


def test_partial_refund_returns_to_previous_state():
    # 部分退款完成必须回到退款前状态，只有全额退款才进入 REFUNDED
    assert resolve_target("REFUNDING", "refund_partial_complete", refund_previous="PAID") == "PAID"
    assert resolve_target("REFUNDING", "refund_partial_complete", refund_previous="SHIPPED") == "SHIPPED"
    assert resolve_target("REFUNDING", "refund_complete") == "REFUNDED"


async def _create_order(status: str = "RESERVED") -> str:
    await init_models()
    order_id = str(uuid.uuid4())
    async with get_session_factory()() as session:
        session.add(
            Order(
                id=order_id,
                order_no=f"T{uuid.uuid4().hex[:12]}",
                user_id="u1",
                status=status,
                amount_cents=1000,
                items=[
                    {
                        "item_id": "item-a",
                        "sku": "SKU-A",
                        "title": "A",
                        "qty": 2,
                        "price_cents": 300,
                        "paid_amount_cents": 600,
                        "shipped_qty": 0,
                        "refunded_qty": 0,
                        "refunded_amount_cents": 0,
                    },
                    {
                        "item_id": "item-b",
                        "sku": "SKU-B",
                        "title": "B",
                        "qty": 1,
                        "price_cents": 400,
                        "paid_amount_cents": 400,
                        "shipped_qty": 0,
                        "refunded_qty": 0,
                        "refunded_amount_cents": 0,
                    },
                ],
            )
        )
        await session.commit()
    return order_id


@pytest.mark.asyncio
async def test_payment_succeeded_moves_order_to_paid():
    order_id = await _create_order()
    await handle_event(
        {
            "id": "evt-paid-1",
            "type": "payment.succeeded",
            "payload": {"order_id": order_id, "amount_cents": 1000},
        }
    )
    async with get_session_factory()() as session:
        order = await session.get(Order, order_id)
        assert order is not None
        assert order.status == "PAID"
        assert order.paid_at is not None


@pytest.mark.asyncio
async def test_partial_shipment_enters_shipping_then_shipped():
    order_id = await _create_order()
    await handle_event(
        {
            "id": "evt-paid-2",
            "type": "payment.succeeded",
            "payload": {"order_id": order_id, "amount_cents": 1000},
        }
    )
    await handle_event(
        {
            "id": "evt-ship-1",
            "type": "fulfillment.shipped",
            "payload": {
                "order_id": order_id,
                "items": [{"item_id": "item-a", "qty": 1}],
                "all_shipped": False,
            },
        }
    )
    async with get_session_factory()() as session:
        order = await session.get(Order, order_id)
        assert order is not None
        # 首次部分发货：PAID 进入 SHIPPING
        assert order.status == "SHIPPING"
        assert order.items[0]["shipped_qty"] == 1

    await handle_event(
        {
            "id": "evt-ship-2",
            "type": "fulfillment.shipped",
            "payload": {
                "order_id": order_id,
                "items": [
                    {"item_id": "item-a", "qty": 1},
                    {"item_id": "item-b", "qty": 1},
                ],
                "all_shipped": True,
            },
        }
    )
    async with get_session_factory()() as session:
        order = await session.get(Order, order_id)
        assert order is not None
        # 全部发完：SHIPPING 收敛到 SHIPPED
        assert order.status == "SHIPPED"
        assert order.items[0]["shipped_qty"] == 2


@pytest.mark.asyncio
async def test_partial_refund_then_full_refund_is_idempotent():
    order_id = await _create_order("PAID")
    partial = {
        "id": "evt-refund-1",
        "type": "payment.refunded",
        "payload": {
            "order_id": order_id,
            "refund_id": "r1",
            "order_item_id": "item-a",
            "quantity": 1,
            "amount_cents": 300,
            "fully_refunded": False,
        },
    }
    await handle_event(partial)
    # 重复投递同一事件不能二次累加
    await handle_event(partial)

    async with get_session_factory()() as session:
        order = await session.get(Order, order_id)
        assert order is not None
        assert order.status == "PAID"
        assert order.refund_amount_cents == 300
        assert order.items[0]["refunded_qty"] == 1
        assert order.items[0]["refunded_amount_cents"] == 300

    await handle_event(
        {
            "id": "evt-refund-2",
            "type": "payment.refunded",
            "payload": {
                "order_id": order_id,
                "refund_id": "r2",
                "order_item_id": None,
                "quantity": 0,
                "amount_cents": 700,
                "fully_refunded": True,
            },
        }
    )
    async with get_session_factory()() as session:
        order = await session.get(Order, order_id)
        assert order is not None
        assert order.status == "REFUNDED"
        assert order.refund_amount_cents == 1000
