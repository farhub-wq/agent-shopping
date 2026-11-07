# 工具输出脱敏修复：结构化结果递归脱敏

## 背景

护栏第 4 步「输出脱敏」目前只对字符串结果跑 `redact()`，结构化结果（dict/list）原样放行，
依赖「各工具实现自行保证不含敏感字段」——但 `order_lookup` 直接返回下游订单服务的完整
payload（收货人姓名/手机号/地址），`data_class=pii` 只是权限标记，内容并未脱敏。
这些数据会进入三处：模型上下文、messages 历史、trace 的 tool.result 预览。

## 改动文件树

```
services/harness/core/tools.py   # 新增 redact_data() 递归脱敏；execute 第 4 步全类型接入
tests/test_tools.py              # 新建：redact/redact_data/execute 脱敏路径测试
```

## 技术方案

1. **`redact_data(data)`**：dict 按值下钻、list 按元素下钻、字符串走现有 `redact()`
   （手机号/身份证/邮箱正则），数字/布尔/None 等标量原样返回。内容级正则兜底，
   不维护字段名清单（字段名方案漏掉自由文本里嵌的 PII）。
2. **`execute()` 第 4 步**：`if isinstance(data, str)` 改为对所有返回类型 `redact_data(data)`。
   trace 预览与 messages 回填都发生在 execute 之后，单点脱敏全覆盖。
3. **明确边界（不脱敏）**：工具的 `args` 不脱敏——它是用户自己发起的内容，
   且待审批负载重放（approve_and_replay）需要原样参数；证据接口已有 IDOR 防护。

## 风险点

- **误脱敏**：手机号正则 `1[3-9]\d{9}` 可能命中 11 位连续数字的编码类字符串；
  订单号（SO 前缀）、金额（数字类型）、SKU 均不受影响。误伤成本是模型看到掩码字符串，
  远低于 PII 泄漏成本，可接受。
- **下游兼容**：脱敏只发生在工具结果返回给模型/前端之前，下游服务收到的请求不受影响。

## 验证

- `pytest tests/test_tools.py`：字符串脱敏回归、dict/list 递归、标量不动、
  订单号/金额不误伤、execute 全链路结构化输出脱敏。
- 全量 `pytest -q` + `ruff check services/harness tests`。
