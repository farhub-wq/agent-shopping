# PRD：可观测与告警（Prometheus + Grafana）

> 状态：待确认。对应 system-design.md §10「可观测与告警」。
> 范围：全服务 HTTP RED 指标 + §10 指标表的业务指标埋点 + Prometheus 抓取与告警规则 + Grafana 预置仪表盘。

## 1. 业务上下文

系统由 10 个服务组成（gateway/catalog/inventory/order/fulfillment/aftersales/harness/identity/payment/audit），各自有 `/healthz` 但没有统一指标出口。system-design §10 已明确 11 个指标与告警阈值，本模块把它们落到代码 + 抓取 + 可视化 + 告警。

约束：

- 每个服务一个进程，无 `--workers` 多进程，用 prometheus_client 默认进程内 REGISTRY 即可，不引入 multiprocess 模式。
- `/metrics` 只在容器内网暴露（各服务生产 `ports: !reset []`），由 Prometheus 直接抓 `service:8000/metrics`，**不经过网关**；网关不代理 `/metrics`。
- 依赖只加 `prometheus-client`（纯 Python，无外部服务），与「自研 TOTP」同样克制，不引重量级 SDK。

## 2. 数据模型

无数据库变更。指标在进程内存中，由 Prometheus 定时抓取。

## 3. 接口契约

新增共享模块 `packages/shop_common/metrics.py`：

- `install_metrics(app, service_name)`：挂载 ASGI 中间件 + `/metrics` 端点（返回 Prometheus 文本格式）。所有服务 `app.py` 调用。
- 指标命名与 §10 表一致（带 `service` 标签区分）：

| 指标 | 类型 | 埋点位置 |
|---|---|---|
| `http_requests_total{service,method,route,status}` | counter | 中间件，全服务 |
| `http_request_duration_seconds{service,method,route,status}` | histogram | 中间件，全服务 |
| `http_requests_in_flight{service}` | gauge | 中间件，全服务 |
| `checkout_total{service,result}` | counter | order 下单成功/失败 |
| `payment_callback_total{result}` | counter | payment 回调 paid/failed/验签失败/金额不符/未找到 |
| `order_stuck_total{service}` | gauge | order 补偿扫单：RESERVED>5min 或 PAYING 超窗 |
| `outbox_lag_seconds{service}` | gauge | outbox relay 每轮投递延迟 |
| `db_pool_exhausted_total{service}` | counter | db.py 会话获取 pool_timeout 超时 |
| `inventory_negative_total{service}` | counter | inventory 库存扣减不足 |
| `ledger_imbalance_total{service}` | counter | payment 总账借贷不平 |
| `agent_tool_denied_total{service}` | counter | harness 工具被策略拒绝 |
| `reconciliation_diff_total{service}` | gauge | payment 日终对账开放差异数 |

## 4. 前端要求

无页面改动。Grafana 提供可视化（预置仪表盘），不走 web 前端。

## 5. 验收标准

1. 每个服务 `GET /metrics` 返回 200，含 `http_requests_total`、`http_request_duration_seconds` 与进程指标。
2. 中间件对 `/metrics`、`/healthz` 不计入 RED，避免抓取自身噪声。
3. §10 指标全部埋点可被 Prometheus 抓到；Prometheus targets 全 up（10 服务 + prometheus 自身）。
4. Prometheus 告警规则覆盖 §10 阈值；Grafana 预置仪表盘能展示各服务 QPS/错误率/时延/关键业务指标。
5. 全量验证绿：`.venv/Scripts/python -m pytest -q`、`.venv/Scripts/python -m ruff check packages services tests`、`cd web && npm run build`；`docker compose config` 可解析。

## 6. 边界（V1 明确不做）

- 不引入 Tempo/Loki 等追踪/日志系统（§10 的 traceparent/JSON 日志另行评估）。
- 告警通知渠道（邮件/Slack/电话）只留占位 receiver，真实通道在部署阶段配置。
- 不实现 metrics 推送到外部（Remote Write），保持抓取模式。
