# 熔断器状态挪 Redis（跨 worker 共享）

## 背景

`CircuitBreaker` 的失败计数与开路时刻存在进程内存里（time.monotonic），
多 worker 部署时各算各的：主模型宕机期，N 个 worker 各自要失败满阈值才开路，
放大了打到故障端点的无效请求量，恢复探测也是各 worker 独立进行。

## 改动文件树

```
services/harness/core/circuit.py   # 重写：Redis 共享状态 + fail-open + 悲观单试探
services/harness/core/llm.py       # 三个方法改 await；build_provider(redis) 接线
services/harness/app.py            # build_provider(redis) 传入
tests/test_circuit.py              # 重写：共享状态/单试探/fail-open/恢复闭合
tests/test_llm.py                  # CircuitBreaker 构造点补 FakeRedis（7 处）
```

## 技术方案

状态两个键（name 维度隔离主/降级端点）：
- `shop:harness:circuit:{name}:failures`：连续失败计数（INCRBY 原子，TTL=2×冷却期）
- `shop:harness:circuit:{name}:open_until`：开路截止的 Unix 时间戳

语义：
1. **放行判断**：无 open_until → 闭合放行；open_until > now → 开路拒绝；
   open_until 已过 → 进入半开：**先悲观续写 open_until=now+冷却期**再放行本进程
   这一次试探——其他 worker 看到新时间戳继续 fail-fast，实现跨进程单试探。
2. **成功**：DELETE 两键，真正闭合。
3. **失败**：INCRBY 计数；达到阈值写 open_until=now+冷却期。
4. **Redis 自身故障：fail-open**。熔断是护栏不是关键路径，护栏基础设施挂了
   不能把模型调用掐死；热路径（allow_request）记 debug 日志防刷屏。
5. 时钟用 `time.time()`（跨进程必须墙钟，monotonic 只在单进程有意义）；
   NTP 漂移会造成开路期少量偏差，护栏口径可接受。构造函数留 `now_fn` 供测试注入。

## 风险点

- 半开试探的悲观续写与探测之间非原子（先 SET 后请求），并发下可能有极少数
  worker 同窗各试探一次；最坏后果是每次冷却多几个探测请求，可接受。
- failures/open_until 的 TTL 自然过期后电路视同闭合，下个失败重新计数——
  经典衰减行为，换来无需后台清理任务。
- llm.py 调用点改 await 是签名级变更，全部调用点与测试同步更新。

## 验证

- `pytest tests/test_circuit.py tests/test_llm.py`
- 全量 `pytest -q` + `ruff check`。
