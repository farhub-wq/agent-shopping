"""越权回归：顾客角色不得调用运营接口；该约束由 Principal 统一保证。"""

import pytest
from shop_common.auth import Principal
from shop_common.errors import ApiError


async def test_customer_cannot_use_staff_action():
    p = Principal("u1", "customer", "default")
    with pytest.raises(ApiError) as exc:
        p.require_staff()
    assert exc.value.code == "FORBIDDEN"


async def test_guest_cannot_use_staff_action():
    p = Principal("guest", "customer", "default")
    with pytest.raises(ApiError) as exc:
        p.require_staff()
    assert exc.value.code == "FORBIDDEN"


def test_operator_passes_staff_check():
    Principal("u2", "operator", "default").require_staff()
