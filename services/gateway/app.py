"""网关 / BFF：统一鉴权、限流、审计与聚合查询。

业务服务只在内网暴露；外部所有流量从这里进入。
"""

import json
import time
import uuid
from contextlib import asynccontextmanager, suppress
from typing import Any

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from redis.asyncio import Redis
from shop_common.auth import Principal, PrincipalDep, create_internal_signature, create_token
from shop_common.config import get_settings
from shop_common.errors import ApiError, install_error_handlers
from shop_common.events import create_redis
from shop_common.logging import configure_logging, get_logger

settings = get_settings()
logger = get_logger(__name__)

# 反向代理白名单：只有这里登记的服务名可被 /api/{service}/... 访问，其余一律 404，防止任意内网探测
SERVICES = {
    "catalog": settings.catalog_url,
    "inventory": settings.inventory_url,
    "orders": settings.order_url,
    "fulfillment": settings.fulfillment_url,
    "aftersales": settings.aftersales_url,
    "agents": settings.harness_url,
    "harness": settings.harness_url,
    "payments": settings.payment_url,
}

RATE_LIMIT_PER_MINUTE = 120
LOGIN_RATE_LIMIT_PER_MINUTE = 10

# 服务名与下游真实路由前缀并非一一对应：inventory/orders/aftersales 带服务前缀，
# fulfillment/harness 的路由没有前缀；agents 是 harness 的别名，还需要兼容
# /agents/{skill}/chat 这类带前缀的路由。
SERVICE_PATH_PREFIX = {
    "catalog": "",
    "inventory": "/inventory",
    "orders": "/orders",
    "fulfillment": "",
    "aftersales": "/aftersales",
    "payments": "/payments",
    "harness": "",
}
# 这些路径属于 harness 根路由，不能再补 /agents 前缀
HARNESS_ROOT_PREFIXES = ("runs/", "memory/", "sandbox/", "mcp")


def _upstream_path(service: str, rest: str) -> str:
    """把浏览器侧 /api/{service}/{rest} 还原成下游服务的真实路径。"""
    if service == "agents":
        # /api/agents/runs/... 等直接对应 harness 根路由，其余（如 {skill}/chat）补 /agents
        return f"/{rest}" if rest.startswith(HARNESS_ROOT_PREFIXES) else f"/agents/{rest}"
    prefix = SERVICE_PATH_PREFIX.get(service, "")
    return f"{prefix}/{rest}" if prefix else f"/{rest}"

redis: Redis | None = None


def _forward(principal: Principal, request_id: str) -> dict[str, str]:
    # 网关鉴权后重建身份头并加内网签名：下游只信任这里写入的身份，客户端自带的同名头已被覆盖
    ts = str(int(time.time()))
    signature = create_internal_signature(
        user_id=principal.user_id,
        role=principal.role,
        tenant=principal.tenant,
        ts=ts,
        request_id=request_id,
        secret=settings.internal_signing_secret,
    )
    return {
        "X-User-Id": principal.user_id,
        "X-User-Role": principal.role,
        "X-Tenant": principal.tenant,
        "X-Request-Id": request_id,
        "X-Internal-Signature": f"{ts}.{signature}",
    }


def _request_id(request: Request) -> str:
    # 优先沿用调用方带来的请求号，没有则现场生成，保证审计链可串起来
    return request.headers.get("x-request-id") or uuid.uuid4().hex


@asynccontextmanager
async def lifespan(_: FastAPI):
    global redis
    configure_logging("gateway")
    redis = create_redis(settings.redis_url)
    yield
    await redis.close()


app = FastAPI(title="api-gateway", version="0.1.0", lifespan=lifespan)
install_error_handlers(app, "gateway")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://localhost:8080", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


async def _hit(key: str) -> int:
    assert redis is not None
    count = await redis.incr(key)
    if count == 1:
        # 首次计数时设 TTL，留 5 秒余量保证跨窗口时旧 key 能自然过期
        await redis.expire(key, 65)
    return count


