"""identity 服务：注册、登录、refresh 轮换、登出与登录锁定。

账号与角色是唯一身份真相；其他服务只认本服务签发的 JWT 与角色声明。
"""

import asyncio
import contextlib
import hmac
import os
import secrets
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from redis.asyncio import Redis
from shop_common.audit import enqueue_audit
from shop_common.auth import PrincipalDep, create_token
from shop_common.config import COMMON_SECRETS, get_settings, validate_production_settings
from shop_common.db import dispose_engine, get_session_factory, init_models
from shop_common.errors import ApiError, install_error_handlers
from shop_common.events import EventBus, create_redis
from shop_common.logging import configure_logging, get_logger
from shop_common.metrics import install_metrics
from shop_common.outbox import OutboxRelay
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from . import (
    models,  # noqa: F401  # 注册到 Base.metadata，init_models 才能建表
    totp,
)
from .models import LoginAttempt, RefreshToken, User, UserRole
from .security import decrypt_secret, encrypt_secret, hash_password, verify_password
from .tokens import hash_refresh_token, new_refresh_token

settings = get_settings()
logger = get_logger(__name__)

redis: Redis | None = None

# 角色优先级：JWT 只放单个 role 声明，多角色用户取最高权限那一个
_ROLE_PRIORITY = ("customer", "operator", "admin")


async def bootstrap_admin() -> None:
    """空库时用环境变量引导第一个管理员；密码只从环境读，不落日志。"""
    phone = os.environ.get("BOOTSTRAP_ADMIN_PHONE")
    password = os.environ.get("BOOTSTRAP_ADMIN_PASSWORD")
    if not phone or not password:
        return
    async with get_session_factory()() as session:
        existing = await session.scalar(select(User).limit(1))
        if existing is not None:
            return
        user = User(id=str(uuid.uuid4()), phone=phone, password_hash=hash_password(password))
        session.add(user)
        session.add(UserRole(user_id=user.id, role="admin", granted_by="bootstrap"))
        await session.commit()
        logger.info("bootstrap admin created phone=%s****", phone[:3])


@asynccontextmanager
async def lifespan(_: FastAPI):
    global redis
    configure_logging("identity")
    # identity 额外持有 MFA 主密钥：TOTP 密文可被解出等于管理员二次验证失效。
    validate_production_settings(settings, secrets=(*COMMON_SECRETS, "mfa_encryption_key"))
    await init_models()
    await bootstrap_admin()
    tasks: list[asyncio.Task] = []
    if settings.background_tasks:
        # 管理员登录审计走 outbox 投递；单测关闭后台任务避免依赖 Redis
        redis = create_redis(settings.redis_url)
        bus = EventBus(redis, namespace="shop", session_factory=get_session_factory())
        relay = OutboxRelay(get_session_factory(), bus)
        tasks = [asyncio.create_task(relay.run_forever())]
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if redis is not None:
            await redis.close()
        await dispose_engine()


app = FastAPI(title="identity", version="0.1.0", lifespan=lifespan)
install_error_handlers(app, "identity")
install_metrics(app, "identity")


# 字段名到展示名的映射，让校验错误能直接告诉调用方具体哪里不符合要求
_FIELD_LABELS = {"phone": "手机号", "password": "密码", "email": "邮箱"}


def _validation_message(errors: list[dict[str, Any]]) -> str:
    """把 Pydantic 错误翻成中文提示，避免前端只看到笼统的“请求参数不合法”。"""
    hints: list[str] = []
    for error in errors:
        # loc 形如 ("body", "phone")，取最后一个已知字段名做标签
        field = next((str(part) for part in reversed(error.get("loc", ())) if part in _FIELD_LABELS), "")
        label = _FIELD_LABELS.get(field, "参数")
        kind = error.get("type", "")
        ctx = error.get("ctx", {})
        if kind == "string_pattern_mismatch" and field == "phone":
            hints.append("手机号需为 11 位大陆手机号（1 开头，第二位 3-9）")
        elif kind == "string_too_short":
            hints.append(f"{label}不能少于 {ctx.get('min_length', 0)} 位")
        elif kind == "string_too_long":
            hints.append(f"{label}不能超过 {ctx.get('max_length', 0)} 位")
        elif kind == "missing":
            hints.append(f"缺少{label}")
        else:
            hints.append(f"{label}格式不正确")
    return "；".join(dict.fromkeys(hints)) or "请求参数不合法"


@app.exception_handler(RequestValidationError)
async def _handle_validation(_: Request, exc: RequestValidationError) -> JSONResponse:
    # 入口参数校验失败对外统一 400：422 是 FastAPI 默认行为，不符合本仓库的错误契约
    errors = exc.errors()
    return JSONResponse(
        status_code=400,
        content={"code": "BAD_REQUEST", "message": _validation_message(errors), "details": {"errors": errors}},
    )


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok", "service": "identity"}


