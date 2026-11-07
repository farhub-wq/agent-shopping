import json

import httpx
import pytest
from harness.core.memory import HashingEmbedder, OpenAIEmbedder, cosine
from shop_common.errors import ApiError


@pytest.mark.asyncio
async def test_embedding_is_normalized():
    embedder = HashingEmbedder(128)
    vector = await embedder.embed("SKU-1001 的库存还有多少")
    norm = sum(value * value for value in vector) ** 0.5
    assert 0.99 < norm < 1.01
    assert embedder.name == "hashing-128"


@pytest.mark.asyncio
async def test_similar_sentences_score_higher_than_unrelated():
    embedder = HashingEmbedder(256)
    base = await embedder.embed("我想查询订单的物流进度")
    related = await embedder.embed("帮我看看订单发货到哪里了")
    unrelated = await embedder.embed("这款防晒霜的保质期是多久")
    assert cosine(base, related) > cosine(base, unrelated)


@pytest.mark.asyncio
async def test_deterministic_embedding():
    embedder = HashingEmbedder(64)
    assert await embedder.embed("同样的输入") == await embedder.embed("同样的输入")
    assert embedder.name == "hashing-64"


@pytest.mark.asyncio
async def test_openai_embedder_returns_embedding():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/embeddings"
        body = json.loads(request.content)
        assert body["model"] == "text-embedding-3-small"
        assert body["input"] == "你好"
        return httpx.Response(200, json={"data": [{"embedding": [0.1, 0.2, 0.3]}]})

    embedder = OpenAIEmbedder(
        "http://embed", "k", "text-embedding-3-small", transport=httpx.MockTransport(handler)
    )
    assert await embedder.embed("你好") == [0.1, 0.2, 0.3]
    assert embedder.name == "openai:text-embedding-3-small"


@pytest.mark.asyncio
async def test_openai_embedder_rejects_missing_embedding():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": []})

    embedder = OpenAIEmbedder("http://embed", "k", "m", transport=httpx.MockTransport(handler))
    with pytest.raises(ApiError):
        await embedder.embed("你好")


def test_build_embedder_defaults_to_hashing(monkeypatch):
    import harness.core.memory as mem

    class _S:
        embedding_base_url = ""
        embedding_api_key = ""
        embedding_model = ""
        embedding_dim = 128
        embedding_timeout_seconds = 15.0

    monkeypatch.setattr(mem, "get_settings", lambda: _S)
    embedder = mem.build_embedder()
    assert isinstance(embedder, HashingEmbedder)
    assert embedder.dim == 128


def test_build_embedder_uses_openai_when_configured(monkeypatch):
    import harness.core.memory as mem

    class _S:
        embedding_base_url = "http://embed"
        embedding_api_key = "k"
        embedding_model = "text-embedding-3-small"
        embedding_dim = 128
        embedding_timeout_seconds = 15.0

    monkeypatch.setattr(mem, "get_settings", lambda: _S)
    embedder = mem.build_embedder()
    assert isinstance(embedder, OpenAIEmbedder)
    assert embedder.name == "openai:text-embedding-3-small"
