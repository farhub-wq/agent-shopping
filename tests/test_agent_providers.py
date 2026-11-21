"""多厂商 provider 领域测试：预设、SSRF 校验、CRUD 归属、降级链与连通测试。"""

import pytest
from harness.core import providers
from harness.core.crypto import decrypt_model_key, encrypt_model_key, mask_key
from harness.core.providers import (
    PRESETS,
    create_provider,
    ensure_default_free_ai,
    resolve_provider,
    to_dict,
    validate_base_url,
)
from harness.models import ModelProvider
from shop_common.db import get_session_factory, init_models
from sqlalchemy import delete, select


@pytest.fixture(autouse=True)
async def _clean_slate():
    # 共享 test.db，跑前清空 harness 相关表，保证「空表幂等写入默认 AI」等断言不受残留影响。
    await init_models()
    async with get_session_factory()() as session:
        for model in (ModelProvider,):
            await session.execute(delete(model))
        await session.commit()
    yield


def test_presets_cover_expected_vendors():
    assert {"deepseek", "glm", "openai-codex", "claude", "chatanywhere", "custom"} <= set(PRESETS)
    # anthropic 预设必须带 /v1 段，调用方直接拼 /messages 才是合法端点。
    assert PRESETS["claude"]["type"] == "anthropic"
    assert PRESETS["claude"]["base_url"].endswith("/v1")


def test_validate_base_url_rejects_internal():
    for url in ("http://127.0.0.1/v1", "http://10.0.0.8/v1", "http://192.168.1.1/v1"):
        with pytest.raises(Exception) as exc:
            validate_base_url(url)
        assert "内网" in str(exc.value)


def test_validate_base_url_rejects_non_http():
    with pytest.raises(Exception):
        validate_base_url("ftp://example.com/v1")


def test_validate_base_url_accepts_public_ip():
    # 字面公网 IP 不走 DNS，直接命中放行分支。
    assert validate_base_url("https://8.8.8.8/v1") == "https://8.8.8.8/v1"


def test_validate_base_url_dns_rebinding_rejected(monkeypatch):
    # 主机名解析到内网地址也要拒绝：防 DNS 重绑定把公网域名指向 127.0.0.1。
    monkeypatch.setattr(
        providers.socket, "getaddrinfo", lambda host, port: [("", "", 0, "", ("127.0.0.1", 0))]
    )
    with pytest.raises(Exception):
        validate_base_url("https://evil.example.com/v1")


def test_key_encrypt_decrypt_and_mask():
    token = encrypt_model_key("sk-1234567890abcd")
    assert token != "sk-1234567890abcd"
    assert decrypt_model_key(token) == "sk-1234567890abcd"
    assert mask_key(token) == "****" + token[-4:]
    assert mask_key("") == ""


@pytest.mark.asyncio
async def test_create_provider_encrypts_key_and_hides_plaintext():
    async with get_session_factory()() as session:
        record = await create_provider(
            session,
            owner_user_id="u1",
            tenant_id="default",
            provider_type="openai_compatible",
            name="我的 DeepSeek",
            base_url="https://api.deepseek.com/v1",
            model="deepseek-chat",
            api_key="sk-secret-key",
        )
    data = to_dict(record)
    assert "api_key" not in data
    assert data["has_key"] is True
    assert data["key_masked"].endswith("key")
    assert decrypt_model_key(record.api_key_enc) == "sk-secret-key"


@pytest.mark.asyncio
async def test_default_free_ai_seeded_idempotently():
    async with get_session_factory()() as session:
        await ensure_default_free_ai(session)
        await ensure_default_free_ai(session)
        rows = (await session.execute(select(ModelProvider))).scalars().all()
    assert len(rows) == 1
    assert rows[0].is_default is True
    assert rows[0].owner_user_id is None
    assert rows[0].api_key_enc == ""  # 免费 AI 初始未填 key，解析链回退离线


