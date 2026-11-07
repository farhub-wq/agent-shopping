"""三层记忆框架（自研）：工作记忆 / 情节记忆 / 语义事实。

- 工作记忆：Redis 列表，保留最近若干轮对话，控制上下文成本。
- 情节记忆：向量化后存 MongoDB，两段式有界候选（重要性/最新各 Top-N）内
  按「相似度 + 重要性 + 时间衰减」混合精排，成本与记忆总量脱钩。
- 语义记忆：从对话中抽取稳定事实（称呼、偏好、地址、过敏史），高权重长期保留。

默认用确定性哈希 n-gram 向量，离线可用；配置 embedding 端点后自动切换为真实语义向量，
端点故障时回退哈希向量，并按向量维度隔离新旧两个向量空间。
"""

import hashlib
import json
import math
import re
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx
from redis.asyncio import Redis
from shop_common.config import get_settings
from shop_common.errors import ApiError
from shop_common.logging import get_logger
from shop_common.metrics import EMBEDDING_FALLBACK
from shop_common.mongo import get_mongo_db

settings = get_settings()
logger = get_logger(__name__)

# 记忆集合名与索引集中在这里：Mongo 没有 schema 迁移，索引需在首次使用时幂等补建。
MEMORY_COLLECTION = "memory_records"

# 有界召回：两路候选（重要性/最新）各取 CANDIDATE_LIMIT 条，合并去重后 Python 精排，
# 单次召回成本与全量记录数脱钩；两路覆盖的正是混合权重里的重要性与新鲜度维度。
CANDIDATE_LIMIT = 100
# 去重只防「近重复」，扫描最近写入的若干条同 kind 记录即可，往前翻没有收益。
DEDUP_SCAN_LIMIT = 50


class Embedder:
    """嵌入器统一接口：name 标识产出的向量空间，embed 为异步接口（真实模型走网络）。"""

    name: str = "base"

    async def embed(self, text: str) -> list[float]:
        raise NotImplementedError


class HashingEmbedder(Embedder):
    """字符 n-gram 哈希向量：稳定、无外部依赖，足以做同义句去重与召回演示。"""

    def __init__(self, dim: int = 256) -> None:
        self.dim = dim
        self.name = f"hashing-{dim}"

    async def embed(self, text: str) -> list[float]:
        return self._embed_sync(text)

    def _embed_sync(self, text: str) -> list[float]:
        vector = [0.0] * self.dim
        cleaned = re.sub(r"\s+", " ", text.lower().strip())
        for size in (1, 2, 3):
            for index in range(max(len(cleaned) - size + 1, 1)):
                gram = cleaned[index : index + size]
                # 每个 gram 做三次独立投影，降低哈希碰撞带来的伪相似度
                for seed in range(3):
                    digest = hashlib.blake2b(
                        f"{seed}:{gram}".encode(), digest_size=8
                    ).digest()
                    slot = int.from_bytes(digest[:4], "big") % self.dim
                    sign = 1.0 if digest[4] % 2 == 0 else -1.0
                    vector[slot] += sign * size
        norm = math.sqrt(sum(value * value for value in vector)) or 1.0
        return [round(value / norm, 6) for value in vector]


def cosine(left: list[float], right: list[float]) -> float:
    # 按定义计算 cos = dot / (|a||b|)：哈希向量自带归一化，但「OpenAI 兼容」的第三方
    # 端点可能返回未归一化向量，只做点积会把相似度放大到 1 以上，破坏去重阈值与召回权重。
    if not left or not right:
        return 0.0
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if not left_norm or not right_norm:
        return 0.0
    return dot / (left_norm * right_norm)