class RegisterReq(BaseModel):
    # 手机号格式与密码长度在入口处收口，错误请求不进业务逻辑
    phone: str = Field(pattern=r"^1[3-9]\d{9}$")
    password: str = Field(min_length=8, max_length=72)
    email: str | None = Field(default=None, max_length=128)


@app.post("/auth/register", status_code=201)
async def register(body: RegisterReq) -> dict[str, Any]:
    async with get_session_factory()() as session:
        user = User(
            id=str(uuid.uuid4()),
            phone=body.phone,
            email=body.email,
            password_hash=hash_password(body.password),
        )
        session.add(user)
        # 注册即默认授予 customer 角色，角色与账号同事务落库，不存在无角色的中间态
        session.add(UserRole(user_id=user.id, role="customer", granted_by="register"))
        try:
            await session.commit()
        except IntegrityError as exc:
            # 并发注册同一手机号撞唯一索引时转 409，而不是把 500 抛给客户端
            await session.rollback()
            raise ApiError("CONFLICT", "手机号已注册") from exc
        # 响应显式挑字段：password_hash 等内部列永不出现在响应里
        return {"id": user.id, "phone": user.phone, "roles": ["customer"]}


class LoginReq(BaseModel):
    phone: str = Field(pattern=r"^1[3-9]\d{9}$")
    password: str = Field(min_length=8, max_length=72)
    # 已启用 MFA 时必填；未启用时忽略，保持单因素登录行为不变
    otp: str = Field(default="", pattern=r"^\d{6}$")


def _mask_phone(phone: str) -> str:
    # 日志脱敏：只留前三后四，完整手机号不进日志
    return f"{phone[:3]}****{phone[-4:]}"


def _aware(value: datetime) -> datetime:
    # SQLite 回读的 DateTime 不带时区，统一补 UTC 后再参与比较
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _attempt_key(request: Request, phone: str) -> str:
    ip = request.client.host if request.client else "unknown"
    return f"{phone}:{ip}"


def _verify_csrf(request: Request) -> None:
    # 双提交校验：Header 与 Cookie 中的 csrf 值必须同时存在且一致，防跨站伪造刷新/登出请求
    cookie = request.cookies.get("shop_csrf")
    header = request.headers.get("x-csrf-token")
    if not cookie or not header or not hmac.compare_digest(cookie, header):
        raise ApiError("FORBIDDEN", "CSRF 校验失败")


async def _roles_of(session, user_id: str) -> list[str]:
    rows = await session.scalars(select(UserRole).where(UserRole.user_id == user_id))
    return sorted(row.role for row in rows)


def _primary_role(roles: list[str]) -> str:
    return max(roles, key=_ROLE_PRIORITY.index) if roles else "customer"


def _set_session_cookies(response: Response, refresh_raw: str, csrf: str) -> None:
    # refresh 走 HttpOnly Cookie，JS 读不到；csrf 非 HttpOnly，前端读取后回放到 X-CSRF-Token
    # path 限定为 /api/auth：浏览器只把凭证发给网关的认证路由，不随业务请求带出去
    refresh_options = {"samesite": "lax", "secure": settings.env == "prod", "max_age": settings.refresh_ttl_seconds, "path": "/api/auth"}
    # CSRF 需要被前端页面读取（双提交），path 不能限定在 /api/auth，
    # 否则刷新页面后 /ops 等页面读不到 Cookie，access token 无法恢复
    csrf_options = {**refresh_options, "path": "/"}
    # 兼容旧版本写入的 /api/auth path：先让旧 Cookie 过期，避免浏览器里同名 Cookie 存在两个
    # path 时读到旧值，导致刷新后 CSRF 校验失败
    response.set_cookie("shop_csrf", "", max_age=0, path="/api/auth", httponly=False, samesite="lax", secure=settings.env == "prod")
    response.set_cookie("shop_refresh", refresh_raw, httponly=True, **refresh_options)
    response.set_cookie("shop_csrf", csrf, httponly=False, **csrf_options)


async def _new_refresh_row(session, user_id: str, request: Request, rotated_from: str | None = None) -> str:
    raw = new_refresh_token()
    session.add(
        RefreshToken(
            id=str(uuid.uuid4()),
            user_id=user_id,
            token_hash=hash_refresh_token(raw),
            expires_at=datetime.now(UTC) + timedelta(seconds=settings.refresh_ttl_seconds),
            rotated_from=rotated_from,
            user_agent=request.headers.get("user-agent", "")[:256],
        )
    )
    return raw


