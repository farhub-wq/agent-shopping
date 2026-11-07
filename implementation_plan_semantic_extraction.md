# 语义记忆抽取升级：LLM 抽取 + 槽位冲突替换

## 背景

`extract_semantic` 只有 4 条正则（称呼/地址/偏好/过敏），换个说法就漏
（「我一般周末不方便收货」「小孩对花生过敏」）；且同一属性多次抽取只会
靠 0.92 相似度去重，表述略变就新旧并存——尺码从 L 改 XL 后两条事实同库，
召回时可能把过期尺码喂给模型。另外抽取在 `_prepare` 里同步跑在回答之前，
接入 LLM 抽取会把延迟加进关键路径。

## 改动文件树

```
services/harness/core/memory.py    # extract_semantic 重写：正则+LLM 双路、槽位冲突替换
services/harness/core/runtime.py   # 抽取从 _prepare 挪到回答完成后（off critical path）
tests/test_memory.py               # _FakeCollection 补点键过滤/delete_many；4 个新用例
tests/test_runtime_stream.py       # _FakeMemory.extract_semantic 签名补 provider
```

## 技术方案

1. **双路抽取**：正则照跑（免费、高精度）；传入 provider 时追加一次
   `decide()` 调用，固定系统提示要求输出 `[{"slot","text"}]` JSON 数组
   （≤3 条、text≤80 字符、slot≤16 字符）。解析失败/模型降级话术/离线
   DeterministicPlanner 产出不可解析 → 静默退回纯正则，行为不劣于现状。
2. **槽位冲突替换**：事实带 slot（正则 label 或 LLM 类别）。落库时同
   (user_id, kind=semantic, metadata.slot) 旧记录整条 delete 后 insert 新事实——
   新事实覆盖旧事实，不再并存。非原子两步，崩溃最坏丢一条可再抽取的事实。
   无 slot 的 LLM 事实走原 remember 去重路径。
3. **移出关键路径**：runtime 在回答完成、写情节记忆之前调用抽取，
   本轮响应不受 LLM 抽取延迟影响；事实供下一轮召回。
4. **成本说明**：每用户消息 +1 次小提示词 decide 调用（不进运行预算的
   prompt_chars 统计——它是平台增强不是对话行为），已在此处记录口径。

## 风险点

- LLM 可能抽出幻觉事实：prompt 限定「只抽取用户明确说的」，且事实权重
  只在召回占 0.25，业务动作仍必须走工具取证，风险可控。
- slot 自由文本可能同义漂移（「尺码」vs「尺寸」）：归一化只做 strip，
  漂移会导致同义槽位并存，危害等同现状（并存），不引入新问题。
- 抽取调用走 FailoverProvider，模型全挂时 CannedProvider 话术解析失败
  自动退回正则——无额外分支。

## 验证

- `pytest tests/test_memory.py tests/test_runtime_stream.py`
- 全量 `pytest -q` + `ruff check`。
