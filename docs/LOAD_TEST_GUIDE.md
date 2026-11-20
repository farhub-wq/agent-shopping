# 生产环境压力测试指南

## 前置条件

1. 启动所有服务:
```bash
docker compose up -d
```

2. 等待所有服务健康（约 30-60 秒）:
```bash
docker compose ps
```

3. 确保以下端口可访问:
   - Gateway: http://localhost:8080
   - Harness: http://localhost:8006
   - Prometheus: http://localhost:9090
   - Grafana: http://localhost:3000

## 运行测试

### 快速测试（10 并发，30 秒）
```bash
.venv\Scripts\python.exe tests\load_test.py
```

### 观察指标

测试运行期间，打开以下链接实时观察:

1. **Prometheus 指标**: http://localhost:9090/graph
   - 查询 `db_pool_exhausted_total` - 应为 0
   - 查询 `llm_fallback_total` - 应为 0
   - 查询 `http_request_duration_seconds` - 观察延迟分布

2. **Grafana 仪表板**: http://localhost:3000
   - 默认账号: admin / admin

3. **Docker 资源监控**:
```bash
docker stats
```

## 测试场景

### 场景 1: 基础并发 (10/50/100)
```python
CONCURRENCY_LEVELS = [10, 50, 100]
TEST_DURATION_SECONDS = 30
```

### 场景 2: 长时间稳定性 (50 并发，5 分钟)
修改 `load_test.py`:
```python
CONCURRENCY_LEVELS = [50]
TEST_DURATION_SECONDS = 300
```

### 场景 3: 峰值压测 (200 并发)
```python
CONCURRENCY_LEVELS = [200]
TEST_DURATION_SECONDS = 60
```

## 通过标准

### 性能指标
- ✓ 错误率 < 5%
- ✓ P95 延迟 < 3000ms
- ✓ P99 延迟 < 5000ms
- ✓ QPS ≥ 10 (10 并发时)

### 资源指标
- ✓ db_pool_exhausted_total = 0（无连接池耗尽）
- ✓ llm_fallback_total = 0（主模型稳定）
- ✓ llm_circuit_open_total = 0（无熔断）
- ✓ 内存增长 < 20%（无明显泄漏）

### Docker 容器
- ✓ harness 内存 < 512MB
- ✓ CPU 使用率 < 80%
- ✓ 无容器重启

## 故障排查

### 错误率过高
```bash
# 查看 harness 日志
docker compose logs harness --tail=100

# 查看数据库连接
docker compose exec mysql mysql -u shop -pshop -e "SHOW PROCESSLIST"
```

### 延迟过高
```bash
# 检查 Redis 连接
docker compose exec redis redis-cli --no-auth-warning -a dev-redis-password PING

# 检查 MongoDB
docker compose exec mongo mongosh --quiet --eval "db.adminCommand('ping')"
```

### 连接池耗尽
```bash
# 查看 Prometheus 指标
curl http://localhost:9090/api/v1/query?query=db_pool_exhausted_total
```

## 测试报告

测试完成后会生成 `load_test_YYYYMMDD_HHMMSS.json`，包含:
- 每个并发级别的详细指标
- 错误率、QPS、延迟分位数
- 状态码分布

## 清理环境

测试完成后:
```bash
# 停止所有服务
docker compose down

# 清理数据卷（可选）
docker compose down -v
```
