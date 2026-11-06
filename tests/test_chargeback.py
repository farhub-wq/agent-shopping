import json
import time
import uuid

import pytest
from fastapi.testclient import TestClient
from payment import app as payment_app
from payment.models import LedgerEntry, LedgerTransaction
from payment.provider import callback_signature
from shop_common.auth import create_token
from shop_common.config import get_settings
from shop_common.db import get_session_factory
from sqlalchemy import func, select

settings = get_settings()

# 记录已按 won 处置的拒付单，供后续异步用例核验总账反向冲正
WON_CHARGEBACK_IDS: list[str] = []


def _operator_headers() -> dict[str, str]:
    token = create_token(user_id="tester", role="operator")
    return {"Authorization": f"Bearer {token}"}


def _customer_headers(user_id: str = "buyer-1") -> dict[str, str]:
    token = create_token(user_id=user_id, role="customer")
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
        json={"order_id": order_id, "amount_cents": amount, "currency": "CNY", "user_id": "tester"},
        headers=_operator_headers(),
    )
    assert response.status_code == 201, response.text
    return response.json()


def _signed_callback(payload: dict) -> tuple[bytes, dict[str, str]]:
    raw = json.dumps(payload, separators=(",", ":")).encode()
    ts = str(int(time.time()))
    sig = callback_signature(settings.payment_webhook_secret, ts, raw)
    return raw, {"X-Payment-Timestamp": ts, "X-Payment-Signature": sig}


def _paid_payment(client: TestClient, amount: int = 500) -> dict:
    # 先建单再走支付回调，得到一张已支付的 payment，作为拒付的前置条件
    payment = _create_payment(client, amount)
    raw, headers = _signed_callback(
        {
            "provider_txn_id": payment["provider_txn_id"],
            "order_id": payment["order_id"],
            "amount_cents": amount,
            "currency": "CNY",
            "status": "paid",
        }
    )
    response = client.post("/payments/callback/mock", content=raw, headers=headers)
    assert response.status_code == 200, response.text
    return payment


def _chargeback_payload(payment: dict, amount: int, case_id: str, **extra) -> dict:
    return {
        "provider_case_id": case_id,
        "provider_txn_id": payment["provider_txn_id"],
        "amount_cents": amount,
        "currency": "CNY",
        **extra,
    }


def _post_chargeback(client: TestClient, payload: dict):
    raw, headers = _signed_callback(payload)
    return client.post("/payments/chargebacks/callback/mock", content=raw, headers=headers)


def _case_id(tag: str) -> str:
    # 争议单号唯一索引跨测试运行持久化，固定字符串会与历史数据撞车，故带随机后缀
    return f"{tag}-{uuid.uuid4().hex[:8]}"


def _find_chargeback_id(client: TestClient, case_id: str) -> str:
    body = client.get("/payments/chargebacks", headers=_operator_headers()).json()
    return next(item["id"] for item in body["items"] if item["provider_case_id"] == case_id)


def test_chargeback_opens_and_is_idempotent(client):
    payment = _paid_payment(client, 500)
    payload = _chargeback_payload(payment, 500, _case_id("open"), reason="fraud")
    first = _post_chargeback(client, payload)
    assert first.status_code == 200, first.text
    assert first.json() == {"status": "opened", "idempotent": False}

    # 同一争议单号重复通知：幂等返回，不重复建单、不重复记账
    second = _post_chargeback(client, payload)
    assert second.status_code == 200
    assert second.json() == {"status": "opened", "idempotent": True}


def test_chargeback_rejects_forged_amount_and_signature(client):
    payment = _paid_payment(client, 500)
    forged = _chargeback_payload(payment, 100, "case-forged")
    assert _post_chargeback(client, forged).status_code == 409

    raw, _ = _signed_callback(_chargeback_payload(payment, 500, "case-badsig"))
    assert client.post(
        "/payments/chargebacks/callback/mock",
        content=raw,
        headers={"X-Payment-Timestamp": str(int(time.time())), "X-Payment-Signature": "bad"},
    ).status_code == 401


def test_chargeback_rejects_unpaid_payment(client):
    # 未支付成功的订单不可拒付，避免凭空冲减应收
    payment = _create_payment(client, 500)
    assert _post_chargeback(client, _chargeback_payload(payment, 500, "case-unpaid")).status_code == 409


