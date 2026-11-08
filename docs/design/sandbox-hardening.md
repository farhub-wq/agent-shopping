# 沙箱加固：问题清单、实现难点与安全泄露点

> 状态跟踪文档。每条问题独立成项（有 ID、证据、方案、验证方式），按优先级逐个解决，解决后勾选并记录日期。
> 关联：[system-design.md](file:///c:/Users/far/Desktop/git/shop-agent/docs/design/system-design.md#L302) · [RUNBOOK](../RUNBOOK.md) · [implementation_plan_sandbox_runner](file:///c:/Users/far/Desktop/git/shop-agent/.ai/implementation_plan_sandbox_runner.md)

## 0. 现状基线

```
浏览器 → gateway → harness ──签名身份头 + X-Execution-Id──→ sandbox-runner ──rootless docker.sock──→ 一次性容器
                                    │                              │
                                    └──── 内网 HTTP 故障转移 ────────┴── Redis（执行锁 + 结果缓存）
```

- 隔离档位：**生产 gVisor（runsc，用户态内核）**；本地开发未装 runsc 时回落容器级（共享宿主内核）。非 microVM。
- Docker 端点：**生产必须接 rootless dockerd**，默认 root socket 仅限本地开发（启动守卫拦截，见 SBX-04）。
- runner 是唯一持有 Docker socket 的组件；生产挂载 rootless dockerd 的 socket，不再是宿主 root 守护进程。
- 执行失败 fail-closed，不回退本地子进程；`SANDBOX_ENABLED=false` 仅限本地演示。
- 已具备：`network=none`、只读根、`/tmp` 32MB tmpfs、CPU/内存/PID 限制、`no-new-privileges`、uid 65534、`python -I`、输出 8KB 截断、跨实例执行去重、all-staff 鉴权。

**判断**：阶段一配置级洞口（SBX-01/02/03/05/06）与阶段三审计/模型安全（SBX-07/08/14）已全部关闭；剩余集中在 gVisor 部署验证、孤儿容器回收、全局配额与冷启动。

---

## 1. 问题总览

| ID | 优先级 | 类别 | 一句话 | 状态 |
|---|---|---|---|---|
| SBX-01 | P0 | 供应链 | 沙箱镜像未固定 digest，仅用 `:latest` tag | ☑ 2026-11-08 |
| SBX-02 | P0 | 资源耗尽 | 容器日志无上限，恶意脚本可写爆宿主磁盘 | ☑ 2026-11-08 |
| SBX-03 | P0 | 身份/密钥 | `INTERNAL_SIGNING_SECRET` 与 `TRUST_IDENTITY_HEADERS` 有 dev 默认值 | ☑ 2026-11-08 |
| SBX-04 | P1 | 宿主逃逸 | runner 持有全权限 docker.sock，被攻破等价宿主 root | ☑ 2026-11-08 |
| SBX-05 | P1 | 隔离可漂移 | `SANDBOX_NETWORK` 等隔离参数可被环境变量改成 `bridge`/`host` | ☑ 2026-11-08 |
| SBX-06 | P1 | 容器加固 | 未显式 `cap_drop=ALL`；`/dev/shm`、nofile 未限；敏感路径未 mask | ☑ 2026-11-08 |
| SBX-07 | P1 | 审计/串读 | 执行结果只在 Redis 存 5 分钟，缓存键无主体归属，无法取证 | ☑ 2026-11-08 |
| SBX-08 | P1 | 提示注入 | stdout 作为低信任数据直接回灌模型，未做标记与隔离 | ☑ 2026-11-08 |
| SBX-09 | P2 | 隔离升级 | 未启用 gVisor（`runsc`），共享内核 CVE 面未收敛 | ◐ 2026-11-08 |
| SBX-10 | P2 | 生命周期 | runner 崩溃时容器可能残留，无 reconcile 回收 | ☐ |
| SBX-11 | P2 | 并发配额 | 并发闸门为进程内 Semaphore，多 worker 时总并发 = 上限 × worker 数，跨 runner 无全局配额 | ☐ |
| SBX-12 | P2 | 冷启动 | 一次一容器无预热池，冷启动直接计入用户等待 | ☐ |
| SBX-13 | P2 | 参数硬编码 | 结果缓存 TTL 固定 300s、锁 TTL 硬编码推导，未随执行超时配置联动 | ☐ |
| SBX-14 | P2 | 输入边界 | harness 侧 `SandboxRequest.code` 无长度上限，超限请求要跨网传到 runner 才被拒 | ☑ 2026-11-08 |

---

## 2. 问题详情

### SBX-01 镜像未固定 digest（P0）

- **证据**：[config.py](file:///c:/Users/far/Desktop/git/shop-agent/packages/shop_common/config.py#L107) `sandbox_image: str = "shop-agent-sandbox:latest"`；[docker-compose.yml](file:///c:/Users/far/Desktop/git/shop-agent/docker-compose.yml#L148) 同样默认 tag。
- **风险**：tag 可被覆盖，镜像仓库被入侵或本地构建漂移即可替换执行环境；与自家设计文档 [system-design.md:302](file:///c:/Users/far/Desktop/git/shop-agent/docs/design/system-design.md#L302) 的「镜像固定 digest」要求不符。
- **已实施（2026-11-08）**：`is_image_pinned()` 只认 `repo@sha256:...`（registry manifest digest）或 `sha256:...`（本地构建的镜像 ID）；生产启动守卫在端点/运行时校验之后追加镜像校验，tag 直接拒绝启动；`/healthz` 暴露 `image_pinned`，非 dev 用 tag 时返回 503。新增 `make build-sandbox-image`，构建后打印可直接写入 `.env` 的 `SANDBOX_IMAGE`。
- **遗留**：镜像仓库白名单与「禁止运行期 `pip install`」是配套项，当前靠 `network=none` + 只读根文件系统阻断；换镜像仍需人工更新 `.env` 并重启 runner。
- **验证**：`test_prod_rejects_tagged_image`、`test_prod_accepts_local_image_id`、`test_healthz_returns_503_when_image_not_pinned`。

### SBX-02 容器日志无上限（P0）

- **证据**：[runner.py](file:///c:/Users/far/Desktop/git/shop-agent/services/sandbox_runner/runner.py#L175-L192) `containers.create` 原先未传 `log_config`，执行结束才用 `container.logs()` 截断。
- **风险**：Docker 默认 json-file 驱动无大小上限；脚本可在 20s 内打印几十 GB，写满宿主磁盘，影响同宿主全部容器与数据库。
- **已实施（2026-11-08）**：创建容器时固定 `log_config={"Type": "json-file", "Config": {"max-size": "1m", "max-file": "1"}}`，单个沙箱容器的 stdout/stderr 日志文件封顶 1MB；配合执行结束强制删除容器，磁盘占用有明确上界。返回给模型的输出仍按 8KB 截断。
- **遗留**：`/dev/shm` 限额与 `nofile` 归 SBX-06；执行中流式读取并超限提前 kill 属额外加固，当前 1MB 上限 + 20s 超时已足够。
- **验证**：`tests/test_sandbox_runner.py::test_run_caps_container_log_size` 断言 create 参数含日志上限；部署机可跑死循环 `print` 集成用例，观察宿主 json-file 不超过 1MB。

### SBX-03 生产密钥默认值（P0）

- **证据**：[docker-compose.yml](file:///c:/Users/far/Desktop/git/shop-agent/docker-compose.yml#L145-L146) `INTERNAL_SIGNING_SECRET: ${INTERNAL_SIGNING_SECRET:-dev-internal-signing-secret}`、`TRUST_IDENTITY_HEADERS: ${TRUST_IDENTITY_HEADERS:-true}`。
- **风险**：生产沿用默认密钥即可伪造任意 staff 身份头直连 runner；信任头若被反向代理透传，外部可伪造身份。
- **方案**：启动期校验——`ENV != dev` 且密钥命中默认值/长度不足 → 拒绝启动（fail-fast）；`TRUST_IDENTITY_HEADERS` 在生产强制 `false` 并只接受网关签名头；`/healthz` 暴露校验结果。
- **已实施（2026-11-08）**：
  - 新增共享守卫 [config.py](file:///c:/Users/far/Desktop/git/shop-agent/packages/shop_common/config.py#L127-L179) 的 `production_security_issues` / `validate_production_settings`：非 dev 下 `JWT_SECRET`、`INTERNAL_SIGNING_SECRET` 不得为空、不得是仓库公开默认值、长度不低于 32；identity 额外校验 `MFA_ENCRYPTION_KEY`，payment 额外校验 `PAYMENT_WEBHOOK_SECRET`；`AUTH_DEV_MODE`、`PAYMENT_MOCK_MODE` 在生产必须关闭。
  - 11 个服务的 lifespan 首行调用守卫，不满足即拒绝启动；runner 的 `/healthz` 新增 `security.secrets_configured` 与问题清单，并在异常时返回 503。
  - **与原方案的偏差**：没有全局强制 `TRUST_IDENTITY_HEADERS=false`。现有架构是 gateway 校验客户端身份后签名并转发 `X-User-*`，下游必须信任签名头才能拿到 staff 身份；全局关闭会让所有后台操作退化为访客。实际做法是保留信任头，但把签名密钥强度作为启动硬前置；无签名、超时间窗、摘要不匹配仍一律拒绝（[auth.py](file:///c:/Users/far/Desktop/git/shop-agent/packages/shop_common/auth.py#L122-L154)）。
  - `docker-compose.prod.yml` 把 `ENV=prod` 写进每个服务的 overlay，避免部署机漏设 `ENV` 时 dev 默认值静默生效；compose 仍保留 dev 默认密钥，仅用于本地启动。
  - runner 的 compose 环境补上 `JWT_SECRET`：runner 同样接受 Bearer JWT，缺密钥时默认值会让人伪造 staff 身份执行代码。
- **验证**：`tests/test_production_config.py` 覆盖「prod 默认密钥 / 短密钥 / 开发开关 → 启动失败」「身份头与强密钥共存 → 通过」「MFA 与支付回调密钥只由对应服务校验」；`test_healthz_returns_503_when_secrets_are_dev_defaults` 断言健康检查判死。

### SBX-04 docker.sock 全权限（P1）

- **证据**：[docker-compose.yml](file:///c:/Users/far/Desktop/git/shop-agent/docker-compose.yml#L158-L160) 挂载 `/var/run/docker.sock`。
- **风险**：能访问 socket 即可创建 privileged 容器、挂宿主根目录，等价宿主 root；runner 任意 RCE（依赖漏洞、签名缺失、未来接口扩展）直接升级为节点失陷。
- **方案**（择一或叠加）：① `docker-socket-proxy` 只放行 `containers create/start/wait/logs/delete`；② rootless Docker / `sysbox`；③ runner 部署在独立沙箱主机或 microVM 内；④ 生产禁用镜像拉取（只允许本地已有镜像）。
- **已实施（2026-11-08，选择 rootless Docker）**：
  - 新增配置 `SANDBOX_DOCKER_HOST`，runner 改为显式构造 Docker 客户端；留空才沿用 SDK 默认 socket（仅本地开发）。
  - 启动守卫 `validate_docker_endpoint`：`ENV != dev` 且端点为空或指向默认 `/var/run/docker.sock` → 拒绝启动。
  - 生产 overlay 把宿主 rootless socket（默认 `/run/user/1000/docker.sock`）挂到容器内 `/run/rootless/docker.sock`，与默认路径解耦，守卫据此可识别。
  - `/healthz` 新增 `docker.rootless` / `docker.insecure`：非 dev 连到 root 守护进程时上报 `unavailable`；同时修正旧实现「SDK 可导入即报 ok」的假阳性，改为真正调用 `info()`。
  - 注意：socket 代理按 API 段放行、不解析请求体，单靠它挡不住 `Privileged:true`，因此没有采用；rootless 让 privileged 容器也无法拿到宿主 root。
- **验证**：`tests/test_sandbox_runner.py` 覆盖「prod + 空端点/默认 socket → 拒绝启动」「prod + rootless socket → 放行」「healthz 报告 rootless 与隔离退化」；部署后 `docker info` 的 `SecurityOptions` 应含 `rootless`。

### SBX-05 隔离参数可漂移（P1）

- **证据**：[config.py](file:///c:/Users/far/Desktop/git/shop-agent/packages/shop_common/config.py#L98-L111) 的 `sandbox_network`、`sandbox_cpu`、`sandbox_memory_mb` 均可配置。
- **风险**：为装依赖临时改 `bridge`，或为跑大脚本抬高内存，隔离退化后无人感知；配置即安全边界，当前没有守卫。
- **方案**：`network` 生产固定 `none`，仅 `ENV=dev` 可覆盖并在健康检查中标红；内存/CPU 设硬上限（配置超过阈值直接拒绝启动）；出网需求走 allowlist egress 代理，不开原生网络。
- **已实施（2026-11-08）**：`validate_sandbox_isolation` 在非 dev 下追加三条硬校验——`SANDBOX_NETWORK` 必须为 `none`；`SANDBOX_CPU` 必须在 `(0, 2.0]`；`SANDBOX_MEMORY_MB` 必须在 `(0, 2048]`。阈值以常量写在 [runner.py](file:///c:/Users/far/Desktop/git/shop-agent/services/sandbox_runner/runner.py#L35-L40)，扩容沙箱主机时才允许改代码，不接受环境变量悄悄抬高。
- **验证**：`test_prod_rejects_network_bridge`、`test_prod_rejects_excessive_cpu`、`test_prod_rejects_excessive_memory`、`test_dev_allows_relaxed_sandbox_params`。

### SBX-06 容器加固缺口（P1）

- **原现状**：[runner.py](file:///c:/Users/far/Desktop/git/shop-agent/services/sandbox_runner/runner.py#L85-L97) 已有 `read_only`、`no-new-privileges`、uid 65534、`pids_limit=64`，但缺：
  - 未显式 `cap_drop=["ALL"]`（依赖镜像默认能力集）；
  - 未限制 `ulimit nofile`（可耗尽宿主文件句柄）；
  - 未限制 `/dev/shm`（默认 64MB 可写）；
  - 未配置 `masked_paths` / `readonly_paths` 加固 `/proc` 敏感入口；
  - 未启用 userns-remap / user namespace 隔离。
- **方案**：按上述逐项补齐；seccomp 保留默认 profile，另加自定义 deny 列表（`keyctl`、`unshare`、`mount` 等）。
- **已实施（2026-11-08）**：
  - `cap_drop=["ALL"]`：不依赖镜像默认能力集，镜像换代不会让能力悄悄漂移；`mount`/`keyctl` 等需要 capability 的 syscall 直接失效。
  - `shm_size=16MB`：把默认 64MB 的 `/dev/shm` 压到 16MB，防止用它放大内存占用。
  - `ulimits nofile=1024`：限制单容器文件句柄数，避免耗尽宿主 nofile 配额。
  - `no-new-privileges`、只读根、`/tmp` 32MB tmpfs、`pids_limit=64`、uid 65534 保持不变。
  - **未显式注入 masked/readonly paths**：docker-py 7.2 的 `HostConfig` 不支持这两个字段；Docker 默认已 mask `/proc/kcore`、`/proc/keys`、`/sys/firmware` 等敏感入口，且 `cap_drop=ALL` 后无法 remount `/proc`。userns 隔离由 rootless dockerd 提供（SBX-04），不再单列。
  - **seccomp**：保留 Docker 默认 profile；`keyctl`/`mount`/`unshare` 的阻断由默认 profile + 丢弃 capability 共同覆盖，自定义 deny 列表留待后续按实际逃逸面评估。
- **验证**：`test_run_applies_container_hardening` 断言 create 参数含 `cap_drop=ALL`、`shm_size`、`nofile`；容器内探测（`ulimit -n`、`/dev/shm` 容量）可部署后在部署机复跑。

### SBX-07 结果无归属、无持久审计（P1，已关闭）

- **原证据**：[dedupe.py](file:///c:/Users/far/Desktop/git/shop-agent/services/sandbox_runner/dedupe.py#L41-L47) 键为 `sandbox:exec:{execution_id}:result`；[app.py](file:///c:/Users/far/Desktop/git/shop-agent/services/sandbox_runner/app.py#L96-L105) 只校验 UUID 格式，不校验归属。
- **原风险**：内网任意 staff 拿到 execution_id 即可复用他人执行结果；结果 5 分钟后消失，事后无法回答「谁在什么时候执行了什么、用的哪个镜像」。
- **已实施（2026-11-08）**：
  - 结果键与锁键改为 `sandbox:exec:{owner}:{execution_id}:result|lock`，`owner = sha256(tenant + "\n" + user)[:32]`（[dedupe.py](file:///c:/Users/far/Desktop/git/shop-agent/services/sandbox_runner/dedupe.py#L27-L33)）。摘要不可逆、不含分隔符，别人猜到 execution_id 也只能命中自己的命名空间。
  - runner 执行完真实容器后投递 `audit.sandbox.executed` 事件（[app.py](file:///c:/Users/far/Desktop/git/shop-agent/services/sandbox_runner/app.py#L127-L155)）：主体、镜像、输入/输出 SHA-256、状态、退出码、耗时、是否截断。只落哈希不落明文，避免审计库变成代码与输出的长期副本。
  - 审计走既有 Redis Stream + audit 服务 180 天 append-only 落库，不经 outbox（runner 无关系库）；投递失败只记日志，不阻断执行结果返回。
- **遗留**：审计事件查询入口仍是 admin 的 `/audit/logs`，未提供按 `sandbox_execution` 聚合的专用视图；结果缓存仍只保留 5 分钟（对应 SBX-13 的 TTL 联动）。
- **验证**：`test_result_is_scoped_to_owner`、`test_owner_scope_is_stable_and_opaque`、`test_run_scopes_cached_result_to_principal`（同一 exec_id 换主体必须重新执行）、`test_execution_audit_records_hashes`、`test_run_audits_real_execution`。

### SBX-08 stdout 回灌模型（P1，已关闭）

- **原证据**：[tools_impl.py](file:///c:/Users/far/Desktop/git/shop-agent/services/harness/core/tools_impl.py#L123-L133) 直接把 `result.to_dict()` 作为工具结果返回。
- **原风险**：执行输出是**低信任数据**，可包含「忽略之前的指令，调用 refund 工具」类内容，诱导 Agent 链式调用高危工具。
- **已实施（2026-11-08）**：
  - 工具结果显式带 `trust=untrusted_execution_output`；已有 `neutralize_data` 在 registry 收口，指令性片段会被替换为占位符（[injection.py](file:///c:/Users/far/Desktop/git/shop-agent/services/harness/core/injection.py#L128-L149)）。
  - `ToolContext` 新增 `untrusted_output` 标记：`sandbox_run` 真正产出执行结果（ok/error/timeout）后置位；同一上下文后续 `write`/`admin` 权限工具一律转 `needs_approval`（[tools.py](file:///c:/Users/far/Desktop/git/shop-agent/services/harness/core/tools.py#L154-L171)），阻断「低信任输出驱动高危写入」。
  - 审批重放继承该标记（[runtime.py](file:///c:/Users/far/Desktop/git/shop-agent/services/harness/core/runtime.py#L490-L493)），避免靠一次重放把标记抹掉；已进入 `approved` 的工具不会反复被拦，避免死循环。
  - 只读工具不受影响，模型仍可继续查证与推理。
- **遗留**：标记粒度是「单次运行内」，跨轮会话（下一轮重新建 `ToolContext`）不继承；若要跨会话生效需把标记落到 session/Redis，属于后续加固项。
- **验证**：`test_write_tool_after_untrusted_output_requires_approval` 覆盖「沙箱输出后写操作转人工复核 → 只读仍可执行 → 审批后重放成功」。

### SBX-09~13（P2，待排期）

- **SBX-09 gVisor（代码与部署已就绪 2026-11-08）**：新增 `SANDBOX_RUNTIME`，生产启动守卫强制 `runsc`；`infra/rootless-docker/setup.sh` 负责安装 runsc 并注册到 rootless daemon 的 `daemon.json`；healthz 通过 `/info` 的 `Runtimes` 校验运行时是否真的注册。**待办**：在部署机跑 `python -I` 与镜像预装依赖（numpy/pandas 等）的兼容性回归，通过后勾选 ☑。
- **SBX-10 孤儿容器回收**：容器打 label（`sandbox.execution_id`），runner 启动时与定时任务清理超龄容器；或 `auto_remove=True` + 结果读取前先取日志（当前先 wait 后 logs，顺序要调整）。
- **SBX-11 全局配额**：多 worker 下并发上限除以 worker 数，或引入 Redis 全局信号量；按租户加权配额。
- **SBX-12 预热池**：镜像分层 + 容器池 / microVM snapshot；先量 P95 冷启动再决定是否投入。
- **SBX-13 TTL 联动**：结果缓存 TTL 与锁 TTL 从 `sandbox_timeout_seconds` 推导，避免改超时后去重窗口失配。

### SBX-14 输入边界（P2，已关闭）

- **原证据**：runner 侧 `RunRequest.code` 已有 `max_length=65536`，但 harness 的 [app.py](file:///c:/Users/far/Desktop/git/shop-agent/services/harness/app.py#L94-L98) `SandboxRequest` 与 `SandboxManager.run` 都没有长度校验，超大请求要跨网到 runner 才被拒。
- **已实施（2026-11-08）**：上限收敛为共享常量 `MAX_CODE_CHARS`（[sandbox.py](file:///c:/Users/far/Desktop/git/shop-agent/packages/shop_common/sandbox.py#L9-L10)），harness 入口 Pydantic `max_length`、客户端 `SandboxManager.run` 前置校验、`sandbox_run` 工具 schema `maxLength` 三处统一；空代码同样在本地拒绝，不占用 runner 队列。
- **验证**：`test_oversized_code_rejected_before_network`、`test_empty_code_rejected_before_network`（断言 mock transport 未被调用）。

---

## 3. 实现难点（设计取舍，不是单点 bug）

| # | 难点 | 为什么难 | 可选方向 |
|---|---|---|---|
| D1 | 冷启动 vs 隔离强度 | 容器创建 ~100ms~1s，microVM 更快但需快照池；交互场景对延迟敏感 | 预置镜像分层、容器池、Firecracker snapshot |
| D2 | 依赖预置 vs 离线安全 | 用户要 numpy/pandas，`pip install` 既需要网络又引入供应链攻击 | 多档位胖镜像 + 无网执行；禁止运行期安装 |
| D3 | 资源隔离真实性 | 内存炸弹/fork bomb/磁盘写满/句柄耗尽可绕过「只设上限」的表层防护 | cgroup v2 + seccomp + 日志/存储配额 + 强杀与回收 |
| D4 | 超时后的清理 | `container.wait` 超时只能靠 `remove(force=True)`；进程组、孤儿容器、卷残留 | PID 命名空间已隔离；补 label + reconcile + auto_remove |
| D5 | 至多一次执行 | 「容器已执行完、结果未落库」崩溃窗口无法靠 Redis 锁消除（现有锁 TTL + 心跳只能压缩到毫秒级） | 执行记录落库 + 容器内副作用幂等；接受至少一次并显式声明 |
| D6 | 多租户公平性 | 进程内信号量 × worker 数不可预测；跨 runner 无全局额度 | Redis 全局信号量 / 按租户令牌桶 / 队列化 |
| D7 | 可交互会话 | 主流沙箱向「有状态会话/notebook」演进，一次性 `python -c` 做不到 | 常驻会话 + 持久卷，隔离与回收复杂度上一个台阶 |
| D8 | 审计证据链 | 执行证据要能回放核对（输入/输出哈希、镜像 digest、资源），当前只留 5 分钟缓存 | 落库 + 对接 180 天 append-only 审计 |
| D9 | 逃逸兜底的运维成本 | gVisor 有 syscall 兼容性/性能折扣；microVM 要主机编排与镜像分发 | 先 gVisor 兜底，再评估 microVM |
| D10 | 配置即边界 | 隔离参数散落在环境变量，漂移后无守卫 | 启动期强校验 + 健康检查暴露偏离项 |

---

## 4. 安全泄露点对照表

| 威胁面 | 具体泄露点 | 现状 | 目标 | 对应条目 |
|---|---|---|---|---|
| 宿主逃逸 | 共享内核 CVE、runc/Docker 漏洞、docker.sock 等价 root、privileged、host path 挂载、cgroup v1 逃逸 | 已切 rootless dockerd + 生产强制 runsc，启动守卫与 healthz 双重校验（待宿主兼容性回归） | rootless + gVisor 已完成代码侧收敛 | SBX-04、SBX-09 |
| 网络外泄 | 内网服务探测、云元数据 `169.254.169.254`、DNS 外带、SSRF、DDoS 跳板 | 生产硬锁 `none`，非 dev 改成 `bridge`/`host` 直接拒绝启动 | 硬锁 none + allowlist egress 代理 | SBX-05 |
| 供应链 | `pip install` 恶意包、基础镜像被替换/tag 漂移 | 生产已强制 digest pinning；无镜像白名单；运行期安装被 `network=none` + 只读根文件系统阻断 | 补镜像白名单 | SBX-01、D2 |
| 资源 DoS | 日志刷爆磁盘、`/dev/shm` 滥用、句柄耗尽、内存/CPU 抢占、并发容器压垮守护进程 | CPU/内存/PID 已限；日志已封顶 1MB/容器；shm 16MB + nofile 1024 已限；生产 CPU/内存有硬上限 | 全局配额 | SBX-02、SBX-06、SBX-11 |
| 多租户串扰 | 结果缓存/日志/临时卷跨主体可读，exec_id 可复用他人结果 | 结果/锁键绑定 `sha256(tenant+user)`，他人 exec_id 只命中自己的命名空间 | 键绑定主体 + 归属比对 | SBX-07 |
| 身份伪造 | 默认签名密钥、信任头被上游透传、内网位置即信任 | 非 dev 启动守卫强制强密钥（≥32 且非默认值）；信任头保留但必须带有效签名，无签名/超窗一律拒绝 | 密钥轮换 + 上游不透传 X-User-* | SBX-03 |
| 提示注入 | stdout 作为可信内容回灌模型，诱导链式调用高危工具 | 输出带 `trust=untrusted_execution_output`，指令片段经 `neutralize_data` 转义；写操作在污染上下文转人工复核 | 标记不可信 + policy 阻断 | SBX-08 |
| 审计缺失 | 无持久执行记录，事后无法取证 | 真实执行投递 `audit.sandbox.executed`（主体/镜像/输入输出哈希/耗时），落 180 天审计流 | 落库对接审计流 | SBX-07、D8 |

---

## 5. 解决顺序

**阶段一（配置级，不改架构）**：SBX-01 → SBX-02 → SBX-03 → SBX-06 → SBX-05
低成本、可独立验证，先把「可直接利用」的洞口堵上。

**阶段二（隔离升级）**：SBX-04 → SBX-09
治 docker.sock 与共享内核，需宿主侧变更（compose / runtime / 代理），按第 8 节要求先给方案等人工批准。

**阶段三（审计与模型安全）**：SBX-07 → SBX-08 → SBX-14（已完成 2026-11-08）
**阶段四（容量与体验）**：SBX-11 → SBX-10 → SBX-13 → SBX-12

单项解决流程：读相关文件 → 给出改动方案（涉及文件 + 关键改动）→ 人工确认 → 编码 → 跑验证 → 勾选本文档并记录结果。

---

## 6. 变更后风险与注意事项

前一轮已关闭 SBX-01/02/04、把 SBX-09 推进到代码就绪，隔离档位从「容器级 + 全权限 socket」变为「rootless dockerd + gVisor + digest 镜像 + 日志/健康检查硬约束」；本轮再关闭 SBX-03/05/06，阶段一配置级洞口全部清零；随后关闭 SBX-07/08/14，执行结果按主体隔离、真实执行留持久审计、沙箱输出不再直接驱动写操作。这些改动同时引入了新的部署前置条件和失败模式，后续运维与开发必须知道。

### 6.1 新增的生产硬前置（不满足就起不来）

| 前置 | 配置 | 缺失后果 |
|---|---|---|
| 生产密钥 | `.env` 注入随机 `JWT_SECRET`、`INTERNAL_SIGNING_SECRET`（均 ≥32 字符）；identity 另需 `MFA_ENCRYPTION_KEY`，payment 另需 `PAYMENT_WEBHOOK_SECRET` | 对应服务启动抛错，容器反复重启 |
| rootless dockerd | `SANDBOX_DOCKER_HOST=unix:///run/rootless/docker.sock` | runner 启动抛错，容器反复重启 |
| gVisor 运行时 | `SANDBOX_RUNTIME=runsc`，且宿主已注册 `runsc` | 启动抛错；注册缺失时 healthz 503 |
| 镜像 digest | `SANDBOX_IMAGE=repo@sha256:...` 或 `sha256:...` | 启动抛错 |
| 沙箱启用 | `SANDBOX_ENABLED=true`（prod overlay 已写死） | harness 启动抛错，禁止回退本地子进程 |
| 宿主初始化 | `sudo make setup-sandbox-host` | 以上三项无法成立 |

这是有意的 breaking change：宁可 runner 起不来，也不让隔离静默退回共享内核或 root socket。沙箱不可用期间 `sandbox_run` 返回 `unavailable`（fail-closed）；订单、支付等主链路不经过沙箱，不受影响。

### 6.2 rootless dockerd 的边界与坑

- 依赖内核 user namespace + cgroup v2；cgroup v1 的老内核装不上，只能改走「专用沙箱主机 + TLS」路线。
- rootless 守护进程靠用户 linger 常驻；关闭 linger、切换部署用户、宿主重启后 socket 路径变化，都会让 runner 503，需要重新跑 setup 并更新 `SANDBOX_ROOTLESS_SOCKET`。
- 被攻破后的上限是「沙箱用户权限」而非宿主 root，但该用户仍能读写自己的 Docker 数据根（`~/.local/share/docker`）。生产应把沙箱用户数据目录与业务数据分盘，最好独立主机。
- 不支持绑定 <1024 端口等特权操作；沙箱本身 `network=none`，不受影响。

### 6.3 gVisor 兼容性

- 在部署机完成回归前，不能认为 SBX-09 已完成；当前状态是「代码 + 部署就绪，待兼容性验证」。部署机验收步骤见 [RUNBOOK §5.1](file:///c:/Users/far/Desktop/git/shop-agent/docs/RUNBOOK.md#L276)。
- runsc 不支持个别 syscall（部分 io_uring/perf/`/proc` 特性），表现是脚本执行失败，不会静默回落 runc。
- 若为兼容性放宽运行时，必须显式修改 `ISOLATED_RUNTIMES` 并留变更记录，不要靠环境变量绕过守卫。

### 6.4 digest 与镜像运维

- 构建端点必须与 runner 连接的守护进程一致：生产 runner 走 rootless dockerd（独立镜像存储），用默认 root 守护进程构建会得到「runner 侧镜像不存在 → 永久 unavailable」。构建时显式指定宿主 socket：
  `SANDBOX_DOCKER_HOST=unix:///run/user/<uid>/docker.sock make build-sandbox-image`（值即 setup.sh 打印的 `SANDBOX_ROOTLESS_SOCKET`）。
- 换镜像流程固定为：在 runner 端点构建 → 更新 `.env` 的 `SANDBOX_IMAGE` → `docker -H <rootless socket> image inspect <digest>` 确认可见 → 重启 runner，四步缺一不可。
- 上线前必须做一次端到端验证：`/sandbox/health` 的 runners 全 `ok`，且 `POST /sandbox/run` 返回 `status=ok`；只看到容器 healthy 不代表镜像真的可用。
- 本地镜像 ID 是内容寻址，重新构建会得到新 ID；旧 ID 被 `docker image prune` 清掉后 runner 会报镜像不存在（`unavailable`）。
- 镜像清理任务必须排除当前 `SANDBOX_IMAGE`；不要把 `:latest` 写进部署配置。

### 6.5 日志上限的副作用

- 单容器 json-file 封顶 1MB，超量输出只保留尾部；返回给模型的内容本来就截断到 8KB，功能无影响，但排障时看不到完整输出。
- 需要完整日志时只能临时调大上限并走评审，不要直接删掉 `log_config`。
- 该上限只覆盖沙箱容器；runner 自身日志由 compose 的 logging 配置控制（prod overlay 已限）。

### 6.6 healthz 语义变化

- 非 dev 下不可达、非 rootless、runsc 未注册、镜像未固定，任一成立都返回 **HTTP 503**，不再是 200 + `status` 字段。
- compose healthcheck 与 harness 探测都按状态码判死；本地调试看到 503 时，直接看响应里的 `docker.error` / `docker.endpoint` / `image_pinned`。
- 不能为了「让容器看起来 healthy」把 503 改回 200，那等于重新引入静默退化。

### 6.7 仍需记住的未解决项

- 本轮已关闭 SBX-07/08/14；未覆盖：SBX-09 部署机兼容性回归、SBX-10 孤儿容器回收、SBX-11 全局配额、SBX-12 冷启动、SBX-13 TTL 联动。
- 沙箱执行的审计证据是 `audit.sandbox.executed` 事件，只含哈希不含明文；事故复盘需要代码或输出原文时，只能靠调用方自己的留存，不要去审计库里找明文。
- `untrusted_output` 标记只在单次运行内生效：同一次工具循环里会拦住写入，下一轮会话会重新建模。跨轮会话的高危操作仍依赖审批，不能假设标记一直在。
- 生产 overlay 已把 `ENV=prod` 写进每个服务；不要依赖部署机 `.env` 是否记得设置，那是上一轮最容易漏的地方。
- 密钥轮换后必须整栈重启：JWT 与内网签名密钥不热更新，旧进程会继续用旧密钥验签。
- 沙箱调用方仍是 staff，不是面向外部租户的多租户隔离；若未来开放给商家/租户，需要重新评估隔离档位与配额模型。

### 6.8 回滚

- 镜像问题：把 `SANDBOX_IMAGE` 改回上一版 digest 并重启 runner，不要回退到 tag。
- 隔离问题：不允许通过删除启动守卫或 healthz 校验来「先跑起来」；确需临时放宽只能在 dev，并记录风险与恢复时间。
- rootless 安装失败：`setup.sh` 幂等可重跑；不要手删 `/run/user/<uid>/docker.sock`，按官方文档卸载。

---

## 7. 单项验收口径

- 每个条目必须落一条可执行断言（单测或集成用例），不接受「配置已改」作为完成依据。
- 涉及 compose / runtime / 权限配置的改动：先出方案等批准，再落地，并同步 RUNBOOK。
- 提交前统一验证：

```bash
.venv/Scripts/python -m pytest -q
.venv/Scripts/python -m ruff check packages services tests
cd web && npm run build
docker compose config -q
```

---

## 8. 变更记录

| 日期 | 内容 |
|---|---|
| 2026-11-08 | 初始建档，梳理 14 项问题、10 项实现难点、8 类泄露面与四阶段解决顺序 |
| 2026-11-08 | SBX-04 关闭：生产切 rootless dockerd，新增端点启动守卫与 healthz 隔离退化告警 |
| 2026-11-08 | SBX-09 代码与部署就绪：生产强制 runsc、healthz 校验运行时注册、宿主初始化脚本化；待部署机兼容性回归 |
| 2026-11-08 | healthz 隔离退化改为 HTTP 503，compose healthcheck 与 harness 探测可直接判死 |
| 2026-11-08 | SBX-02 关闭：沙箱容器固定 json-file 日志上限 1MB，刷屏脚本无法写爆宿主磁盘 |
| 2026-11-08 | SBX-01 关闭：生产强制 digest 固定镜像，tag 拒绝启动；新增 build-sandbox-image 输出镜像 ID |
| 2026-11-08 | 新增「变更后风险与注意事项」：生产硬前置、rootless/gVisor 边界、digest 运维、日志与 503 语义、未解决项、回滚口径 |
| 2026-11-08 | SBX-03 关闭：非 dev 启动守卫强制强密钥（≥32 且非默认值），identity/payment 分别校验 MFA 与回调密钥；信任头保留但依赖强签名密钥；prod overlay 逐服务写死 ENV=prod |
| 2026-11-08 | SBX-05 关闭：生产硬锁 `SANDBOX_NETWORK=none`，CPU/内存设硬上限（2 核 / 2048MB），超限拒绝启动 |
| 2026-11-08 | SBX-06 关闭：容器显式 `cap_drop=ALL`、`shm_size=16MB`、`nofile=1024`；masked/readonly paths 依赖 Docker 默认值并在文档记录取舍 |
| 2026-11-08 | P0 阻塞项补齐：非 dev 强制 `SANDBOX_ENABLED`（harness 启动守卫 + 运行时 fail-closed + prod overlay 写死）；镜像构建支持 `SANDBOX_DOCKER_HOST` 指向 rootless 端点，并在 RUNBOOK 增加端点校验与端到端验收 |
| 2026-11-08 | SBX-07 关闭：结果/锁键绑定 `sha256(tenant+user)` 主体摘要，杜绝他人 exec_id 串读；真实执行投递 `audit.sandbox.executed`（镜像/输入输出哈希/耗时）落 180 天审计流 |
| 2026-11-08 | SBX-08 关闭：沙箱输出标记 `untrusted_execution_output`，`ToolContext.untrusted_output` 污染后写/管理权限工具强制转人工复核，审批重放继承标记 |
| 2026-11-08 | SBX-14 关闭：`MAX_CODE_CHARS` 共享常量，harness Pydantic 入口、客户端本地校验、工具 schema `maxLength` 三处统一；空代码同样本地拒绝 |
