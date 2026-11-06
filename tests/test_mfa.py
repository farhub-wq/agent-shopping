"""管理员 MFA（TOTP）测试：绑定、二次验证、重放防护、关闭。

TOTP 算法用 RFC 4226 官方 6 位 HOTP 测试向量固定 secret 验证；
登录流程通过 bootstrap admin + 临时 SQLite 走真实 HTTP 接口。
"""

import os
import time

import pytest
from fastapi.testclient import TestClient
from identity import totp

# RFC 4226 附录 D：secret = base32("12345678901234567890")，6 位数字
_RFC_SECRET = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
_RFC_VECTORS = {
    0: "755224",
    1: "287082",
    2: "359152",
    3: "969429",
    4: "338314",
    5: "254676",
    6: "287922",
    7: "162583",
    8: "399871",
    9: "520489",
}


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    # bootstrap 出唯一的 admin：读环境变量创建，密码固定，供整条 MFA 流程使用
    old_phone = os.environ.get("BOOTSTRAP_ADMIN_PHONE")
    old_pwd = os.environ.get("BOOTSTRAP_ADMIN_PASSWORD")
    os.environ["BOOTSTRAP_ADMIN_PHONE"] = "13900000001"
    os.environ["BOOTSTRAP_ADMIN_PASSWORD"] = "AdminPass1!"

    import shop_common.db as db

    db.settings.database_url = f"sqlite+aiosqlite:///{tmp_path_factory.mktemp('identity_mfa') / 'test.db'}"
    db._engine = None
    db._session_factory = None

    from identity.app import app

    with TestClient(app) as test_client:
        yield test_client

    if old_phone is None:
        os.environ.pop("BOOTSTRAP_ADMIN_PHONE", None)
    else:
        os.environ["BOOTSTRAP_ADMIN_PHONE"] = old_phone
    if old_pwd is None:
        os.environ.pop("BOOTSTRAP_ADMIN_PASSWORD", None)
    else:
        os.environ["BOOTSTRAP_ADMIN_PASSWORD"] = old_pwd


def _login(client, phone: str, password: str, otp: str | None = None):
    body: dict = {"phone": phone, "password": password}
    if otp is not None:
        body["otp"] = otp
    return client.post("/auth/login", json=body)


def _bearer(resp) -> dict[str, str]:
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def test_totp_hotp_matches_rfc4226_vectors():
    for counter, expected in _RFC_VECTORS.items():
        assert totp._hotp(_RFC_SECRET, counter) == expected


def test_totp_verify_window_and_reject():
    # now=59 → step 1，其码为 counter 1 的 287082
    assert totp.verify(_RFC_SECRET, "287082", 59) == 1
    # 窗口 ±1：前一步的码也接受
    assert totp.verify(_RFC_SECRET, "755224", 59) == 0
    assert totp.verify(_RFC_SECRET, "000000", 59) is None


def test_enroll_requires_admin(client):
    client.post("/auth/register", json={"phone": "13800000020", "password": "Passw0rd!"})
    resp = _login(client, "13800000020", "Passw0rd!")
    assert resp.status_code == 200
    assert client.post("/auth/mfa/enroll", headers=_bearer(resp)).status_code == 403


def test_mfa_full_flow(client):
    phone, password = "13900000001", "AdminPass1!"
    # 未启用：单因素登录不受影响
    first = _login(client, phone, password)
    assert first.status_code == 200
    headers = _bearer(first)

    # 绑定：enroll 返回明文 secret（仅此一次）与 otpauth 链接
    enroll = client.post("/auth/mfa/enroll", headers=headers)
    assert enroll.status_code == 200
    secret = enroll.json()["secret"]
    assert enroll.json()["otpauth_url"].startswith("otpauth://totp/shop-agent:")

    # 确认绑定：当前时间步的码
    confirm = client.post("/auth/mfa/confirm", json={"code": totp.code_at(secret, int(time.time()))}, headers=headers)
    assert confirm.status_code == 200
    assert confirm.json()["enabled"] is True

    # 状态接口确认已启用
    status = client.get("/auth/mfa", headers=headers)
    assert status.status_code == 200
    assert status.json()["enabled"] is True

    # 已启用后重复 enroll 被拒
    assert client.post("/auth/mfa/enroll", headers=headers).status_code == 409

    # 仅密码 → 401 + MFA_REQUIRED，不签发 session
    missing = _login(client, phone, password)
    assert missing.status_code == 401
    assert missing.json()["code"] == "MFA_REQUIRED"

    # 错误动态码 → 401
    assert _login(client, phone, password, otp="000000").status_code == 401

    # 正确动态码（取下一时间步，避免与 confirm 已用的 step 撞重放）→ 登录成功
    login_code = totp.code_at(secret, int(time.time()) + 30)
    ok = _login(client, phone, password, otp=login_code)
    assert ok.status_code == 200

    # 同一动态码二次使用 → 重放拒绝
    assert _login(client, phone, password, otp=login_code).status_code == 401

    # 关闭需重新校验密码：密码错被拒
    assert client.post("/auth/mfa/disable", json={"password": "WrongPass1!"}, headers=headers).status_code == 401
    # 密码对 → 关闭成功，恢复单因素登录
    off = client.post("/auth/mfa/disable", json={"password": password}, headers=headers)
    assert off.status_code == 200
    assert off.json()["enabled"] is False
    assert client.get("/auth/mfa", headers=headers).json()["enabled"] is False
    assert _login(client, phone, password).status_code == 200