def test_chargeback_rejects_amount_over_payment(client):
    payment = _paid_payment(client, 500)
    assert _post_chargeback(client, _chargeback_payload(payment, 501, "case-over")).status_code == 409


def test_chargeback_requires_case_id(client):
    payment = _paid_payment(client, 500)
    raw, headers = _signed_callback(
        {"provider_txn_id": payment["provider_txn_id"], "amount_cents": 500, "currency": "CNY"}
    )
    response = client.post("/payments/chargebacks/callback/mock", content=raw, headers=headers)
    assert response.status_code == 400


def test_chargeback_list_requires_staff(client):
    assert client.get("/payments/chargebacks", headers=_customer_headers()).status_code == 403
    response = client.get("/payments/chargebacks", headers=_operator_headers())
    assert response.status_code == 200, response.text
    body = response.json()
    assert "items" in body and "total" in body


def test_chargeback_resolve_requires_staff(client):
    payment = _paid_payment(client, 500)
    case_id = _case_id("resolve")
    _post_chargeback(client, _chargeback_payload(payment, 500, case_id))
    chargeback_id = _find_chargeback_id(client, case_id)

    assert client.post(
        f"/payments/chargebacks/{chargeback_id}/resolve",
        json={"outcome": "won"},
        headers=_customer_headers(),
    ).status_code == 403
    assert client.post(
        "/payments/chargebacks/not-exist/resolve",
        json={"outcome": "won"},
        headers=_operator_headers(),
    ).status_code == 404


def test_chargeback_resolve_won_reverses_ledger(client):
    payment = _paid_payment(client, 500)
    case_id = _case_id("won")
    _post_chargeback(client, _chargeback_payload(payment, 500, case_id))
    chargeback_id = _find_chargeback_id(client, case_id)

    first = client.post(
        f"/payments/chargebacks/{chargeback_id}/resolve",
        json={"outcome": "won"},
        headers=_operator_headers(),
    )
    assert first.status_code == 200, first.text
    assert first.json() == {"status": "won", "idempotent": False}
    WON_CHARGEBACK_IDS.append(chargeback_id)

    # 已处置的拒付单重复处置幂等，不重复冲正
    again = client.post(
        f"/payments/chargebacks/{chargeback_id}/resolve",
        json={"outcome": "won"},
        headers=_operator_headers(),
    )
    assert again.json() == {"status": "won", "idempotent": True}


def test_chargeback_resolve_lost_keeps_loss(client):
    payment = _paid_payment(client, 500)
    case_id = _case_id("lost")
    _post_chargeback(client, _chargeback_payload(payment, 500, case_id))
    chargeback_id = _find_chargeback_id(client, case_id)

    first = client.post(
        f"/payments/chargebacks/{chargeback_id}/resolve",
        json={"outcome": "lost"},
        headers=_operator_headers(),
    )
    assert first.status_code == 200, first.text
    assert first.json() == {"status": "lost", "idempotent": False}

    # 非法 outcome 被请求模型拦截
    assert client.post(
        f"/payments/chargebacks/{chargeback_id}/resolve",
        json={"outcome": "maybe"},
        headers=_operator_headers(),
    ).status_code == 422


@pytest.mark.asyncio
async def test_chargeback_won_ledger_net_zero():
    # won 冲正后，该拒付单的 CHARGEBACK 科目借贷相等、净额归零
    assert WON_CHARGEBACK_IDS
    async with get_session_factory()() as session:
        for chargeback_id in WON_CHARGEBACK_IDS:
            txn_ids = select(LedgerTransaction.id).where(LedgerTransaction.ref_id == chargeback_id)
            debit = await session.scalar(
                select(func.coalesce(func.sum(LedgerEntry.amount_cents), 0)).where(
                    LedgerEntry.txn_id.in_(txn_ids),
                    LedgerEntry.account_code == "CHARGEBACK",
                    LedgerEntry.direction == "debit",
                )
            )
            credit = await session.scalar(
                select(func.coalesce(func.sum(LedgerEntry.amount_cents), 0)).where(
                    LedgerEntry.txn_id.in_(txn_ids),
                    LedgerEntry.account_code == "CHARGEBACK",
                    LedgerEntry.direction == "credit",
                )
            )
            assert int(debit) == int(credit)
