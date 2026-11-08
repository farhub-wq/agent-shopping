# 实施计划（P1）：沙箱专用 worker 节点（rootful dockerd + gVisor）

## 1. 背景与目标

现有生产基线的隔离档位是「rootless dockerd + runsc」，在部署机 121.41.73.188 实测不可行：

- rootless + runsc：systemd cgroup manager 走系统总线被 polkit 拒（`Interactive authentication required`）；改 cgroupfs 驱动后 `cgroup.subtree_control` 无写权限；`--rootless` 与 `create` 不兼容；仅 `--ignore-cgroups` 可跑但丢失资源限额（等同放弃 CPU/内存约束）。
- rootful + runsc：实测正常（`Linux 4.19.0-gvisor`）。

因此生产隔离档位改为：**rootful dockerd + gVisor + 节点级独占**——用「Docker 守护进程只服务沙箱、节点不承载业务容器与业务数据」补偿 rootful socket 等价节点 root 的风险，gVisor 继续收敛共享内核 CVE 面。rootless 档位保留为可选（未来内核/发行版支持时可直接切回），但不是本轮默认。

P1 目标（只做代码与配置闭环，不做镜像仓库与多节点调度）：

1. runner 新增 `dedicated` 隔离档位：允许 rootful socket，但仍强制 digest 镜像、`network=none`、CPU/内存硬上限、**节点无其他业务容器**。
2. `/healthz` 输出隔离档位与各项校验结果；不满足即 503（compose healthcheck 与 harness 探测按状态码判死）。
3. compose 拆分：专用节点独立编排文件、端口只绑私网、业务栈不再与 runner 混部。
4. `setup.sh` 增加 rootful 分支（安装 docker-ce + runsc、注册 runtime、可选 registry mirror）。
5. 文档同步：RUNBOOK §5/§5.1、sandbox-hardening（SBX-04/SBX-09 状态与新增条目）。

**P1 不做**：镜像仓库搭建与推送流程、多 runner 调度/全局配额（SBX-11）、孤儿容器 reconcile（SBX-10）、microVM、节点自动化发放与监控接入。这些留 P2。

## 2. 隔离档位设计

新增配置 `SANDBOX_ISOLATION_MODE`，取值 `rootless`（默认，现状行为）与 `dedicated`。

| 校验项 | rootless（默认） | dedicated |
|---|---|---|
| Docker 端点 | 必须显式，且不得是默认 `/var/run/docker.sock` | 必须显式 `unix://` 端点（禁止空值回落 SDK 默认、禁止 `tcp://` 远程守护进程） |
| 守护进程模式 | 必须 rootless（`SecurityOptions` 含 rootless） | 必须 **非** rootless（rootful）；若报 rootless 视为档位不符 → healthz 503 |
| 运行时 | 必须 `runsc` | 必须 `runsc`（不变） |
| 镜像 | 必须 digest 固定 | 必须 digest 固定（不变） |
| 网络 | 必须 `none` | 必须 `none`（不变） |
| CPU / 内存 | `(0, 2.0]` 核 / `(0, 2048]` MB | 同左（不变） |
| 节点独占 | 不校验 | 启动 + 每次执行前校验：运行中容器只允许带 `shop.sandbox.role ∈ {execution, runner, infra}` 标签；出现其他容器即拒绝执行（`unavailable`），healthz 503 |
| 运行身份 | runner 以沙箱用户 uid/gid 运行（socket 属主匹配） | runner 以 uid 0 运行（rootful socket 需要 root 访问）；节点专用，不再依赖 uid 映射 |

节点独占的实现口径：

- 沙箱执行容器创建时打标签 `shop.sandbox.role=execution`（顺带为 SBX-10 的 reconcile 铺路）。
- runner 容器自身在 compose 中打标签 `shop.sandbox.role=runner`。
- 专用节点上必要的配套容器（如需本地 Redis）打 `shop.sandbox.role=infra`，由 compose 显式声明，不做隐式豁免。
- **执行边界强制**比启动期检查更重要：harness 不读 compose health，只要 runner HTTP 可达就会派发请求。因此 `_run_blocking` 在创建容器前必须重新校验一次独占性，不满足直接返回 `unavailable`（fail-closed，容器不创建、已启动的 execution 墓碑清理逻辑沿用现状）。
- 启动期检查为「尽力」：守护进程可达且有非白名单容器 → `RuntimeError` 拒绝启动；守护进程不可达 → 只记日志不拒绝（保留「启动成功、healthz 503」的现有语义，避免临时故障导致崩溃循环）。

