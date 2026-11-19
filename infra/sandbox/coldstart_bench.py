"""沙箱冷启动延迟基线：串行跑 N 次脚本，统计 runner 侧 duration_ms 分位。

冷启动 = 每次执行都是全新容器（一次一容器、无预热池）。runner 返回的
duration_ms 覆盖容器 create + start + 脚本执行 + 日志读取 + dedicated 节点独占检查，
即用户感知的 runner 侧端到端耗时；用「空脚本」逼近纯容器/解释器开销，
用「import 依赖脚本」逼近带 numpy/pandas 的真实业务冷启动。

在控制面机器执行（需要仓库依赖与 runner 专用签名密钥）：

    SANDBOX_RUNNER_URLS=http://<node>:8000 \
    RUNNER_SIGNING_SECRET=<runner 专用密钥> \
    python infra/sandbox/coldstart_bench.py

样本数与依赖可用环境变量覆盖：
COLDSTART_SAMPLES=30（默认）、SANDBOX_PROBE_MODULES=numpy,pandas（默认）。
只测单 runner；多节点请逐个跑。前几次含守护进程冷缓存，默认丢弃 3 次 warmup。
"""

import json
import os
import sys
import uuid
from pathlib import Path


def _ensure_packages_path() -> None:
    """补仓库包路径，兼容把脚本复制到节点/容器里执行的场景。"""
    for start in (Path(__file__).resolve().parent, Path.cwd()):
        for directory in (start, *start.parents):
            candidate = directory / "packages" / "shop_common"
            if candidate.is_dir():
                sys.path.insert(0, str(directory / "packages"))
                return


_ensure_packages_path()

import httpx  # noqa: E402
from shop_common.auth import signed_identity_headers  # noqa: E402

REQUEST_TIMEOUT_SECONDS = 60
PROBE_TIMEOUT_SECONDS = 30
WARMUP_SAMPLES = 3

# 空脚本本身执行 <10ms，duration_ms 逼近「容器创建 + 启动 + 日志读取」的纯冷启动。
EMPTY_CODE = "print('ok')\n"


def _deps_code(modules: list[str]) -> str:
    """带依赖脚本：贴近数据分析业务的冷启动，包含 numpy/pandas 的 import 开销。"""
    return (
        "import importlib\n"
        f"for name in {modules!r}:\n"
        "    importlib.import_module(name)\n"
        "print('ok')\n"
    )


def _runner_urls() -> list[str]:
    raw = os.environ.get("SANDBOX_RUNNER_URLS") or os.environ.get("SANDBOX_RUNNER_URL", "")
    return [item.strip().rstrip("/") for item in raw.split(",") if item.strip()]


def _samples() -> int:
    try:
        return max(1, int(os.environ.get("COLDSTART_SAMPLES", "30")))
    except ValueError:
        return 30


def _probe_modules() -> list[str]:
    raw = os.environ.get("SANDBOX_PROBE_MODULES", "numpy,pandas")
    return [item.strip() for item in raw.split(",") if item.strip()]


def _percentile(values: list[int], p: float) -> float:
    """线性插值分位：样本量小（30~50）时也比取整索引稳定。"""
    if not values:
        return 0.0
    ordered = sorted(values)
    k = (len(ordered) - 1) * p
    low = int(k)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] * (1 - (k - low)) + ordered[high] * (k - low)


def _report(name: str, durations: list[int]) -> None:
    if not durations:
        print(f"{name}: 无有效样本")
        return
    print(
        f"{name}: n={len(durations)} min={min(durations)} "
        f"p50={_percentile(durations, 0.50):.0f} p90={_percentile(durations, 0.90):.0f} "
        f"p95={_percentile(durations, 0.95):.0f} p99={_percentile(durations, 0.99):.0f} "
        f"max={max(durations)} (ms)"
    )


def _run_once(
    client: httpx.Client, url: str, secret: str, code: str, tag: str
) -> tuple[int | None, str | None]:
    payload = {"code": code, "timeout": PROBE_TIMEOUT_SECONDS}
    # 签名必须绑定请求体：runner 用原始字节摘要验签，签名与发送必须用同一份字节。
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    headers = {
        **signed_identity_headers("coldstart-bench", "admin", "platform", secret=secret, body=body),
        "X-Execution-Id": f"coldstart-{tag}-{uuid.uuid4().hex[:16]}",
        "Content-Type": "application/json",
    }
    try:
        response = client.post(f"{url}/run", content=body, headers=headers)
    except httpx.HTTPError as exc:
        return None, f"请求失败: {type(exc).__name__}"
    if response.status_code != 200:
        return None, f"HTTP {response.status_code}: {response.text[:120]}"
    payload = response.json()
    if payload.get("status") != "ok":
        return None, f"status={payload.get('status')} stderr={str(payload.get('stderr') or '')[:120]}"
    duration = payload.get("duration_ms")
    if duration is None:
        return None, "响应缺少 duration_ms"
    return int(duration), None


def _measure(
    client: httpx.Client, url: str, secret: str, name: str, code: str
) -> list[int]:
    durations: list[int] = []
    errors = 0
    total = WARMUP_SAMPLES + _samples()
    for i in range(total):
        duration, error = _run_once(client, url, secret, code, name)
        if error is not None:
            errors += 1
            print(f"  #{i + 1} 失败: {error}")
            continue
        assert duration is not None
        if i < WARMUP_SAMPLES:
            # 前几次含守护进程/镜像层冷缓存，属于一次性开销，不计入基线。
            continue
        durations.append(duration)
    if errors:
        print(f"  {name}: {errors}/{total} 次失败（已计入，基线只统计成功样本）")
    return durations


def main() -> int:
    urls = _runner_urls()
    secret = os.environ.get("RUNNER_SIGNING_SECRET", "").strip()
    if not urls:
        print("缺少 SANDBOX_RUNNER_URLS（或 SANDBOX_RUNNER_URL）")
        return 2
    if len(secret) < 32:
        print("RUNNER_SIGNING_SECRET 缺失或不足 32 字符，runner 会拒绝签名头")
        return 2

    url = urls[0]
    modules = _probe_modules()
    print(f"冷启动基线：{url}（warmup 丢弃 {WARMUP_SAMPLES} 次，样本 {_samples()} 次）")

    with httpx.Client(timeout=REQUEST_TIMEOUT_SECONDS) as client:
        empty = _measure(client, url, secret, "empty", EMPTY_CODE)
        _report("空脚本（纯容器冷启动）", empty)

        deps = _measure(client, url, secret, "deps", _deps_code(modules))
        _report(f"依赖脚本（import {','.join(modules)}）", deps)

    print("提示：空脚本 p95 即纯冷启动；依赖脚本与空脚本之差 ≈ import 开销。")
    print("决策参考：p95 < 300ms 无需预热池；p95 > 1s 值得做 warm 容器池。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
