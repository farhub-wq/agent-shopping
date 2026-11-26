"""顾客侧高风险写工具与订单改址：必须经人工审批，且只在允许状态生效。"""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from harness.core import tools_impl
from harness.core.tools import ToolContext, registry
from order.app import AddressUpdate, update_order_address
from order.models import Order
from shop_common.auth import Principal
from shop_common.db import get_session_factory, init_models
from shop_common.errors import ApiError
from starlette.requests import Request

_ADDRESS = {
    "receiver": "张三",
    "phone": "13800001111",
    "province": "上海",
    "city": "上海",
    "detail": "科技路 1 号",
}


def _ctx(approved: frozenset[str] = frozenset()) -> ToolContext:
    return ToolContext(
        user_id="u1",
        role="customer",
        tenant="default",
        session_id="s1",
        run_id="r1",
        approved=set(approved),
    )


def _request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "PATCH",
            "path": "/orders/o/address",
            "raw_path": b"/orders/o/address",
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 1234),
            "server": ("test", 80),
            "scheme": "http",
            "root_path": "",
        }
    )


async def _order(status: str, user_id: str = "u1") -> str:
    await init_models()
    order_id = str(uuid.uuid4())
    async with get_session_factory()() as session:
        session.add(
            Order(
                id=order_id,
                order_no=f"T{uuid.uuid4().hex[:12]}",
                user_id=user_id,
                tenant="default",
                status=status,
                amount_cents=1000,
                items=[],
                address=_ADDRESS,
            )
        )
        await session.commit()
    return order_id


@pytest.mark.asyncio
async def test_order_cancel_requires_approval_without_downstream_call(monkeypatch):
    service = SimpleNamespace(request=AsyncMock())
    monkeypatch.setattr(tools_impl, "orders", service)

    result = await registry.execute("order_cancel", {"order_id": "SO-1"}, _ctx(), trust="vendor")

    assert result.status == "needs_approval"
    service.request.assert_not_awaited()


@pytest.mark.asyncio
async def test_order_cancel_executes_after_approval_with_encoded_reason(monkeypatch):
    service = SimpleNamespace(request=AsyncMock(return_value={"status": "CANCELLED"}))
    monkeypatch.setattr(tools_impl, "orders", service)

    result = await registry.execute(
        "order_cancel",
        {"order_id": "SO-1", "reason": "下错单 & 不想要"},
        _ctx(frozenset({"order_cancel"})),
        trust="vendor",
    )

    assert result.ok
    method, path = service.request.await_args.args
    assert method == "POST"
    assert path.startswith("/orders/SO-1/cancel?reason=")
    # 空格与 & 必须被编码，模型输入不能注入额外查询参数。
    assert " " not in path and "&" not in path


@pytest.mark.asyncio
async def test_order_update_address_requires_approval(monkeypatch):
    service = SimpleNamespace(request=AsyncMock(return_value={"id": "SO-1"}))
    monkeypatch.setattr(tools_impl, "orders", service)

    denied = await registry.execute(
        "order_update_address",
        {"order_id": "SO-1", "address": _ADDRESS},
        _ctx(),
        trust="vendor",
    )

    assert denied.status == "needs_approval"
    service.request.assert_not_awaited()

    ok = await registry.execute(
        "order_update_address",
        {"order_id": "SO-1", "address": _ADDRESS},
        _ctx(frozenset({"order_update_address"})),
        trust="vendor",
    )

    assert ok.ok
    method, path = service.request.await_args.args
    assert (method, path) == ("PATCH", "/orders/SO-1/address")
    assert service.request.await_args.kwargs["json"] == {"address": _ADDRESS}


def test_write_tools_are_approval_gated_and_granted_to_aftersales():
    from harness.core.agents import build_builtin_agents

    granted = build_builtin_agents()["aftersales"].manifest.allowed_tools
    for name in ("order_cancel", "order_update_address"):
        tool = registry.get(name)
        assert tool is not None
        assert tool.policy.permission == "write"
        assert tool.policy.requires_approval is True
        assert name in granted


@pytest.mark.asyncio
async def test_customer_can_change_address_before_shipping():
    order_id = await _order("PAID")

    result = await update_order_address(
        order_id,
        AddressUpdate(address={**_ADDRESS, "detail": "新地址 2 号"}),
        Principal("u1", "customer", "default"),
        _request(),
    )

    assert result["address"]["detail"] == "新地址 2 号"


@pytest.mark.asyncio
async def test_address_change_after_shipping_is_rejected():
    order_id = await _order("SHIPPED")

    with pytest.raises(ApiError) as exc:
        await update_order_address(
            order_id,
            AddressUpdate(address=_ADDRESS),
            Principal("u1", "customer", "default"),
            _request(),
        )

    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_address_change_rejects_other_users_order():
    order_id = await _order("PAID", user_id="u2")

    with pytest.raises(ApiError) as exc:
        await update_order_address(
            order_id,
            AddressUpdate(address=_ADDRESS),
            Principal("u1", "customer", "default"),
            _request(),
        )

    assert exc.value.status_code == 403
