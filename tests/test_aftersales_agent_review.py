from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio
from aftersales import app as aftersales_app
from aftersales.models import AfterSaleTicket
from harness.core import tools_impl
from harness.core.tools import ToolContext, registry
from shop_common.auth import Principal, get_principal
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


@pytest_asyncio.fixture
async def sales_env(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(AfterSaleTicket.__table__.create)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(aftersales_app, "get_session_factory", lambda: factory)

    order = {
        "status": "PAID",
        "created_at": (datetime.now(UTC) - timedelta(hours=3)).isoformat(),
        "amount_cents": 1000,
        "refund_amount_cents": 0,
        "items": [
            {
                "item_id": "item-a",
                "qty": 2,
                "price_cents": 300,
                "paid_amount_cents": 600,
                "refunded_qty": 0,
                "refunded_amount_cents": 0,
            },
            {
                "item_id": "item-b",
                "qty": 1,
                "price_cents": 400,
                "paid_amount_cents": 400,
                "refunded_qty": 0,
                "refunded_amount_cents": 0,
            },
        ],
    }
    order_service = SimpleNamespace(request=AsyncMock(return_value=order))
    payment_service = SimpleNamespace(request=AsyncMock(return_value={}))
    monkeypatch.setattr(aftersales_app, "order_client", order_service)
    monkeypatch.setattr(aftersales_app, "payment_client", payment_service)
    aftersales_app.app.dependency_overrides[get_principal] = lambda: Principal(
        "customer-1", "customer", "default"
    )
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=aftersales_app.app),
            base_url="http://aftersales.test",
        ) as client:
            yield SimpleNamespace(
                client=client,
                order=order,
                order_service=order_service,
                payment_service=payment_service,
            )
    finally:
        aftersales_app.app.dependency_overrides.pop(get_principal, None)
        await engine.dispose()


async def _create_ticket(env, **payload):
    return await env.client.post(
        "/aftersales/tickets",
        json={"order_id": "SO1", "reason": "quality_issue", **payload},
    )


def _assert_waiting_for_review(response):
    assert response.status_code == 201
    ticket = response.json()
    assert ticket["status"] == "pending"
    assert ticket["decided_by"] == ""
    assert ticket["decided_at"] is None
    assert ticket["refund_id"] is None
    assert ticket["decision_note"] == "Agent 申请，待人工审核"
    return ticket


@pytest.mark.asyncio
async def test_manual_review_keeps_auto_approve_pending_without_refund(sales_env):
    response = await _create_ticket(sales_env, review_mode="manual")

    ticket = _assert_waiting_for_review(response)
    assert ticket["policy"]["decision"] == "auto_approve"
    assert ticket["risk_score"] == 0
    assert ticket["risk_reasons"] == []
    sales_env.payment_service.request.assert_not_awaited()


@pytest.mark.asyncio
async def test_manual_review_keeps_auto_reject_pending_without_refund(sales_env):
    sales_env.order["created_at"] = (datetime.now(UTC) - timedelta(days=10)).isoformat()

    response = await _create_ticket(sales_env, review_mode="manual")

    ticket = _assert_waiting_for_review(response)
    assert ticket["policy"]["decision"] == "auto_reject"
    assert ticket["policy"]["risk_reasons"] == ["超出售后申请窗口"]
    sales_env.payment_service.request.assert_not_awaited()


@pytest.mark.asyncio
async def test_default_policy_mode_keeps_auto_approval_and_refund(sales_env):
    response = await _create_ticket(sales_env)

    assert response.status_code == 201
    ticket = response.json()
    assert ticket["status"] == "approved"
    assert ticket["decided_by"] == "policy-engine"
    assert ticket["decided_at"] is not None
    assert ticket["policy"]["decision"] == "auto_approve"
    sales_env.payment_service.request.assert_awaited_once()
    call = sales_env.payment_service.request.await_args
    assert call.args == ("POST", "/payments/refunds")
    assert call.kwargs["json"] == {
        "order_id": "SO1",
        "ticket_id": ticket["id"],
        "amount_cents": 1000,
        "order_item_id": None,
        "quantity": 0,
    }
    assert call.kwargs["headers"]["X-User-Role"] == "operator"