## 3. 改动文件树

```
新增
  .ai/implementation_plan_sandbox_dedicated.md   本文档
  docker-compose.sandbox-node.yml                专用沙箱节点编排（仅 runner）

修改
  packages/shop_common/config.py                 + sandbox_isolation_mode
  services/sandbox_runner/runner.py              档位分支守卫 + 节点独占检查 + 容器标签
  services/sandbox_runner/app.py                 healthz 输出档位/独占结果，不满足 503
  docker-compose.yml                             SANDBOX_RUNNER_URLS 可覆盖、ISOLATION_MODE、runner 标签
  docker-compose.prod.yml                        同机栈显式 rootless 档位；专用节点不再走该块
  infra/rootless-docker/setup.sh                 SANDBOX_SETUP_MODE=rootless|dedicated 分支
  Makefile                                       + setup-sandbox-node（rootful 分支入口）
  .env.example                                   + SANDBOX_ISOLATION_MODE / SANDBOX_RUNNER_BIND 等
  tests/test_sandbox_runner.py                   档位守卫、healthz、独占检查用例
  docs/RUNBOOK.md                                §5 改为双档位，§5.1 增专用节点清单
  docs/design/sandbox-hardening.md               SBX-04/09 状态修订 + 新增 SBX-17 专用节点条目
```

不新增独立测试文件：档位与 runner 的用例语义同属 runner 契约，并入 `tests/test_sandbox_runner.py` 便于维护。

## 4. 逐文件改动说明

### 4.1 `packages/shop_common/config.py`

```python
# 沙箱隔离档位：rootless = 同机 rootless dockerd（默认）；dedicated = 专用节点 rootful dockerd，
# 由节点独占 + gVisor 补偿 rootful socket 的权限半径。
sandbox_isolation_mode: str = "rootless"
```

保持默认值 `rootless`，现有部署与全部存量单测无需改动即可通过。

### 4.2 `services/sandbox_runner/runner.py`

新增常量：

- `ISOLATION_MODES = frozenset({"rootless", "dedicated"})`
- `SANDBOX_CONTAINER_LABEL = "shop.sandbox.role"`、`ALLOWED_NODE_ROLES = frozenset({"execution", "runner", "infra"})`

`validate_sandbox_isolation()` 重构为「公共校验 + 档位分支」：

1. 读取 `mode = settings.sandbox_isolation_mode.strip().lower()`；不在 `ISOLATION_MODES` → `RuntimeError`（拼错档位不能静默落回默认）。
2. 公共项（两档位一致）：runsc、digest 镜像、`network=none`、CPU/内存上限——沿用现有实现与报错文案。
3. `rootless` 分支：保留现有「禁止空值 / 禁止默认 root socket」校验，报错文案追加「若为专用节点请设 SANDBOX_ISOLATION_MODE=dedicated」。
4. `dedicated` 分支：
   - 端点必须非空且以 `unix://` 开头（空值会回落 SDK 默认，无法证明连的是哪台守护进程；`tcp://` 意味着远端 root，直接拒绝）。
   - 尽力独占检查：守护进程可达时列举运行中容器，存在非白名单标签 → 抛出含容器名的 `RuntimeError`；不可达仅告警。

`runtime_info()` 扩展（healthz 数据源）：

```python
"isolation_mode": mode,
"node_exclusive": bool,        # dedicated 下实时校验；rootless 恒为 True
"foreign_containers": [...],   # 仅 dedicated 且发现异常时非空
"mode_mismatch": bool,         # dedicated 但守护进程报告 rootless
```

`_run_blocking()`：

- 创建容器前，若 `mode == "dedicated"` 而节点存在非白名单容器 → 返回 `SandboxResult("unavailable", stderr="节点存在非沙箱容器: a,b")`，**不创建容器**（instance 级不可用语义，harness 可安全转移）。
- `create_kwargs` 增加 `"labels": {SANDBOX_CONTAINER_LABEL: "execution"}`；其余加固参数（cap_drop、read_only、tmpfs、shm、nofile、pids、log_config、user 65534、runtime）全部不变。

