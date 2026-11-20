# 实现计划：Agent 多模型接入、用量监控与对话隔离

> 状态：待确认。PRD 见 `.ai/PRD_agent_models.md`。

## 1. 改动文件树

```
packages/shop_common/config.py          (改) 新增 model_key_encryption_key + 生产守卫
services/harness/core/crypto.py         (新) 模型 key 的 Fernet 加密/解密助手
services/harness/core/providers.py      (新) ModelProvider CRUD 领域 + 解析降级链 + 连通测试
services/harness/core/llm.py            (改) provider 按记录装配 + AnthropicProvider + usage 回传
services/harness/core/runtime.py        (改) 真实 usage 采集与落库挂钩
services/harness/core/memory.py         (改) 工作记忆 key 加 user_id 前缀
services/harness/models.py              (新) ModelProvider/Conversation/Message/UsageRecord ORM
services/harness/app.py                 (改) chat 归属校验/持久化 + providers/usage/conversations 路由 + lifespan 建表与默认免费 AI 幂等
migrations/harness/env.py + alembic.ini + versions/0001_add_agent_tables.py  (新)
migrations/_common.py                   (改) DB_SERVICES 加 "harness"
tests/test_agent_providers.py           (新) provider CRUD/加密/连通测试/默认免费 AI
tests/test_agent_isolation.py           (新) 会话归属/工作记忆隔离/用量落库
web/src/lib/api.ts                      (改) 类型 + 函数封装
web/src/pages/ModelSettings.tsx         (新) 模型设置页
web/src/pages/Usage.tsx                 (新) 用量监控页
web/src/pages/Agent.tsx                 (改) provider 选择器 + 会话历史
web/src/App.tsx                         (改) /models、/usage 路由
```

## 2. 技术方案

### 2.1 配置与密钥