def _session_body(user: User, roles: list[str]) -> dict[str, Any]:
    return {
        "access_token": create_token(user_id=user.id, role=_primary_role(roles), tenant=user.tenant_id),
        "token_type": "bearer",
        "expires_in": settings.jwt_ttl_seconds,
        "user": {"id": user.id, "phone": user.phone, "roles": roles},
    }


async def _record_failure(session, key: str, now: datetime) -> None:
    window = timedelta(minutes=settings.login_lock_minutes)
    attempt = await session.get(LoginAttempt, key)
    if attempt is None or attempt.window_start is None or _aware(attempt.window_start) + window <= now:
        # 无记录或统计窗口已过，重新计数
        if attempt is None:
            attempt = LoginAttempt(key=key)
            session.add(attempt)
        attempt.failed_count = 1
        attempt.window_start = now
        attempt.locked_until = None
    else:
        attempt.failed_count += 1
        # 达到阈值才锁窗口；本次请求仍按密码错误返回 401，从下一次起统一 429
        if attempt.failed_count >= settings.login_max_failures:
            attempt.locked_until = now + window
    await session.commit()


@app.post("/auth/login")
async def login(body: LoginReq, request: Request, response: Response) -> dict[str, Any]:
    async with get_session_factory()() as session:
        now = datetime.now(UTC)
        key = _attempt_key(request, body.phone)
        attempt = await session.get(LoginAttempt, key)
        if attempt and attempt.locked_until and _aware(attempt.locked_until) > now:
            # 锁定期间统一 429，不区分账号是否存在、密码是否正确
            raise ApiError("RATE_LIMITED", "登录尝试过于频繁，请稍后再试")
        user = await session.scalar(select(User).where(User.phone == body.phone))
        if user is None or user.status != "active" or not verify_password(body.password, user.password_hash):
            await _record_failure(session, key, now)
            logger.info("login failed phone=%s", _mask_phone(body.phone))
            raise ApiError("UNAUTHORIZED", "账号或密码错误")
        # MFA 二次验证：启用后必须携带 6 位动态码；密码正确但缺码时返回专用错误码，
        # 前端据此弹出验证码输入框。缺码不计失败（密码本身是对的），错误码/重放才计失败。
        if user.totp_enabled and user.totp_secret:
            if not body.otp:
                raise ApiError("MFA_REQUIRED", "需要动态验证码")
            try:
                secret = decrypt_secret(user.totp_secret)
            except ValueError as exc:
                logger.error("mfa secret decrypt failed user=%s", user.id)
                raise ApiError("INTERNAL", str(exc)) from exc
            step = totp.verify(secret, body.otp, int(now.timestamp()))
            if step is None or step == user.last_totp_step:
                await _record_failure(session, key, now)
                logger.info("mfa verify failed phone=%s", _mask_phone(body.phone))
                raise ApiError("UNAUTHORIZED", "动态验证码错误或已使用")
            user.last_totp_step = step
        # 登录成功清除失败计数；角色从 DB 读取，请求体与 Header 都无权指定
        if attempt:
            await session.delete(attempt)
        roles = await _roles_of(session, user.id)
        refresh_raw = await _new_refresh_row(session, user.id, request)
        # 管理员登录属敏感操作：成功进入即留痕，IP 哈希必填，供 audit 服务追溯
        if _primary_role(roles) == "admin":
            enqueue_audit(
                session,
                actor_id=user.id,
                actor_role="admin",
                action="identity.admin_login",
                target_type="user",
                target_id=user.id,
                request=request,
                tenant=user.tenant_id,
            )
        await session.commit()
    _set_session_cookies(response, refresh_raw, secrets.token_urlsafe(24))
    return _session_body(user, roles)


@app.post("/auth/refresh")
async def refresh(request: Request, response: Response) -> dict[str, Any]:
    _verify_csrf(request)
    raw = request.cookies.get("shop_refresh")
    if not raw:
        raise ApiError("UNAUTHORIZED", "缺少刷新令牌")
    async with get_session_factory()() as session:
        token = await session.scalar(select(RefreshToken).where(RefreshToken.token_hash == hash_refresh_token(raw)))
        now = datetime.now(UTC)
        if token is None or token.revoked_at is not None or _aware(token.expires_at) <= now:
            # 不存在、已撤销（含被轮换）、已过期统一 401；旧令牌重放也落在这里
            raise ApiError("UNAUTHORIZED", "刷新令牌无效或已过期")
        user = await session.get(User, token.user_id)
        if user is None or user.status != "active":
            raise ApiError("UNAUTHORIZED", "账号不可用")
        roles = await _roles_of(session, user.id)
        # 轮换：同事务撤销旧令牌并写入新令牌，新旧令牌不存在同时有效的窗口
        token.revoked_at = now
        new_raw = await _new_refresh_row(session, user.id, request, rotated_from=token.id)
        await session.commit()
    _set_session_cookies(response, new_raw, secrets.token_urlsafe(24))
    return _session_body(user, roles)


