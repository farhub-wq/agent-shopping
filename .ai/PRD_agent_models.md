# PRD：Agent 多模型接入、用量监控与对话隔离

> 状态：待确认。范围：harness 服务——多 AI 厂商 provider 管理（含默认免费 AI）、真实 token 用量落库与监控、用户对话隔离与会话持久化。不重写 Agent 决策循环。
> 参考：Agent-Harness（默认免费 AI + 身份隔离）、LiteLLM（provider 抽象）、Open WebUI / LibreChat（厂商连接管理与用量看板交互）。

## 1. 业务上下文

当前 harness 的 Agent 决策链（ReAct 循环、计划执行、熔断降级、MCP、流式、三层记忆、沙箱、证据追踪、人工审批）已完整。缺口集中在"模型供给与可观测性"：

- 模型只有一种 `OpenAICompatibleProvider`，base_url/api_key/model 全部来自环境变量（`LLM_BASE_URL/LLM_API_KEY/LLM_MODEL`），**无法运行时按用户选厂商**，Claude 原生协议不支持。
- 真实 token `usage` 在 [llm.py](file:///c:/Users/far/Desktop/git/shop-agent/services/harness/core/llm.py) 收到后未持久化，现有"用量"只是 `字符数//3` 的估算，仅做 Redis 配额护栏。
- 会话工作记忆 key 只含 session_id（`shop:mem:work:{session_id}`），session_id 由前端自造，chat 入口不校验归属，**不同用户可撞 session_id 串上下文（IDOR 隐患）**。
- 无 conversation/message 结构化存储，刷新页面历史即丢。
- 前端无模型配置、厂商选择、用量监控页面。

本 PRD 交付四件事：① 多厂商 provider（用户自填 key，可测连通/延迟）；② 数据库内置一个默认免费 AI；③ 真实用量落库 + 监控页；④ 用户对话隔离 + 会话持久化。

## 2. 数据模型

新增 4 张表，落在 **harness 自己的关系库**（harness 从"无关系库"变"有关系库"，迁移目录 `migrations/harness/` 新建，首版迁移 `0001_add_agent_tables.py`）。`user_id` 统一 `String(36)`，与 identity `users.id` 对齐。

### 2.1 model_providers（模型提供商）

| 列 | 类型 | 说明 |
|---|---|---|
| `id` | String(36) PK | provider 唯一标识 |
| `tenant_id` | String(36) index，default "default" | 租户隔离 |
| `owner_user_id` | String(36) nullable index | 空=全局共享（管理员），非空=用户私有（仅本人可见用） |
| `provider_type` | String(24) | `openai_compatible` / `anthropic` |
| `name` | String(64) | 展示名，如「DeepSeek」「我的 Claude」 |
| `base_url` | String(256) | 厂商端点 |
| `model` | String(64) | 模型名 |
| `api_key_enc` | String(512) nullable | Fernet 密文，前端永不回传明文 |
| `is_default` | Boolean，default False | 系统默认免费 AI 标记 |
| `enabled` | Boolean，default True | 停用后不进解析链 |
| `created_at` / `updated_at` | UTCDateTime | 时间戳 |

### 2.2 conversations（会话）

| 列 | 类型 | 说明 |
|---|---|---|
| `id` | String(36) PK | 会话 id |
| `user_id` | String(36) index | 归属用户 |
| `tenant_id` | String(36) index，default "default" | 租户隔离 |
| `skill` | String(64) | 绑定的技能名 |
| `title` | String(128)，default "" | 首条用户消息截断生成 |
| `created_at` / `last_message_at` | UTCDateTime | 时间戳 |

### 2.3 messages（消息）

| 列 | 类型 | 说明 |
|---|---|---|
| `id` | String(36) PK | 消息 id |
| `conversation_id` | String(36) index | 归属会话 |
| `role` | String(16) | `user` / `assistant` / `tool` |
| `content` | Text | 正文 |
| `tool_calls_json` | Text nullable | 工具调用轨迹（JSON 序列化），供历史回放展示 |
| `created_at` | UTCDateTime | 时间戳 |

### 2.4 usage_records（用量）

| 列 | 类型 | 说明 |
|---|---|---|
| `id` | String(36) PK | 记录 id |
| `user_id` | String(36) index | 发起用户 |
| `tenant_id` | String(36) index，default "default" | 租户隔离 |
| `provider_id` | String(36) nullable | 命中的 provider |
| `model` | String(64) | 实际模型名 |
| `conversation_id` | String(36) nullable | 关联会话 |
| `run_id` | String(36) nullable | 关联 Agent run |
| `prompt_tokens` / `completion_tokens` / `total_tokens` | BigInteger，default 0 | 真实 usage（厂商返回） |
| `latency_ms` | Integer | 本次模型调用延迟 |
| `status` | String(16) | `ok` / `error` |
| `created_at` | UTCDateTime | 时间戳 |

复合索引：`(user_id, created_at)`、`(provider_id, created_at)`（监控页按用户/按模型按时间聚合用）。

### 2.5 默认免费 AI

服务启动时若 `model_providers` 表为空，幂等写入一条：

```
provider_type=openai_compatible, name=默认免费, base_url=https://api.chatanywhere.tech/v1,
model=gpt-4o-mini, is_default=true, enabled=true, api_key_enc=空
```

即 Agent-Harness 项目的默认（ChatAnywhere 网关）。key 由管理员在设置页填一次（ChatAnywhere 免费额度，chatanywhere.apifox.cn 获取）。key 为空时，解析链回退现有 `DeterministicPlanner`（离线规则兜底，行为与现状一致）。

## 3. 接口契约

全部挂在 `services/harness`，复用 `ApiError` 统一出口与 `PrincipalDep` 鉴权。

### 3.1 厂商预设 `GET /providers/presets`

无需鉴权（公开元数据）。返回内置厂商预设清单，每项含 `type / name / base_url / default_model / key_label`。预设：`deepseek`、`glm`、`openai-codex`（OpenAI 官方 codex 系列）、`claude`（Anthropic）、`chatanywhere`（默认免费网关）、`custom`（自定义 OpenAI 兼容端点）。

### 3.2 provider CRUD

- `GET /providers`：列出当前用户可见 provider（全局共享 + 本人私有；`require_staff` 可看全部）。响应每项 `api_key` 字段替换为 `has_key` + `key_masked`（尾 4 位），**永不回传明文**。
- `POST /providers`：创建。`owner_user_id` 为空即共享（需 `require_staff`），否则强制写当前 principal.user_id（用户无权造共享 provider）。校验 `provider_type ∈ {openai_compatible, anthropic}`、`base_url` 为 http(s)、`model` 非空；`api_key` 用 Fernet 加密落库。
- `PATCH /providers/{id}`：更新名称/模型/base_url/key/是否默认。归属校验：非 staff 只能改自己的私有 provider。空 key 表示"不更新 key"。
- `DELETE /providers/{id}`：删除（软删或 `enabled=false`）。归属校验同上；`is_default` 系统记录禁止删除。

### 3.3 连通性与延迟测试 `POST /providers/{id}/test`

归属校验后，用该 provider 配置发一次最小请求（`openai_compatible` 走 `/chat/completions`、`anthropic` 走 `/v1/messages`，`max_tokens=1`、短超时 10s）。返回 `{ok: bool, latency_ms: int, message: str}`。失败时 `message` 带脱敏后的原因（不含 key）。SSRF 防护：`base_url` 只允许 http(s) 且解析出的 host 不在内网地址段黑名单（127.0.0.0/8、10/8、172.16/12、192.168/16、169.254/16、::1）。

### 3.4 会话

- `GET /conversations`：当前用户会话列表（`skill` 可选过滤，`page/size` 分页，按 `last_message_at` 倒序）。
- `GET /conversations/{id}/messages`：会话消息历史。归属校验：`user_id` 与 `tenant_id` 必须匹配当前 principal，否则 403。
- `POST /agents/{skill}/chat`（改）：`ChatRequest` 增 `conversation_id: str | None`、`provider_id: str | None`。流程：校验 `conversation_id` 归属（非本人 403）→ 解析 provider（见 3.5）→ 消息落库 → 运行 → 真实 usage 落 `usage_records`。`session_id` 保留兼容，作为 Redis 工作记忆后缀。

### 3.5 provider 解析降级链

每次 chat 按 `user_id` 解析：`provider_id 显式指定 → 用户最近选用 → is_default 默认免费 → 离线 DeterministicPlanner`。被选 provider `enabled=false` 或熔断开路时沿链降级，复用现有 `CircuitBreaker`。

### 3.6 用量查询 `GET /usage`

`require_staff()`（运营/管理员）。查询参数 `from/to`（时间范围）、`group_by ∈ {day, model, user}`、`page/size`。返回按维度聚合的 `{total_tokens, prompt_tokens, completion_tokens, calls, errors, avg_latency_ms, p95_latency_ms}`。

## 4. 前端要求

- [api.ts](file:///c:/Users/far/Desktop/git/shop-agent/web/src/lib/api.ts)：新增类型 `ProviderPreset / ModelProvider / Conversation / Message / UsageSummary` 与函数 `listProviderPresets / listProviders / createProvider / updateProvider / deleteProvider / testProvider / listConversations / listMessages / listUsage`。全部类型化，禁 `any`。
- 新页 [ModelSettings.tsx](file:///c:/Users/far/Desktop/git/shop-agent/web/src/pages/ModelSettings.tsx)（登录用户可见）：厂商预设下拉 → 填 key → 「测试连通/延迟」按钮（显示 ms 与可用性）→ 保存/设为默认；已配 provider 列表（含 `key_masked`）。
- 新页 [Usage.tsx](file:///c:/Users/far/Desktop/git/shop-agent/web/src/pages/Usage.tsx)（运营/管理员可见）：按 天/模型/用户 切换聚合维度，展示 token、次数、平均/分位延迟、失败数。
- [Agent.tsx](file:///c:/Users/far/Desktop/git/shop-agent/web/src/pages/Agent.tsx)（改）：顶部 provider 选择器（当前用户可见 provider 列表）+ 会话历史列表（新建/切换会话）；发送时携带 `conversation_id` 与 `provider_id`。
- [App.tsx](file:///c:/Users/far/Desktop/git/shop-agent/web/src/App.tsx)（改）：加 `/models`、`/usage` 路由与导航入口。

## 5. 验收标准

1. 用户可选不同厂商（DeepSeek/GLM/Codex/Claude/ChatAnywhere/自定义）创建 provider，输入 key 后 `test` 返回真实 `ok + latency_ms`；`base_url` 指向内网地址时拒绝（SSRF 防护）。
2. `api_key` 以 Fernet 密文落库；所有 provider 响应只含 `has_key + key_masked`，日志不含明文 key。
3. 服务启动时默认免费 AI 幂等写入（重复启动不产生第二条）；key 为空时 chat 回退离线规则，不报错。
4. 跨用户隔离：用户 A 传用户 B 的 `conversation_id` 一律 403；两个用户即使传相同 session_id，工作记忆也因 `user_id` 前缀互不可见。
5. 会话持久化：多轮对话落 `conversations/messages`，刷新页面后历史可恢复、列表可切换。
6. 用量监控：真实 `usage`（prompt/completion/total tokens）落 `usage_records`，`GET /usage` 聚合结果与库中记录一致（含延迟、失败数）。
7. 全量验证绿：`.venv/Scripts/python -m pytest -q`、`.venv/Scripts/python -m ruff check packages services tests`、`cd web && npm run build`。

## 6. 边界（V1 明确不做）

- 成本金额折算（各厂商价格表维护）不做，只做 token/次数/延迟。
- 并行多工具、真实 embedding、MCP stdio 传输属既有 backlog，本次不混入。
- guest（未登录）用户：只用默认免费 AI，会话不落库、不记 usage（保持现状的访客体验）。
- 多租户接入暂只沿用 `tenant_id` 列预留，不做租户级配额/计费。
