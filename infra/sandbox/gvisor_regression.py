"""gVisor 兼容性回归：通过 runner 的真实执行链路验证 runsc 下的运行时兼容性。

在控制面机器执行（需要仓库依赖与 runner 专用签名密钥）：

    SANDBOX_RUNNER_URLS=http://<node>:8000 \
    RUNNER_SIGNING_SECRET=<runner 专用密钥> \
    python infra/sandbox/gvisor_regression.py

业务镜像预装依赖用逗号分隔追加，例如 `SANDBOX_PROBE_MODULES=numpy,pandas`。
逐个 runner 执行探针，任何一项失败都返回非零退出码；stderr 里出现
ModuleNotFoundError 说明是「镜像缺依赖」，其余多为 runsc syscall 不兼容。
"""

import os
import sys
import uuid
from pathlib import Path
from typing import Any

import httpx


def _ensure_packages_path() -> None:
    """补仓库包路径。

    在仓库内直接运行时脚本位于 infra/sandbox；把它复制到节点/容器里执行时
    `parents[2]` 可能越界，因此从脚本目录与当前工作目录逐级向上找 packages/。
    """
    for start in (Path(__file__).resolve().parent, Path.cwd()):
        for directory in (start, *start.parents):
            candidate = directory / "packages" / "shop_common"
            if candidate.is_dir():
                sys.path.insert(0, str(directory / "packages"))
                return


_ensure_packages_path()

from shop_common.auth import signed_identity_headers  # noqa: E402

# 探针各自覆盖一类 syscall/运行时行为：纯标准库、线程、子进程、文件与 sqlite、浮点统计。
PROBES: list[tuple[str, str]] = [
    (
        "stdlib",
        "import collections, datetime, decimal, hashlib, ipaddress, json, math, "
        "pathlib, re, statistics, tempfile, uuid\n"
        "print('OK stdlib')",
    ),
    (
        "threads",
        "import threading\n"
        "results = []\n"
        "lock = threading.Lock()\n"
        "def work(n):\n"
        "    total = sum(range(n))\n"
        "    with lock:\n"
        "        results.append(total)\n"
        "threads = [threading.Thread(target=work, args=(10000,)) for _ in range(4)]\n"
        "[t.start() for t in threads]\n"
        "[t.join() for t in threads]\n"
        "print('OK threads', len(results))",
    ),
    (
        "subprocess",
        "import subprocess, sys\n"
        "result = subprocess.run(\n"
        "    [sys.executable, '-c', \"print('child')\"],\n"
        "    capture_output=True, text=True, timeout=10,\n"
        ")\n"
        "print('OK subprocess', result.stdout.strip())",
    ),
    (
        "files",
        "import os, sqlite3, tempfile\n"
        "with tempfile.NamedTemporaryFile('w', dir='/tmp', delete=False) as fh:\n"
        "    fh.write('hello')\n"
        "    path = fh.name\n"
        "con = sqlite3.connect(':memory:')\n"
        "con.execute('create table t (a int)')\n"
        "con.execute('insert into t values (1)')\n"
        "assert con.execute('select count(*) from t').fetchone()[0] == 1\n"
        "os.unlink(path)\n"
        "print('OK files')",
    ),
    (
        "math",
        "import math, statistics\n"
        "data = [1, 2, 3, 4, 5]\n"
        "assert abs(statistics.mean(data) - 3) < 1e-9\n"
        "assert math.isclose(math.sqrt(2) ** 2, 2.0, rel_tol=1e-9)\n"
        "print('OK math')",
    ),
]

REQUEST_TIMEOUT_SECONDS = 30
PROBE_TIMEOUT_SECONDS = 20


def _runner_urls() -> list[str]:
    """读取 runner 地址：支持多节点（SANDBOX_RUNNER_URLS 逗号分隔）。"""
    raw = os.environ.get("SANDBOX_RUNNER_URLS") or os.environ.get("SANDBOX_RUNNER_URL", "")
    return [item.strip().rstrip("/") for item in raw.split(",") if item.strip()]