def _rate_limited(message: str) -> JSONResponse:
    # 中间件里抛 ApiError 到不了异常处理器，直接组装 429 响应返回
    return JSONResponse(status_code=429, content={"code": "RATE_LIMITED", "message": message, "details": {}})


@app.middleware("http")
async def rate_limit(request: Request, call_next):
    path = request.url.path
    # 只限流业务接口；非 /api 路径与认证类接口另行处理，否则登录会被自己的限流挡住
    if not path.startswith("/api"):
        return await call_next(request)
    window = int(time.time() // 60)
    if path == "/api/auth/login":
        # 登录单独按客户端 IP 严格限流，提高暴力破解成本
        ip = request.client.host if request.client else "unknown"
        if await _hit(f"shop:rl:login:{ip}:{window}") > LOGIN_RATE_LIMIT_PER_MINUTE:
            return _rate_limited("登录请求过于频繁，请稍后再试")
        return await call_next(request)
    if path in {"/api/auth/register", "/api/auth/refresh", "/api/auth/token"}:
        return await call_next(request)
    # 渠道回调由服务商按签名鉴权且可能短时高频重试，不能套用按用户的业务限流
    if path.startswith("/api/payments/callback/"):
        return await call_next(request)
    # 优先按已鉴权用户维度计数，未带身份时退化为客户端 IP，避免匿名流量绕过限额
    identity = request.headers.get("x-user-id") or (request.client.host if request.client else "unknown")
    if await _hit(f"shop:rl:{identity}:{window}") > RATE_LIMIT_PER_MINUTE:
        return _rate_limited("请求过于频繁，请稍后再试")
    return await call_next(request)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok", "service": "gateway"}


class TokenRequest(BaseModel):
    user_id: str = Field(min_length=2)
    role: str = "customer"


def _dev_token_enabled() -> bool:
    # dev 换 token 只用于本地演示：生产环境或非显式开启时等同接口不存在
    return settings.env == "dev" and settings.auth_dev_mode


@app.post("/api/auth/token")
async def issue_token(payload: TokenRequest) -> dict[str, Any]:
    if not _dev_token_enabled():
        raise ApiError("NOT_FOUND", "接口不存在")
    # 角色白名单收口在这里：签发端只认这三种，避免下游拿到未定义角色
    if payload.role not in {"customer", "operator", "admin"}:
        raise ApiError("BAD_REQUEST", "不支持的角色")
    token = create_token(user_id=payload.user_id, role=payload.role)
    return {"token": token, "role": payload.role, "user_id": payload.user_id}


async def _proxy_auth(request: Request, path: str) -> Response:
    """把认证请求代理到 identity；Set-Cookie 原样回传，让会话 Cookie 落在网关域上。"""
    headers = {}
    for name in ("content-type", "cookie", "x-csrf-token", "authorization"):
        if value := request.headers.get(name):
            headers[name] = value
    body = await request.body()
    async with httpx.AsyncClient(timeout=10) as client:
        upstream = await client.request(
            request.method, f"{settings.identity_url}{path}", content=body or None, headers=headers
        )
    # 只透传 set-cookie，不复制 content-length 等 hop-by-hop 头
    response = Response(
        content=upstream.content,
        status_code=upstream.status_code,
        media_type=upstream.headers.get("content-type"),
    )
    for value in upstream.headers.get_list("set-cookie"):
        response.headers.append("set-cookie", value)
    return response


@app.post("/api/auth/register")
async def auth_register(request: Request) -> Response:
    return await _proxy_auth(request, "/auth/register")


@app.post("/api/auth/login")
async def auth_login(request: Request) -> Response:
    return await _proxy_auth(request, "/auth/login")


@app.post("/api/auth/refresh")
async def auth_refresh(request: Request) -> Response:
    return await _proxy_auth(request, "/auth/refresh")


@app.post("/api/auth/logout")
async def auth_logout(request: Request) -> Response:
    return await _proxy_auth(request, "/auth/logout")


@app.get("/api/me")
async def auth_me(request: Request) -> Response:
    return await _proxy_auth(request, "/me")


@app.get("/api/storefront/products/{product_id}")
async def product_detail(product_id: str, principal: PrincipalDep) -> dict[str, Any]:
    # BFF 聚合：商品基础信息来自 catalog，可售库存来自 inventory，前端一次拿到整份详情
    async with httpx.AsyncClient(timeout=10) as client:
        product_resp = await client.get(f"{settings.catalog_url}/products/{product_id}")
        if product_resp.status_code == 404:
            raise ApiError("NOT_FOUND", "商品不存在")
        product = product_resp.json()
        stock_resp = await client.get(f"{settings.inventory_url}/inventory/{product['sku']}")
        # 库存服务异常时降级为 0，页面仍可展示商品，只是显示无可售库存
        stock = stock_resp.json() if stock_resp.status_code == 200 else {"available": 0}
    return {"product": product, "stock": stock}


@app.get("/api/storefront/feed")
async def storefront_feed(principal: PrincipalDep, q: str | None = None, category: str | None = None) -> dict[str, Any]:
    params = {key: value for key, value in {"q": q, "category": category}.items() if value}
    async with httpx.AsyncClient(timeout=10) as client:
        products = (await client.get(f"{settings.catalog_url}/products", params=params)).json()
        items = []
        # 首页只展示前 12 条并逐条补库存；这是展示级聚合，不承担分页查询职责
        for product in products["items"][:12]:
            stock = (await client.get(f"{settings.inventory_url}/inventory/{product['sku']}")).json()
            items.append({**product, "available": stock.get("available", 0)})
    return {**products, "items": items}


@app.post("/api/storefront/checkout")
async def checkout(request: Request, principal: PrincipalDep) -> dict[str, Any]:
    body = await request.json()
    headers = {"Content-Type": "application/json", **_forward(principal, _request_id(request))}
    # 透传幂等键给订单服务：客户端重试下单时由下游保证只创建一单
    if key := request.headers.get("idempotency-key"):
        headers["Idempotency-Key"] = key
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post(f"{settings.order_url}/orders", json=body, headers=headers)
    # 下游错误原样映射成网关的 ApiError，保留 code/message/details，避免前端拿到无结构响应
    if response.status_code >= 400:
        payload = response.json()
        raise ApiError(payload.get("code", "CONFLICT"), payload.get("message", "下单失败"), details=payload.get("details", {}))
    return response.json()


@app.get("/api/storefront/orders")
async def my_orders(request: Request, principal: PrincipalDep, status: str | None = None) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=10) as client:
        # 店铺前台只回当前用户订单：强制带 user_id，避免 staff 身份复用后台全量列表接口看到他人订单
        params: dict[str, str] = {"user_id": principal.user_id}
        if status:
            params["status"] = status
        response = await client.get(
            f"{settings.order_url}/orders",
            params=params,
            headers=_forward(principal, _request_id(request)),
        )
    return response.json()


