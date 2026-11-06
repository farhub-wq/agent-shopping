import pytest
from order.state_machine import resolve_target
from shop_common.errors import ApiError


def test_happy_path_transitions():
    assert resolve_target("CREATED", "reserve") == "RESERVED"
    assert resolve_target("RESERVED", "pay_start") == "PAYING"
    assert resolve_target("PAYING", "pay_success") == "PAID"
    assert resolve_target("PAID", "ship") == "SHIPPED"
    assert resolve_target("SHIPPED", "deliver") == "COMPLETED"


def test_partial_shipment_path():
    # 首次部分发货进 SHIPPING，全部发完才进 SHIPPED，SHIPPING 不能直接签收
    assert resolve_target("PAID", "ship_partial") == "SHIPPING"
    assert resolve_target("SHIPPING", "ship") == "SHIPPED"
    assert resolve_target("SHIPPING", "refund_request") == "REFUNDING"
    with pytest.raises(ApiError):
        resolve_target("SHIPPING", "deliver")


def test_illegal_transition_rejected():
    with pytest.raises(ApiError) as exc:
        resolve_target("CANCELLED", "ship")
    assert exc.value.code == "CONFLICT"


def test_avoid_silent_overwrite_after_refund():
    with pytest.raises(ApiError):
        resolve_target("REFUNDING", "ship")


def test_refund_reject_returns_to_previous_state():
    assert resolve_target("REFUNDING", "refund_reject", refund_previous="SHIPPED") == "SHIPPED"
