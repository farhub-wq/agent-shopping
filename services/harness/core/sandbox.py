"""沙箱客户端：生产经独立 sandbox-runner 执行，本地演示才允许子进程。

安全边界：
- `SANDBOX_ENABLED=true`：只调用独立 runner（runner 持有 Docker socket），
  harness 自身不接触容器运行时；runner 不可用即 fail-closed，绝不回退本地进程。
- `SANDBOX_ENABLED=false`：本地子进程只用于本地演示/测试，不是安全边界。
"""

import asyncio
import shutil
import sys
import time
import uuid
from typing import Any

import httpx
from shop_common.auth import signed_identity_headers
from shop_common.config import get_settings
from shop_common.logging import get_logger
from shop_common.sandbox import SandboxResult

logger = get_logger(__name__)

MAX_OUTPUT_BYTES = 8192
# 客户端超时 = 容器执行超时 + 网络往返与容器创建/删除开销；
# 否则 runner 还在等容器退出，客户端就先超时并误报 unavailable。
RUNNER_TIMEOUT_MARGIN = 10
# 故障 runner 冷却时间：期间排到最后，避免每个请求都先白等一次超时。
RUNNER_COOLDOWN_SECONDS = 30
# 可安全换实例重试的 HTTP 状态：连接级失败或上游不可用；
# 500 不在此列——它可能是容器已执行后响应序列化失败，重试会造成重复执行。
TRANSIENT_STATUSES = {429, 502, 503, 504}


def _truncate(raw: bytes) -> tuple[str, bool]:
    # 输出上限 8KB：既限制回传给模型的上下文成本，也防止脚本用日志刷爆内存。
    if len(raw) > MAX_OUTPUT_BYTES:
        return raw[:MAX_OUTPUT_BYTES].decode("utf-8", errors="replace"), True
    return raw.decode("utf-8", errors="replace"), False


