import json
from datetime import UTC, datetime, timedelta

import harness.core.memory as mem
import httpx
import pytest
from harness.core.llm import Decision
from harness.core.memory import (
    CANDIDATE_LIMIT,
    DEDUP_SCAN_LIMIT,
    HashingEmbedder,
    MemoryManager,
    OpenAIEmbedder,
    _compatible,
    cosine,
)
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


class _FakeCursor:
    """motor 游标的链式替身：sort/limit 留痕，验证查询确实带了上界。"""

    def __init__(self, rows, limits):
        self._rows = rows
        self._limits = limits

    def sort(self, key, direction):
        self._rows = sorted(self._rows, key=lambda row: row[key], reverse=direction == -1)
        return self

    def limit(self, n):
        self._limits.append(n)
        self._rows = self._rows[:n]
        return self

    async def to_list(self, length=None):
        return list(self._rows)


def _row_match(row, key, value):
    # 支持 "metadata.slot" 点键：语义事实的槽位查询依赖嵌套字段过滤。
    if "." in key:
        top, sub = key.split(".", 1)
        nested = row.get(top)
        return isinstance(nested, dict) and nested.get(sub) == value
    return row.get(key) == value


class _FakeCollection:
    """Mongo 集合的内存替身：只实现等值过滤，记录 find/limit 形态供有界性断言。"""

    def __init__(self, rows=None):
        self.rows = list(rows or [])
        self.find_filters: list[dict] = []
        self.limits: list[int] = []
        self.updated: list[tuple[dict, dict]] = []

    async def create_index(self, keys, **kwargs):
        return "idx"

    def find(self, filter=None, *args, **kwargs):
        self.find_filters.append(filter or {})
        matched = [row for row in self.rows if all(_row_match(row, k, v) for k, v in (filter or {}).items())]
        return _FakeCursor(matched, self.limits)

    async def insert_one(self, doc):
        self.rows.append(doc)

    async def delete_many(self, filter):
        self.rows = [row for row in self.rows if not all(_row_match(row, k, v) for k, v in (filter or {}).items())]

    async def update_one(self, filter, update, **kwargs):
        self.updated.append((filter, update))

    async def update_many(self, filter, update, **kwargs):
        self.updated.append((filter, update))


def _record(record_id, user_id="u1", kind="episodic", embedder="hashing-16", importance=0.5, days_old=0, text="记忆"):
    # 测试夹具：字段与 MemoryManager.remember 落盘结构对齐，向量取单位基向量（确定且同维）。
    now = datetime.now(UTC)
    return {
        "_id": record_id,
        "user_id": user_id,
        "kind": kind,
        "text": text,
        "embedding": [1.0] + [0.0] * 15,
        "embedder": embedder,
        "importance": importance,
        "metadata": {},
        "access_count": 0,
        "created_at": now - timedelta(days=days_old),
        "last_access": now,
    }


def _manager_with_collection(collection, monkeypatch):
    # 与既有用例一致：redis 传 None，被测路径不触碰工作记忆。
    manager = MemoryManager(None, embedder=HashingEmbedder(16))
    monkeypatch.setattr(mem, "get_mongo_db", lambda: {mem.MEMORY_COLLECTION: collection})
    return manager


@pytest.mark.asyncio
async def test_candidates_bounded_and_cover_both_lanes(monkeypatch):
    # 低重要性新记录塞满最新通道；高重要性旧记录只能靠重要性通道入围候选。
    rows = [_record(f"r{i}", importance=0.1) for i in range(CANDIDATE_LIMIT + 50)]
    rows.append(_record("old-important", importance=0.99, days_old=60))
    collection = _FakeCollection(rows)
    manager = _manager_with_collection(collection, monkeypatch)

    result = await manager._candidates("u1", "hashing-16")

    assert len(result) <= 2 * CANDIDATE_LIMIT
    assert "old-important" in {item["_id"] for item in result}
    # 两路查询都带 embedder 等值条件且都 limit：证明没有全表扫描。
    assert collection.find_filters == [
        {"user_id": "u1", "embedder": "hashing-16"},
        {"user_id": "u1", "embedder": "hashing-16"},
    ]
    assert collection.limits == [CANDIDATE_LIMIT, CANDIDATE_LIMIT]


@pytest.mark.asyncio
async def test_candidates_isolate_vector_spaces(monkeypatch):
    rows = [
        _record("same-space", embedder="hashing-16"),
        _record("other-space", embedder="openai:text-embedding-3-small"),
    ]
    collection = _FakeCollection(rows)
    manager = _manager_with_collection(collection, monkeypatch)

    result = await manager._candidates("u1", "hashing-16")

    assert [item["_id"] for item in result] == ["same-space"]


