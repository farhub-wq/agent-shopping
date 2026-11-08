"""Docker 沙箱执行器：由独立 runner 进程持有 Docker socket，harness 不接触运行时。

安全基线：默认关闭网络、只读根文件系统、/tmp 限额可写、限制 CPU/内存/PID、
禁止提权、以 65534 低权用户运行、python -I 隔离用户 site-packages。

Docker 端点：留空沿用 SDK 默认 socket 仅供本地开发；生产必须显式指向 rootless
dockerd（或受限 socket 代理），启动守卫会拒绝默认 root socket——否则 runner 一旦
被攻破就等价拿到宿主 root，前面所有容器加固都失去意义。
"""

import asyncio
import re
import time
from typing import Any

from shop_common.config import ServiceSettings, get_settings
from shop_common.logging import get_logger
from shop_common.sandbox import SandboxResult

logger = get_logger(__name__)

MAX_OUTPUT_BYTES = 8192

# 默认 root Docker socket 等于宿主 root 的 API 入口，生产环境一律禁止。
ROOT_DOCKER_ENDPOINTS = frozenset(
    {
        "",
        "/var/run/docker.sock",
        "unix:/var/run/docker.sock",
        "unix:///var/run/docker.sock",
    }
)


# 生产允许的隔离运行时：gVisor 在用户态拦截 syscall，逃逸不再直接命中宿主内核。
ISOLATED_RUNTIMES = frozenset({"runsc"})

# 只有内容寻址的镜像引用才不可变：registry 的 repo@sha256:... 或本地镜像 ID sha256:...
PINNED_IMAGE_RE = re.compile(r"^(?:[^@\s]+@sha256:[0-9a-f]{64}|sha256:[0-9a-f]{64})$")


def is_image_pinned(image: str) -> bool:
    """判断镜像是否按 digest 固定；tag（含 :latest）可被覆盖，不算固定。"""
    return bool(PINNED_IMAGE_RE.fullmatch(image.strip()))


