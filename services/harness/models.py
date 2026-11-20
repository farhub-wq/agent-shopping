"""harness 关系库的 ORM 模型：模型提供商、会话、消息与用量。

harness 原先是「无关系库」服务（记忆/轨迹走 Mongo，工作记忆/配额走 Redis），
本轮为支持多厂商模型、用量监控与对话隔离引入关系库：
- model_providers：用户自填或管理员共享的模型提供商，密钥 Fernet 加密落库。
- conversations / messages：会话与消息的结构化持久化，按 user_id + tenant 隔离。
- usage_records：真实 token 用量与延迟，供监控页聚合。
"""

from datetime import UTC, datetime

from shop_common.db import Base, UTCDateTime
from sqlalchemy import BigInteger, Boolean, Integer, String, Text
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
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=_now)
    last_message_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=_now)


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(String(36), index=True)
    role: Mapped[str] = mapped_column(String(16))  # user | assistant | tool
    content: Mapped[str] = mapped_column(Text, default="")
    tool_calls_json: Mapped[str | None] = mapped_column(Text, nullable=True)
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
