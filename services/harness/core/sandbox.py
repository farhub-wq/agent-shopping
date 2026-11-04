"""沙箱执行器：Docker 优先，本地子进程兜底。

明确边界：本地子进程不是安全边界，只能防误操作，不能防恶意代码；
Docker 模式关闭网络、只读根文件系统、限制 CPU/内存/PID，并禁止提权。
"""

import asyncio
import shutil
import sys
import time
from dataclasses import dataclass
from typing import Any

from shop_common.config import get_settings
from shop_common.logging import get_logger

logger = get_logger(__name__)

MAX_OUTPUT_BYTES = 8192


@dataclass
class SandboxResult:
    status: str
    stdout: str = ""
    stderr: str = ""
    exit_code: int = -1
    duration_ms: int = 0
    backend: str = "none"
    truncated: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "exit_code": self.exit_code,
            "duration_ms": self.duration_ms,
            "backend": self.backend,
            "truncated": self.truncated,
        }


def _truncate(raw: bytes) -> tuple[str, bool]:
    # 输出上限 8KB：既限制回传给模型的上下文成本，也防止脚本用日志刷爆内存。
    if len(raw) > MAX_OUTPUT_BYTES:
        return raw[:MAX_OUTPUT_BYTES].decode("utf-8", errors="replace"), True
    return raw.decode("utf-8", errors="replace"), False


class SandboxManager:
    def __init__(self) -> None:
        self.settings = get_settings()

    def docker_available(self) -> bool:
        try:
            import docker  # noqa: F401

            return True
        except ImportError:
            return False

    async def run(self, code: str, *, language: str = "python", timeout: int | None = None) -> SandboxResult:
        if language != "python":
            # 只支持 python：其他语言需要额外镜像与运行时，当前不静默降级。
            return SandboxResult("error", stderr="当前沙箱仅支持 python", backend="none")
        limit = timeout or self.settings.sandbox_timeout_seconds
        if self.settings.sandbox_enabled:
            result = await asyncio.to_thread(self._run_docker, code, limit)
            # 只有「Docker 根本不可用」才降级到本地子进程；
            # 容器已启动但执行出错时不降级，避免用更弱的安全边界重跑同一段代码。
            if result.status != "unavailable":
                return result
            logger.warning("Docker 沙箱不可用，降级到本地子进程（非安全边界）")
        return await self._run_local(code, limit)

    def _run_docker(self, code: str, timeout: int) -> SandboxResult:
        started = time.perf_counter()
        try:
            import docker
            from docker.errors import ContainerError, ImageNotFound

            client = docker.from_env()
        except Exception as exc:
            return SandboxResult("unavailable", stderr=f"docker 不可达: {type(exc).__name__}", backend="docker")

        container = None
        try:
            # 安全基线：网络模式走配置（默认 none）、只读根文件系统、/tmp 限额可写、
            # 限制 CPU/内存/PID、禁止提权、以 65534 低权用户运行、python -I 隔离用户 site-packages。
            container = client.containers.create(
                self.settings.sandbox_image,
                command=["python", "-I", "-c", code],
                network_mode=self.settings.sandbox_network,
                mem_limit=f"{self.settings.sandbox_memory_mb}m",
                nano_cpus=int(self.settings.sandbox_cpu * 1_000_000_000),
                pids_limit=64,
                read_only=True,
                tmpfs={"/tmp": "rw,size=32m"},
                security_opt=["no-new-privileges"],
                user="65534:65534",
                working_dir="/tmp",
            )
            container.start()
            wait_result = container.wait(timeout=timeout)
            stdout_raw = container.logs(stdout=True, stderr=False)
            stderr_raw = container.logs(stdout=False, stderr=True)
            stdout, truncated = _truncate(stdout_raw)
            stderr, _ = _truncate(stderr_raw)
            exit_code = int(wait_result.get("StatusCode", -1))
            return SandboxResult(
                "ok" if exit_code == 0 else "error",
                stdout=stdout,
                stderr=stderr,
                exit_code=exit_code,
                duration_ms=int((time.perf_counter() - started) * 1000),
                backend="docker",
                truncated=truncated,
            )
        except ImageNotFound:
            return SandboxResult(
                "unavailable", stderr=f"镜像 {self.settings.sandbox_image} 不存在", backend="docker"
            )
        except ContainerError as exc:
            return SandboxResult(
                "error", stderr=str(exc)[:MAX_OUTPUT_BYTES], backend="docker",
                duration_ms=int((time.perf_counter() - started) * 1000),
            )
        except Exception as exc:
            logger.exception("Docker 沙箱执行失败")
            return SandboxResult(
                "error",
                stderr=f"{type(exc).__name__}: {exc}"[:MAX_OUTPUT_BYTES],
                backend="docker",
                duration_ms=int((time.perf_counter() - started) * 1000),
            )
        finally:
            if container is not None:
                # 无论成功与否都强制删除容器，避免残留进程与磁盘占用。
                try:
                    container.remove(force=True)
                except Exception:
                    pass

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