class OpenAIEmbedder(Embedder):
    """OpenAI 兼容 /embeddings 端点的真实语义向量；transport 仅用于单测注入 MockTransport。"""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        timeout: float = 15.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.transport = transport
        self.name = f"openai:{model}"

    async def embed(self, text: str) -> list[float]:
        payload = {"model": self.model, "input": text}
        async with httpx.AsyncClient(timeout=self.timeout, transport=self.transport) as client:
            response = await client.post(
                f"{self.base_url}/embeddings",
                json=payload,
                headers={"Authorization": f"Bearer {self.api_key}"},
            )
            response.raise_for_status()
            body = response.json()
        data = body.get("data") or []
        embedding = data[0].get("embedding") if data and isinstance(data[0], dict) else None
        # 端点缺向量或返回空数组都属坏响应：抛 ApiError 让调用方感知，
        # 而不是静默存空向量——空向量无法召回，落盘即污染记忆。
        if not isinstance(embedding, list) or not embedding:
            raise ApiError("EMBEDDING_FAILED", "嵌入端点返回缺少 embedding 向量")
        return embedding


SEMANTIC_PATTERNS = [
    (re.compile(r"(?:我叫|叫我)([^\s，。]{1,8})"), "称呼"),
    (re.compile(r"(?:我住在|我的地址是)([^。]{3,30})"), "地址"),
    (re.compile(r"(?:我喜欢|我偏好)([^。]{2,20})"), "偏好"),
    (re.compile(r"(?:我对)([^。]{1,10})(?:过敏)"), "过敏"),
]

# LLM 抽取的系统提示：只收用户明确陈述的稳定事实，限定输出契约便于严格解析。
_EXTRACT_SYSTEM = (
    "你是事实抽取器。从用户的话里抽取值得长期记住的稳定事实"
    "（称呼、偏好、尺码、联系方式、地址、过敏史等），只抽取用户明确说出的，禁止推测。\n"
    '只输出 JSON 数组，每项形如 {"slot": "类别", "text": "事实陈述"}；'
    "没有可抽取的事实就输出 []。不要输出任何其他文字。\n"
    "slot 用简短中文类别名；text 用简洁陈述；最多 3 条。"
)
# 从模型产出里定位 JSON 数组：模型可能在前后包一层废话，取第一个 [...] 块。
_FACT_JSON = re.compile(r"\[.*\]", re.DOTALL)
_LLM_FACT_LIMIT = 3
_FACT_TEXT_LIMIT = 80
_FACT_SLOT_LIMIT = 16


@dataclass(frozen=True)
class _Fact:
    """一条待存语义事实：slot 为归一化类别（空串表示无类别，走去重路径）。"""

    slot: str
    text: str


def _compatible(query: list[float], stored: list[float] | None) -> bool:
    # 换 embedder 后新旧向量维度不同，cosine 用 zip 截断会得到伪相似度；
    # 维度不一致的旧向量直接跳过，宁可少召回也不错召回。
    return bool(stored) and len(stored) == len(query)