### 4.3 `services/sandbox_runner/app.py`

`/healthz` 新增：

```json
"isolation": {
  "mode": "rootless|dedicated",
  "node_exclusive": true,
  "foreign_containers": [],
  "mode_mismatch": false
}
```

`healthy` 追加三个条件：

- `runtime.get("node_exclusive", True)` 为真（默认 True，存量用例 monkeypatch 的 runtime_info 无需补字段）；
- `not runtime.get("mode_mismatch", False)`；
- dedicated 下 `rootless` 为 `False`（档位声明与实际守护进程一致）。

`security.issues`、`limits`、`max_concurrency`、`docker` 等字段保持不变。

### 4.4 `docker-compose.yml`（同机 dev/单机形态）

- `sandbox-runner.environment` 增加 `SANDBOX_ISOLATION_MODE: ${SANDBOX_ISOLATION_MODE:-rootless}`。
- `sandbox-runner` 增加容器标签 `labels: { shop.sandbox.role: runner }`，使同机部署也能通过独占校验（同机形态下其他业务容器仍会触发告警，这是预期的：专用档位不该混部）。
- `harness.environment.SANDBOX_RUNNER_URLS` 改为 `${SANDBOX_RUNNER_URLS:-http://sandbox-runner:8000}`——专用节点部署必须能指向节点私网地址。

### 4.5 `docker-compose.prod.yml`

- `sandbox-runner` 块显式写 `SANDBOX_ISOLATION_MODE: rootless`，保住「单机小规模部署」路径，不随 `.env` 漂移。
- 该块保留 rootless socket 挂载与 `SANDBOX_UID/GID` 运行身份；专用节点改由新文件承接，不再套用此 overlay。

### 4.6 `docker-compose.sandbox-node.yml`（新增）

专用节点唯一编排文件，只跑沙箱栈：

```yaml
name: shop-agent-sandbox
services:
  sandbox-runner:
    # 与业务栈同镜像，但只注入沙箱所需配置
    build: { context: ., dockerfile: infra/Dockerfile, args: { SERVICE_NAME: sandbox_runner } }
    user: "0:0"                      # rootful socket 需要 root；节点专用，不依赖 uid 映射
    labels: { shop.sandbox.role: runner }
    environment:
      ENV: prod
      SANDBOX_ISOLATION_MODE: dedicated
      SANDBOX_RUNTIME: runsc
      SANDBOX_IMAGE: ${SANDBOX_IMAGE:?必须注入 digest 镜像}
      SANDBOX_NETWORK: none
      SANDBOX_DOCKER_HOST: unix:///var/run/docker.sock
      RUNNER_SIGNING_SECRET: ${RUNNER_SIGNING_SECRET:?}
      TRUST_IDENTITY_HEADERS: "true"
      SANDBOX_REDIS_URL: ${SANDBOX_REDIS_URL:?受限 ACL 用户}
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
    ports:
      # 只绑私网；默认 127.0.0.1 即「未显式配置就不对外」，不提供 0.0.0.0 默认值
      - "${SANDBOX_RUNNER_BIND:-127.0.0.1}:8000:8000"
    deploy: { resources: { limits: { cpus: "1.0", memory: 512M } } }
    logging: { driver: json-file, options: { max-size: "10m", max-file: "3" } }
    healthcheck: { ... 同业务栈 healthz 探测 ... }
```

要点：

- `SANDBOX_IMAGE` 无默认值、缺失直接 compose 解析失败——避免 :latest 进生产。
- 不发布 Redis、MySQL、Mongo 任何端口；节点上若需本地 Redis，另起服务并打 `shop.sandbox.role=infra`（P1 默认仍建议指向共享受限 ACL Redis，见 §5）。
- harness 侧配置 `SANDBOX_RUNNER_URLS=http://<节点私网 IP>:8000`。

### 4.7 `infra/rootless-docker/setup.sh`

加分支变量 `SANDBOX_SETUP_MODE="${SANDBOX_SETUP_MODE:-rootless}"`，保留现有 rootless 逻辑不动：

