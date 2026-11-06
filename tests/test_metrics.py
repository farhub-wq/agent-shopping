"""可观测指标单测：/metrics 端点、RED 中间件计数、业务指标注册与增量。

不依赖 Redis/DB：用最小 FastAPI 应用挂 install_metrics，进程内 REGISTRY 直接断言样本值。
"""

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from prometheus_client import REGISTRY, generate_latest
from shop_common.metrics import CHECKOUT_TOTAL, UNMATCHED_ROUTE, install_metrics

# §10 指标表全量清单（system-design.md §10）
EXPECTED_METRICS = {
    "http_requests_total",
    "http_request_duration_seconds",
    "http_requests_in_flight",
    "checkout_total",
    "payment_callback_total",
    "order_stuck_total",
    "outbox_lag_seconds",
    "db_pool_exhausted_total",
    "inventory_negative_total",
    "ledger_imbalance_total",
    "agent_tool_denied_total",
    "reconciliation_diff_total",
}


def _sample(name: str, labels: dict[str, str]) -> float | None:
    return REGISTRY.get_sample_value(name, labels)


def _make_client() -> TestClient:
    app = FastAPI()
    install_metrics(app, "test-svc")

    @app.get("/ping")
    async def ping() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/fail")
    async def fail() -> None:
        raise HTTPException(status_code=503)

    return TestClient(app)


def test_metrics_endpoint_serves_prometheus_text():
    with _make_client() as client:
        response = client.get("/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert "http_requests_total" in response.text


def test_all_section10_metrics_registered():
    # 给每个指标强制产生一个样本：同时验证注册成功与标签维度可用。
    # 真实埋点由各服务代码触发，这里只校验指标表与 labels 契约不被破坏。
    from shop_common import metrics as M

    M.HTTP_REQUESTS.labels("t", "GET", "/x", "200").inc()
    M.HTTP_DURATION.labels("t", "GET", "/x", "200").observe(0.1)
    M.HTTP_IN_FLIGHT.labels("t").inc()
    M.CHECKOUT_TOTAL.labels("t", "success").inc()
    M.PAYMENT_CALLBACK_TOTAL.labels("paid").inc()
    M.ORDER_STUCK.labels("t").set(0)
    M.OUTBOX_LAG.labels("t").set(0)
    M.DB_POOL_EXHAUSTED.labels("t").inc()
    M.INVENTORY_NEGATIVE.labels("t").inc()
    M.LEDGER_IMBALANCE.labels("t").inc()
    M.AGENT_TOOL_DENIED.labels("t").inc()
    M.RECONCILIATION_DIFF.labels("t").set(0)

    # 计数器在导出格式里带 _total 后缀，直接比对导出文本更贴合 Prometheus 抓取视角。
    exported = generate_latest().decode()
    for name in EXPECTED_METRICS:
        assert name in exported, f"{name} 未出现在指标导出中"


def test_red_middleware_counts_by_route_and_status():
    labels_ok = {"service": "test-svc", "method": "GET", "route": "/ping", "status": "200"}
    labels_err = {"service": "test-svc", "method": "GET", "route": "/fail", "status": "503"}
    before_ok = _sample("http_requests_total", labels_ok) or 0
    before_err = _sample("http_requests_total", labels_err) or 0

    with _make_client() as client:
        for _ in range(3):
            assert client.get("/ping").status_code == 200
        assert client.get("/fail").status_code == 503
        # 未匹配路由应收敛到 <unmatched>，不产生高基数路径标签
        assert client.get("/nope").status_code == 404

    assert (_sample("http_requests_total", labels_ok) or 0) - before_ok == 3
    assert (_sample("http_requests_total", labels_err) or 0) - before_err == 1
    unmatched = {
        "service": "test-svc",
        "method": "GET",
        "route": UNMATCHED_ROUTE,
        "status": "404",
    }
    assert (_sample("http_requests_total", unmatched) or 0) >= 1


def test_metrics_and_healthz_excluded_from_red():
    with _make_client() as client:
        client.get("/metrics")
        client.get("/healthz")
    # 两个抓取/探活路径都不应产生 RED 序列（中间件在入口直接放行）
    assert _sample("http_requests_total", {"service": "test-svc", "method": "GET", "route": "/metrics", "status": "200"}) is None
    assert _sample("http_requests_total", {"service": "test-svc", "method": "GET", "route": "/healthz", "status": "404"}) is None


def test_business_counter_increments():
    labels = {"service": "order", "result": "success"}
    before = _sample("checkout_total", labels) or 0
    CHECKOUT_TOTAL.labels("order", "success").inc()
    assert (_sample("checkout_total", labels) or 0) == before + 1
