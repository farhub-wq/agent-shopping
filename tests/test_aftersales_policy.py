from datetime import UTC, datetime, timedelta

from aftersales.policy import evaluate


def _evaluate(**overrides):
    base = dict(
        order_status="PAID",
        order_created_at=datetime.now(UTC) - timedelta(hours=3),
        amount_cents=9900,
        reason="quality_issue",
        description="收到时包装破损",
        recent_ticket_count=0,
        window_days=7,
    )
    base.update(overrides)
    return evaluate(**base)


def test_small_low_risk_ticket_auto_approved():
    assert _evaluate()["decision"] == "auto_approve"


def test_expired_window_auto_rejected():
    result = _evaluate(order_created_at=datetime.now(UTC) - timedelta(days=10))
    assert result["decision"] == "auto_reject"


def test_large_amount_goes_manual():
    result = _evaluate(amount_cents=80000)
    assert result["decision"] == "manual"
    assert "大额退款" in result["risk_reasons"]


def test_fraud_keywords_raise_risk():
    result = _evaluate(description="收到是空包，要求退款", amount_cents=30000)
    assert result["decision"] == "manual"
    assert any("欺诈" in reason for reason in result["risk_reasons"])


def test_frequent_tickets_go_manual():
    result = _evaluate(recent_ticket_count=6)
    assert result["decision"] == "manual"