@pytest.mark.asyncio
async def test_resolve_provider_chain_and_ownership(monkeypatch):
    # api.example.com 为占位域名，mock DNS 到公网 IP，避免测试依赖真实网络解析。
    monkeypatch.setattr(
        providers.socket, "getaddrinfo", lambda host, port: [("", "", 0, "", ("8.8.8.8", 0))]
    )
    async with get_session_factory()() as session:
        await ensure_default_free_ai(session)
        # 用户 u1 建私有 provider
        mine = await create_provider(
            session,
            owner_user_id="u1",
            tenant_id="default",
            provider_type="openai_compatible",
            name="mine",
            base_url="https://api.example.com/v1",
            model="m1",
            api_key="k",
        )
        # 显式指定 → 命中本人私有
        hit = await resolve_provider(session, user_id="u1", tenant_id="default", provider_id=mine.id)
        assert hit is not None and hit.id == mine.id
        # 用户 u2 显式传 u1 的私有 provider_id → 不命中，落到默认免费 AI
        fallback = await resolve_provider(session, user_id="u2", tenant_id="default", provider_id=mine.id)
        assert fallback is not None and fallback.is_default is True
        # 无显式 provider_id → 用户私有优先，其次默认
        assert (await resolve_provider(session, user_id="u1", tenant_id="default")).id == mine.id


@pytest.mark.asyncio
async def test_resolve_skips_disabled_provider(monkeypatch):
    monkeypatch.setattr(
        providers.socket, "getaddrinfo", lambda host, port: [("", "", 0, "", ("8.8.8.8", 0))]
    )
    async with get_session_factory()() as session:
        await ensure_default_free_ai(session)
        mine = await create_provider(
            session,
            owner_user_id="u1",
            tenant_id="default",
            provider_type="openai_compatible",
            name="mine",
            base_url="https://api.example.com/v1",
            model="m1",
            api_key="k",
        )
        mine.enabled = False
        await session.commit()
        # 私有的被停用，应落到默认免费 AI 而不是离线。
        hit = await resolve_provider(session, user_id="u1", tenant_id="default")
        assert hit is not None and hit.is_default is True


class _FakeResponse:
    def __init__(self, status_code: int):
        self.status_code = status_code


class _FakeClient:
    def __init__(self, *args, **kwargs):
        self.last_url = ""
        self.last_headers = {}
        self.status_code = 200

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, json=None, headers=None):
        self.last_url = url
        self.last_headers = headers or {}
        return _FakeResponse(self.status_code)


@pytest.mark.asyncio
async def test_test_provider_openai_compatible_success(monkeypatch):
    fake = _FakeClient()
    monkeypatch.setattr(providers.httpx, "AsyncClient", lambda *a, **k: fake)
    ok, latency_ms, message = await providers.test_provider(
        "https://api.deepseek.com/v1", "sk-x", "deepseek-chat", "openai_compatible"
    )
    assert ok is True
    assert message == "ok"
    assert latency_ms >= 0
    assert fake.last_url == "https://api.deepseek.com/v1/chat/completions"
    assert fake.last_headers["Authorization"] == "Bearer sk-x"


@pytest.mark.asyncio
async def test_test_provider_anthropic_uses_x_api_key(monkeypatch):
    fake = _FakeClient()
    monkeypatch.setattr(providers.httpx, "AsyncClient", lambda *a, **k: fake)
    ok, _, _ = await providers.test_provider(
        "https://api.anthropic.com/v1", "sk-ant", "claude-3-5-haiku-latest", "anthropic"
    )
    assert ok is True
    assert fake.last_url == "https://api.anthropic.com/v1/messages"
    assert fake.last_headers["x-api-key"] == "sk-ant"


@pytest.mark.asyncio
async def test_test_provider_http_error_is_desensitized(monkeypatch):
    fake = _FakeClient()
    fake.status_code = 401
    monkeypatch.setattr(providers.httpx, "AsyncClient", lambda *a, **k: fake)
    ok, _, message = await providers.test_provider(
        "https://api.deepseek.com/v1", "sk-x", "m", "openai_compatible"
    )
    assert ok is False
    assert message == "HTTP 401"  # 不回传错误体，避免泄露 key 细节
    assert "sk-x" not in message
