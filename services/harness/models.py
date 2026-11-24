"""harness 关系库的 ORM 模型：模型提供商、会话、消息与用量。

harness 原先是「无关系库」服务（记忆/轨迹走 Mongo，工作记忆/配额走 Redis），
本轮为支持多厂商模型、用量监控与对话隔离引入关系库：
- model_providers：用户自填或管理员共享的模型提供商，密钥 Fernet 加密落库。
- conversations / messages：会话与消息的结构化持久化，按 user_id + tenant 隔离。
- usage_records：真实 token 用量与延迟，供监控页聚合。
"""

from datetime import UTC, datetime

from shop_common.db import Base, UTCDateTime
from sqlalchemy import BigInteger, Boolean, Index, Integer, String, Text
from sqlalchemy.dialects.mysql import LONGTEXT
from sqlalchemy.orm import Mapped, mapped_column


def _now() -> datetime:
    return datetime.now(UTC)


class ModelProvider(Base):
    __tablename__ = "model_providers"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    # 空 owner_user_id = 全局共享（管理员创建，所有用户可用）；非空 = 用户私有（仅本人可用）。
    owner_user_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    tenant_id: Mapped[str] = mapped_column(String(36), default="default", index=True)
    provider_type: Mapped[str] = mapped_column(String(24))  # openai_compatible | anthropic
    name: Mapped[str] = mapped_column(String(64))
    base_url: Mapped[str] = mapped_column(String(256))
    model: Mapped[str] = mapped_column(String(64))
    # Fernet 密文；空串 = 尚未配置密钥，解析链会跳过该 provider。
    api_key_enc: Mapped[str] = mapped_column(String(512), default="")
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=_now, onupdate=_now)


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(36), index=True)
    tenant_id: Mapped[str] = mapped_column(String(36), default="default", index=True)
    skill: Mapped[str] = mapped_column(String(64))
    title: Mapped[str] = mapped_column(String(128), default="")
    # 会话父子链：拆分出的新会话 parent_id 指向旧会话，root_id 始终指向链路起点。
    # 无父会话时 root_id 等于自身 id，方便一次查询拿到整条会话链。
    root_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    parent_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    # 会话累计 token（按历史 user/assistant 原文粗估），到 split 比例即可拆分。
    token_count: Mapped[int] = mapped_column(BigInteger, default=0)
    # ok | warn | split：列表页直接展示，无需重新估算。
    context_state: Mapped[str] = mapped_column(String(16), default="ok")
    # 拆分/回灌时携带的交接摘要，指向 conversation_summaries.id。
    summary_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=_now)
    last_message_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=_now)


class Message(Base):
    __tablename__ = "messages"
    # 会话内分页与归档查询都按 (conversation_id, is_archived, created_at) 走索引。
    __table_args__ = (
        Index("ix_messages_conversation_archived", "conversation_id", "is_archived", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(String(36), index=True)
    role: Mapped[str] = mapped_column(String(16))  # user | assistant | tool
    content: Mapped[str] = mapped_column(Text, default="")
    tool_calls_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 会话内自增序号，仅用于稳定排序与前端增量加载；跨会话不保证全局单调。
    seq: Mapped[int] = mapped_column(BigInteger, default=0)
    # 单条消息 token 粗估，累计进 Conversation.token_count。
    token_count: Mapped[int] = mapped_column(Integer, default=0)
    # 软归档：原文保留可追溯，但默认不再参与历史加载与回灌。
    is_archived: Mapped[bool] = mapped_column(Boolean, default=False)
    archive_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=_now)


class ConversationSummary(Base):
    """会话交接摘要：拆分/回灌时只带这一份结构化摘要，不复制整段历史。"""

    __tablename__ = "conversation_summaries"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(String(36), index=True)
    user_id: Mapped[str] = mapped_column(String(36), index=True)
    tenant_id: Mapped[str] = mapped_column(String(36), default="default", index=True)
    text: Mapped[str] = mapped_column(Text, default="")
    # 订单号 / SKU 等关键事实，JSON 数组；回灌时保证不丢。
    pinned_json: Mapped[str] = mapped_column(Text, default="[]")
    summary_level: Mapped[int] = mapped_column(Integer, default=1)
    source: Mapped[str] = mapped_column(String(16), default="auto")  # auto | split | backfill
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=_now)


class MessageArchive(Base):
    """消息归档：较早消息整批 gzip+base64 压缩落库，原始行置为软归档。"""

    __tablename__ = "message_archives"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(String(36), index=True)
    user_id: Mapped[str] = mapped_column(String(36), index=True)
    tenant_id: Mapped[str] = mapped_column(String(36), default="default", index=True)
    message_count: Mapped[int] = mapped_column(Integer, default=0)
    first_message_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    last_message_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    encoding: Mapped[str] = mapped_column(String(16), default="gzip+base64")
    # 批量压缩后的归档可能超过 MySQL TEXT 的 64KB 上限，显式用 LONGTEXT。
    payload: Mapped[str] = mapped_column(Text().with_variant(LONGTEXT(), "mysql"), default="")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=_now)


class UsageRecord(Base):
    __tablename__ = "usage_records"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(36), index=True)
    tenant_id: Mapped[str] = mapped_column(String(36), default="default", index=True)
    provider_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    model: Mapped[str] = mapped_column(String(64), default="")
    conversation_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    run_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    prompt_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    completion_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    total_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(16), default="ok")  # ok | error
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=_now, index=True)
