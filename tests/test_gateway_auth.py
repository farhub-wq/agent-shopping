"""网关鉴权改造测试：dev 换 token 只在显式开启时存在，转发头必须带内网签名。"""

import time

import gateway.app as gateway_app
from shop_common.auth import Principal


def test_dev_token_disabled_outside_dev(monkeypatch):
    monkeypatch.setattr(gateway_app.settings, "env", "prod")
    assert gateway_app._dev_token_enabled() is False


def test_forward_carries_signature():
    p = Principal("u1", "operator", "default")
    headers = gateway_app._forward(p, request_id="r1")
    assert headers["X-Internal-Signature"].startswith(f"{int(time.time())}.")
    assert headers["X-User-Role"] == "operator"
