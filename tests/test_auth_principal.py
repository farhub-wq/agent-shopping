"""共享鉴权加固测试：伪造身份头必须被忽略，签名头只在时间窗内可信。"""

import time

import pytest
from shop_common import auth
from shop_common.errors import ApiError


async def test_forged_identity_headers_are_ignored(monkeypatch):
    monkeypatch.setattr(auth.settings, "trust_identity_headers", False)
    p = await auth.get_principal(authorization=None, x_user_id="u1", x_user_role="admin", x_tenant="default")
    assert p.role == "customer"


async def test_signed_headers_are_trusted(monkeypatch):
    monkeypatch.setattr(auth.settings, "trust_identity_headers", True)
    monkeypatch.setattr(auth.settings, "internal_signing_secret", "s")
    ts = str(int(time.time()))
    sig = auth.create_internal_signature(user_id="u1", role="operator", tenant="default", ts=ts, request_id="r1", secret="s")
    p = await auth.get_principal(
        authorization=None,
        x_user_id="u1",
        x_user_role="operator",
        x_tenant="default",
        x_internal_signature=f"{ts}.{sig}",
        x_request_id="r1",
    )
    assert p.role == "operator"


async def test_stale_signature_is_rejected(monkeypatch):
    monkeypatch.setattr(auth.settings, "trust_identity_headers", True)
    monkeypatch.setattr(auth.settings, "internal_signing_secret", "s")
    ts = str(int(time.time()) - 60)
    sig = auth.create_internal_signature(user_id="u1", role="operator", tenant="default", ts=ts, request_id="r1", secret="s")
    with pytest.raises(ApiError) as exc:
        await auth.get_principal(
            authorization=None,
            x_user_id="u1",
            x_user_role="operator",
            x_tenant="default",
            x_internal_signature=f"{ts}.{sig}",
            x_request_id="r1",
        )
    assert exc.value.code == "UNAUTHORIZED"
