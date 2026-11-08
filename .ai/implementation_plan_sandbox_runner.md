# 实施计划：沙箱执行迁出 API 进程（独立 sandbox-runner）

## 1. 背景与目标

现状：[sandbox.py](file:///c:/Users/far/Desktop/git/shop-agent/services/harness/core/sandbox.py) 在 `SANDBOX_ENABLED=true` 时由 harness 进程直接 `import docker` 并访问 Docker 守护进程，违反 [system-design.md](file:///c:/Users/far/Desktop/git/shop-agent/docs/design/system-design.md#L302) 的「API 进程不持有 Docker socket」。

目标：新增独立 `sandbox-runner` 服务持有 Docker socket；harness 只通过内网 HTTP 调用它。harness 代码与镜像不再需要 Docker 权限，Docker 不可用时仍保持 fail-closed（上一轮已修复）。

**不在本次范围**：镜像 digest 固定、沙箱执行证据写入审计流、runner 多副本/队列。

## 2. 目标架构

```
浏览器 → gateway → harness ──HTTP + 签名身份头──→ sandbox-runner ──/var/run/docker.sock──→ 一次性容器
```

- harness：不再 `import docker`，不再访问 socket；`SANDBOX_ENABLED=true` 时只走 runner。
- runner：仅内网可达（不映射宿主端口、不经 gateway 代理），挂载 `docker.sock`，执行容器安全基线不变（network=none、只读根、低权用户、CPU/内存/PID 限制）。
- 本地开发：`SANDBOX_ENABLED=false` 仍走本地子进程（仅演示/测试，非安全边界）。

## 3. 改动文件树

```
新增
  services/sandbox_runner/__init__.py
  services/sandbox_runner/app.py          # FastAPI：POST /run、GET /healthz、鉴权与错误映射
  services/sandbox_runner/runner.py       # Docker 执行基线（从 harness 的 _run_docker/_truncate 迁移）
  tests/test_sandbox_runner.py            # runner 鉴权、结果映射、Docker 不可用语义

修改
  packages/shop_common/config.py          # + sandbox_runner_url
  services/harness/core/sandbox.py        # 删除 docker 直连；enabled 改为 HTTP 调 runner
  services/harness/app.py                 # /sandbox/health 改为探测 runner 健康
  pyproject.toml                          # packages include 增加 sandbox_runner*
  docker-compose.yml                      # + sandbox-runner 服务；harness 注入 SANDBOX_RUNNER_URL
  Makefile                                # + dev-sandbox-runner
  tests/test_sandbox.py                   # enabled 模式断言调用 runner、不回退本地
  docs/RUNBOOK.md                         # 沙箱章节改为 runner 拓扑与排障
```

## 4. 接口契约

`POST /run`（内网）

请求头：`signed_identity_headers()` 产出的 `X-User-Id / X-User-Role / X-Tenant / X-Request-Id / X-Internal-Signature`。

```json
{"code": "print(1)", "language": "python", "timeout": 20}
```

响应（与现有 `SandboxResult` 同形）：

```json
{"status": "ok|error|timeout|unavailable", "stdout": "", "stderr": "", "exit_code": 0,
 "duration_ms": 0, "backend": "docker", "truncated": false}
```

鉴权：`principal.require_staff()`，非 staff 返回 403；签名缺失/失效由 `PrincipalDep` 统一拦截。
错误语义：runner 不可达或非 2xx 时，harness 返回 `unavailable`，绝不回退本地执行。

`GET /healthz`：`{"status": "ok", "docker_sdk": bool, "image": str, "limits": {...}}`，仅供 harness 健康检查。

## 5. 风险与取舍

- runner 成为单点：宕机时沙箱能力不可用（符合 fail-closed 设计，不影响其余 Agent 能力）。
- runner 挂载 Docker socket 属高权限组件：必须仅内网、无宿主端口、无 gateway 路由；容器内只跑受控镜像。
- 部署配置变更（compose）属人工审批范围，按确认范围落地。
- 现有 3 个调用点（`/sandbox/run`、`sandbox_run` 工具、健康检查）自动切换，无需前端改动。

## 6. 验证计划

```bash
.venv/Scripts/python -m pytest -q
.venv/Scripts/python -m ruff check packages services tests
docker compose config   # 仅校验 compose 语法（如确认改 compose）
```

新增用例必须覆盖：enabled 调用 runner 成功、runner 返回 5xx/超时、runner 不可达、非 staff 拒绝、Docker 不可用返回 unavailable 且 `_run_local` 未被调用。

## 7. 追加：生产可用性（单点问题）

- `SANDBOX_RUNNER_URLS` 支持逗号分隔多地址：逐请求轮询、连接失败/429/5xx 可转移状态/「繁忙」自动换下一台，故障实例 30s 冷却。
- 500 不转移：可能是容器已执行后响应失败，重试会造成重复执行。
- runner 增加并发闸门 `SANDBOX_MAX_CONCURRENCY` + `SANDBOX_QUEUE_TIMEOUT_SECONDS`，排队超时返回「繁忙」触发转移，避免无界并发打垮宿主。
- 真主机级 HA 要求每台沙箱主机各部署一个 runner；同宿主多副本只抗进程崩溃，Docker 守护进程仍是单点（见 RUNBOOK 第 5 节）。
- 跨 runner 去重：harness 每次调用生成 `X-Execution-Id` 并在整次故障转移中复用；runner 用 Redis「执行锁（心跳续租）+ 结果缓存」保证同一 execution_id 最多真正执行一次。只有 ok/error/timeout 入缓存，unavailable 不入缓存以免挡住健康 runner；Redis 不可用时 runner fail-closed。
- 固有边界：若 runner 在「容器已执行完、结果尚未写入 Redis」的窗口内崩溃，锁按 TTL 过期后同一 execution_id 仍可能被另一台 runner 再执行一次。当前用「锁 TTL = 执行超时 + 20s + 心跳续租」把窗口压到毫秒级，但无法彻底消除；需要严格至多一次时，应改为「执行记录落库 + 容器内副作用幂等」。
- 验证：241 passed，ruff 通过，`docker compose config -q` 通过。

## 8. 跨 runner 重复执行防护（实现摘要）

| 环节 | 做法 | 目的 |
|---|---|---|
| 执行标识 | harness `_run_remote` 每次调用生成一个 `X-Execution-Id`，整次故障转移复用 | 换 runner 只是换执行者，语义仍是同一次执行 |
| 结果复用 | runner 先查 `sandbox:exec:{id}:result`，命中直接返回 | 故障转移后不再跑第二遍 |
| 互斥 | `sandbox:exec:{id}:lock` 走 `RedisLock`（SET NX + TTL + 心跳续租） | 多 runner 同时收到同一 id 时只有一个真正执行 |
| 竞争方 | 抢锁失败 → 0.25s 轮询等结果（`SANDBOX_DEDUPE_WAIT_SECONDS`），等不到返回 `unavailable("同一执行仍在进行中")` | 绝不重复执行，把「进行中」交给 harness 转移 |
| 缓存范围 | 仅 `ok/error/timeout` 入缓存；`unavailable` 不入 | 运行时故障不能被固化成结果，否则健康 runner 被假失败挡住 |
| 故障策略 | Redis 不可用 → 拒绝执行（fail-closed） | 宁可沙箱暂时不可用，也不失去跨实例去重保护 |
| 实例数量 | 每个 uvicorn worker 一份 `ConcurrencyGate`，总并发 = 上限 × worker 数 | 并发上限可预测，避免无界并发打垮宿主 |

harness 侧的配合约束：500 不转移（可能是执行后响应失败），只有连接级异常与 `{429,502,503,504}` 才换实例；`unavailable` 换下一台。
