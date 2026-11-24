"""长对话治理的纯函数工具：归档压缩、游标分页与拆分判定。

放在 core 层且不依赖 ORM/Redis，便于单测直接覆盖往返与边界；
数据库读写留在 app.py，避免工具函数耦合事务细节。
"""

from __future__ import annotations

import base64
import binascii
import gzip
import json
from datetime import UTC, datetime
from typing import Any

from .context import stage_for

ARCHIVE_ENCODING = "gzip+base64"


def compress_records(records: list[dict[str, Any]]) -> str:
    """把消息列表序列化后 gzip 压缩，再 base64 编码成可存 Text 的字符串。"""
    raw = json.dumps(records, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return base64.b64encode(gzip.compress(raw, mtime=0)).decode("ascii")


def decompress_records(payload: str) -> list[dict[str, Any]]:
    """还原归档 payload；坏数据返回空列表，避免单个坏归档阻断整页历史。"""
    if not payload:
        return []
    try:
        raw = gzip.decompress(base64.b64decode(payload))
        data = json.loads(raw.decode("utf-8"))
    except (binascii.Error, OSError, ValueError):
        return []
    return data if isinstance(data, list) else []


def encode_cursor(created_at: datetime, message_id: str) -> str:
    """游标 = base64(时间 ISO | 消息 id)，同时保证稳定排序与不可读性。"""
    raw = f"{created_at.isoformat()}|{message_id}".encode()
    return base64.urlsafe_b64encode(raw).decode("ascii")


def decode_cursor(cursor: str | None) -> tuple[datetime, str] | None:
    """解析游标；格式非法返回 None，由调用方按无游标处理（首屏最新一页）。"""
    if not cursor:
        return None
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8")
        stamp, message_id = raw.split("|", 1)
        return _aware(datetime.fromisoformat(stamp)), message_id
    except (ValueError, UnicodeDecodeError, binascii.Error):
        return None


def _aware(value: datetime) -> datetime:
    # 关系库可能返回 naive datetime（取决于方言）；统一按 UTC 解释，避免比较报错。
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def conversation_state(token_count: int, window: int) -> str:
    """会话累计 token 的占用阶段，落到 conversations.context_state。"""
    if window <= 0:
        return "ok"
    return stage_for(max(token_count, 0) / window)


def should_split(
    token_count: int,
    last_message_at: datetime | None,
    *,
    now: datetime,
    window: int,
    split_ratio: float,
    idle_hours: int,
) -> str | None:
    """拆分建议：累计 token 到 split 比例返回 context，空闲超时返回 idle。

    只给建议不自动切换会话：拆分会产生新会话 id，静默替换会让前端拿错上下文。
    """
    if window > 0 and token_count / window >= split_ratio:
        return "context"
    if last_message_at is not None and idle_hours > 0:
        idle_seconds = (_aware(now) - _aware(last_message_at)).total_seconds()
        if idle_seconds >= idle_hours * 3600:
            return "idle"
    return None
