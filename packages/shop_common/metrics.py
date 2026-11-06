"""Prometheus 指标：进程内共享的计数器/直方图/仪表，按 service 标签区分。

每个服务一个进程，prometheus_client 默认全局 REGISTRY 即可，无需 multiprocess 模式。
/metrics 端点只在容器内网暴露，Prometheus 直接抓取各服务，不经过网关。
"""

import time
from collections.abc import Awaitable, Callable
from typing import Any

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest
from starlette.requests import Request
from starlette.responses import Response

# 未匹配到路由的请求收敛到该标签，避免 404 探测把路径标签基数打爆
UNMATCHED_ROUTE = "<unmatched>"

# ---- HTTP RED 指标 ----
HTTP_REQUESTS = Counter(
    "http_requests_total", "HTTP 请求总数", ["service", "method", "route", "status"]
)
HTTP_DURATION = Histogram(
    "http_request_duration_seconds", "HTTP 请求耗时（秒）", ["service", "method", "route", "status"]
)
HTTP_IN_FLIGHT = Gauge("http_requests_in_flight", "当前在途请求数", ["service"])

# ---- 业务指标（system-design §10）----
CHECKOUT_TOTAL = Counter("checkout_total", "下单结果", ["service", "result"])
PAYMENT_CALLBACK_TOTAL = Counter("payment_callback_total", "支付回调结果", ["result"])
ORDER_STUCK = Gauge("order_stuck_total", "卡死订单数（RESERVED/PAYING 超时）", ["service"])
OUTBOX_LAG = Gauge("outbox_lag_seconds", "outbox 投递延迟（秒）", ["service"])
DB_POOL_EXHAUSTED = Counter("db_pool_exhausted_total", "连接池耗尽次数", ["service"])
INVENTORY_NEGATIVE = Counter("inventory_negative_total", "库存扣减不足次数", ["service"])
LEDGER_IMBALANCE = Counter("ledger_imbalance_total", "总账借贷不平次数", ["service"])
AGENT_TOOL_DENIED = Counter("agent_tool_denied_total", "Agent 工具被策略拒绝次数", ["service"])
RECONCILIATION_DIFF = Gauge("reconciliation_diff_total", "对账开放差异数", ["service"])

# 抓取与健康检查本身不计入 RED，避免 self-scrape 噪声
_EXCLUDED_PATHS = {"/metrics", "/healthz"}


class MetricsMiddleware:
    """纯 ASGI 中间件：只在响应开始与结束钩点记录，不读 body，不破坏 SSE 流式转发。"""

    def __init__(self, app: Callable, service_name: str) -> None:
        self.app = app
        self.service = service_name

    async def __call__(
        self,
        scope: dict[str, Any],
        receive: Callable[[], Awaitable[dict[str, Any]]],
        send: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        if scope["type"] != "http" or scope.get("path") in _EXCLUDED_PATHS:
            await self.app(scope, receive, send)
            return
        method = scope.get("method", "")
        start = time.perf_counter()
        status: list[int] = []

        async def send_wrapper(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                status.append(message["status"])
            await send(message)

        HTTP_IN_FLIGHT.labels(self.service).inc()
        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            HTTP_IN_FLIGHT.labels(self.service).dec()
            route = scope.get("route")
            route_label = getattr(route, "path", None) or UNMATCHED_ROUTE
            code = str(status[0]) if status else "500"
            duration = time.perf_counter() - start
            HTTP_REQUESTS.labels(self.service, method, route_label, code).inc()
            HTTP_DURATION.labels(self.service, method, route_label, code).observe(duration)


def install_metrics(app: Any, service_name: str) -> None:
    """给 FastAPI 应用挂指标中间件与 /metrics 端点；服务创建时调用一次。"""
    app.add_middleware(MetricsMiddleware, service_name=service_name)

    # add_route 是原生 Starlette 路由，handler 必须接收 request 参数（即使不用）。
    async def metrics(request: Request) -> Response:
        return Response(generate_latest().decode(), media_type=CONTENT_TYPE_LATEST)

    app.add_route("/metrics", metrics)