- `dedicated`：
  1. 安装 `docker-ce`（rootful，含 `docker-ce-cli`/`containerd.io`/`docker-buildx-plugin`），存在则跳过。
  2. 安装 runsc（沿用现有的 gVisor 源与键）。
  3. 合并写 `/etc/docker/daemon.json`：`runtimes.runsc`；若注入 `DOCKER_REGISTRY_MIRRORS`（逗号分隔）则写 `registry-mirrors`（默认不写，避免把第三方镜像源固化进仓库）。
  4. `systemctl enable --now docker` → `systemctl restart docker`。
  5. 实机校验：`docker info` 的 `Runtimes` 含 runsc，且 `SecurityOptions` 不含 rootless；不满足直接失败。
  6. 打印：`SANDBOX_DOCKER_HOST=unix:///var/run/docker.sock`、`SANDBOX_RUNTIME=runsc`、`SANDBOX_ISOLATION_MODE=dedicated`，并提示这些写入专用节点 `.env`。
- 脚本头部注释与用法同步说明两种模式；rootless 分支输出保持不变。
- 目录名暂不改（`infra/rootless-docker/` 已出现在 Makefile、RUNBOOK、历史文档中，改名属于无关变更）；在 RUNBOOK 中注明该目录同时提供两种模式。

### 4.8 `Makefile`

- 新增 `setup-sandbox-node`：`sudo bash infra/rootless-docker/setup.sh`，`SANDBOX_SETUP_MODE=dedicated`。
- `.PHONY` 同步。

### 4.9 `.env.example` / 部署 `.env`

新增或注明：

| 变量 | 用途 | 专用节点取值 |
|---|---|---|
| `SANDBOX_ISOLATION_MODE` | 隔离档位 | `dedicated` |
| `SANDBOX_DOCKER_HOST` | runner 端点 | `unix:///var/run/docker.sock`（容器内同名路径） |
| `SANDBOX_RUNNER_BIND` | runner 发布地址 | 节点私网 IP（不设则 127.0.0.1） |
| `SANDBOX_RUNNER_URLS` | harness 调用地址 | `http://<节点私网 IP>:8000` |
| `SANDBOX_IMAGE` | 镜像 digest | `sha256:...` 或 `repo@sha256:...` |

### 4.10 测试（`tests/test_sandbox_runner.py`）

新增用例：

1. `test_prod_dedicated_accepts_root_socket`：mode=dedicated + `unix:///var/run/docker.sock` + runsc + digest → 通过。
2. `test_prod_dedicated_requires_explicit_endpoint`：端点为空 → `RuntimeError`。
3. `test_prod_dedicated_rejects_tcp_endpoint`：`tcp://docker.example:2375` → `RuntimeError`。
4. `test_prod_rejects_unknown_isolation_mode`：`SANDBOX_ISOLATION_MODE=dedicated-node` → `RuntimeError`。
5. `test_prod_rootless_still_rejects_root_socket`：默认档位行为不回退（现有用例已覆盖，保留）。
6. `test_healthz_dedicated_reports_mode_and_flags_foreign_containers`：`node_exclusive=False` → 503，且 `isolation.foreign_containers` 透出。
7. `test_healthz_dedicated_ok_when_node_clean`：独占成立 → 200。
8. `test_healthz_flags_dedicated_on_rootless_daemon`：`mode_mismatch=True` → 503。
9. `test_dedicated_refuses_execution_on_contaminated_node`：`_run_blocking` 返回 `unavailable`，`containers.create` 未被调用。
10. `test_run_labels_execution_container`：create 参数含 `{"shop.sandbox.role": "execution"}`。
11. 存量用例回归：`test_run_applies_container_hardening`、`test_healthz_exposes_runtime_state` 等不改断言即通过（`node_exclusive` 默认 True）。

`DockerRunner` 的独占检查需要可注入：新增内部方法 `_foreign_containers(client)`，测试用假 client 覆盖，不引入额外抽象层。

### 4.11 文档

