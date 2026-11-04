from harness.core.memory import HashingEmbedder, cosine


def test_embedding_is_normalized():
    embedder = HashingEmbedder(128)
    vector = embedder.embed("SKU-1001 的库存还有多少")
    norm = sum(value * value for value in vector) ** 0.5
    assert 0.99 < norm < 1.01


def test_similar_sentences_score_higher_than_unrelated():
    embedder = HashingEmbedder(256)
    base = embedder.embed("我想查询订单的物流进度")
    related = embedder.embed("帮我看看订单发货到哪里了")
    unrelated = embedder.embed("这款防晒霜的保质期是多久")
    assert cosine(base, related) > cosine(base, unrelated)


def test_deterministic_embedding():
    embedder = HashingEmbedder(64)
    assert embedder.embed("同样的输入") == embedder.embed("同样的输入")
