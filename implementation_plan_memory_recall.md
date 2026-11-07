# 记忆召回全表扫描修复（两段式有界召回）

## 背景

`MemoryManager.retrieve()` 与 `remember()` 去重扫描目前都是 `find({"user_id": ...})` 全量拉取该用户记录后
在 Python 内逐条算余弦，单用户记录量上千后内存与 CPU 都会失控。自托管 `mongo:7.0` 没有 `$vectorSearch`
（Atlas 能力），因此在应用层做两段式有界召回，不动基础设施。

## 改动文件树

```
services/harness/core/memory.py   # _candidates() 两路有界查询；retrieve/remember 接入；补两个复合索引
tests/test_memory.py              # 候选有界性/两路合并/向量空间隔离/去重有界 测试
```

## 技术方案

1. **召回路径（retrieve）**：`_embed()` 已返回查询向量的实际产出者 `embedder_name`，
   以此为等值条件走两个复合索引各取一路候选（各 `CANDIDATE_LIMIT=100` 条）：
   - `importance` 降序 Top-N：保住「重要但旧」的记忆；
   - `created_at` 降序 Top-N：保住「新但重要性低」的记忆。
   合并按 `_id` 去重（≤200 条）后，沿用现有混合权重（0.6 相似度 + 0.25 重要性 + 0.15 新鲜度）精排。
   embedder 名下推到查询后，跨向量空间的记录在索引层就被隔离，语义比 Python 侧按维度跳过更正确
   （不同模型即使同维度也不应混比），`_compatible` 保留为兜底。
2. **去重路径（remember）**：去重只防「近重复」，扫描范围收窄为同 user+kind+embedder 的
   最近 `DEDUP_SCAN_LIMIT=50` 条（`created_at` 降序 + limit），不再全表。
3. **索引**：新增 `(user_id, embedder, importance)` 与 `(user_id, embedder, created_at)`，
   `create_index` 幂等、在线构建，非破坏性变更；集合当前很小，构建开销可忽略。
4. **行为不变量**：打分权重、token 预算、去重阈值（0.92）、返回结构全部不变；
   召回上限从「全量」变为「200 候选」，对 k=5 / budget=900 的实际结果无影响。

## 风险点

- **近似召回漏检**：极旧且低重要性的记录即使语义最像也进不了候选。两路候选覆盖的恰是
  混合权重里的重要性/新鲜度两个维度，漏检记录即使召回也排不进 top-k，影响可接受；
  规模上到单用户万级后再评估升级 Mongo 8.2+ `$vectorSearch` 或专用向量库。
- **存量脏数据**：若存在极早期无 `embedder` 字段的记录会被新查询漏掉（本集合随 embedding
  改造新建，所有写入都带该字段；如有手工灌入的脏数据需一次性清理）。
- **候选内排序成本**：200 条 × 256 维余弦为微秒级，无性能风险。

## 验证

- `pytest tests/test_memory.py`：新增 4 个用例（候选有界、两路合并、空间隔离、去重命中与有界扫描）。
- `.venv/Scripts/python -m pytest -q` 全量回归；`ruff check services/harness tests`。