- `docs/RUNBOOK.md`
  - §5 开头改为「两种档位」：同机 rootless（历史/小规模）与专用节点 dedicated（生产推荐），给出一张对照表（端点、守护进程模式、运行身份、节点要求）。
  - 新增「专用节点部署」小节：`setup.sh` rootful 分支、独立 compose、私网绑定、防火墙只放行 harness 网段、节点不部署业务容器与业务数据、`.env` 清单。
  - §5.1 拆成「同机 rootless 清单」（保留现有 A–F）与「专用节点清单」（rootful 校验、独占校验、端口暴露面、端到端）。
  - §6 故障排查新增：`节点存在非沙箱容器`、`mode_mismatch`、`SANDBOX_ISOLATION_MODE 非法`。
- `docs/design/sandbox-hardening.md`
  - 现状基线图与「生产必须是 rootless dockerd」的表述改为「默认档位 rootless；生产推荐 dedicated（rootful + 节点独占 + gVisor）」。
  - 新增 **SBX-17（P1，专用沙箱节点）**：证据（rootless+runsc 实测不可行）、方案（节点独占 + gVisor + digest + 私网）、补偿边界（runner RCE 仍等于节点 root，故节点不得承载业务数据与密钥）、验证方式。
  - SBX-04 状态说明补充「rootful 由节点独占补偿」，SBX-09 注明实测档位。
  - §6.2 增补 rootful 边界与坑（守护进程即节点 root、镜像存储在本机、registry mirror 需显式配置）。
- **AGENTS.md 无 rootless 硬性条款**：已核对仓库 `AGENTS.md` 与 `docs/design/system-design.md` 均无「沙箱必须 rootless」表述，无需修改；相关约束只存在于 RUNBOOK 与 sandbox-hardening，按上述两处同步即可。若你希望把「专用节点档位」写成项目级硬规则，我可以在 AGENTS.md §5 增一条（需你确认后加）。

## 5. 部署拓扑与运行要求

```
浏览器 → gateway → harness ──签名身份头 + X-Execution-Id──→ sandbox-runner（专用节点）
                                                              │  rootful /var/run/docker.sock
                                                              └─→ 一次性容器（runsc + network=none）
                                                              │
                        受限 ACL Redis（去重锁 + 结果缓存 + 审计流）
```

- **节点职责**：只跑沙箱栈（runner 容器 + 可选 infra 容器）；不部署业务服务、不存业务库、不挂业务密钥。节点失陷的爆炸半径 = 该节点自身，可整机重建。
- **端口**：runner `8000` 只绑节点私网地址（`SANDBOX_RUNNER_BIND`），防火墙仅放行 harness 所在网段；节点不开放 Redis/MySQL 端口。
- **密钥**：节点 `.env` 只放 `RUNNER_SIGNING_SECRET`（≥32 字符、与 `INTERNAL_SIGNING_SECRET` 不同）与 `SANDBOX_REDIS_URL`（非 default、带密码、不复用业务 URL）；不得出现 `JWT_SECRET` / `INTERNAL_SIGNING_SECRET` / `REDIS_PASSWORD`。
- **Redis**：P1 继续指向共享受限 ACL Redis（跨节点去重依赖同一份锁与结果缓存）。若在节点本地起 Redis，跨节点去重会退化为「每节点独立锁」，必须在 RUNBOOK 明示该取舍（单节点部署无影响）。
- **镜像**：`SANDBOX_IMAGE` 必须 digest；构建端点必须是节点自身 rootful dockerd（`make build-sandbox-image`），或 P2 由 registry 推送 digest。换镜像流程沿用现有四步（构建 → 写 `.env` → `image inspect` 确认 → 重启 runner）。
- **gVisor**：`runsc` 由 setup.sh 注册进 `/etc/docker/daemon.json`；healthz 校验 `Runtimes` 是否含 runsc。

## 6. 验收标准

静态验证（提交前）：

```bash
.venv/Scripts/python -m pytest -q
.venv/Scripts/python -m ruff check packages services tests
cd web && npm run build
docker compose -f docker-compose.yml -f docker-compose.prod.yml config -q
docker compose -f docker-compose.sandbox-node.yml config -q
```

部署实测（必须在**干净专用节点**执行并回填 RUNBOOK §5.1 记录表；不能在混部 erp-agent 的 121.41.73.188 上验收 dedicated）：

