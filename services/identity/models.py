"""identity_db 的 ORM 模型：账号、角色、refresh 令牌与登录锁定。

角色只能来自 user_roles 表，任何请求体/Header 都无权指定角色。
"""

from datetime import UTC, datetime

from shop_common.db import Base
from sqlalchemy import DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column


def _now() -> datetime:
    return datetime.now(UTC)


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    # 单租户预留字段：默认 default，后续多租户接入时无需改表结构
    tenant_id: Mapped[str] = mapped_column(String(36), default="default")
    phone: Mapped[str] = mapped_column(String(11), unique=True, index=True)
    email: Mapped[str | None] = mapped_column(String(128), nullable=True)
    password_hash: Mapped[str] = mapped_column(String(256))
    status: Mapped[str] = mapped_column(String(16), default="active", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class UserRole(Base):
    __tablename__ = "user_roles"

    user_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    role: Mapped[str] = mapped_column(String(16), primary_key=True)
    tenant: Mapped[str] = mapped_column(String(36), primary_key=True, default="default")
    granted_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    granted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(36), index=True)
    # 只存 sha256 哈希：即使库泄露也无法还原出可用令牌
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 轮换链：指向被本令牌替换的旧令牌 id，用于审计与重放排查
    rotated_from: Mapped[str | None] = mapped_column(String(36), nullable=True)
    user_agent: Mapped[str] = mapped_column(String(256), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class LoginAttempt(Base):
    __tablename__ = "login_attempts"

    # key 为 phone+ip 的组合，防同一来源对同一账号的暴力破解
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    failed_count: Mapped[int] = mapped_column(Integer, default=0)
    window_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
