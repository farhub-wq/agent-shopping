import uuid

import inventory.app as inventory_app
import pytest
from inventory.app import RestockRequest, restock
from inventory.models import Restock, Stock
from shop_common.auth import Principal
from shop_common.db import get_session_factory, init_models
from shop_common.errors import ApiError
from sqlalchemy import select


class _ScriptStub:
    """替代注册后的 Lua 脚本：记录调用，验证热点库存只在有增量时同步。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def __call__(self, *, keys, args):
        self.calls.append({"keys": keys, "args": args})
        return 1


async def _seed_stock(sku: str, total: int) -> None:
    await init_models()
    async with get_session_factory()() as session:
        session.add(Stock(sku=sku, total=total, reserved=0))
        await session.commit()


@pytest.mark.asyncio
async def test_restock_is_idempotent_by_target(monkeypatch):
    stub = _ScriptStub()
    monkeypatch.setattr(inventory_app, "_restock_script", stub)
    sku = f"SKU-{uuid.uuid4().hex[:8]}"
    await _seed_stock(sku, 10)
    order_id = str(uuid.uuid4())
    principal = Principal("op", "operator", "default")

    first = await restock(
        RestockRequest(order_id=order_id, items=[{"sku": sku, "qty": 3}]), principal
    )
    assert first["restocked"] == [{"sku": sku, "qty": 3}]
    # 同目标重放：零增量，不重复回补
    second = await restock(
        RestockRequest(order_id=order_id, items=[{"sku": sku, "qty": 3}]), principal
    )
    assert second["restocked"] == []
    # 目标上调：只回补差值
    third = await restock(
        RestockRequest(order_id=order_id, items=[{"sku": sku, "qty": 5}]), principal
    )
    assert third["restocked"] == [{"sku": sku, "qty": 2}]

    async with get_session_factory()() as session:
        stock = await session.get(Stock, sku)
        assert stock is not None
        assert stock.total == 15
        rec = await session.scalar(select(Restock).where(Restock.order_id == order_id))
        assert rec is not None
        assert rec.qty == 5
    assert len(stub.calls) == 2


@pytest.mark.asyncio
async def test_restock_rejects_shrinking_target(monkeypatch):
    stub = _ScriptStub()
    monkeypatch.setattr(inventory_app, "_restock_script", stub)
    sku = f"SKU-{uuid.uuid4().hex[:8]}"
    await _seed_stock(sku, 10)
    order_id = str(uuid.uuid4())
    principal = Principal("op", "operator", "default")

    await restock(RestockRequest(order_id=order_id, items=[{"sku": sku, "qty": 4}]), principal)
    # 目标变小说明上游计算出错，必须拒绝而不是悄悄冲减库存
    with pytest.raises(ApiError):
        await restock(RestockRequest(order_id=order_id, items=[{"sku": sku, "qty": 2}]), principal)
    assert len(stub.calls) == 1
