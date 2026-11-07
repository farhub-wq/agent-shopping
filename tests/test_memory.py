import json

import httpx
import pytest
from harness.core.memory import HashingEmbedder, MemoryManager, OpenAIEmbedder, _compatible, cosine
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


@pytest.mark.asyncio
async def test_openai_embedder_rejects_empty_embedding():
    # 空数组不是合法向量：落盘后无法被 _compatible 接纳，等于污染记忆。
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"embedding": []}]})

    embedder = OpenAIEmbedder("http://embed", "k", "m", transport=httpx.MockTransport(handler))
    with pytest.raises(ApiError):
        await embedder.embed("你好")


def test_cosine_handles_unnormalized_vectors():
    # 第三方端点可能返回未归一化向量：点积会得到 2.0，按定义算余弦应仍为 1.0。
    assert cosine([1.0, 1.0], [2.0, 2.0]) == pytest.approx(1.0)
    assert cosine([1.0, 0.0], [0.0, 5.0]) == pytest.approx(0.0)


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


class _BoomEmbedder:
    name = "boom"

    async def embed(self, text):
        raise RuntimeError("down")


class _StubEmbedder:
    name = "stub"

    async def embed(self, text):
        return [1.0, 0.0]


@pytest.mark.asyncio
async def test_embed_falls_back_to_hashing_on_failure():
    manager = MemoryManager(None, embedder=_BoomEmbedder())
    vector, name = await manager._embed("你好")
    assert name == "hashing-256"
    assert len(vector) == 256


@pytest.mark.asyncio
async def test_embed_uses_primary_when_ok():
    manager = MemoryManager(None, embedder=_StubEmbedder())
    vector, name = await manager._embed("你好")
    assert name == "stub"
    assert vector == [1.0, 0.0]


def test_compatible_guards_dim_mismatch():
    assert _compatible([1.0, 0.0], [0.0, 1.0]) is True
    assert _compatible([1.0, 0.0], [1.0, 0.0, 0.0]) is False
    assert _compatible([1.0, 0.0], []) is False
    assert _compatible([1.0, 0.0], None) is False