class MemoryManager:
    def __init__(self, redis: Redis, namespace: str = "shop", embedder: Embedder | None = None) -> None:
        self.redis = redis
        self.namespace = namespace
        self.embedder = embedder or build_embedder()
        # 主嵌入失败时的离线保底：哈希向量与主嵌入可能不同维，落盘时标签记录实际来源。
        self._fallback = HashingEmbedder(settings.embedding_dim)
        self._indexed = False

    async def _embed(self, text: str) -> tuple[list[float], str]:
        # 嵌入是增强路径不是关键路径：失败回退哈希并打点，保证记忆写入/召回不因端点故障 5xx。
        try:
            return await self.embedder.embed(text), self.embedder.name
        except Exception as exc:
            logger.warning("嵌入失败，回退哈希向量：%s", exc)
            EMBEDDING_FALLBACK.labels("harness").inc()
            return await self._fallback.embed(text), self._fallback.name

    def _collection(self):
        return get_mongo_db()[MEMORY_COLLECTION]

    async def _ensure_indexes(self) -> None:
        """首次访问时补建索引；create_index 幂等，重复调用不会报错。"""
        if self._indexed:
            return
        collection = self._collection()
        # (user_id, kind, created_at) 支撑去重时按用户+类型取最近记录；
        # (user_id, created_at) 支撑列表的时间排序；
        # (user_id, embedder, importance|created_at) 支撑有界召回的两路候选：
        # embedder 等值过滤在索引层隔离向量空间，排序不回表全扫。
        await collection.create_index([("user_id", 1), ("kind", 1), ("created_at", -1)])
        await collection.create_index([("user_id", 1), ("created_at", -1)])
        await collection.create_index([("user_id", 1), ("embedder", 1), ("importance", -1)])
        await collection.create_index([("user_id", 1), ("embedder", 1), ("created_at", -1)])
        self._indexed = True

    def _work_key(self, session_id: str) -> str:
        return f"{self.namespace}:mem:work:{session_id}"

    async def append_turn(self, session_id: str, role: str, content: str, max_turns: int = 12) -> None:
        # 工作记忆只保留最近 max_turns 轮并设 24h 过期：
        # 上下文成本有上界，也避免 Redis key 无限增长。
        await self.redis.lpush(
            self._work_key(session_id), json.dumps({"role": role, "content": content}, ensure_ascii=False)
        )
        await self.redis.ltrim(self._work_key(session_id), 0, max_turns - 1)
        await self.redis.expire(self._work_key(session_id), 86400)

    async def working_messages(self, session_id: str, limit: int = 8) -> list[dict[str, str]]:
        raw = await self.redis.lrange(self._work_key(session_id), 0, limit - 1)
        return [json.loads(item) for item in reversed(raw)]

    async def extract_semantic(
        self, user_id: str, text: str, tenant: str = "default", provider: Any = None
    ) -> list[str]:
        """双路抽取：正则（免费高精度）+ 可选 LLM（覆盖非模板表述）。

        事实带 slot 落库：同槽位旧事实整条替换（冲突检测），无槽位走去重路径。
        返回存储的事实文本列表。
        """
        facts: list[_Fact] = []
        for pattern, label in SEMANTIC_PATTERNS:
            match = pattern.search(text)
            if match:
                facts.append(_Fact(slot=label, text=f"{label}：{match.group(1).strip()}"))
        if provider is not None:
            seen = {fact.text.strip().lower() for fact in facts}
            for fact in await self._llm_extract(provider, text):
                if fact.text.strip().lower() not in seen:
                    facts.append(fact)
        stored: list[str] = []
        for fact in facts:
            await self._store_fact(user_id, fact, tenant)
            stored.append(fact.text)
        return stored

    async def _llm_extract(self, provider: Any, text: str) -> list[_Fact]:
        """LLM 抽取并严格解析：任何失败（含模型降级话术）都退回空列表，正则结果兜底。"""
        try:
            decision = await provider.decide(
                _EXTRACT_SYSTEM, [{"role": "user", "content": text[:500]}], []
            )
            match = _FACT_JSON.search(decision.content or "")
            payload = json.loads(match.group(0)) if match else []
        except Exception as exc:
            logger.warning("LLM 语义抽取失败，退回纯正则结果：%s", exc)
            return []
        facts: list[_Fact] = []
        if isinstance(payload, list):
            for item in payload[:_LLM_FACT_LIMIT]:
                if isinstance(item, dict) and isinstance(item.get("text"), str) and item["text"].strip():
                    slot = item.get("slot") if isinstance(item.get("slot"), str) else ""
                    facts.append(
                        _Fact(slot=slot.strip()[:_FACT_SLOT_LIMIT], text=item["text"].strip()[:_FACT_TEXT_LIMIT])
                    )
        return facts

    async def _store_fact(self, user_id: str, fact: _Fact, tenant: str) -> None:
        """落库一条事实：语义事实高权重（0.85），保证称呼/地址/偏好优先被召回。"""
        if not fact.slot:
            await self.remember(user_id, fact.text, kind="semantic", importance=0.85, tenant=tenant)
            return
        # 同槽位即冲突：旧事实整条作废后写新事实（delete+insert 非原子，
        # 崩溃最坏丢一条下轮可再抽取的事实，不会产生双事实并存）。
        embedding, embedder_name = await self._embed(fact.text)
        await self._ensure_indexes()
        collection = self._collection()
        await collection.delete_many(
            {"user_id": user_id, "kind": "semantic", "metadata.slot": fact.slot}
        )
        now = datetime.now(UTC)
        await collection.insert_one(
            {
                "_id": str(uuid.uuid4()),
                "user_id": user_id,
                "kind": "semantic",
                "text": fact.text,
                "embedding": embedding,
                "embedder": embedder_name,
                "importance": 0.85,
                "metadata": {"tenant": tenant, "slot": fact.slot},
                "access_count": 0,
                "created_at": now,
                "last_access": now,
            }
        )

    async def remember(
        self,
        user_id: str,
        text: str,
        *,
        kind: str = "episodic",
        importance: float = 0.5,
        tenant: str = "default",
        metadata: dict[str, Any] | None = None,
    ) -> str:
        embedding, embedder_name = await self._embed(text)
        await self._ensure_indexes()
        now = datetime.now(UTC)
        collection = self._collection()
        # 去重只在同一向量空间内、最近写入的若干条里找近重复：
        # embedder 等值条件顺带给出空间隔离，limit 让扫描成本与总量脱钩。
        recent = await (
            collection.find({"user_id": user_id, "kind": kind, "embedder": embedder_name})
            .sort("created_at", -1)
            .limit(DEDUP_SCAN_LIMIT)
            .to_list(length=DEDUP_SCAN_LIMIT)
        )
        for record in recent:
            stored = record.get("embedding") or []
            # 查询已按 embedder 隔离空间，这里仍按维度兜底，防手工写入的脏数据。
            if not _compatible(embedding, stored):
                continue
            # 相似度 ≥ 0.92 视为同一条记忆：用新文本覆盖并提升重要性，
            # 既去重又保留最新表述，避免同一事实反复膨胀检索结果。
            if cosine(embedding, stored) >= 0.92:
                await collection.update_one(
                    {"_id": record["_id"]},
                    {
                        "$set": {
                            "text": text,
                            "importance": max(record.get("importance", 0.0), importance),
                            "last_access": now,
                        }
                    },
                )
                return str(record["_id"])
        memory_id = str(uuid.uuid4())
        await collection.insert_one(
            {
                "_id": memory_id,
                "user_id": user_id,
                "kind": kind,
                "text": text,
                "embedding": embedding,
                # 记录实际产出向量的嵌入器，换模型后可按此审计并区分新旧向量空间。
                "embedder": embedder_name,
                "importance": importance,
                # metadata 里带上租户：多租户接入后仍可按租户隔离记忆
                "metadata": {**(metadata or {}), "tenant": tenant},
                "access_count": 0,
                "created_at": now,
                "last_access": now,
            }
        )
        return memory_id

    async def _candidates(
        self, user_id: str, embedder_name: str, limit: int = CANDIDATE_LIMIT
    ) -> list[dict[str, Any]]:
        """两段式有界召回：重要性 Top-N 与最新 Top-N 两路候选合并去重。

        不做全表扫描：候选集最大 2*limit 条，与用户的记忆总量脱钩。
        漏检只可能发生在「极旧且低重要性」的记录上，而这类记录即使召回，
        在混合权重下也排不进 top-k，不影响实际结果。
        """
        base = {"user_id": user_id, "embedder": embedder_name}
        collection = self._collection()
        by_importance = await (
            collection.find(base).sort("importance", -1).limit(limit).to_list(length=limit)
        )
        by_recency = await (
            collection.find(base).sort("created_at", -1).limit(limit).to_list(length=limit)
        )
        merged: dict[Any, dict[str, Any]] = {}
        for record in [*by_importance, *by_recency]:
            merged.setdefault(record["_id"], record)
        return list(merged.values())

    async def retrieve(
        self, user_id: str, query: str, *, k: int = 5, token_budget: int = 900
    ) -> list[dict[str, Any]]:
        query_vector, embedder_name = await self._embed(query)
        now_ts = time.time()
        await self._ensure_indexes()
        rows = await self._candidates(user_id, embedder_name)
        scored: list[tuple[float, dict[str, Any]]] = []
        for record in rows:
            stored = record.get("embedding") or []
            # 查询已按 embedder 隔离空间，这里仍按维度兜底，防手工写入的脏数据。
            if not _compatible(query_vector, stored):
                continue
            similarity = cosine(query_vector, stored)
            created_at = record.get("created_at")
            age_days = max((now_ts - created_at.timestamp()) / 86400, 0) if created_at else 0
            # 时间衰减尺度为 7 天：exp(-age/7)，越久权重越低。
            recency = math.exp(-age_days / 7)
            # 混合打分固定权重：相关性 0.6 / 重要性 0.25 / 新鲜度 0.15；
            # 相关性仍是主导项，重要事实可跨越时间被召回，但不会压过查询意图。
            score = 0.6 * similarity + 0.25 * record.get("importance", 0.0) + 0.15 * recency
            scored.append((score, record))
        scored.sort(key=lambda item: item[0], reverse=True)

        selected: list[dict[str, Any]] = []
        touched: list[str] = []
        used = 0
        for score, record in scored:
            # 按字符数粗估 token（中文约 2 字符/token）；装不下的条目跳过而非中断，
            # 让后面的短记忆仍有机会占满预算。
            cost = max(len(record.get("text", "")) // 2, 1)
            if used + cost > token_budget:
                continue
            touched.append(record["_id"])
            selected.append(
                {
                    "id": str(record["_id"]),
                    "kind": record.get("kind"),
                    "text": record.get("text"),
                    "score": round(score, 4),
                    "importance": record.get("importance"),
                }
            )
            used += cost
            if len(selected) >= k:
                break
        if touched:
            # 批量回写访问统计，避免在一次召回里逐条 update。
            await self._collection().update_many(
                {"_id": {"$in": touched}},
                {"$inc": {"access_count": 1}, "$set": {"last_access": datetime.now(UTC)}},
            )
        return selected

    async def summarize_session(self, session_id: str, user_id: str, keep_turns: int = 4) -> str | None:
        """把较早的工作记忆压缩成情节记忆，只保留最近若干轮原文。"""
        messages = await self.working_messages(session_id, limit=24)
        if len(messages) < keep_turns + 2:
            return None
        # 早于 keep_turns 的轮次压成一条摘要落情节记忆，Redis 里只留最近的原文，
        # 兼顾长期可追溯与短期上下文成本。
        older = messages[:-keep_turns]
        summary = "；".join(f"{item['role']}:{item['content'][:60]}" for item in older)[:500]
        await self.remember(
            user_id, f"会话摘要：{summary}", kind="episodic", importance=0.6,
            metadata={"session_id": session_id, "summarized": True},
        )
        await self.redis.ltrim(self._work_key(session_id), 0, keep_turns - 1)
        return summary

    async def list_records(self, user_id: str) -> list[dict[str, Any]]:
        await self._ensure_indexes()
        rows = (
            await self._collection()
            .find({"user_id": user_id})
            .sort("created_at", -1)
            .to_list(length=None)
        )
        return [
            {
                "id": str(row["_id"]),
                "kind": row.get("kind"),
                "text": row.get("text"),
                "importance": row.get("importance"),
                "created_at": row["created_at"].isoformat(),
                "access_count": row.get("access_count", 0),
            }
            for row in rows
        ]


def build_embedder() -> Embedder:
    """按配置选择嵌入器：base_url 与 api_key 齐备才启用真实嵌入，否则退回离线哈希。

    与 build_provider 同一策略——缺任一配置都不依赖外部网络，本地演示与 CI 可离线跑。
    """
    settings = get_settings()
    if not (settings.embedding_base_url and settings.embedding_api_key):
        return HashingEmbedder(settings.embedding_dim)
    return OpenAIEmbedder(
        settings.embedding_base_url,
        settings.embedding_api_key,
        settings.embedding_model,
        timeout=settings.embedding_timeout_seconds,
    )
