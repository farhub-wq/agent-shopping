"""顾客侧只读查询工具：本人订单、售后进度与运单。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from harness.core import tools_impl
from harness.core.tools import ToolContext, registry


def _ctx() -> ToolContext:
    return ToolContext(user_id="u1", role="customer", tenant="t1", session_id="s1", run_id="r1")


@pytest.mark.asyncio
async def test_after_sales_status_lists_own_tickets(monkeypatch):
    service = SimpleNamespace(request=AsyncMock(return_value={"items": []}))
    monkeypatch.setattr(tools_impl, "aftersales", service)

    result = await registry.execute("after_sales_status", {}, _ctx(), trust="vendor")

    assert result.ok
    assert service.request.await_args.args == ("GET", "/aftersales/tickets")
    # 身份头必须透传，下游据此只返回当前用户的数据。
    assert service.request.await_args.kwargs["headers"]


@pytest.mark.asyncio
async def test_after_sales_status_supports_ticket_and_status_filter(monkeypatch):
    service = SimpleNamespace(request=AsyncMock(return_value={"id": "T1"}))
    monkeypatch.setattr(tools_impl, "aftersales", service)

    await registry.execute("after_sales_status", {"ticket_id": "T1"}, _ctx(), trust="vendor")
    assert service.request.await_args.args == ("GET", "/aftersales/tickets/T1")

    await registry.execute("after_sales_status", {"status": "pending"}, _ctx(), trust="vendor")
    assert service.request.await_args.args == ("GET", "/aftersales/tickets?status=pending")


@pytest.mark.asyncio
async def test_shipment_track_queries_by_order(monkeypatch):
    service = SimpleNamespace(request=AsyncMock(return_value={"tracking_no": "SF1"}))
    monkeypatch.setattr(tools_impl, "fulfillment", service)

    result = await registry.execute("shipment_track", {"order_id": "SO-1"}, _ctx(), trust="vendor")

    assert result.ok
    assert service.request.await_args.args == ("GET", "/shipments/by-order/SO-1")
    assert service.request.await_args.kwargs["headers"]


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_order_id", ["SO-1/../x", r"SO-1\ship", "SO-1?x=1", ".."])
async def test_shipment_track_rejects_structure_segments(monkeypatch, bad_order_id):
    service = SimpleNamespace(request=AsyncMock())
    monkeypatch.setattr(tools_impl, "fulfillment", service)

    result = await registry.execute(
        "shipment_track", {"order_id": bad_order_id}, _ctx(), trust="vendor"
    )

    assert result.status == "error"
    service.request.assert_not_awaited()


@pytest.mark.asyncio
async def test_shipment_track_requires_order_id(monkeypatch):
    service = SimpleNamespace(request=AsyncMock())
    monkeypatch.setattr(tools_impl, "fulfillment", service)

    result = await registry.execute("shipment_track", {}, _ctx(), trust="vendor")

    # handler 用业务错误体回填模型，不抛异常；关键是不能触达下游。
    assert result.ok
    assert result.data["status"] == "error"
    service.request.assert_not_awaited()


def test_customer_read_tools_are_read_only_and_pii_scoped():
    for name in ("order_list", "after_sales_status", "shipment_track"):
        tool = registry.get(name)
        assert tool is not None
        assert tool.policy.permission == "read"
        assert tool.policy.requires_approval is False
        assert tool.policy.data_class == "pii"