@app.post("/api/payments/callback/{provider}")
async def payment_callback(provider: str, request: Request) -> JSONResponse:
    """渠道回调入口：不做网关鉴权，必须原样透传原始 body 与签名头，由 payment 服务验签。"""
    raw_body = await request.body()
    headers = {
        key: value
        for key, value in request.headers.items()
        if key.lower() not in {"host", "content-length", "connection", "accept-encoding"}
    }
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.post(
            f"{settings.payment_url}/payments/callback/{provider}",
            content=raw_body,
            headers=headers,
        )
    # 原样返回下游状态码：验签失败保持 401、金额不符保持 409，渠道据此重试或告警
    return JSONResponse(status_code=response.status_code, content=response.json())


@app.post("/api/storefront/payments/{payment_id}/pay")
async def pay(payment_id: str, request: Request, principal: PrincipalDep) -> dict[str, Any]:
    """本地 mock 收银台确认入口；真实渠道是用户跳转服务商页面后由回调驱动。"""
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            f"{settings.payment_url}/payments/cashier/{payment_id}/pay",
            headers=_forward(principal, _request_id(request)),
        )
    payload = response.json()
    if response.status_code >= 400:
        raise ApiError(
            payload.get("code", "CONFLICT"),
            payload.get("message", "支付失败"),
            details=payload.get("details", {}),
        )
    return payload


