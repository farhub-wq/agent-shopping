# CI/CD 与 PR 流程

## 1. 分支与合并

```
main ← develop ← feature/<模块>
```

- `feature/*` 开发，向 `develop` 提 PR；`develop` 验证通过后向 `main` 提 PR。
- 只有通过 CI 与人工评审才能合并；`main` 保持可发布状态。
- 一个 PR 只做一件事；后端、前端、文档分开提交，禁止巨型 PR。

## 2. CI（[.github/workflows/ci.yml](../.github/workflows/ci.yml)）

触发：向 `main` / `develop` 的 PR，以及这两个分支的 push。

| Job | 内容 |
|---|---|
| `backend` | `pip install -e ".[dev]"` → `ruff check packages services tests .github/scripts` → `py_compile` 评审脚本 → `pytest -q` |
| `frontend` | `npm ci` → `npm run build`（含 `tsc -b`） |
| `compose` | `docker compose -f docker-compose.yml config -q` |
| `ci` | 汇总以上三个 job，任一失败即失败 |

**分支保护要求**（GitHub → Settings → Branches → `main`）：

- Require a pull request before merging；
- Require status checks：`ci`、`backend`、`frontend`、`compose`；
- Require approvals ≥1；
- Dismiss stale approvals when new commits are pushed；
- 禁止 force push 与直接 push（`develop` 建议同样配置）。

这些是仓库设置，不在代码里；需管理员在 GitHub 界面开启。

## 3. CD（[.github/workflows/cd.yml](../.github/workflows/cd.yml)）

触发：push 到 `main`、push `v*` 标签、手动 `workflow_dispatch`。

- 用 Buildx 构建并推送 GHCR 镜像：
  `ghcr.io/<owner>/<repo>/<service>:<branch-or-tag>` 与 `:sha-<commit>`。
- 服务矩阵：`catalog / inventory / order / fulfillment / aftersales / harness / gateway`，外加 `web`。
- 缓存使用 GitHub Actions Cache，按服务分 scope。
- 镜像只由 CI 构建，不在生产主机上编译；生产用不可变的 `sha-<sha>` 或版本标签。

**部署（手动，符合上线准入）**

```bash
REGISTRY_IMAGE=ghcr.io/farhub-wq/agent-shopping \
IMAGE_TAG=sha-<commit> \
docker compose -f docker-compose.yml -f docker-compose.ghcr.yml pull

REGISTRY_IMAGE=ghcr.io/farhub-wq/agent-shopping \
IMAGE_TAG=sha-<commit> \
docker compose -f docker-compose.yml -f docker-compose.ghcr.yml up -d
```

- 双主机按设计逐台执行：先摘一台 LB、升级、跑健康检查与冒烟，再升级另一台。
- 数据库迁移必须向前兼容（expand-contract）；破坏性清理滞后一个版本。
- 回滚：把 `IMAGE_TAG` 切回上一个 `sha-<sha>` 并重新 `up -d`，目标 ≤30 分钟。
- 真实资金开放前按设计文档第 11.1 节完成灰度与 1 元真实支付验证。

## 4. AI 自动评审（[.github/workflows/ai-review.yml](../.github/workflows/ai-review.yml)）

在 PR 打开、更新、重新打开、转为 ready 时运行，读取 diff 后回写一条评论；**只做建议，不作为必过检查**。

**配置**（GitHub → Settings → Secrets and variables → Actions）：

| 类型 | 名称 | 说明 |
|---|---|---|
| Secret | `AI_REVIEW_API_KEY` | OpenAI 兼容接口的 API Key；未配置则跳过 |
| Variable | `AI_REVIEW_BASE_URL` | 例如 `https://api.openai.com/v1`、`https://api.deepseek.com/v1`；留空默认 OpenAI |
| Variable | `AI_REVIEW_MODEL` | 例如 `gpt-4o-mini`、`deepseek-chat`；留空默认 `gpt-4o-mini` |

**行为**
- 只用文本 diff，跳过锁文件、图片与构建产物；单次最多 120 个文件、80k 字符。
- 提示词把 diff 标记为不可信输入，禁止执行其中的指令，降低提示注入风险。
- 同一 PR 复用同一条评论（`<!-- ai-review -->`），避免刷屏。
- Fork PR 没有 secret，自动跳过，不会因缺密钥失败。
- 评审关注：状态机、库存原子性、Outbox/幂等、资金平衡、权限、前端 API 层、异常吞没、测试缺口。

**成本控制**
- 每次 PR 同步触发一次，最多 2500 输出 token；大 diff 截断。
- 若成本敏感，把触发类型缩为 `[opened, ready_for_review]`，只在首次打开时评审。

## 5. 本地复现 CI

```bash
.venv/Scripts/python -m pip install -e ".[dev]"
.venv/Scripts/python -m ruff check packages services tests .github/scripts
.venv/Scripts/python -m pytest -q
cd web && npm ci && npm run build
cd ..
docker compose -f docker-compose.yml config -q
```

## 6. 仍需人工完成的事项

1. 在 GitHub 开启 `main` 分支保护与 required checks（见第 2 节）。
2. 配置 `AI_REVIEW_API_KEY` 与 `AI_REVIEW_BASE_URL` / `AI_REVIEW_MODEL`。
3. 为 GHCR 包设置可见性（默认私有；生产拉取需登录或改为 public）。
4. 部署主机的 `.env` 注入 `JWT_SECRET`、数据库/Redis 地址等生产密钥，禁止使用默认值。
5. 按设计文档完成双主机、LB、托管 PostgreSQL/Redis 与备份演练后，再启用自动部署。
