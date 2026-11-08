"""独立沙箱 runner：唯一持有 Docker socket 的服务。

部署约束：只在内网可达，不映射宿主端口、不经 gateway 代理；
调用方必须携带平台签名的身份头与 execution_id，且只允许运营/管理员触发。
"""

import re
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Header, Response
from pydantic import BaseModel, Field
from redis.asyncio import Redis
from shop_common.auth import PrincipalDep
from shop_common.config import get_settings
from shop_common.errors import ApiError, install_error_handlers
from shop_common.logging import configure_logging, get_logger
from shop_common.metrics import install_metrics
from shop_common.sandbox import SandboxResult

from .dedupe import CACHEABLE_STATUSES, ExecutionDedupe
from .runner import ConcurrencyGate, DockerRunner, validate_sandbox_isolation

logger = get_logger(__name__)

# execution_id 来自 harness 的 UUID；只允许安全字符，防止拼接进 Redis key 时注入分隔符。
EXECUTION_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,64}")

settings = get_settings()
runner = DockerRunner()
# 每个 uvicorn 进程一份并发闸门；多 worker 时总并发 = 闸门上限 × worker 数。
gate = ConcurrencyGate(settings.sandbox_max_concurrency, settings.sandbox_queue_timeout_seconds)
# 去重状态放 Redis 而不是进程内存：多 runner 必须共享同一份执行状态才能跨实例生效。
redis_client = Redis.from_url(settings.redis_url, decode_responses=False)
dedupe = ExecutionDedupe(
    redis_client,
    # 锁 TTL 覆盖单次容器执行；执行期间心跳续租，runner 崩溃后才按 TTL 自动过期。
    lock_ttl_seconds=settings.sandbox_timeout_seconds + 20,
)


@asynccontextmanager
async def lifespan(_: FastAPI):
    configure_logging("sandbox-runner")
    # 配置漂移直接拒绝启动：生产连默认 root socket 或未启用 gVisor 都等于隔离退化。
    validate_sandbox_isolation(settings)
    yield
    await redis_client.aclose()


app = FastAPI(title="sandbox-runner", version="0.1.0", lifespan=lifespan)
install_error_handlers(app, "sandbox-runner")
install_metrics(app, "sandbox-runner")


class RunRequest(BaseModel):
    # 代码长度设上限：runner 是执行边界，不能接受无界请求体。
    code: str = Field(min_length=1, max_length=65536)
    language: str = "python"
    timeout: int | None = None


@app.get("/healthz")
async def healthz(response: Response) -> dict[str, Any]:
    runtime = runner.runtime_info()
    # 不可达、隔离退化、或配置的 runsc 未注册，都按失败处理并返回 503：
    # compose healthcheck 与 harness 探测都以 HTTP 状态判死，不能只靠 status 字段。
    healthy = (
        bool(runtime.get("reachable"))
        and not runtime.get("insecure")
        and not runtime.get("insecure_image")
        and bool(runtime.get("runtime_ok", True))
    )
    if not healthy:
        response.status_code = 503
    return {
        "status": "ok" if healthy else "unavailable",
        "service": "sandbox-runner",
        "docker_sdk": runtime.get("docker_sdk", False),
        # 端点形态、是否 rootless、运行时是否注册、镜像是否 digest 固定，供运维直接定位。
        "docker": runtime,
        "image": settings.sandbox_image,
        "image_pinned": runtime.get("image_pinned", False),
        "network": settings.sandbox_network,
        "max_concurrency": gate.limit,
        "dedupe": "redis",
        "limits": {
            "cpu": settings.sandbox_cpu,
            "memory_mb": settings.sandbox_memory_mb,
            "timeout_seconds": settings.sandbox_timeout_seconds,
        },
    }


def _unavailable(reason: str) -> dict[str, Any]:
    # 未真正执行容器：不写结果缓存，让 harness 可以安全转移到其他 runner。
    return SandboxResult("unavailable", stderr=reason, backend="docker").to_dict()


@app.post("/run")
async def run_sandbox(
    payload: RunRequest,
    principal: PrincipalDep,
    execution_id: str = Header(default="", alias="X-Execution-Id"),
) -> dict[str, Any]:
    # 与 harness 的 /sandbox/run 同一权限口径：沙箱执行只对 staff 开放。
    principal.require_staff()
    exec_id = execution_id.strip()
    if not EXECUTION_ID_PATTERN.fullmatch(exec_id):
        raise ApiError("BAD_REQUEST", "X-Execution-Id 缺失或格式非法")

    try:
        cached = await dedupe.cached(exec_id)
    except Exception:
        # 去重存储不可用时拒绝执行：宁可沙箱不可用，也不能失去跨实例去重保护。
        logger.exception("读取沙箱执行缓存失败 exec=%s", exec_id)
        return _unavailable("去重存储不可用")
    if cached is not None:
        # 同一执行已完成的复用：故障转移后返回第一次真实执行的结果。
        return cached.to_dict()

    try:
        lock = dedupe.lock(exec_id)
        acquired = await lock.acquire()
    except Exception:
        logger.exception("获取沙箱执行锁失败 exec=%s", exec_id)
        return _unavailable("去重存储不可用")

    if not acquired:
        # 另一台 runner 正在执行同一份代码：短轮询结果，绝不再跑一遍。
        try:
            cached = await dedupe.wait_for_result(exec_id, settings.sandbox_dedupe_wait_seconds)
        except Exception:
            logger.exception("等待沙箱执行结果失败 exec=%s", exec_id)
            cached = None
        if cached is not None:
            return cached.to_dict()
        return _unavailable("同一执行仍在进行中")

    try:
        # 抢到锁后再查一次：上一任可能刚写完结果并释放锁，堵住这个竞态窗口。
        try:
            cached = await dedupe.cached(exec_id)
        except Exception:
            logger.exception("复核沙箱执行缓存失败 exec=%s", exec_id)
            return _unavailable("去重存储不可用")
        if cached is not None:
            return cached.to_dict()

        if not await gate.acquire():
            # 本实例并发已满：释放锁后返回繁忙，由 harness 转移到其他 runner。
            return _unavailable("runner 繁忙，请稍后重试")
        try:
            result = await runner.run(payload.code, language=payload.language, timeout=payload.timeout)
        finally:
            # 无论执行成功、失败还是异常都必须归还并发额度，否则容量会被永久泄漏。
            gate.release()

        if result.status in CACHEABLE_STATUSES:
            try:
                await dedupe.store(exec_id, result)
            except Exception:
                # 缓存失败不影响本次返回；最坏情况是后续故障转移重复执行一次。
                logger.exception("写入沙箱执行结果失败 exec=%s", exec_id)
        return result.to_dict()
    finally:
        await lock.release()
