import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fulfillment.app import ShipRequest, ship
from fulfillment.models import Shipment, ShipmentItem
from shop_common.auth import Principal
from shop_common.db import get_session_factory, init_models
from shop_common.errors import ApiError
from shop_common.outbox import OutboxEvent
from sqlalchemy import select


async def _pending_shipment(qty: int = 2) -> str:
    await init_models()
    shipment_id = str(uuid.uuid4())
    async with get_session_factory()() as session:
        session.add(
            Shipment(
                id=shipment_id,
                order_id=str(uuid.uuid4()),
                deadline_at=datetime.now(UTC) + timedelta(hours=24),
            )
        )
        session.add(
            ShipmentItem(
                shipment_id=shipment_id,
                order_item_id="item-a",
                sku="SKU-A",
                qty=qty,
            )
        )
        await session.commit()
    return shipment_id


@pytest.mark.asyncio
async def test_partial_ship_splits_remaining_quantity():
    shipment_id = await _pending_shipment(2)
    result = await ship(
        shipment_id,
        ShipRequest(carrier="SF", items=[{"order_item_id": "item-a", "qty": 1}]),
        Principal("op", "operator", "default"),
        None,
    )
    assert result["status"] == "shipped"
    assert [row["qty"] for row in result["items"]] == [1]

    async with get_session_factory()() as session:
        # 剩余 1 件自动拆到新的 pending 履约单，避免一张单同时出现已发/未发
        children = (
            (await session.execute(select(Shipment).where(Shipment.status == "pending")))
            .scalars()
            .all()
        )
        assert any(row.order_id == result["order_id"] for row in children)
        events = (await session.execute(select(OutboxEvent))).scalars().all()
        assert any(event.event_type == "fulfillment.shipped" for event in events)


@pytest.mark.asyncio
async def test_ship_rejects_quantity_over_shipment():
    shipment_id = await _pending_shipment(1)
    with pytest.raises(ApiError):
        await ship(
            shipment_id,
            ShipRequest(carrier="SF", items=[{"order_item_id": "item-a", "qty": 2}]),
            Principal("op", "operator", "default"),
            None,
        )
