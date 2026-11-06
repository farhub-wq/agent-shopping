import uuid

import inventory.app as inventory_app
import pytest
from inventory.app import InboundRequest, StocktakeRequest, inbound, stocktake
from inventory.models import Stock, StockAdjustment
from shop_common.auth import Principal
from shop_common.db import get_session_factory, init_models
from shop_common.errors import ApiError
from sqlalchemy import select


class _ScriptStub:
    """替代注册后的 Lua 脚本：记录调用，验证热点库存同步参数。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def __call__(self, *, keys, args):
        self.calls.append({"keys": keys, "args": args})
        return 1


async def _seed_stock(sku: str, total: int, reserved: int = 0) -> None:
    await init_models()
    async with get_session_factory()() as session:
        session.add(Stock(sku=sku, total=total, reserved=reserved))
        await session.commit()


@pytest.mark.asyncio
async def test_inbound_is_idempotent_by_key(monkeypatch):
    stub = _ScriptStub()
    monkeypatch.setattr(inventory_app, "_sync_script", stub)
    sku = f"SKU-{uuid.uuid4().hex[:8]}"
    await _seed_stock(sku, 10)
    principal = Principal("op", "operator", "default")
    key = str(uuid.uuid4())

    first = await inbound(InboundRequest(sku=sku, qty=5), principal, None, idempotency_key=key)
    assert first["delta"] == 5
    assert "idempotent" not in first

    # 同一幂等键重放：不再入库，返回既有调整
    dup = await inbound(InboundRequest(sku=sku, qty=5), principal, None, idempotency_key=key)
    assert dup["idempotent"] is True
    assert dup["delta"] == 5

    async with get_session_factory()() as session:
        stock = await session.get(Stock, sku)
        assert stock is not None
        assert stock.total == 15
        rows = (
            await session.execute(
                select(StockAdjustment).where(StockAdjustment.sku == sku)
            )
        ).scalars().all()
        assert len(rows) == 1


@pytest.mark.asyncio
async def test_inbound_requires_idempotency_key():
    principal = Principal("op", "operator", "default")
    with pytest.raises(ApiError):
        await inbound(InboundRequest(sku="SKU-X", qty=1), principal, None, idempotency_key=None)


@pytest.mark.asyncio
async def test_stocktake_sets_absolute_and_records_delta(monkeypatch):
    stub = _ScriptStub()
    monkeypatch.setattr(inventory_app, "_sync_script", stub)
    sku = f"SKU-{uuid.uuid4().hex[:8]}"
    await _seed_stock(sku, 10)
    principal = Principal("op", "operator", "default")

    res = await stocktake(StocktakeRequest(sku=sku, total=3), principal, None)
    assert res["total"] == 3
    assert res["delta"] == -7

    async with get_session_factory()() as session:
        stock = await session.get(Stock, sku)
        assert stock is not None
        assert stock.total == 3
        rows = (
            await session.execute(
                select(StockAdjustment).where(StockAdjustment.sku == sku)
            )
        ).scalars().all()
        assert len(rows) == 1
        assert rows[0].delta == -7
    # 同步热点缓存为 总量-预占
    assert stub.calls[-1]["args"] == [3]


@pytest.mark.asyncio
async def test_stocktake_rejects_below_reserved(monkeypatch):
    stub = _ScriptStub()
    monkeypatch.setattr(inventory_app, "_sync_script", stub)
    sku = f"SKU-{uuid.uuid4().hex[:8]}"
    await _seed_stock(sku, 10, reserved=4)
    principal = Principal("op", "operator", "default")

    with pytest.raises(ApiError):
        await stocktake(StocktakeRequest(sku=sku, total=3), principal, None)
    assert stub.calls == []
