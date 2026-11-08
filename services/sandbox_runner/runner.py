"""Docker 沙箱执行器：由独立 runner 进程持有 Docker socket，harness 不接触运行时。

安全基线：默认关闭网络、只读根文件系统、/tmp 限额可写、限制 CPU/内存/PID、
禁止提权、以 65534 低权用户运行、python -I 隔离用户 site-packages。
"""

import asyncio
import time

from shop_common.config import get_settings
from shop_common.logging import get_logger
from shop_common.sandbox import SandboxResult

logger = get_logger(__name__)

MAX_OUTPUT_BYTES = 8192


def _truncate(raw: bytes) -> tuple[str, bool]:
    # 输出上限 8KB：既限制回传给模型的上下文成本，也防止脚本用日志刷爆内存。
    if len(raw) > MAX_OUTPUT_BYTES:
        return raw[:MAX_OUTPUT_BYTES].decode("utf-8", errors="replace"), True
    return raw.decode("utf-8", errors="replace"), False


class ConcurrencyGate:
    """有界排队：超过并发上限时短暂等待，等不到就明确拒绝，避免把宿主压垮。

    沙箱容器是重资源操作，单实例并发数必须有上限；无界并发会让 Docker 守护进程
    和宿主内存先于业务被打满。
    """

    def __init__(self, limit: int, queue_timeout: float) -> None:
        self.limit = max(1, limit)
        self._semaphore = asyncio.Semaphore(self.limit)
        self._queue_timeout = max(0.0, queue_timeout)

    async def acquire(self) -> bool:
        try:
            async with asyncio.timeout(self._queue_timeout):
                await self._semaphore.acquire()
            return True
        except TimeoutError:
            return False

    def release(self) -> None:
        self._semaphore.release()


class DockerRunner:
    def __init__(self) -> None:
        self.settings = get_settings()

    def docker_available(self) -> bool:
        # 只探测 SDK 是否可导入；守护进程可达性在真正执行时由 SDK 暴露。
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
        # Docker SDK 是同步阻塞调用：必须放到线程池，避免卡住事件循环拖垮健康检查。
        return await asyncio.to_thread(self._run_blocking, code, limit)

    def _run_blocking(self, code: str, timeout: int) -> SandboxResult:
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
