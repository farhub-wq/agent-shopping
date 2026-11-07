# 审批重放续跑：从断点恢复计划，而非只重放单个工具

## 背景

`approve_and_replay` 目前只执行被拦的那一个工具就结束：被中断 plan 的
后续 step 全部丢弃，模型也永远看不到工具结果，复杂多步操作留下半成品。
附带发现两处既有缺陷：
- 重放用 `trust="certified"`  blanket 执行，审批一个工具等于给整个重放
  放开最高权限——提权通道。
- 重放新建 `TraceLedger(run_id)` 从 seq=1 重新编号，与原 run 的事件在
  同一 Mongo 文档里产生重复 seq，digest 只覆盖重放段，证据链断裂。

## 改动文件树

```
services/harness/core/trace.py      # TraceLedger.load()：续接既有事件序列
services/harness/core/runtime.py    # 抽离 _run_loop；pending 负载带续跑状态；
                                    # approve_and_replay 重写为断点续跑
tests/test_runtime_stream.py        # 4 个新用例 + 存量审批用例适配
```

## 技术方案

1. **负载扩容**：needs_approval 停顿时，Redis 负载除 tool/args 外增加
   `system` / `messages`（含 assistant tool_calls 与已回填结果）/
   剩余 `steps`+`call_ids` / `ctx` / `user_message` / 已耗模型步数。
2. **续跑两段式**（approve_and_replay）：
   - Phase A 确定性执行：重建原始发起人 ctx（审批人身份只进 trace），
     `approved.add(被拦工具)` + **manifest.trust 原等级** 逐 step 执行被拦步
     与剩余计划步（无模型参与、不耗 token）；再撞 needs_approval → 更新
     负载（剩余步骤 + 最新快照）覆盖写回，run 保持待审批。
   - Phase B 模型消化：全部步骤落地后删除 Redis key（一次性语义），
     走同样的配额预留/运行预算护栏恢复 ReAct 循环，模型产出最终答案。
     配额不足时工具已落地，诚实交代执行结果（degraded），不编造总结。
3. **账本延续**：`TraceLedger.load(run_id)` 从 Mongo 读回既有事件再追加，
   seq 连续、digest 覆盖「原 run + 审批 + 续跑」完整历史；顺带修复
   重放 seq 重号缺陷。
4. **重构**：`_run_events` 主循环机械抽离为 `_run_loop`（状态经 state
   字典回传），`_run_events` 与 `approve_and_replay` 共用，行为不变。

## 风险点

- 消息快照进 Redis（1h TTL）：体积随会话长度增长，工作记忆 6 轮 + 计划
  步数有界，量级 KB~几十 KB，可接受。
- 续跑的 ReAct 循环重新计 max_steps：审批相当于授予一轮新的推理预算，
  步数上限本身仍在，风险可控。
- Phase A 执行期间进程崩溃：key 未删，审批可重试；已执行工具是幂等性
  由工具自身保证（如工单起草可重入），与现状一致。

## 验证

- 新用例：续跑全链路、二次审批保持待审批、一次性语义、续跑不提权。
- 全量 `pytest -q` + `ruff check`。