| # | 项 | 期望 |
|---|---|---|
| 1 | `sudo make setup-sandbox-node` | 脚本退出 0，`docker info` 含 `runsc`，`SecurityOptions` 不含 rootless |
| 2 | 节点上意外启动一个业务容器 | runner `/healthz` 返回 503，`isolation.foreign_containers` 列出该容器；`/run` 返回 `unavailable` 且未创建沙箱容器 |
| 3 | 移除该容器后重启 runner | `/healthz` 200，`isolation.mode=dedicated`、`node_exclusive=true` |
| 4 | 实际执行 `print(6*7)` | `status=ok`、`stdout=42`；`docker ps` 显示执行容器 runtime=runsc、镜像为 digest、标签 `shop.sandbox.role=execution` |
| 5 | 隔离探针（RUNBOOK §5.1 D） | `UID=65534`、网络被拒、根文件系统只读 |
| 6 | 端口暴露面 | `ss -lntp` 显示 8000 只绑私网；节点无 3306/6379/27017 |
| 7 | harness 指向节点 | `/sandbox/health` runners 全 `ok`；停 runner 后沙箱调用 `unavailable`，harness 不回退本地子进程 |
| 8 | `.env` 密钥半径 | `docker compose -f docker-compose.sandbox-node.yml config --format json` 中环境只有 `RUNNER_SIGNING_SECRET`、`SANDBOX_REDIS_URL` 与隔离类参数 |
| 9 | gVisor 兼容性回归 | RUNBOOK §5.1 E 的清单全部通过 |

## 7. 风险与回滚

| 风险 | 影响 | 缓解 |
|---|---|---|
| rootful socket 等价节点 root | runner 任意 RCE = 节点 root | 节点不承载业务数据/密钥/业务容器；私网端口 + 防火墙；gVisor + digest + `network=none` 压缩逃逸面；节点可整机重建（残余风险已接受，须在文档写明） |
| 节点独占检查误报 | 运维临时容器导致 runner 503、沙箱不可用 | 白名单仅三类标签；文档给出「临时维护容器请加 `shop.sandbox.role=infra`」；执行边界检查让误报只影响沙箱，不影响主链路 |
| 执行边界列举容器带来延迟 | 每次执行多一次 Docker API 调用（毫秒级） | 只对 dedicated 生效；rootless 档位零变化 |
| 跨节点去重依赖共享 Redis | 节点网络故障或 Redis 不可达时 runner fail-closed | 沿用现有 fail-closed 语义；RUNBOOK 明确 Redis 可达性是专用节点的部署前提 |
| 镜像只在节点本地 | 重建/换节点后 `ImageNotFound` → `unavailable` | 沿用 digest + `image inspect` 验收；P2 补 registry 推送 |
| 两档位并存导致配置漂移 | 误把专用节点配成 rootless 或反之 | 非法档位值直接拒绝启动；healthz 暴露 `mode` + `mode_mismatch`；prod overlay 写死同机档位 |
| 回滚 | dedicated 上线后需退回同机 rootless | 把 `SANDBOX_ISOLATION_MODE` 改回 `rootless`、端点改回 rootless socket、runner 恢复 `SANDBOX_UID/GID` 运行身份并重启；代码默认值仍是 rootless，回滚不需要回退代码 |

不引入的行为变更（刻意保持）：

- `network=none`、digest 强制、CPU/内存上限、容器加固参数、去重/审计/签名鉴权逻辑全部不动。
- rootless 档位的报错文案只追加提示，不放宽校验。

## 8. 待确认项（需你拍板后再编码）

1. **专用节点是否需要本地 Redis**：默认沿用共享受限 ACL Redis（跨节点去重成立，但节点需能访问 Redis）；若你倾向节点自带 Redis，我会在 compose 里加 `infra` 标签的 Redis 服务，并在 RUNBOOK 写明「同节点多 runner 才共享去重」。
2. **节点独占白名单是否接受三类标签**（`execution` / `runner` / `infra`）：不接受的话只能允许 `execution` + `runner`，节点上不能有任何其他容器（含 Redis）。
3. **compose 文件命名** `docker-compose.sandbox-node.yml` 是否符合你的部署习惯；也可以叫 `docker-compose.sandbox.yml`。
4. **是否在 AGENTS.md §5 增一条项目级硬规则**（「生产沙箱节点必须 dedicated 或 rootless + gVisor，禁止混部」）。默认不增，仅写文档。