def validate_sandbox_isolation(settings: ServiceSettings) -> None:
    """生产启动守卫：禁止 root Docker socket，并强制 gVisor 运行时。

    dev 不做限制，保证本地 Docker Desktop / DOCKER_HOST 与缺省 runc 可用；
    生产只有 rootless dockerd + runsc 同时成立，才认为隔离档位达标。
    """
    if settings.env.strip().lower() == "dev":
        return
    if settings.sandbox_docker_host.strip() in ROOT_DOCKER_ENDPOINTS:
        raise RuntimeError(
            "生产环境禁止 runner 直连默认 Docker socket（等价宿主 root），"
            "请配置 SANDBOX_DOCKER_HOST 指向 rootless dockerd 或受限代理"
        )
    if settings.sandbox_runtime.strip() not in ISOLATED_RUNTIMES:
        # 共享宿主内核意味着内核 CVE 可直接穿透沙箱，生产不接受 runc 兜底。
        raise RuntimeError(
            "生产环境必须启用 gVisor 运行时（SANDBOX_RUNTIME=runsc），"
            "共享宿主内核的逃逸面不可接受"
        )
    if not is_image_pinned(settings.sandbox_image):
        # tag 可被覆盖，镜像仓库失陷或本地重建都会静默替换执行环境。
        raise RuntimeError(
            "生产环境 SANDBOX_IMAGE 必须固定 digest（repo@sha256:... 或 sha256:...），"
            "禁止使用可被覆盖的 tag；make build-sandbox-image 会打印可用的值"
        )


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

    def _client(self):
        """构造 Docker 客户端：未显式配置端点时沿用 SDK 默认，保证本地开发可用。"""
        import docker

        endpoint = self.settings.sandbox_docker_host.strip()
        if not endpoint:
            return docker.from_env()
        return docker.DockerClient(base_url=endpoint)

    def runtime_info(self) -> dict[str, Any]:
        """探测 Docker 端点：可达性、是否 rootless，供 healthz 暴露。

        rootless 判定取自 dockerd 自身上报的 SecurityOptions，而不是客户端路径，
        这样即使把 rootless socket 挂到同名路径也不会误判。
        """
        endpoint = self.settings.sandbox_docker_host.strip() or "sdk-default"
        try:
            client = self._client()
        except Exception as exc:
            # SDK 未安装或端点参数非法：healthz 不能抛异常，按不可用上报。
            return {
                "docker_sdk": False,
                "reachable": False,
                "endpoint": endpoint,
                "error": type(exc).__name__,
            }
        try:
            info = client.info()
        except Exception as exc:
            # 守护进程不可达：旧实现对 SDK 可导入就报 ok，会掩盖真实故障。
            return {
                "docker_sdk": True,
                "reachable": False,
                "endpoint": endpoint,
                "error": type(exc).__name__,
            }
        finally:
            client.close()

        options = info.get("SecurityOptions") or []
        rootless = any("rootless" in str(option) for option in options)
        runtime = self.settings.sandbox_runtime.strip()
        # 配了 runsc 但宿主没注册该运行时，容器会直接起不来；提前在健康检查暴露。
        runtime_ok = runtime in (info.get("Runtimes") or {}) if runtime else True
        pinned = is_image_pinned(self.settings.sandbox_image)
        return {
            "docker_sdk": True,
            "reachable": True,
            "endpoint": endpoint,
            "rootless": rootless,
            "runtime": runtime or "runc",
            "runtime_ok": runtime_ok,
            "image": self.settings.sandbox_image,
            "image_pinned": pinned,
            # 非 dev 连到 root 守护进程属于隔离退化，必须让健康检查报警。
            "insecure": self.settings.env.strip().lower() != "dev" and not rootless,
            # 非 dev 用 tag 镜像等于执行环境可被静默替换，同样视为退化。
            "insecure_image": self.settings.env.strip().lower() != "dev" and not pinned,
        }

    def docker_available(self) -> bool:
        # 保留布尔快捷方法：真正 ping 守护进程，避免只检查 SDK 导入的假阳性。
        return bool(self.runtime_info().get("reachable"))

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
            from docker.errors import ContainerError, ImageNotFound

            # 显式走配置的端点（生产为 rootless socket），不再隐式使用默认 socket。
            client = self._client()
        except Exception as exc:
            return SandboxResult("unavailable", stderr=f"docker 不可达: {type(exc).__name__}", backend="docker")

        container = None
        try:
            # 安全基线：网络模式走配置（默认 none）、只读根文件系统、/tmp 限额可写、
            # 限制 CPU/内存/PID、禁止提权、以 65534 低权用户运行、python -I 隔离用户 site-packages。
            create_kwargs: dict[str, Any] = {
                "command": ["python", "-I", "-c", code],
                "network_mode": self.settings.sandbox_network,
                "mem_limit": f"{self.settings.sandbox_memory_mb}m",
                "nano_cpus": int(self.settings.sandbox_cpu * 1_000_000_000),
                "pids_limit": 64,
                "read_only": True,
                "tmpfs": {"/tmp": "rw,size=32m"},
                "security_opt": ["no-new-privileges"],
                "user": "65534:65534",
                "working_dir": "/tmp",
                # 容器 stdout 落在宿主 json-file 里，只读根文件系统管不到；
                # 单容器封顶 1MB，防止死循环 print 在超时删除前写满磁盘（SBX-02）。
                "log_config": {"Type": "json-file", "Config": {"max-size": "1m", "max-file": "1"}},
            }
            runtime = self.settings.sandbox_runtime.strip()
            if runtime:
                # 生产为 runsc（gVisor）；留空时不传，本地开发继续走守护进程默认 runc。
                create_kwargs["runtime"] = runtime
            container = client.containers.create(self.settings.sandbox_image, **create_kwargs)
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
            # 每次执行都新建客户端，用完必须关闭，避免长期运行泄漏连接。
            client.close()
