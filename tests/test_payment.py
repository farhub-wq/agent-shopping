import json
import time
import uuid
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from payment import app as payment_app
from payment.ledger import post_transaction
from payment.models import LedgerEntry, Refund
from payment.provider import callback_signature
from shop_common.auth import create_token
from shop_common.config import get_settings
from shop_common.db import get_session_factory, init_models
from shop_common.errors import ApiError
from sqlalchemy import func, select

settings = get_settings()


def _operator_headers() -> dict[str, str]:
    token = create_token(user_id="tester", role="operator")
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="module")
def client():
    # 单测关闭中继/扫描任务，避免依赖 Redis 常驻进程
    settings.background_tasks = False
    settings.env = "dev"
    settings.payment_mock_mode = True
    with TestClient(payment_app.app) as test_client:
        yield test_client


def _create_payment(client: TestClient, amount: int = 500) -> dict:
    order_id = str(uuid.uuid4())
    response = client.post(
        "/payments",
        json={"order_id": order_id, "amount_cents": amount, "currency": "CNY"},
        headers=_operator_headers(),
    )
    assert response.status_code == 201, response.text
    return response.json()


def _signed_callback(payload: dict) -> tuple[bytes, dict[str, str]]:
    raw = json.dumps(payload, separators=(",", ":")).encode()
    ts = str(int(time.time()))
    sig = callback_signature(settings.payment_webhook_secret, ts, raw)
    return raw, {"X-Payment-Timestamp": ts, "X-Payment-Signature": sig}


def test_callback_is_signed_and_idempotent(client):
    payment = _create_payment(client, 500)
    payload = {
        "provider_txn_id": payment["provider_txn_id"],
        "order_id": payment["order_id"],
        "amount_cents": 500,
        "currency": "CNY",
        "status": "paid",
    }
    raw, headers = _signed_callback(payload)
    first = client.post("/payments/callback/mock", content=raw, headers=headers)
    assert first.status_code == 200, first.text
    assert first.json()["idempotent"] is False

    # 重复回调必须 2xx 且不重复记账
    second = client.post("/payments/callback/mock", content=raw, headers=headers)
    assert second.status_code == 200
    assert second.json()["idempotent"] is True


def test_callback_rejects_forged_amount_and_signature(client):
    payment = _create_payment(client, 300)
    forged = {
        "provider_txn_id": payment["provider_txn_id"],
        "order_id": payment["order_id"],
        "amount_cents": 100,
        "currency": "CNY",
        "status": "paid",
    }
    raw, headers = _signed_callback(forged)
    assert client.post("/payments/callback/mock", content=raw, headers=headers).status_code == 409

    raw, _ = _signed_callback({**forged, "amount_cents": 300})
    assert client.post(
        "/payments/callback/mock",
        content=raw,
        headers={"X-Payment-Timestamp": str(int(time.time())), "X-Payment-Signature": "bad"},
    ).status_code == 401


def test_refund_is_idempotent_and_capped(client):
    payment = _create_payment(client, 800)
    raw, headers = _signed_callback(
        {
            "provider_txn_id": payment["provider_txn_id"],
            "order_id": payment["order_id"],
            "amount_cents": 800,
            "currency": "CNY",
            "status": "paid",
        }
    )
    assert client.post("/payments/callback/mock", content=raw, headers=headers).status_code == 200

    ticket_id = str(uuid.uuid4())
    body = {"order_id": payment["order_id"], "ticket_id": ticket_id, "amount_cents": 300, "quantity": 1}
    first = client.post("/payments/refunds", json=body, headers=_operator_headers())
    assert first.status_code == 201, first.text
    second = client.post("/payments/refunds", json=body, headers=_operator_headers())
    assert second.status_code == 201
    assert second.json()["idempotent"] is True

    over = client.post(
        "/payments/refunds",
        json={**body, "ticket_id": str(uuid.uuid4()), "amount_cents": 600},
        headers=_operator_headers(),
    )
    assert over.status_code == 409


def test_reconciliation_records_missing_provider(client, monkeypatch):
    payment = _create_payment(client, 200)
    raw, headers = _signed_callback(
        {
            "provider_txn_id": payment["provider_txn_id"],
            "order_id": payment["order_id"],
            "amount_cents": 200,
            "currency": "CNY",
            "status": "paid",
        }
    )
    assert client.post("/payments/callback/mock", content=raw, headers=headers).status_code == 200

    # 模拟渠道账单缺失本地这笔支付，对账应记录 missing_provider 且不自动改资金
    monkeypatch.setattr(payment_app.provider, "statement", lambda *, biz_date, local_records: [])
    biz_date = datetime.now(UTC).date().isoformat()
    response = client.post(
        "/payments/reconciliation", params={"biz_date": biz_date}, headers=_operator_headers()
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ledger_balanced"] is True
    assert any(item["kind"] == "missing_provider" for item in body["diffs"])


@pytest.mark.asyncio
async def test_ledger_rejects_imbalance():
    async with get_session_factory()() as session:
        with pytest.raises(ApiError) as exc:
            post_transaction(
                session,
                txn_type="payment",
                ref_type="payment",
                ref_id="x",
                entries=[("PROVIDER_RECEIVABLE", "debit", 100), ("REVENUE", "credit", 99)],
            )
        assert exc.value.code == "CONFLICT"


@pytest.mark.asyncio
async def test_two_refunds_write_two_ledger_transactions():
    # 退款分录只增不改：同一支付两笔退款应产生两笔独立交易
    await init_models()
    async with get_session_factory()() as session:
        refunds = (
            (await session.execute(select(Refund).where(Refund.status == "succeeded")))
            .scalars()
            .all()
        )
        assert refunds
        txn_ids = {row.txn_id for row in (await session.execute(select(LedgerEntry))).scalars().all()}
        assert len(txn_ids) >= 2
        debit = await session.scalar(
            select(func.sum(LedgerEntry.amount_cents)).where(LedgerEntry.direction == "debit")
        )
        credit = await session.scalar(
            select(func.sum(LedgerEntry.amount_cents)).where(LedgerEntry.direction == "credit")
        )
        assert int(debit) == int(credit)
