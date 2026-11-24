"""P2 长对话治理的单测：归档压缩往返、游标、拆分判定与摘要回灌。

这些函数不依赖数据库/Redis，直接用假 Redis 验证工作记忆注入行为。
"""

import json
from datetime import UTC, datetime, timedelta

import pytest
from harness.core.conversation import (
    compress_records,
    conversation_state,
    decode_cursor,
    decompress_records,
    encode_cursor,
    should_split,
)
from harness.core.memory import HashingEmbedder, MemoryManager


class _Redis:
    """只模拟工作记忆用到的列表命令；key -> 最新在前的消息列表。"""

    def __init__(self):
        self.values: dict[str, list[str]] = {}

    async def llen(self, key):
        return len(self.values.get(key, []))

    async def lrange(self, key, start, end):
        items = self.values.get(key, [])
        return items[start:] if end == -1 else items[start : end + 1]

    async def lpush(self, key, value):
        self.values.setdefault(key, []).insert(0, value)

    async def ltrim(self, key, start, end):
        self.values[key] = self.values.get(key, [])[start : end + 1]

    async def expire(self, key, ttl):
        return True


def test_archive_payload_roundtrip():
    records = [
        {"id": "m1", "role": "user", "content": "查订单 SO-123", "seq": 1},
        {"id": "m2", "role": "assistant", "content": "已发货", "seq": 2},
    ]

    payload = compress_records(records)

    assert decompress_records(payload) == records


def test_decompress_invalid_payload_returns_empty():
    # 坏归档不能抛异常阻断整页历史，统一降级为空列表。
    assert decompress_records("not-base64") == []
    assert decompress_records("") == []


def test_cursor_roundtrip_and_invalid():
    created_at = datetime(2026, 11, 24, 10, 30, tzinfo=UTC)

    cursor = encode_cursor(created_at, "m-1")

    decoded = decode_cursor(cursor)
    assert decoded is not None
    assert decoded[0] == created_at
    assert decoded[1] == "m-1"
    assert decode_cursor("bad-cursor") is None
    assert decode_cursor(None) is None


def test_conversation_state_follows_context_ratios():
    assert conversation_state(100, 1000) == "ok"
    assert conversation_state(850, 1000) == "warn"
    assert conversation_state(900, 1000) == "split"
    assert conversation_state(960, 1000) == "hard"


def test_should_split_reason_context_and_idle():
    now = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)

    # 累计 token 到 90% 窗口 → context。
    assert (
        should_split(
            900,
            now,
            now=now,
            window=1000,
            split_ratio=0.9,
            idle_hours=24,
        )
        == "context"
    )
    # 未到比例但空闲超过 24h → idle；naive datetime 也要能比较。
    naive_last = (now - timedelta(hours=25)).replace(tzinfo=None)
    assert (
        should_split(
            10,
            naive_last,
            now=now,
            window=1000,
            split_ratio=0.9,
            idle_hours=24,
        )
        == "idle"
    )
    assert (
        should_split(
            10,
            now - timedelta(hours=1),
            now=now,
            window=1000,
            split_ratio=0.9,
            idle_hours=24,
        )
        is None
    )


@pytest.mark.asyncio
async def test_seed_handoff_only_when_working_memory_empty():
    redis = _Redis()
    manager = MemoryManager(redis, embedder=HashingEmbedder(16))

    injected = await manager.seed_handoff("u1", "c1", "用户关注订单 SO-123", ["SO-123"])
    assert injected is True

    messages = await manager.working_messages("u1", "c1")
    assert messages[0]["role"] == "system"
    assert "会话摘要" in messages[0]["content"]
    assert "SO-123" in messages[0]["content"]

    # 已有工作记忆时不再覆盖，避免把旧摘要盖到正在进行的对话上。
    assert await manager.seed_handoff("u1", "c1", "另一段摘要") is False
    assert len(redis.values[manager._work_key("u1", "c1")]) == 1
    # 空摘要不注入无意义的 system 消息。
    assert await manager.seed_handoff("u2", "c2", "") is False


def test_conversation_summary_keeps_pinned_facts():
    # 拆分交接复用 P1 的合并逻辑：关键事实（订单号/SKU）永不丢弃。
    from harness.core.memory import merge_session_summary

    text, meta = merge_session_summary(
        None,
        None,
        [
            {"role": "user", "content": "订单 SO-777 什么时候到"},
            {"role": "assistant", "content": "明天送达"},
        ],
        max_tokens=200,
    )

    assert "SO-777" in text
    assert json.dumps(meta["pinned"], ensure_ascii=False).find("SO-777") >= 0