def _probe_modules() -> list[str]:
    raw = os.environ.get("SANDBOX_PROBE_MODULES", "")
    return [item.strip() for item in raw.split(",") if item.strip()]


def _deps_probe(modules: list[str]) -> tuple[str, str]:
    """预装依赖探针：逐个 import，缺依赖与 syscall 不兼容要能从错误信息区分开。"""
    code = (
        "import importlib\n"
        f"for name in {modules!r}:\n"
        "    importlib.import_module(name)\n"
        f"print('OK deps', {len(modules)})"
    )
    return "deps", code


def _health(client: httpx.Client, url: str) -> tuple[bool, str]:
    """探针前先确认 runner 确实以 runsc 运行；否则探针结论没有意义。"""
    try:
        response = client.get(f"{url}/healthz")
    except httpx.HTTPError as exc:
        return False, f"healthz 请求失败: {type(exc).__name__}"
    try:
        payload: dict[str, Any] = response.json()
    except ValueError:
        return False, f"healthz 非 JSON（HTTP {response.status_code}）"
    docker = payload.get("docker") or {}
    runtime = docker.get("runtime")
    runtime_ok = docker.get("runtime_ok")
    if response.status_code != 200 or runtime != "runsc" or not runtime_ok:
        return False, (
            f"未按 runsc 运行: http={response.status_code} "
            f"runtime={runtime} runtime_ok={runtime_ok}"
        )
    return True, f"runtime={runtime} image={payload.get('image')}"


def _run_probe(
    client: httpx.Client,
    url: str,
    secret: str,
    name: str,
    code: str,
) -> tuple[bool, str]:
    headers = {
        **signed_identity_headers("gvisor-regression", "admin", "platform", secret=secret),
        "X-Execution-Id": f"gvisor-{name}-{uuid.uuid4().hex[:16]}",
    }
    body = {"code": code, "timeout": PROBE_TIMEOUT_SECONDS}
    try:
        response = client.post(f"{url}/run", json=body, headers=headers)
    except httpx.HTTPError as exc:
        return False, f"请求失败: {type(exc).__name__}"
    if response.status_code != 200:
        return False, f"HTTP {response.status_code}: {response.text[:200]}"
    payload = response.json()
    status = payload.get("status")
    stdout = str(payload.get("stdout") or "")
    stderr = str(payload.get("stderr") or "")
    if status != "ok":
        hint = "（镜像缺依赖，需增肥 sandbox 镜像）" if "ModuleNotFoundError" in stderr else ""
        return False, f"status={status}{hint} stderr={stderr.strip()[:200]}"
    if "OK" not in stdout:
        return False, f"缺少成功标记 stdout={stdout.strip()[:200]}"
    return True, f"stdout={stdout.strip().splitlines()[-1][:120]}"


def main() -> int:
    urls = _runner_urls()
    secret = os.environ.get("RUNNER_SIGNING_SECRET", "").strip()
    if not urls:
        print("缺少 SANDBOX_RUNNER_URLS（或 SANDBOX_RUNNER_URL）")
        return 2
    if len(secret) < 32:
        print("RUNNER_SIGNING_SECRET 缺失或不足 32 字符，runner 会拒绝签名头")
        return 2

    probes = list(PROBES)
    modules = _probe_modules()
    if modules:
        # 依赖探针放在最后：先证明运行时本身兼容，再看镜像预装依赖是否齐备。
        probes.append(_deps_probe(modules))

    failures = 0
    with httpx.Client(timeout=REQUEST_TIMEOUT_SECONDS) as client:
        for url in urls:
            print(f"\n== {url} ==")
            healthy, detail = _health(client, url)
            print(f"{'PASS' if healthy else 'FAIL'} healthz: {detail}")
            if not healthy:
                failures += 1
                continue
            for name, code in probes:
                passed, detail = _run_probe(client, url, secret, name, code)
                failures += 0 if passed else 1
                print(f"{'PASS' if passed else 'FAIL'} {name}: {detail}")

    print(f"\n结果：{failures} 项失败")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
