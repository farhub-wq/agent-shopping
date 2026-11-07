import pytest
from harness.core.memory import HashingEmbedder, cosine


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