class SandboxManager:
    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.settings = get_settings()
        # transport 仅用于单测注入 MockTransport，生产保持 None（走真实网络）。
        self.transport = transport
        # 轮询游标与按 endpoint 的冷却截止时间；仅进程内状态，不依赖 Redis。
        self._cursor = 0
        self._cooldown_until: dict[str, float] = {}

    @property
    def backend(self) -> str:
        return "remote" if self.settings.sandbox_enabled else "local"

    @property
    def endpoints(self) -> list[str]:
        # 配置是逗号分隔的 URL 列表；去空、去尾斜杠，避免拼出 //run。
        raw = self.settings.sandbox_runner_urls
        return [item.strip().rstrip("/") for item in raw.split(",") if item.strip()]

    def _mark_failure(self, url: str) -> None:
        # 单实例内的短冷却：坏掉的 runner 在冷却期内不再优先接新请求，
        # 避免每个请求都先等一次连接超时；冷却结束后自动重新参与轮询。
        self._cooldown_until[url] = time.monotonic() + RUNNER_COOLDOWN_SECONDS

    def _mark_success(self, url: str) -> None:
        self._cooldown_until.pop(url, None)

    def _ordered_endpoints(self) -> list[str]:
        endpoints = self.endpoints
        if not endpoints:
            return []
        # 轮询起点逐请求后移，避免所有请求同时压到第一台；
        # 冷却中的 runner 排在最后而不是直接丢弃，全部冷却时仍会尝试一次。
        start = self._cursor % len(endpoints)
        self._cursor += 1
        rotated = endpoints[start:] + endpoints[:start]
        now = time.monotonic()
        ready = [url for url in rotated if self._cooldown_until.get(url, 0) <= now]
        cooling = [url for url in rotated if self._cooldown_until.get(url, 0) > now]
        return ready + cooling

    async def runner_status(self) -> dict[str, Any]:
        """并发探测全部 runner，供 /sandbox/health 展示；失败不抛异常。"""
        endpoints = self.endpoints
        if not endpoints:
            return {"status": "unavailable", "endpoints": [], "error": "未配置 SANDBOX_RUNNER_URLS"}
        results = await asyncio.gather(*(self._probe_runner(url) for url in endpoints))
        healthy = any(item.get("status") == "ok" for item in results)
        return {"status": "ok" if healthy else "unavailable", "endpoints": results}

    async def _probe_runner(self, url: str) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=3.0, transport=self.transport) as client:
                response = await client.get(f"{url}/healthz")
                response.raise_for_status()
                payload = response.json()
            if isinstance(payload, dict):
                return {**payload, "url": url}
            return {"url": url, "status": "ok"}
        except Exception as exc:
            return {"url": url, "status": "unavailable", "error": type(exc).__name__}

    async def run(
        self,
        code: str,
        *,
        language: str = "python",
        timeout: int | None = None,
        user_id: str = "harness",
        role: str = "operator",
        tenant: str = "default",
    ) -> SandboxResult:
        if language != "python":
            # 只支持 python：其他语言需要额外镜像与运行时，当前不静默降级。
            return SandboxResult("error", stderr="当前沙箱仅支持 python", backend="none")
        limit = timeout or self.settings.sandbox_timeout_seconds
        if self.settings.sandbox_enabled:
            # 生产边界：必须在独立 runner 的容器里执行；runner 不可用直接拒绝，
            # 绝不回退本地子进程——本地进程与宿主同权限，回退等于抹掉安全边界。
            return await self._run_remote(code, limit, user_id, role, tenant)
        # 未启用沙箱只允许用于本地演示/测试：本地子进程不是安全边界，不能承接生产流量。
        logger.warning("沙箱未启用，使用本地子进程执行（非安全边界，仅限本地演示）")
        return await self._run_local(code, limit)

    async def _run_remote(
        self, code: str, timeout: int, user_id: str, role: str, tenant: str
    ) -> SandboxResult:
        targets = self._ordered_endpoints()
        if not targets:
            # 没配 runner 就拒绝执行，绝不用本地进程顶替生产沙箱。
            return SandboxResult("unavailable", stderr="未配置 SANDBOX_RUNNER_URLS", backend="remote")
        # 身份头必须现签：runner 会再次校验 staff 权限，沙箱能力不能靠内网位置默认放行。
        headers = signed_identity_headers(user_id, role, tenant)
        # execution_id 在整次故障转移中保持不变：换 runner 只是换执行者，
        # 由 runner 用 Redis 去重保证同一段代码最多真正跑一次。
        headers["X-Execution-Id"] = uuid.uuid4().hex
        payload = {"code": code, "language": "python", "timeout": timeout}
        failures: list[str] = []
        # connect 单独限 3s：坏掉的主机快速失败，执行阶段仍给足容器超时。
        client_timeout = httpx.Timeout(timeout + RUNNER_TIMEOUT_MARGIN, connect=3.0)
        async with httpx.AsyncClient(timeout=client_timeout, transport=self.transport) as client:
            for url in targets:
                try:
                    response = await client.post(f"{url}/run", json=payload, headers=headers)
                except Exception as exc:
                    self._mark_failure(url)
                    failures.append(f"{url} 不可达({type(exc).__name__})")
                    continue
                if response.status_code in TRANSIENT_STATUSES:
                    # 429/502/503/504 属于可重试的实例级故障，换下一台。
                    self._mark_failure(url)
                    failures.append(f"{url} HTTP {response.status_code}")
                    continue
                if response.status_code >= 500:
                    # 500 可能是执行后响应失败，换实例会重复执行，直接 fail-closed。
                    self._mark_failure(url)
                    return SandboxResult(
                        "unavailable",
                        stderr=f"sandbox-runner 返回 {response.status_code}",
                        backend="remote",
                    )
                if response.status_code >= 400:
                    # 4xx 是身份/参数问题，换实例结果相同，如实回传。
                    return SandboxResult(
                        "error",
                        stderr=f"sandbox-runner 拒绝请求：{response.status_code}",
                        backend="remote",
                    )
                try:
                    result = SandboxResult.from_dict(response.json())
                except ValueError:
                    self._mark_failure(url)
                    failures.append(f"{url} 响应格式错误")
                    continue
                if result.status == "unavailable":
                    # runner 可达但自身不可用（Docker 故障/排队超时），换一台重试。
                    self._mark_failure(url)
                    failures.append(f"{url} {result.stderr or 'unavailable'}")
                    continue
                self._mark_success(url)
                return result
        logger.error("全部 sandbox-runner 不可用: %s", "; ".join(failures))
        return SandboxResult(
            "unavailable",
            stderr="全部 sandbox-runner 不可用：" + "; ".join(failures),
            backend="remote",
        )

    async def _run_local(self, code: str, timeout: int) -> SandboxResult:
        started = time.perf_counter()
        # 本地兜底只防误操作：进程仍与宿主同权限、可访问真实文件系统，不构成安全边界。
        python = sys.executable or shutil.which("python") or "python"
        process = await asyncio.create_subprocess_exec(
            python,
            "-I",
            "-c",
            code,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_raw, stderr_raw = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except TimeoutError:
            # 超时必须杀掉子进程并回收，否则脚本会在后台继续占用资源。
            process.kill()
            await process.wait()
            return SandboxResult(
                "timeout",
                stderr=f"执行超过 {timeout}s 已终止",
                backend="local",
                duration_ms=int((time.perf_counter() - started) * 1000),
            )
        stdout, truncated = _truncate(stdout_raw)
        stderr, _ = _truncate(stderr_raw)
        return SandboxResult(
            "ok" if process.returncode == 0 else "error",
            stdout=stdout,
            stderr=stderr,
            exit_code=process.returncode or 0,
            duration_ms=int((time.perf_counter() - started) * 1000),
            backend="local",
            truncated=truncated,
        )
