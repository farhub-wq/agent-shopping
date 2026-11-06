# 实施计划：可观测与告警（Prometheus + Grafana）

> 对应 `.ai/PRD_observability.md`，等人工确认后动手。

## 一、改动文件树

```
packages/shop_common/metrics.py            新增：指标定义 + 中间件 + install_metrics()
packages/shop_common/db.py                 修改：会话获取包一层，pool_timeout 超时计 db_pool_exhausted_total
packages/shop_common/outbox.py             修改：relay 每轮更新 outbox_lag_seconds
pyproject.toml                             修改：dependencies 加 prometheus-client
services/<10 个服务>/app.py                修改：app 初始化处调用 install_metrics(app, "<svc>")
services/order/app.py                      修改：checkout_total + order_stuck_total 埋点
services/payment/app.py                    修改：payment_callback_total + reconciliation_diff_total
services/payment/ledger.py                 修改：ledger_imbalance_total
services/inventory/scripts.py（或 app.py） 修改：inventory_negative_total
services/harness/core/tools.py             修改：agent_tool_denied_total
infra/prometheus/prometheus.yml            新增：抓取配置（10 服务 + 自身）
infra/prometheus/alerts.yml                新增：§10 阈值告警规则
infra/alertmanager/alertmanager.yml        新增：默认路由 + 占位 receiver
infra/grafana/provisioning/datasources/*.yml 新增：Prometheus 数据源
infra/grafana/provisioning/dashboards/*.yml  新增：仪表盘自动装载
infra/grafana/dashboards/services.json     新增：服务 QPS/错误率/时延 + 业务指标面板
docker-compose.yml                         修改：加 prometheus/grafana/alertmanager 服务
docker-compose.prod.yml                    修改：prometheus/alertmanager 内网、grafana 暴露
.env.example                               修改：GRAFANA_ADMIN_PASSWORD
tests/test_metrics.py                      新增：/metrics 端点与中间件计数单测
```

## 二、技术方案

1. **共享指标模块**：`metrics.py` 进程内定义 §10 全部指标（`service` 为标签），`MetricsMiddleware` 用纯 ASGI 中间件（不用 `BaseHTTPMiddleware`，避免缓冲 SSE 流式响应）。route 标签取 `scope["route"].path` 模板（有界），未匹配收敛为 `<unmatched>`，防止 404 探测打爆标签基数；`/metrics`、`/healthz` 不计入。
2. **db 连接池耗尽**：`get_session_factory()` 返回薄包装，`__aenter__` 捕获 `asyncio.TimeoutError` 计 `db_pool_exhausted_total` 后重抛；对外类型不变。
3. **outbox 延迟**：relay 每轮按「最老未投递事件距今时长」更新 `outbox_lag_seconds`。
4. **业务埋点**：在既有失败分支处加一行 `*.labels(...).inc()`，不改控制流、不吞异常。
5. **抓取与告警**：Prometheus 静态配置 10 个 `service:8000/metrics` target（容器内网）；`alerts.yml` 按 §10 阈值写 `expr`；Alertmanager 用占位 webhook receiver。
6. **Grafana**：数据源指向 Prometheus，`services.json` 预置面板（每服务 QPS/5xx 率/P95、订单卡死、outbox 延迟、对账差异、库存负数、总账不平）。

## 三、风险点

- **路径标签基数**：网关通用路由模板单一，安全；其余服务 route 模板有界。404 收敛为 `<unmatched>`，杜绝高基数。
- **SSE 流式**：中间件只在 response.start 与结束钩点记录，不读 body，不破坏流式转发。
- **多服务同进程测试**：默认 REGISTRY 全局共享，指标按 `service` 标签区分，不冲突。
- **进程指标重复**：prometheus_client 默认 collector（process/gc/platform）只注册一次，`generate_latest()` 自动包含，无需手动处理。
- **告警误报**：阈值按 §10 初值，环境噪音大时在 `alerts.yml` 调 `for` 窗口，不改代码。