@app.get("/api/agents")
async def agents(request: Request, principal: PrincipalDep) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.get(f"{settings.harness_url}/agents", headers=_forward(principal, _request_id(request)))
    return response.json()


@app.get("/api/policies")
async def policies(request: Request, principal: PrincipalDep) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.get(f"{settings.harness_url}/policies", headers=_forward(principal, _request_id(request)))
    return response.json()


@app.get("/api/orders")
async def all_orders(request: Request, principal: PrincipalDep, status: str | None = None) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.get(
            f"{settings.order_url}/orders",
            params={"status": status} if status else {},
            headers=_forward(principal, _request_id(request)),
        )
    return response.json()


@app.api_route("/api/{service}/{rest:path}", methods=["GET", "POST", "PATCH", "PUT", "DELETE"])
async def proxy(service: str, rest: str, request: Request, principal: PrincipalDep):
    # 白名单命中校验：SERVICES 未登记的服务名直接 404，杜绝把网关当内网扫描器
    base = SERVICES.get(service)
    if base is None:
        raise ApiError("NOT_FOUND", f"未知服务 {service}")
    url = f"{base}{_upstream_path(service, rest)}"
    # 透传业务头但剔除 hop-by-hop 头（Host/Content-Length 等），否则 httpx 重建请求会冲突
    headers = {
        key: value
        for key, value in request.headers.items()
        if key.lower() not in {"host", "content-length", "connection", "accept-encoding"}
    }
    # 身份头以网关鉴权结果为准，覆盖客户端传入值
    headers.update(_forward(principal, _request_id(request)))
    body = await request.body()
    method = request.method

    # 审计写入是旁路能力：Redis 不可用不能阻断业务请求，因此用 suppress 吞掉异常
    with suppress(Exception):
        assert redis is not None
        await redis.lpush(
            "shop:audit",
            json.dumps(
                {
                    "at": time.time(),
                    "user": principal.user_id,
                    "role": principal.role,
                    "method": method,
                    "path": f"/api/{service}/{rest}",
                },
                ensure_ascii=False,
            ),
        )
        # 只保留最近 5000 条审计，防止 Redis 列表无限增长
        await redis.ltrim("shop:audit", 0, 4999)

    # 聊天/流式接口按 SSE 处理：无超时、逐块转发，且不缓冲完整响应
    if "chat" in rest or "stream" in rest:
        client = httpx.AsyncClient(timeout=None)
        upstream_request = client.build_request(
            method, url, headers=headers, content=body, params=request.query_params
        )
        upstream = await client.send(upstream_request, stream=True)

        async def stream_body():
            # 逐块透传上游原始字节；客户端断开或异常时也要关闭上游连接与客户端，避免连接泄漏
            try:
                async for chunk in upstream.aiter_raw():
                    yield chunk
            finally:
                await upstream.aclose()
                await client.aclose()

        # 保留上游 content-type，默认 SSE，前端才能按事件流解析
        return StreamingResponse(
            stream_body(), media_type=upstream.headers.get("content-type", "text/event-stream")
        )

    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.request(
            method, url, headers=headers, content=body, params=request.query_params
        )
    # 响应头也只回传白名单字段，避免把上游 Server/Set-Cookie 等内部信息暴露给浏览器
    passthrough = {
        key: value
        for key, value in response.headers.items()
        if key.lower() in {"content-type", "etag", "cache-control"}
    }
    return Response(
        content=response.content,
        status_code=response.status_code,
        headers=passthrough,
        media_type=response.headers.get("content-type"),
    )
