import pytest
from fulfillment import app as fulfillment_app


@pytest.mark.asyncio
async def test_fulfillment_ignores_non_paid_order_events(monkeypatch):
    """同一个 topic 会收到 created/reserved/cancelled，未支付不应建履约单。"""

    def refuse_session():
        raise AssertionError("非 order.paid 事件不应打开数据库会话")

    monkeypatch.setattr(fulfillment_app, "get_session_factory", refuse_session)
    await fulfillment_app.handle_order_paid({"type": "order.created", "payload": {"order_id": "o-1"}})
