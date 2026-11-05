"""identity 登录 / 刷新 / 登出 / 锁定测试。

Cookie 的 path=/api/auth 是给网关用的，直连 identity 的测试不走 jar，
改为手工拼 Cookie 头，正好覆盖服务端真实的解析逻辑。
"""

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    # 每个测试模块用独立临时库，并把进程级引擎重置指向它，保证用例可重复运行
    import shop_common.db as db

    db.settings.database_url = f"sqlite+aiosqlite:///{tmp_path_factory.mktemp('identity') / 'test.db'}"
    db._engine = None
    db._session_factory = None

    from identity.app import app

    with TestClient(app) as test_client:
        yield test_client


def _auth_headers(resp) -> dict[str, str]:
    """从登录/刷新响应中取出两个 Cookie，拼成后续请求要用的头。"""
    refresh = resp.cookies.get("shop_refresh")
    csrf = resp.cookies.get("shop_csrf")
    assert refresh and csrf
    return {"Cookie": f"shop_refresh={refresh}; shop_csrf={csrf}", "X-CSRF-Token": csrf}


def test_login_returns_roles_from_db(client):
    client.post("/auth/register", json={"phone": "13800000010", "password": "Passw0rd!"})
    resp = client.post("/auth/login", json={"phone": "13800000010", "password": "Passw0rd!"})
    assert resp.status_code == 200
    assert resp.json()["user"]["roles"] == ["customer"]
    assert resp.json()["expires_in"] == 900
    assert "HttpOnly" in resp.headers["set-cookie"]


def test_refresh_replay_is_rejected(client):
    client.post("/auth/register", json={"phone": "13800000011", "password": "Passw0rd!"})
    login = client.post("/auth/login", json={"phone": "13800000011", "password": "Passw0rd!"})
    old = _auth_headers(login)
    # 第一次刷新成功并轮换出新令牌
    assert client.post("/auth/refresh", headers=old).status_code == 200
    # 旧 refresh 再次使用即重放，必须 401
    replay = client.post("/auth/refresh", headers=old)
    assert replay.status_code == 401


def test_logout_revokes_refresh(client):
    client.post("/auth/register", json={"phone": "13800000012", "password": "Passw0rd!"})
    login = client.post("/auth/login", json={"phone": "13800000012", "password": "Passw0rd!"})
    headers = _auth_headers(login)
    assert client.post("/auth/logout", headers=headers).status_code == 204
    assert client.post("/auth/refresh", headers=headers).status_code == 401


def test_fifth_failure_locks_account(client):
    client.post("/auth/register", json={"phone": "13800000013", "password": "Passw0rd!"})
    for _ in range(5):
        resp = client.post("/auth/login", json={"phone": "13800000013", "password": "WrongPass1!"})
        assert resp.status_code == 401
    # 锁定窗口内即使密码正确也返回 429
    locked = client.post("/auth/login", json={"phone": "13800000013", "password": "Passw0rd!"})
    assert locked.status_code == 429