- [config.py](file:///c:/Users/far/Desktop/git/shop-agent/packages/shop_common/config.py)：新增 `model_key_encryption_key: str`（Fernet 32 字节 urlsafe-base64），加入 `DEV_SECRET_DEFAULTS` 与 `COMMON_SECRETS` 校验（生产守卫拒绝默认值/过短值），与现有 `mfa_encryption_key` 同款处理。
- [core/crypto.py](file:///c:/Users/far/Desktop/git/shop-agent/services/harness/core/crypto.py)：`encrypt_model_key(plain) / decrypt_model_key(token)`，沿用 identity/security.py 的 Fernet 模式（解密失败抛 `ValueError` 由调用方转 `ApiError`，不静默吞）。

### 2.2 模型与迁移

[models.py](file:///c:/Users/far/Desktop/git/shop-agent/services/harness/models.py) 定义 4 张表（见 PRD 2），`user_id/tenant_id` 用 `String(36)`，时间用 `UTCDateTime`，token 用 `BigInteger`。

- 新建 `migrations/harness/`：`env.py` 照抄其它服务（`from _common import run_migrations`），`alembic.ini` 指到 harness。
- [migrations/_common.py](file:///c:/Users/far/Desktop/git/shop-agent/migrations/_common.py)：`DB_SERVICES` 加 `"harness"`。
- 首版迁移显式 `op.create_table` 四张表 + 两条复合索引。**注意**：harness 加关系库后，`_load_service_models` import `harness.app` 会把 import 链上的共享表（outbox_events 等，若被引入）一并纳入 autogenerate——首版迁移只显式建 4 张业务表，若 autogenerate 出现共享表需人工确认是否纳入（对齐其它服务"共享表随服务库落地"的既有行为）。
- harness lifespan 增 `init_models()`（dev/test 的 SQLite 用 create_all 补表），生产走 Alembic；`EventBus` 保持 `session_factory=None`（harness 只发审计不消费，审计仍走 Redis Stream，现状不变）。

### 2.3 provider 领域与降级链

[core/providers.py](file:///c:/Users/far/Desktop/git/shop-agent/services/harness/core/providers.py)：

- `PRESETS`：内置厂商预设字典（deepseek/glm/openai-codex/claude/chatanywhere/custom），供 `GET /providers/presets`。
- `resolve_provider(session, user_id, provider_id) -> ProviderRecord | None`：按 PRD 3.5 降级链查库（`provider_id → 用户最近 → is_default → None`），`enabled=false` 跳过。
- `test_provider(record) -> (ok, latency_ms, message)`：按 `provider_type` 发最小请求（openai_compatible 走 `/chat/completions`、anthropic 走 `/v1/messages`，`max_tokens=1`，10s 超时），返回脱敏结果。
- SSRF 校验：`base_url` 解析 host 拒绝内网地址段（PRD 3.3 所列）。

### 2.4 LLM 装配与 usage 采集

[llm.py](file:///c:/Users/far/Desktop/git/shop-agent/services/harness/core/llm.py)：

- `build_provider(redis)` 改为 `build_provider(redis, record: ProviderRecord | None)`：`record=None` → `DeterministicPlanner`（离线兜底）；`record.provider_type=openai_compatible` → 现有 `OpenAICompatibleProvider`；`anthropic` → 新增 `AnthropicProvider`（POST `{base_url}/v1/messages`，`x-api-key` 头，SSE 流式解析 `content_block_delta`）。
- `OpenAICompatibleProvider` / `AnthropicProvider` 在 `decide`/`decide_stream` 成功后，把真实 `usage`（prompt/completion/total tokens）与 `latency_ms` 附到 `Decision`（已有 `usage: dict` 字段，补 latency）。

### 2.5 运行时挂钩

[runtime.py](file:///c:/Users/far/Desktop/git/shop-agent/services/harness/core/runtime.py)：`run`/`run_stream` 结束时，从各步 `Decision.usage` 汇总真实 token 与总延迟，异步写 `usage_records`（单条 per run，含 provider_id/model/conversation_id/run_id/status）。写失败只记日志、不阻断对话（观测数据）。写入用一次性 `session`，不做循环逐条。

### 2.6 会话与隔离

- [memory.py](file:///c:/Users/far/Desktop/git/shop-agent/services/harness/core/memory.py)：工作记忆 key 由 `shop:mem:work:{session_id}` 改为 `shop:mem:work:{user_id}:{session_id}`（`_work_key` 加 user_id 参数，调用链透传）。
- chat 端点（[app.py](file:///c:/Users/far/Desktop/git/shop-agent/services/harness/app.py)）：`ChatRequest` 增 `conversation_id/provider_id`；`conversation_id` 非空时查库校验 `user_id + tenant_id`，不匹配抛 403；新会话创建 `conversation`，消息（user 与 final assistant 各一条）落 `messages`；`last_message_at` 更新。`session_id` 缺省时用 `conversation_id` 作为 Redis 工作记忆后缀。
- guest：`principal.user_id` 为访客身份时跳过落库/usage，只跑默认免费 AI。

### 2.7 路由

[app.py](file:///c:/Users/far/Desktop/git/shop-agent/services/harness/app.py) 新增：`GET /providers/presets`、`GET/POST /providers`、`PATCH/DELETE /providers/{id}`、`POST /providers/{id}/test`、`GET /conversations`、`GET /conversations/{id}/messages`、`GET /usage`（`require_staff`）。响应一律 `_to_dict` 显式挑字段，`api_key` 永不进响应。

### 2.8 前端

- [api.ts](file:///c:/Users/far/Desktop/git/shop-agent/web/src/lib/api.ts)：类型 + 函数（见 PRD 4），`chatStream` 增 `conversationId/providerId` 参数。
- [ModelSettings.tsx](file:///c:/Users/far/Desktop/git/shop-agent/web/src/pages/ModelSettings.tsx)：预设下拉 + key 输入 + 测试按钮（展示 latency_ms/ok）+ 保存/默认 + 列表。
- [Usage.tsx](file:///c:/Users/far/Desktop/git/shop-agent/web/src/pages/Usage.tsx)：聚合维度切换 + 表格（token/次数/平均与 p95 延迟/失败数）。
- [Agent.tsx](file:///c:/Users/far/Desktop/git/shop-agent/web/src/pages/Agent.tsx)：顶部 provider 下拉 + 会话历史侧栏；发送携带新参数。
- [App.tsx](file:///c:/Users/far/Desktop/git/shop-agent/web/src/App.tsx)：`/models`、`/usage` 路由 + 导航（按角色显示）。

## 3. 风险点与对策

| 风险 | 对策 |
|---|---|
| harness 无关系库 → 有关系库的迁移/部署影响 | 迁移目录 + `DB_SERVICES` 白名单 + compose 给 harness 建库；EventBus 保持 `session_factory=None`，审计仍走 Redis Stream；共享表若被 import 链带入需在首版迁移人工确认 |
| 用户自填 key 的 SSRF | test 端点 host 内网段黑名单 + 仅 http(s) + 10s 超时 |
| key 外泄 | Fernet 加密落库；响应只回 `key_masked`；日志/异常脱敏；解密失败转 ApiError |
| 跨用户串上下文（IDOR） | 工作记忆 key 加 user_id 前缀 + conversation 归属强制校验 + usage 查询 require_staff |
| 用量写放大（高并发） | 每 run 合并一条记录、异步写入、失败不阻断；监控页查询走复合索引 |
| provider 降级链误选 | `enabled=false` 跳过 + 熔断开路沿链降级 + 兜底离线规则；`resolve_provider` 单测覆盖 |

## 4. 验证

1. `tests/test_agent_providers.py`：presets 返回；CRUD 归属校验（非 staff 不可建共享/改他人）；key 加密落库且响应无明文；test 连通成功与失败路径；SSRF 内网地址拒绝；默认免费 AI 幂等写入。
2. `tests/test_agent_isolation.py`：A 访问 B 的 conversation 403；同 session_id 不同 user_id 工作记忆隔离；消息持久化与恢复；真实 usage 落库与 `GET /usage` 聚合一致。
3. 全量：`.venv/Scripts/python -m pytest -q`、`.venv/Scripts/python -m ruff check packages services tests`、`cd web && npm run build`。
4. 手工冒烟（dev）：模型设置页新建 DeepSeek provider → 测试延迟 → Agent 页选它对话 → 刷新会话历史仍在 → 用量页看到 token/延迟。
