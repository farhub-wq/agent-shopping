"""三层记忆框架（自研）：工作记忆 / 情节记忆 / 语义事实。

- 工作记忆：Redis 列表，保留最近若干轮对话，控制上下文成本。
- 情节记忆：向量化后存 MongoDB，按「相似度 + 重要性 + 时间衰减」混合召回。
- 语义记忆：从对话中抽取稳定事实（称呼、偏好、地址、过敏史），高权重长期保留。

向量由确定性哈希 n-gram 生成，离线可用；配置模型后可替换为真实 embedding。
"""

import hashlib
import json
import math
import re
import time
import uuid
from datetime import UTC, datetime
from typing import Any

from redis.asyncio import Redis
from shop_common.config import get_settings
from shop_common.mongo import get_mongo_db

settings = get_settings()

# 记忆集合名与索引集中在这里：Mongo 没有 schema 迁移，索引需在首次使用时幂等补建。
MEMORY_COLLECTION = "memory_records"


class HashingEmbedder:
    """字符 n-gram 哈希向量：稳定、无外部依赖，足以做同义句去重与召回演示。"""

    def __init__(self, dim: int = 256) -> None:
        self.dim = dim

    def embed(self, text: str) -> list[float]:
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
    if not left or not right:
        return 0.0
    return sum(a * b for a, b in zip(left, right))


SEMANTIC_PATTERNS = [
    (re.compile(r"(?:我叫|叫我)([^\s，。]{1,8})"), "称呼"),
    (re.compile(r"(?:我住在|我的地址是)([^。]{3,30})"), "地址"),
    (re.compile(r"(?:我喜欢|我偏好)([^。]{2,20})"), "偏好"),
    (re.compile(r"(?:我对)([^。]{1,10})(?:过敏)"), "过敏"),
]


class MemoryManager:
    def __init__(self, redis: Redis, namespace: str = "shop") -> None:
        self.redis = redis
        self.namespace = namespace
        self.embedder = HashingEmbedder(settings.embedding_dim)
        self._indexed = False

    def _collection(self):
        return get_mongo_db()[MEMORY_COLLECTION]

    async def _ensure_indexes(self) -> None:
        """首次访问时补建索引；create_index 幂等，重复调用不会报错。"""
        if self._indexed:
            return
        collection = self._collection()
        # (user_id, kind, created_at) 支撑去重时按用户+类型取最近记录；
        # (user_id, created_at) 支撑列表与召回的时间排序。
        await collection.create_index([("user_id", 1), ("kind", 1), ("created_at", -1)])
        await collection.create_index([("user_id", 1), ("created_at", -1)])
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

    async def extract_semantic(self, user_id: str, text: str, tenant: str = "default") -> list[str]:
        facts = []
        for pattern, label in SEMANTIC_PATTERNS:
            match = pattern.search(text)
            if match:
                fact = f"{label}：{match.group(1).strip()}"
                facts.append(fact)
                # 语义事实高权重入库（0.85），保证称呼/地址/偏好这类信息优先被召回。
                await self.remember(user_id, fact, kind="semantic", importance=0.85, tenant=tenant)
        return facts

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
        embedding = self.embedder.embed(text)
        await self._ensure_indexes()
        now = datetime.now(UTC)
        collection = self._collection()
        recent = await collection.find({"user_id": user_id, "kind": kind}).to_list(length=None)
        for record in recent:
            # 相似度 ≥ 0.92 视为同一条记忆：用新文本覆盖并提升重要性，
            # 既去重又保留最新表述，避免同一事实反复膨胀检索结果。
            if cosine(embedding, record.get("embedding") or []) >= 0.92:
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
                "importance": importance,
                # metadata 里带上租户：多租户接入后仍可按租户隔离记忆
                "metadata": {**(metadata or {}), "tenant": tenant},
                "access_count": 0,
                "created_at": now,
                "last_access": now,
            }
        )
        return memory_id

    async def retrieve(
        self, user_id: str, query: str, *, k: int = 5, token_budget: int = 900
    ) -> list[dict[str, Any]]:
        query_vector = self.embedder.embed(query)
        now_ts = time.time()
        await self._ensure_indexes()
        rows = await self._collection().find({"user_id": user_id}).to_list(length=None)
        scored: list[tuple[float, dict[str, Any]]] = []
        for record in rows:
            similarity = cosine(query_vector, record.get("embedding") or [])
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
