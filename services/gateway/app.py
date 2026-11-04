"""网关 / BFF：统一鉴权、限流、审计与聚合查询。

业务服务只在内网暴露；外部所有流量从这里进入。
"""

import json
import time
from contextlib import asynccontextmanager, suppress
from typing import Any

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from redis.asyncio import Redis

from shop_common.auth import Principal, PrincipalDep, create_token
from shop_common.config import get_settings
from shop_common.errors import ApiError, install_error_handlers
from shop_common.events import create_redis
from shop_common.logging import configure_logging, get_logger

settings = get_settings()
logger = get_logger(__name__)

SERVICES = {
    "catalog": settings.catalog_url,
    "inventory": settings.inventory_url,
    "orders": settings.order_url,
    "fulfillment": settings.fulfillment_url,
    "aftersales": settings.aftersales_url,
    "agents": settings.harness_url,
    "harness": settings.harness_url,
}

RATE_LIMIT_PER_MINUTE = 120
redis: Redis | None = None


def _forward(principal: Principal) -> dict[str, str]:
    return {
        "X-User-Id": principal.user_id,
        "X-User-Role": principal.role,
        "X-Tenant": principal.tenant,
    }


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


@app.middleware("http")
async def rate_limit(request: Request, call_next):
    if not request.url.path.startswith("/api") or request.url.path == "/api/auth/token":
        return await call_next(request)
    assert redis is not None
    identity = request.headers.get("x-user-id") or (request.client.host if request.client else "unknown")
    window = int(time.time() // 60)
    key = f"shop:rl:{identity}:{window}"
    count = await redis.incr(key)
    if count == 1:
        await redis.expire(key, 65)
    if count > RATE_LIMIT_PER_MINUTE:
        raise ApiError("RATE_LIMITED", "请求过于频繁，请稍后再试")
    return await call_next(request)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok", "service": "gateway"}


class TokenRequest(BaseModel):
    user_id: str = Field(min_length=2)
    role: str = "customer"


@app.post("/api/auth/token")
async def issue_token(payload: TokenRequest) -> dict[str, Any]:
    if payload.role not in {"customer", "operator", "admin"}:
        raise ApiError("BAD_REQUEST", "不支持的角色")
    token = create_token(user_id=payload.user_id, role=payload.role)
    return {"token": token, "role": payload.role, "user_id": payload.user_id}


@app.get("/api/storefront/products/{product_id}")
async def product_detail(product_id: str, principal: PrincipalDep) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=10) as client:
        product_resp = await client.get(f"{settings.catalog_url}/products/{product_id}")
        if product_resp.status_code == 404:
            raise ApiError("NOT_FOUND", "商品不存在")
        product = product_resp.json()
        stock_resp = await client.get(f"{settings.inventory_url}/inventory/{product['sku']}")
        stock = stock_resp.json() if stock_resp.status_code == 200 else {"available": 0}
    return {"product": product, "stock": stock}


@app.get("/api/storefront/feed")
async def storefront_feed(principal: PrincipalDep, q: str | None = None, category: str | None = None) -> dict[str, Any]:
    params = {key: value for key, value in {"q": q, "category": category}.items() if value}
    async with httpx.AsyncClient(timeout=10) as client:
        products = (await client.get(f"{settings.catalog_url}/products", params=params)).json()
        items = []
        for product in products["items"][:12]:
            stock = (await client.get(f"{settings.inventory_url}/inventory/{product['sku']}")).json()
            items.append({**product, "available": stock.get("available", 0)})
    return {**products, "items": items}


@app.post("/api/storefront/checkout")
async def checkout(request: Request, principal: PrincipalDep) -> dict[str, Any]:
    body = await request.json()
    headers = {"Content-Type": "application/json", **_forward(principal)}
    if key := request.headers.get("idempotency-key"):
        headers["Idempotency-Key"] = key
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post(f"{settings.order_url}/orders", json=body, headers=headers)
    if response.status_code >= 400:
        payload = response.json()
        raise ApiError(payload.get("code", "CONFLICT"), payload.get("message", "下单失败"), details=payload.get("details", {}))
    return response.json()


@app.get("/api/storefront/orders")
async def my_orders(principal: PrincipalDep, status: str | None = None) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.get(
            f"{settings.order_url}/orders",
            params={"status": status} if status else {},
            headers=_forward(principal),
        )
    return response.json()


@app.post("/api/storefront/orders/{order_id}/pay")
async def pay(order_id: str, request: Request, principal: PrincipalDep) -> dict[str, Any]:
    body = await request.json() if await request.body() else {}
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            f"{settings.order_url}/orders/{order_id}/pay", json=body, headers=_forward(principal)
        )
    payload = response.json()
    if response.status_code >= 400:
        raise ApiError(payload.get("code", "CONFLICT"), payload.get("message", "支付失败"), details=payload.get("details", {}))
    return payload


@app.get("/api/agents")
async def agents(principal: PrincipalDep) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.get(f"{settings.harness_url}/agents", headers=_forward(principal))
    return response.json()


@app.get("/api/policies")
async def policies(principal: PrincipalDep) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.get(f"{settings.harness_url}/policies", headers=_forward(principal))
    return response.json()


@app.get("/api/orders")
async def all_orders(principal: PrincipalDep, status: str | None = None) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.get(
            f"{settings.order_url}/orders",
            params={"status": status} if status else {},
            headers=_forward(principal),
        )
    return response.json()


@app.api_route("/api/{service}/{rest:path}", methods=["GET", "POST", "PATCH", "PUT", "DELETE"])
async def proxy(service: str, rest: str, request: Request, principal: PrincipalDep):
    base = SERVICES.get(service)
    if base is None:
        raise ApiError("NOT_FOUND", f"未知服务 {service}")
    url = f"{base}/{rest}"
    headers = {
        key: value
        for key, value in request.headers.items()
        if key.lower() not in {"host", "content-length", "connection", "accept-encoding"}
    }
    headers.update(_forward(principal))
    body = await request.body()
    method = request.method

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
        await redis.ltrim("shop:audit", 0, 4999)

    if "chat" in rest or "stream" in rest:
        client = httpx.AsyncClient(timeout=None)
        upstream_request = client.build_request(
            method, url, headers=headers, content=body, params=request.query_params
        )
        upstream = await client.send(upstream_request, stream=True)

        async def stream_body():
            try:
                async for chunk in upstream.aiter_raw():
                    yield chunk
            finally:
                await upstream.aclose()
                await client.aclose()

        return StreamingResponse(
            stream_body(), media_type=upstream.headers.get("content-type", "text/event-stream")
        )

    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.request(
            method, url, headers=headers, content=body, params=request.query_params
        )
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