@pytest.mark.asyncio
async def test_default_policy_mode_keeps_auto_rejection_without_refund(sales_env):
    sales_env.order["created_at"] = (datetime.now(UTC) - timedelta(days=10)).isoformat()

    response = await _create_ticket(sales_env)

    assert response.status_code == 201
    ticket = response.json()
    assert ticket["status"] == "rejected"
    assert ticket["decided_by"] == "policy-engine"
    assert ticket["decided_at"] is not None
    assert ticket["policy"]["decision"] == "auto_reject"
    sales_env.payment_service.request.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("review_mode", ["automatic", "", None, 1])
async def test_invalid_review_mode_returns_422_before_order_lookup(sales_env, review_mode):
    response = await _create_ticket(sales_env, review_mode=review_mode)

    assert response.status_code == 422
    assert any(error["loc"] == ["body", "review_mode"] for error in response.json()["detail"])
    sales_env.order_service.request.assert_not_awaited()
    sales_env.payment_service.request.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "expected_item", "expected_quantity", "expected_amount"),
    [
        ({"order_item_id": "item-a", "quantity": 1}, "item-a", 1, 300),
        ({}, None, 0, 1000),
    ],
)
async def test_policy_mode_preserves_item_and_full_refund_amounts(
    sales_env, payload, expected_item, expected_quantity, expected_amount
):
    response = await _create_ticket(sales_env, **payload)

    assert response.status_code == 201
    ticket = response.json()
    assert ticket["status"] == "approved"
    assert ticket["order_item_id"] == expected_item
    assert ticket["quantity"] == expected_quantity
    assert ticket["amount_cents"] == expected_amount
    assert sales_env.payment_service.request.await_args.kwargs["json"] == {
        "order_id": "SO1",
        "ticket_id": ticket["id"],
        "amount_cents": expected_amount,
        "order_item_id": expected_item,
        "quantity": expected_quantity,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "expected_item", "expected_quantity", "expected_amount"),
    [
        ({"order_item_id": "item-a"}, "item-a", 1, 300),
        ({}, None, 0, 700),
    ],
)
async def test_policy_mode_refunds_only_remaining_item_or_order_amount(
    sales_env, payload, expected_item, expected_quantity, expected_amount
):
    sales_env.order["refund_amount_cents"] = 300
    sales_env.order["items"][0]["refunded_qty"] = 1
    sales_env.order["items"][0]["refunded_amount_cents"] = 300

    response = await _create_ticket(sales_env, **payload)

    assert response.status_code == 201
    ticket = response.json()
    assert ticket["status"] == "approved"
    assert ticket["order_item_id"] == expected_item
    assert ticket["quantity"] == expected_quantity
    assert ticket["amount_cents"] == expected_amount
    assert sales_env.payment_service.request.await_args.kwargs["json"]["amount_cents"] == expected_amount


@pytest.mark.asyncio
async def test_after_sales_draft_forces_manual_review(monkeypatch):
    aftersales_service = SimpleNamespace(request=AsyncMock(return_value={"status": "pending"}))
    monkeypatch.setattr(tools_impl, "aftersales", aftersales_service)
    ctx = ToolContext(user_id="customer-1", role="customer", tenant="default", session_id="s", run_id="r")

    result = await registry.execute(
        "after_sales_draft",
        {
            "order_id": "SO1",
            "reason": "quality_issue",
            "description": "包装破损",
            "review_mode": "policy",
        },
        ctx,
        trust="vendor",
    )

    assert result.ok
    call = aftersales_service.request.await_args
    assert call.args == ("POST", "/aftersales/tickets")
    assert call.kwargs["json"] == {
        "order_id": "SO1",
        "reason": "quality_issue",
        "description": "包装破损",
        "review_mode": "manual",
    }


def test_after_sales_draft_description_promises_review_without_refund():
    tool = registry.get("after_sales_draft")

    assert tool is not None
    assert "只创建待审核申请" in tool.description
    assert "不执行退款" in tool.description
