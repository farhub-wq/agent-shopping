"""Docker 沙箱执行器：由独立 runner 进程持有 Docker socket，harness 不接触运行时。

安全基线：默认关闭网络、只读根文件系统、/tmp 限额可写、限制 CPU/内存/PID、
丢弃全部 capability、限制 /dev/shm 与 nofile、禁止提权、以 65534 低权用户运行、
python -I 隔离用户 site-packages。

Docker 端点：留空沿用 SDK 默认 socket 仅供本地开发。生产有两种隔离档位：
rootless（默认）要求显式指向 rootless dockerd，启动守卫拒绝默认 root socket；
dedicated 允许 rootful socket，但要求节点只运行沙箱自身容器，并把「节点独占」
作为执行前置——否则 runner 一旦被攻破就等价拿到宿主 root。
"""

import asyncio
import re
import time
from typing import Any

import requests
from shop_common.config import ServiceSettings, get_settings
from shop_common.logging import get_logger
from shop_common.sandbox import SandboxResult, clamp_timeout

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

# 隔离档位：rootless 是同机 rootless dockerd；dedicated 是专用节点上的 rootful dockerd。
ISOLATION_MODES = frozenset({"rootless", "dedicated"})

# 专用节点独占判定：运行中容器只有带这些角色标签的才属于沙箱栈，
# 其余任何容器都意味着节点混部，rootful socket 的风险半径不再可控。
SANDBOX_CONTAINER_LABEL = "shop.sandbox.role"
ALLOWED_NODE_ROLES = frozenset({"execution", "runner", "infra"})

# 资源硬上限：配置可以按部署规模上调，但不能无限抬高，否则单个沙箱容器就能压垮宿主。
MAX_SANDBOX_CPU = 2.0
MAX_SANDBOX_MEMORY_MB = 2048

# 只有内容寻址的镜像引用才不可变：registry 的 repo@sha256:... 或本地镜像 ID sha256:...
PINNED_IMAGE_RE = re.compile(r"^(?:[^@\s]+@sha256:[0-9a-f]{64}|sha256:[0-9a-f]{64})$")


def is_image_pinned(image: str) -> bool:
    """判断镜像是否按 digest 固定；tag（含 :latest）可被覆盖，不算固定。"""
    return bool(PINNED_IMAGE_RE.fullmatch(image.strip()))


def isolation_mode(settings: ServiceSettings) -> str:
    """规范化隔离档位；非法值由启动守卫拒绝，运行期不做静默回落。"""
    return settings.sandbox_isolation_mode.strip().lower()


