"""identity 注册接口测试：只验证服务端真实行为，不 mock 数据库。"""

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

    # TestClient 上下文会执行 lifespan：建表 + 日志初始化
    with TestClient(app) as test_client:
        yield test_client


def test_register_creates_customer(client):
    resp = client.post("/auth/register", json={"phone": "13800000001", "password": "Passw0rd!"})
    assert resp.status_code == 201
    body = resp.json()
    assert body["phone"] == "13800000001"
    assert body["roles"] == ["customer"]
    assert "password" not in resp.text


def test_register_duplicate_phone_conflicts(client):
    payload = {"phone": "13800000002", "password": "Passw0rd!"}
    assert client.post("/auth/register", json=payload).status_code == 201
    assert client.post("/auth/register", json=payload).status_code == 409


def test_register_rejects_short_password(client):
    resp = client.post("/auth/register", json={"phone": "13800000003", "password": "short"})
    assert resp.status_code == 400
