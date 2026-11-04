## 变更说明

<!-- 一句话说明这个 PR 解决什么问题，为什么现在做。 -->

## 变更类型

- [ ] feat：新功能
- [ ] fix：缺陷修复
- [ ] refactor：重构（无行为变化）
- [ ] perf：性能优化
- [ ] docs：文档
- [ ] test：测试
- [ ] chore：构建 / CI / 依赖

## 影响范围

- [ ] 订单状态机 / OrderEvent
- [ ] 库存预占 / Lua 脚本
- [ ] Outbox / 事件消费 / 幂等
- [ ] 支付 / 退款 / 资金
- [ ] 权限 / Agent 工具 / 沙箱
- [ ] 前端 API 层 / 页面
- [ ] 仅文档或 CI

## 验证

<!-- 贴命令与结果，不要只写“已测试”。 -->

```bash
.venv/Scripts/python -m pytest -q
.venv/Scripts/python -m ruff check packages services tests .github/scripts
cd web && npm run build
docker compose -f docker-compose.yml config -q
```

- [ ] 上述命令本地通过
- [ ] 新增/修改逻辑有对应测试
- [ ] 涉及状态流转时，确认走 `resolve_target`
- [ ] 涉及库存时，确认未绕过 Lua 原子脚本
- [ ] 涉及事件时，确认同事务 Outbox + 按 type 过滤 + 幂等
- [ ] 涉及资金时，确认借贷平衡、回调幂等、可对账
- [ ] 涉及权限时，确认没有请求体/Header 自选角色
- [ ] 前端未新增 `any`，未在组件内直接 fetch

## 风险与回滚

<!-- 失败会怎样？如何回滚？是否涉及 DDL、权限或退款逻辑？ -->

- 风险：
- 回滚方式：

## 截图 / 录屏（UI 变更必填）

<!-- 前后对比或关键状态截图。 -->