def validate_sandbox_isolation(settings: ServiceSettings) -> None:
    """生产启动守卫：按隔离档位校验端点、运行时、镜像与资源上限。

    dev 不做限制，保证本地 Docker Desktop / DOCKER_HOST 与缺省 runc 可用。
    rootless 要求显式非默认端点；dedicated 允许 rootful socket，但端点必须是
    本地 unix socket（空值会回落 SDK 默认，tcp 端点等于远端 root）。
    节点独占无法只靠配置证明，由启动期与执行边界另行校验。
    """
    if settings.env.strip().lower() == "dev":
        return
    mode = isolation_mode(settings)
    if mode not in ISOLATION_MODES:
        # 拼错档位不能静默落回默认，否则隔离预期与实际部署不一致。
        raise RuntimeError(
            f"生产环境 SANDBOX_ISOLATION_MODE 必须是 "
            f"{'/'.join(sorted(ISOLATION_MODES))}，当前值 "
            f"{settings.sandbox_isolation_mode!r} 无法识别"
        )
    host = settings.sandbox_docker_host.strip()
    if mode == "rootless":
        if host in ROOT_DOCKER_ENDPOINTS:
            raise RuntimeError(
                "生产环境禁止 runner 直连默认 Docker socket（等价宿主 root），"
                "请配置 SANDBOX_DOCKER_HOST 指向 rootless dockerd；"
                "若部署在专用沙箱节点，请设 SANDBOX_ISOLATION_MODE=dedicated"
            )
    elif not host.startswith("unix://"):
        # 专用节点只接受本地 socket：空值无法证明连的哪台守护进程，tcp 等同于远端 root。
        raise RuntimeError(
            "dedicated 档位必须显式配置本地 unix socket，"
            "例如 SANDBOX_DOCKER_HOST=unix:///var/run/docker.sock"
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
    if settings.sandbox_network.strip().lower() != "none":
        # 出网等于把沙箱变成内网探测/云元数据读取/数据外带的跳板；确需出网只能走 allowlist 代理。
        raise RuntimeError(
            "生产环境 SANDBOX_NETWORK 必须为 none，禁止 bridge/host 等可出网模式"
        )
    if not 0 < settings.sandbox_cpu <= MAX_SANDBOX_CPU:
        raise RuntimeError(
            f"生产环境 SANDBOX_CPU 必须在 (0, {MAX_SANDBOX_CPU}] 内，"
            "抬高上限前先扩容专用沙箱主机"
        )
    if not 0 < settings.sandbox_memory_mb <= MAX_SANDBOX_MEMORY_MB:
        raise RuntimeError(
            f"生产环境 SANDBOX_MEMORY_MB 必须在 (0, {MAX_SANDBOX_MEMORY_MB}] 内，"
            "内存炸弹会把宿主先于业务打满"
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

    def _foreign_containers(self, client) -> list[str]:
        """列出专用节点上的非沙箱容器；只允许带沙箱角色标签的容器运行。"""
        foreign: list[str] = []
        for container in client.containers.list():
            # 标签缺失即视为业务容器：专用节点不做隐式豁免，宁可误报也不放宽。
            labels = (container.attrs.get("Config", {}).get("Labels") or {})
            role = str(labels.get(SANDBOX_CONTAINER_LABEL) or "")
            if role not in ALLOWED_NODE_ROLES:
                foreign.append(container.name)
        return foreign

    def foreign_containers(self) -> list[str]:
        """启动期入口：守护进程不可达时抛异常，由调用方决定是否阻断启动。"""
        client = self._client()
        try:
            return self._foreign_containers(client)
        finally:
            client.close()

    def _node_exclusive(self, client, mode: str) -> tuple[list[str], bool]:
        """执行/健康检查共用的节点独占判定；非 dedicated 档位不校验。"""
        if mode != "dedicated":
            return [], True
        try:
            foreign = self._foreign_containers(client)
        except Exception as exc:
            # 守护进程 info() 可达但列容器失败：无法证明节点干净，按不独占处理。
            logger.warning("专用节点独占检查失败: %s", type(exc).__name__)
            return ["<容器列表读取失败>"], False
        return foreign, not foreign

    def runtime_info(self) -> dict[str, Any]:
        """探测 Docker 端点：可达性、隔离档位、节点独占与镜像状态，供 healthz 暴露。

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
            options = info.get("SecurityOptions") or []
            rootless = any("rootless" in str(option) for option in options)
            runtime = self.settings.sandbox_runtime.strip()
            # 配了 runsc 但宿主没注册该运行时，容器会直接起不来；提前在健康检查暴露。
            runtime_ok = runtime in (info.get("Runtimes") or {}) if runtime else True
            pinned = is_image_pinned(self.settings.sandbox_image)
            mode = isolation_mode(self.settings)
            foreign, node_exclusive = self._node_exclusive(client, mode)
            non_dev = self.settings.env.strip().lower() != "dev"
            return {
                "docker_sdk": True,
                "reachable": True,
                "endpoint": endpoint,
                "rootless": rootless,
                "runtime": runtime or "runc",
                "runtime_ok": runtime_ok,
                "image": self.settings.sandbox_image,
                "image_pinned": pinned,
                # 档位声明必须与守护进程实际模式一致：dedicated 却连 rootless 守护进程，
                # 说明隔离强度低于声明，healthz 需判死。
                "isolation_mode": mode,
                "node_exclusive": node_exclusive,
                "foreign_containers": foreign,
                "mode_mismatch": mode == "dedicated" and rootless,
                # rootless 档位连到 root 守护进程属于隔离退化；dedicated 档位本来就
                # 跑 rootful，退化只体现在档位与守护进程模式不符（mode_mismatch）。
                "insecure": non_dev and not rootless and mode != "dedicated",
                # 非 dev 用 tag 镜像等于执行环境可被静默替换，同样视为退化。
                "insecure_image": non_dev and not pinned,
            }
        finally:
            client.close()

    def docker_available(self) -> bool:
        # 保留布尔快捷方法：真正 ping 守护进程，避免只检查 SDK 导入的假阳性。
        return bool(self.runtime_info().get("reachable"))

    async def run(self, code: str, *, language: str = "python", timeout: int | None = None) -> SandboxResult:
        if language != "python":
            # 只支持 python：其他语言需要额外镜像与运行时，当前不静默降级。
            return SandboxResult("error", stderr="当前沙箱仅支持 python", backend="none")
        # 配置超时是上限：直接调用 runner 的一方也不能靠超大 timeout 长期占用并发槽。
        limit = clamp_timeout(timeout, self.settings.sandbox_timeout_seconds)
        # Docker SDK 是同步阻塞调用：必须放到线程池，避免卡住事件循环拖垮健康检查。
        return await asyncio.to_thread(self._run_blocking, code, limit)

    def _run_blocking(self, code: str, timeout: int) -> SandboxResult:
        started = time.perf_counter()
        try:
            from docker.errors import ContainerError, DockerException, ImageNotFound
            from docker.types import Ulimit

            # 显式走配置的端点（生产为 rootless socket 或专用节点 rootful socket），
            # 不再隐式使用默认 socket。
            client = self._client()
        except Exception as exc:
            return SandboxResult("unavailable", stderr=f"docker 不可达: {type(exc).__name__}", backend="docker")

        container = None
        # 标记容器是否真正启动过：启动前的失败可以安全换实例重试，启动后不能。
        container_started = False
        try:
            # 专用节点的独占性必须在执行边界再校验一次：harness 不读 compose health，
            # 只要 runner 可达就会派发请求，节点混部时不能把容器交到 rootful socket 上。
            if isolation_mode(self.settings) == "dedicated":
                try:
                    foreign = self._foreign_containers(client)
                except Exception as exc:
                    return SandboxResult(
                        "unavailable",
                        stderr=f"专用节点独占检查失败: {type(exc).__name__}",
                        backend="docker",
                    )
                if foreign:
                    return SandboxResult(
                        "unavailable",
                        stderr=f"专用节点存在非沙箱容器: {','.join(foreign[:5])}",
                        backend="docker",
                    )
            # 安全基线：网络模式走配置（默认 none）、只读根文件系统、/tmp 限额可写、
            # 限制 CPU/内存/PID、禁止提权、以 65534 低权用户运行、python -I 隔离用户 site-packages。
            create_kwargs: dict[str, Any] = {
                "command": ["python", "-I", "-c", code],
                "network_mode": self.settings.sandbox_network,
                # 标签用于专用节点独占判定（忽略执行中的容器），也为后续孤儿回收铺路。
                "labels": {SANDBOX_CONTAINER_LABEL: "execution"},
                "mem_limit": f"{self.settings.sandbox_memory_mb}m",
                # 只设 mem_limit 时 Docker 默认允许同等大小的 swap，脚本可以绕过
                # 内存上限继续占用宿主 swap（实测 256MB 限制下 400MB 写入成功）；
                # 设为与 mem_limit 相同即禁用 swap，超限立即 OOM。
                "memswap_limit": f"{self.settings.sandbox_memory_mb}m",
                "nano_cpus": int(self.settings.sandbox_cpu * 1_000_000_000),
                "pids_limit": 64,
                # 显式丢弃全部 capability：即使存在提权漏洞也无法 mount/keyctl 等，
                # 不依赖镜像自身能力集（镜像换代时能力集会漂移）。
                "cap_drop": ["ALL"],
                "read_only": True,
                # /tmp 必须可写；其余路径全部只读。
                "tmpfs": {"/tmp": "rw,size=32m"},
                # /dev/shm 默认 64MB 且可写，脚本可用它放大内存占用；这里单独压到 16MB。
                "shm_size": 16 * 1024 * 1024,
                # 文件句柄上限：避免脚本用海量 socket/文件耗尽宿主 nofile 配额。
                "ulimits": [Ulimit(name="nofile", soft=1024, hard=1024)],
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
            container_started = True
            try:
                wait_result = container.wait(timeout=timeout)
            except (requests.exceptions.ReadTimeout, requests.exceptions.ConnectionError) as exc:
                # wait 超时是脚本超出时间预算，不是基础设施故障；
                # 必须映射 timeout，否则会被当成脚本失败并固化进结果缓存。
                # urllib3 2.x 可能把读超时包装成 ConnectionError，只有错误信息
                # 明确是读超时才按超时处理；其余连接故障重新抛出，走基础设施错误。
                if isinstance(exc, requests.exceptions.ConnectionError) and "read timed out" not in str(exc).lower():
                    raise
                return SandboxResult(
                    "timeout",
                    stderr=f"执行超过 {timeout}s 已终止",
                    backend="docker",
                    duration_ms=int((time.perf_counter() - started) * 1000),
                )
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
            # 镜像缺失属于实例级不可用：换一台 runner 可能已有该 digest，必须允许故障转移。
            return SandboxResult(
                "unavailable",
                stderr=f"镜像 {self.settings.sandbox_image} 不存在",
                backend="docker",
                duration_ms=int((time.perf_counter() - started) * 1000),
            )
        except ContainerError as exc:
            return SandboxResult(
                "error", stderr=str(exc)[:MAX_OUTPUT_BYTES], backend="docker",
                duration_ms=int((time.perf_counter() - started) * 1000),
            )
        except (DockerException, requests.exceptions.RequestException) as exc:
            duration_ms = int((time.perf_counter() - started) * 1000)
            if container_started:
                # 容器已启动，代码可能已经执行：既不能换实例重跑（会重复副作用），
                # 也不能混同成脚本 error，否则会污染脚本错误率并误置 untrusted_output。
                logger.exception("Docker 沙箱执行阶段失败")
                return SandboxResult(
                    "infra_error",
                    stderr=f"{type(exc).__name__}: {exc}"[:MAX_OUTPUT_BYTES],
                    backend="docker",
                    duration_ms=duration_ms,
                )
            # 启动前失败（镜像/运行时/端点）是实例级不可用，交给 harness 故障转移。
            logger.warning("Docker 沙箱不可用: %s", type(exc).__name__)
            return SandboxResult(
                "unavailable",
                stderr=f"docker 不可用: {type(exc).__name__}",
                backend="docker",
                duration_ms=duration_ms,
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
