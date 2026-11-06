"""网关鉴权改造测试：dev 换 token 只在显式开启时存在，转发头必须带内网签名。"""

import time

import gateway.app as gateway_app
import pytest
from shop_common.auth import Principal


def test_dev_token_disabled_outside_dev(monkeypatch):
    monkeypatch.setattr(gateway_app.settings, "env", "prod")
    assert gateway_app._dev_token_enabled() is False


def test_forward_carries_signature():
    p = Principal("u1", "operator", "default")
    headers = gateway_app._forward(p, request_id="r1")
    assert headers["X-Internal-Signature"].startswith(f"{int(time.time())}.")
    assert headers["X-User-Role"] == "operator"


class _FakeRequest:
    """my_orders 只用到 headers，用最小替身即可。"""

    headers: dict[str, str] = {}


class _FakeResponse:
    def json(self) -> dict:
        return {"items": []}


class _FakeClient:
    def __init__(self, *args, **kwargs) -> None:
        pass

    async def __aenter__(self) -> "_FakeClient":
        return self

    async def __aexit__(self, *args) -> bool:
        return False

    async def get(self, url: str, params=None, headers=None) -> _FakeResponse:
        self.url = url
        self.params = params
        self.headers = headers
        return _FakeResponse()


@pytest.mark.asyncio
async def test_storefront_orders_scopes_to_current_user(monkeypatch):
    # 店铺前台「我的订单」必须强制带 user_id，staff 身份也不能顺带看到他人订单
    fake = _FakeClient()
    monkeypatch.setattr(gateway_app.httpx, "AsyncClient", lambda *a, **kw: fake)
    await gateway_app.my_orders(_FakeRequest(), Principal("u1", "operator", "default"))
    assert fake.params == {"user_id": "u1"}


@pytest.mark.asyncio
async def test_storefront_orders_passes_status(monkeypatch):
    fake = _FakeClient()
    monkeypatch.setattr(gateway_app.httpx, "AsyncClient", lambda *a, **kw: fake)
    await gateway_app.my_orders(_FakeRequest(), Principal("u1", "operator", "default"), status="PAID")
    assert fake.params == {"user_id": "u1", "status": "PAID"}


@pytest.mark.asyncio
async def test_mfa_proxy_routes_forward_to_identity(monkeypatch):
    # 回归：MFA 接口必须经网关代理到 identity，否则前端 /api/auth/mfa/* 会落到 404
    calls: list[str] = []

    async def fake_proxy(request, path: str) -> str:
        calls.append(path)
        return f"ok:{path}"

    monkeypatch.setattr(gateway_app, "_proxy_auth", fake_proxy)
    req = _FakeRequest()

    await gateway_app.auth_mfa_status(req)
    await gateway_app.auth_mfa_enroll(req)
    await gateway_app.auth_mfa_confirm(req)
    await gateway_app.auth_mfa_disable(req)

    assert calls == ["/auth/mfa", "/auth/mfa/enroll", "/auth/mfa/confirm", "/auth/mfa/disable"]
