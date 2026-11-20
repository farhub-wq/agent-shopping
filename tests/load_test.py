#!/usr/bin/env python3
"""生产环境压力测试脚本 - 验证连接池复用和并发安全修复"""

import asyncio
import json
import statistics
import time
from collections import defaultdict
from datetime import datetime
from typing import Any

import httpx

# 测试配置
GATEWAY_URL = "http://localhost:8080"
HARNESS_URL = "http://localhost:8006"
PROMETHEUS_URL = "http://localhost:9090"

# 测试参数
CONCURRENCY_LEVELS = [10, 50, 100]
TEST_DURATION_SECONDS = 30
WARMUP_REQUESTS = 10


class MetricsCollector:
    def __init__(self):
        self.latencies: list[float] = []
        self.errors: list[str] = []
        self.start_time = time.time()
        self.end_time = 0
        self.status_codes: dict[int, int] = defaultdict(int)

    def add_latency(self, latency_ms: float):
        self.latencies.append(latency_ms)

    def add_error(self, error: str, status_code: int = 0):
        self.errors.append(error)
        if status_code:
            self.status_codes[status_code] += 1

    def finish(self):
        self.end_time = time.time()

    def summary(self) -> dict[str, Any]:
        if not self.latencies:
            return {"error": "没有成功的请求", "errors": len(self.errors), "error_rate": 1.0}

        sorted_latencies = sorted(self.latencies)
        duration = self.end_time - self.start_time
        total = len(self.latencies) + len(self.errors)

        return {
            "total": total,
            "success": len(self.latencies),
            "failed": len(self.errors),
            "error_rate": len(self.errors) / total if total else 0,
            "duration_s": duration,
            "qps": len(self.latencies) / duration if duration > 0 else 0,
            "p50_ms": sorted_latencies[len(sorted_latencies) // 2],
            "p95_ms": sorted_latencies[int(len(sorted_latencies) * 0.95)],
            "p99_ms": sorted_latencies[int(len(sorted_latencies) * 0.99)],
            "avg_ms": statistics.mean(self.latencies),
            "min_ms": min(self.latencies),
            "max_ms": max(self.latencies),
        }




async def check_health() -> bool:
    """检查服务健康状态"""
    print("\n" + "=" * 60)
    print("步骤 1: 服务健康检查")
    print("=" * 60)

    services = {"Gateway": f"{GATEWAY_URL}/healthz", "Harness": f"{HARNESS_URL}/healthz"}

    all_healthy = True
    async with httpx.AsyncClient(timeout=10.0) as client:
        for name, url in services.items():
            try:
                r = await client.get(url)
                ok = r.status_code == 200
                print(f"  {name:15s} {'✓ 健康' if ok else f'✗ HTTP {r.status_code}'}")
                all_healthy = all_healthy and ok
            except Exception as e:
                print(f"  {name:15s} ✗ 无法连接: {e}")
                all_healthy = False
    return all_healthy


async def get_token() -> str:
    """获取测试 token"""
    async with httpx.AsyncClient() as client:
        r = await client.post(
            f"{GATEWAY_URL}/api/auth/dev/token",
            json={"role": "operator", "user_id": "load-test", "tenant": "default"},
        )
        if r.status_code != 200:
            raise RuntimeError(f"无法获取 token: HTTP {r.status_code}")
        return r.json()["access_token"]


async def test_chat_request(client: httpx.AsyncClient, token: str, metrics: MetricsCollector):
    """单次聊天请求"""
    start = time.perf_counter()
    try:
        r = await client.post(
            f"{HARNESS_URL}/chat",
            json={"message": "查询商品 SKU-1001 的库存", "skill": "ops-catalog"},
            headers={"Authorization": f"Bearer {token}"},
            timeout=30.0,
        )
        if r.status_code >= 400:
            metrics.add_error(f"HTTP {r.status_code}", r.status_code)
        else:
            metrics.add_latency((time.perf_counter() - start) * 1000)
    except httpx.TimeoutException:
        metrics.add_error("timeout", 0)
    except Exception as e:
        metrics.add_error(str(e), 0)


async def run_concurrency_test(concurrency: int, duration: int, token: str) -> MetricsCollector:
    """并发测试"""
    metrics = MetricsCollector()

    async with httpx.AsyncClient() as client:
        # 预热
        print(f"  预热 {WARMUP_REQUESTS} 个请求...")
        warmup = [test_chat_request(client, token, MetricsCollector()) for _ in range(WARMUP_REQUESTS)]
        await asyncio.gather(*warmup, return_exceptions=True)

        # 正式测试
        print(f"  开始: {concurrency} 并发, {duration} 秒")
        end = time.time() + duration

        async def worker():
            while time.time() < end:
                await test_chat_request(client, token, metrics)
                await asyncio.sleep(0.1)

        await asyncio.gather(*[worker() for _ in range(concurrency)], return_exceptions=True)

    metrics.finish()
    return metrics


def print_summary(level: int, m: MetricsCollector):
    """打印结果"""
    s = m.summary()
    if "error" in s:
        print(f"  ✗ 失败: {s['error']}")
        return

    print(f"  总请求: {s['total']}, 成功: {s['success']}, 失败: {s['failed']}")
    print(f"  错误率: {s['error_rate']*100:.2f}%, QPS: {s['qps']:.1f}")
    print(f"  延迟 P50/P95/P99: {s['p50_ms']:.0f}/{s['p95_ms']:.0f}/{s['p99_ms']:.0f} ms")
    print(f"  平均/最小/最大: {s['avg_ms']:.0f}/{s['min_ms']:.0f}/{s['max_ms']:.0f} ms")

    passed = s["error_rate"] < 0.05 and s["p95_ms"] < 3000
    print(f"  {'✓ 通过' if passed else '✗ 失败'}\n")


async def main():
    print("\n生产环境压力测试 - 验证连接池复用")
    print(f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    # 健康检查
    if not await check_health():
        print("\n✗ 服务不健康，终止测试")
        return

    # 获取 token
    try:
        token = await get_token()
        print("\n✓ 已获取认证 token")
    except Exception as e:
        print(f"\n✗ 无法获取 token: {e}")
        return

    # 并发测试
    print("\n" + "=" * 60)
    print("步骤 2: LLM 并发测试")
    print("=" * 60 + "\n")

    results = {}
    for level in CONCURRENCY_LEVELS:
        print(f"测试 {level} 并发...")
        metrics = await run_concurrency_test(level, TEST_DURATION_SECONDS, token)
        print_summary(level, metrics)
        results[f"concurrency_{level}"] = metrics.summary()
        await asyncio.sleep(3)

    # 保存报告
    report = f"load_test_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    with open(report, "w") as f:
        json.dump(results, f, indent=2)

    print("=" * 60)
    print(f"✓ 测试完成，报告: {report}")
    print("=" * 60 + "\n")


if __name__ == "__main__":
    asyncio.run(main())
