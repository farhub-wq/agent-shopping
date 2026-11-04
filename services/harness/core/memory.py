"""三层记忆框架（自研）：工作记忆 / 情节记忆 / 语义事实。

- 工作记忆：Redis 列表，保留最近若干轮对话，控制上下文成本。
- 情节记忆：向量化后落库，按「相似度 + 重要性 + 时间衰减」混合召回。
- 语义记忆：从对话中抽取稳定事实（称呼、偏好、地址、过敏史），高权重长期保留。

向量由确定性哈希 n-gram 生成，离线可用；配置模型后可替换为真实 embedding。
"""

import hashlib
import json
import math
import re
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON, Float, Integer
from sqlalchemy import DateTime, String, Text

from shop_common.config import get_settings
from shop_common.db import Base, get_session_factory

settings = get_settings()


class MemoryRecord(Base):
    __tablename__ = "memory_records"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    kind: Mapped[str] = mapped_column(String(16), index=True)  # episodic | semantic
    text: Mapped[str] = mapped_column(Text)
    embedding: Mapped[list] = mapped_column(JSON)
    importance: Mapped[float] = mapped_column(Float, default=0.5)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)
    access_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    last_access: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


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
                        f"{seed}:{gram}".encode("utf-8"), digest_size=8
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
        async with get_session_factory()() as session:
            recent = (
                (
                    await session.execute(
                        select(MemoryRecord).where(
                            MemoryRecord.user_id == user_id, MemoryRecord.kind == kind
                        )
                    )
                )
                .scalars()
                .all()
            )
            for record in recent:
                # 相似度 ≥ 0.92 视为同一条记忆：用新文本覆盖并提升重要性，
                # 既去重又保留最新表述，避免同一事实反复膨胀检索结果。
                if cosine(embedding, record.embedding or []) >= 0.92:
                    record.text = text
                    record.importance = max(record.importance, importance)
                    record.last_access = datetime.now(timezone.utc)
                    await session.commit()
                    return record.id
            record = MemoryRecord(
                id=str(uuid.uuid4()),
                user_id=user_id,
                kind=kind,
                text=text,
                embedding=embedding,
                importance=importance,
                metadata_json={**(metadata or {}), "tenant": tenant},
            )
            session.add(record)
            await session.commit()
            return record.id

    async def retrieve(
        self, user_id: str, query: str, *, k: int = 5, token_budget: int = 900
    ) -> list[dict[str, Any]]:
        query_vector = self.embedder.embed(query)
        now = time.time()
        async with get_session_factory()() as session:
            rows = (
                (
                    await session.execute(
                        select(MemoryRecord).where(MemoryRecord.user_id == user_id)
                    )
                )
                .scalars()
                .all()
            )
            scored: list[tuple[float, MemoryRecord]] = []
            for record in rows:
                similarity = cosine(query_vector, record.embedding or [])
                age_days = max((now - record.created_at.timestamp()) / 86400, 0)
                # 时间衰减尺度为 7 天：exp(-age/7)，越久权重越低。
                recency = math.exp(-age_days / 7)
                # 混合打分固定权重：相关性 0.6 / 重要性 0.25 / 新鲜度 0.15；
                # 相关性仍是主导项，重要事实可跨越时间被召回，但不会压过查询意图。
                score = 0.6 * similarity + 0.25 * record.importance + 0.15 * recency
                scored.append((score, record))
            scored.sort(key=lambda item: item[0], reverse=True)

            selected: list[dict[str, Any]] = []
            used = 0
            for score, record in scored:
                # 按字符数粗估 token（中文约 2 字符/token）；装不下的条目跳过而非中断，
                # 让后面的短记忆仍有机会占满预算。
                cost = max(len(record.text) // 2, 1)
                if used + cost > token_budget:
                    continue
                record.access_count += 1
                record.last_access = datetime.now(timezone.utc)
                selected.append(
                    {
                        "id": record.id,
                        "kind": record.kind,
                        "text": record.text,
                        "score": round(score, 4),
                        "importance": record.importance,
                    }
                )
                used += cost
                if len(selected) >= k:
                    break
            await session.commit()
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
        async with get_session_factory()() as session:
            rows = (
                (
                    await session.execute(
                        select(MemoryRecord)
                        .where(MemoryRecord.user_id == user_id)
                        .order_by(MemoryRecord.created_at.desc())
                    )
                )
                .scalars()
                .all()
            )
            return [
                {
                    "id": row.id,
                    "kind": row.kind,
                    "text": row.text,
                    "importance": row.importance,
                    "created_at": row.created_at.isoformat(),
                    "access_count": row.access_count,
                }
                for row in rows
            ]