@app.post("/auth/logout", status_code=204)
async def logout(request: Request) -> None:
    _verify_csrf(request)
    raw = request.cookies.get("shop_refresh")
    if raw:
        async with get_session_factory()() as session:
            token = await session.scalar(select(RefreshToken).where(RefreshToken.token_hash == hash_refresh_token(raw)))
            if token and token.revoked_at is None:
                token.revoked_at = datetime.now(UTC)
                await session.commit()


class MfaConfirmReq(BaseModel):
    code: str = Field(pattern=r"^\d{6}$")


class MfaDisableReq(BaseModel):
    password: str = Field(min_length=8, max_length=72)


@app.post("/auth/mfa/enroll")
async def enroll_mfa(principal: PrincipalDep, request: Request) -> dict[str, Any]:
    principal.require_admin()
    async with get_session_factory()() as session:
        user = await session.get(User, principal.user_id)
        if user is None:
            raise ApiError("UNAUTHORIZED", "账号不存在")
        if user.totp_enabled:
            raise ApiError("CONFLICT", "已启用 MFA，需先关闭后再重新绑定")
        # 明文 secret 只在此次响应返回一次；落库为密文，扫码未确认前不阻塞登录
        secret = totp.new_secret()
        user.totp_secret = encrypt_secret(secret)
        user.totp_enabled = False
        enqueue_audit(
            session,
            actor_id=user.id,
            actor_role="admin",
            action="identity.mfa_enroll",
            target_type="user",
            target_id=user.id,
            request=request,
            tenant=user.tenant_id,
        )
        await session.commit()
    return {"otpauth_url": totp.otpauth_url(secret, user.phone), "secret": secret}


@app.post("/auth/mfa/confirm")
async def confirm_mfa(body: MfaConfirmReq, principal: PrincipalDep, request: Request) -> dict[str, Any]:
    principal.require_admin()
    async with get_session_factory()() as session:
        user = await session.get(User, principal.user_id)
        if user is None:
            raise ApiError("UNAUTHORIZED", "账号不存在")
        if user.totp_enabled:
            raise ApiError("CONFLICT", "MFA 已启用")
        if not user.totp_secret:
            raise ApiError("CONFLICT", "尚未发起绑定，请先调用 enroll")
        try:
            secret = decrypt_secret(user.totp_secret)
        except ValueError as exc:
            raise ApiError("INTERNAL", str(exc)) from exc
        step = totp.verify(secret, body.code, int(datetime.now(UTC).timestamp()))
        if step is None:
            raise ApiError("UNAUTHORIZED", "动态验证码错误")
        user.totp_enabled = True
        user.last_totp_step = step
        enqueue_audit(
            session,
            actor_id=user.id,
            actor_role="admin",
            action="identity.mfa_enabled",
            target_type="user",
            target_id=user.id,
            request=request,
            tenant=user.tenant_id,
        )
        await session.commit()
    return {"enabled": True}


@app.post("/auth/mfa/disable")
async def disable_mfa(body: MfaDisableReq, principal: PrincipalDep, request: Request) -> dict[str, Any]:
    principal.require_admin()
    async with get_session_factory()() as session:
        user = await session.get(User, principal.user_id)
        if user is None:
            raise ApiError("UNAUTHORIZED", "账号不存在")
        # 关闭 MFA 会削弱账号保护：必须重新校验密码，防已登录会话被劫持后直接关掉二次验证
        if not verify_password(body.password, user.password_hash):
            raise ApiError("UNAUTHORIZED", "密码错误")
        user.totp_secret = None
        user.totp_enabled = False
        user.last_totp_step = None
        enqueue_audit(
            session,
            actor_id=user.id,
            actor_role="admin",
            action="identity.mfa_disabled",
            target_type="user",
            target_id=user.id,
            request=request,
            tenant=user.tenant_id,
        )
        await session.commit()
    return {"enabled": False}


@app.get("/auth/mfa")
async def mfa_status(principal: PrincipalDep) -> dict[str, Any]:
    principal.require_admin()
    async with get_session_factory()() as session:
        user = await session.get(User, principal.user_id)
        if user is None:
            raise ApiError("UNAUTHORIZED", "账号不存在")
        return {"enabled": bool(user.totp_enabled)}


@app.get("/me")
async def me(principal: PrincipalDep) -> dict[str, Any]:
    async with get_session_factory()() as session:
        user = await session.get(User, principal.user_id)
        if user is None:
            raise ApiError("UNAUTHORIZED", "账号不存在")
        roles = await _roles_of(session, user.id)
    return {"id": user.id, "phone": user.phone, "roles": roles}