@pytest.mark.asyncio
async def test_retrieve_scores_only_bounded_candidates(monkeypatch):
    rows = [_record(f"r{i}", text=f"第{i}条记忆") for i in range(CANDIDATE_LIMIT + 20)]
    collection = _FakeCollection(rows)
    manager = _manager_with_collection(collection, monkeypatch)

    items = await manager.retrieve("u1", "记忆", k=5)

    assert 0 < len(items) <= 5
    # 召回走两路有界候选，且访问统计仍批量回写，不因有界化丢失。
    assert collection.limits == [CANDIDATE_LIMIT, CANDIDATE_LIMIT]
    assert any("$in" in filter.get("_id", {}) for filter, _ in collection.updated)


@pytest.mark.asyncio
async def test_remember_dedup_scans_recent_bounded(monkeypatch):
    rows = [_record(f"r{i}") for i in range(DEDUP_SCAN_LIMIT + 10)]
    collection = _FakeCollection(rows)
    manager = _manager_with_collection(collection, monkeypatch)

    await manager.remember("u1", "全新的记忆", kind="episodic", importance=0.5)

    # 去重查询限定同 kind + 同向量空间，且只扫最近 DEDUP_SCAN_LIMIT 条。
    assert collection.find_filters == [{"user_id": "u1", "kind": "episodic", "embedder": "hashing-16"}]
    assert collection.limits == [DEDUP_SCAN_LIMIT]
    assert len(collection.rows) == DEDUP_SCAN_LIMIT + 11  # 无近重复，正常新增


@pytest.mark.asyncio
async def test_remember_dedup_hits_recent_duplicate(monkeypatch):
    existing = _record("r0", text="用户喜欢黑色")
    existing["embedding"] = await HashingEmbedder(16).embed("用户喜欢黑色")
    collection = _FakeCollection([existing])
    manager = _manager_with_collection(collection, monkeypatch)

    record_id = await manager.remember("u1", "用户喜欢黑色", kind="episodic", importance=0.9)

    # 同文向量余弦为 1：命中去重，抬升重要性并复用原记录，不新增。
    assert record_id == "r0"
    assert len(collection.rows) == 1
    assert collection.updated[0][0] == {"_id": "r0"}


class _ExtractProvider:
    """LLM 抽取的 provider 替身：decide 固定返回指定 content。"""

    name = "fake-extractor"

    def __init__(self, content):
        self._content = content

    async def decide(self, system, messages, tools):
        return Decision(kind="final", content=self._content, model="fake")


@pytest.mark.asyncio
async def test_extract_semantic_regex_only_without_provider(monkeypatch):
    collection = _FakeCollection()
    manager = _manager_with_collection(collection, monkeypatch)

    facts = await manager.extract_semantic("u1", "我叫小王，我住在上海浦东")

    assert facts == ["称呼：小王", "地址：上海浦东"]
    slots = {row["metadata"].get("slot") for row in collection.rows if row["kind"] == "semantic"}
    assert slots == {"称呼", "地址"}


@pytest.mark.asyncio
async def test_llm_extraction_merges_and_stores_slot_facts(monkeypatch):
    collection = _FakeCollection()
    manager = _manager_with_collection(collection, monkeypatch)
    provider = _ExtractProvider('[{"slot": "收货时间", "text": "周末不方便收货"}, {"text": "偏好顺丰快递"}]')

    facts = await manager.extract_semantic("u1", "我一般周末不方便收货，发顺丰吧", provider=provider)

    assert "周末不方便收货" in facts
    assert "偏好顺丰快递" in facts
    slot_row = next(row for row in collection.rows if row["metadata"].get("slot") == "收货时间")
    assert slot_row["importance"] == 0.85
    # 无 slot 事实走 remember 路径，同样入库。
    assert any(row["text"] == "偏好顺丰快递" for row in collection.rows)


@pytest.mark.asyncio
async def test_llm_extraction_falls_back_to_regex_on_unparseable_output(monkeypatch):
    collection = _FakeCollection()
    manager = _manager_with_collection(collection, monkeypatch)
    # 模拟模型降级话术/自由文本：解析失败必须静默退回纯正则，不劣于纯规则方案。
    provider = _ExtractProvider("当前咨询量较大，暂时无法处理。")

    facts = await manager.extract_semantic("u1", "我叫小李", provider=provider)

    assert facts == ["称呼：小李"]


@pytest.mark.asyncio
async def test_slot_conflict_replaces_old_fact(monkeypatch):
    collection = _FakeCollection()
    manager = _manager_with_collection(collection, monkeypatch)

    await manager.extract_semantic("u1", "我喜欢吃辣")
    await manager.extract_semantic("u1", "我喜欢清淡饮食")

    semantic = [
        row for row in collection.rows if row["kind"] == "semantic" and row["metadata"].get("slot") == "偏好"
    ]
    # 同槽位冲突替换：库里只剩新事实，不再新旧并存。
    assert len(semantic) == 1
    assert semantic[0]["text"] == "偏好：清淡饮食"
