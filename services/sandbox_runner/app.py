"""独立沙箱 runner：唯一持有 Docker socket 的服务。

部署约束：只在内网可达，不映射宿主端口、不经 gateway 代理；
调用方必须携带平台签名的身份头与 execution_id，且只允许运营/管理员触发。
"""

import asyncio
import hashlib
import re
from contextlib import asynccontextmanager, suppress
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, Response
from pydantic import BaseModel, Field
from redis.asyncio import Redis
from shop_common.audit import publish_audit
from shop_common.auth import Principal, principal_from_signed_headers
from shop_common.config import (
    get_settings,
    runner_security_issues,
    validate_runner_settings,
)
from shop_common.errors import ApiError, install_error_handlers
from shop_common.events import EventBus, create_redis
from shop_common.logging import configure_logging, get_logger
from shop_common.metrics import install_metrics
from shop_common.sandbox import EXECUTED_STATUSES, MAX_CODE_CHARS, SandboxResult

from .dedupe import ExecutionDedupe, owner_scope
from .runner import (
    ORPHAN_RECONCILE_INTERVAL_SECONDS,
    ConcurrencyGate,
    DockerRunner,
    isolation_mode,
    validate_sandbox_isolation,
)

logger = get_logger(__name__)

# execution_id 来自 harness 的 UUID；只允许安全字符，防止拼接进 Redis key 时注入分隔符。
EXECUTION_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,64}")

settings = get_settings()
runner = DockerRunner()
# 每个 uvicorn 进程一份并发闸门；多 worker 时总并发 = 闸门上限 × worker 数。
gate = ConcurrencyGate(settings.sandbox_max_concurrency, settings.sandbox_queue_timeout_seconds)
# 去重状态放 Redis 而不是进程内存：多 runner 必须共享同一份执行状态才能跨实例生效。
# 连接使用受限 ACL 用户：即使 runner 被攻破，凭据也只能读写沙箱键与审计流，碰不到业务数据。
redis_client = Redis.from_url(settings.resolved_sandbox_redis_url, decode_responses=False)
dedupe = ExecutionDedupe(
    redis_client,
    # 锁 TTL 覆盖单次容器执行；执行期间心跳续租，runner 崩溃后才按 TTL 自动过期。
    lock_ttl_seconds=settings.sandbox_timeout_seconds + 20,
)
# 审计走同一 Redis 流：audit 服务异步消费落库（只增不改、保留 180 天），
# 执行证据不能只留 5 分钟的结果缓存。
audit_redis: Redis | None = None
audit_bus: EventBus | None = None


async def _reconcile_orphans_loop() -> None:
    """后台兜底回收：正常执行由 finally 删除容器，这里只处理 runner 崩溃遗留。"""
    while True:
        await asyncio.sleep(ORPHAN_RECONCILE_INTERVAL_SECONDS)
        try:
            # Docker SDK 是同步阻塞调用：放线程池，避免拖慢执行与健康检查。
            await asyncio.to_thread(runner.reconcile_orphans)
        except Exception:
            logger.warning("定时孤儿容器回收失败", exc_info=True)


@asynccontextmanager
async def lifespan(_: FastAPI):
    global audit_redis, audit_bus
    configure_logging("sandbox-runner")
    # runner 只接受专用签名密钥签发的身份头，且 Redis 必须使用受限 ACL 凭据；
    # 这两项不达标等于密钥半径仍覆盖全系统，直接拒绝启动。
    validate_runner_settings(settings)
    # 配置漂移直接拒绝启动：生产连默认 root socket 或未启用 gVisor 都等于隔离退化。
    validate_sandbox_isolation(settings)
    # 专用节点的启动期尽力校验：已有非沙箱容器说明节点混部，拒绝启动；
    # 守护进程暂时不可达不算部署错误（healthz 会持续判死，避免临时故障导致崩溃循环）。
    if isolation_mode(settings) == "dedicated":
        try:
            foreign = runner.foreign_containers()
        except Exception:
            logger.warning("启动期无法校验专用节点独占性，交由 healthz 持续探测")
        else:
            if foreign:
                raise RuntimeError(f"专用沙箱节点存在非沙箱容器: {','.join(foreign[:5])}")
    # 审计只发布不消费，不需要 session_factory；独立连接避免与执行锁的 bytes 解码混用。
    audit_redis = create_redis(settings.resolved_sandbox_redis_url)
    audit_bus = EventBus(audit_redis, namespace="shop", session_factory=None)
    # 孤儿回收兜底：runner 崩溃时 finally 无法执行，遗留容器会持续占用节点资源；
    # 启动先扫一次，之后按固定间隔后台回收。回收失败只告警，不能阻断启动。
    await asyncio.to_thread(runner.reconcile_orphans)
    reconcile_task = asyncio.create_task(_reconcile_orphans_loop())
    try:
        yield
    finally:
        # 先停回收任务再关 Redis，避免关闭后仍有任务发起 Docker 调用。
        reconcile_task.cancel()
        with suppress(asyncio.CancelledError):
            await reconcile_task
        await redis_client.aclose()
        if audit_redis is not None:
            await audit_redis.aclose()


app = FastAPI(title="sandbox-runner", version="0.1.0", lifespan=lifespan)
install_error_handlers(app, "sandbox-runner")
install_metrics(app, "sandbox-runner")


async def get_runner_principal(
    x_user_id: Annotated[str | None, Header()] = None,
    x_user_role: Annotated[str | None, Header()] = None,
    x_tenant: Annotated[str | None, Header()] = None,
    x_internal_signature: Annotated[str | None, Header()] = None,
    x_request_id: Annotated[str | None, Header()] = None,
) -> Principal:
    """runner 专属鉴权：只接受平台专用密钥签名的身份头，明确不接受 Bearer JWT。

    JWT 由 gateway/identity 使用；runner 不持有 JWT 密钥，前端令牌泄露也无法直接执行代码。
    """
    return principal_from_signed_headers(
        user_id=x_user_id,
        role=x_user_role,
        tenant=x_tenant,
        signature=x_internal_signature,
        request_id=x_request_id,
        secret=settings.resolved_runner_signing_secret,
        trusted=settings.trust_identity_headers,
    )


RunnerPrincipalDep = Annotated[Principal, Depends(get_runner_principal)]


class RunRequest(BaseModel):
    # 代码长度设上限：runner 是执行边界，不能接受无界请求体。
    code: str = Field(min_length=1, max_length=MAX_CODE_CHARS)
    language: str = "python"
    # 非正数超时没有业务含义；上限由 DockerRunner 按 SANDBOX_TIMEOUT_SECONDS 收敛。
    timeout: int | None = Field(default=None, ge=1)


@app.get("/healthz")
async def healthz(response: Response) -> dict[str, Any]:
    runtime = runner.runtime_info()
    mode = isolation_mode(settings)
    # 密钥仍是开发默认值或 Redis 仍用共享凭据，都意味着 runner 的密钥半径没有收敛。
    security_issues = runner_security_issues(settings)
    # 不可达、隔离退化、档位与守护进程不符、节点混部、或配置的 runsc 未注册，
    # 任一成立都按失败处理并返回 503：compose healthcheck 与 harness 探测都以
    # HTTP 状态判死，不能只靠 status 字段。
    healthy = (
        bool(runtime.get("reachable"))
        and not runtime.get("insecure")
        and not runtime.get("insecure_image")
        and bool(runtime.get("runtime_ok", True))
        and bool(runtime.get("node_exclusive", True))
        and not runtime.get("mode_mismatch", False)
        and not security_issues
    )
    if not healthy:
        response.status_code = 503
    return {
        "status": "ok" if healthy else "unavailable",
        "service": "sandbox-runner",
        "docker_sdk": runtime.get("docker_sdk", False),
        # 端点形态、是否 rootless、运行时是否注册、镜像是否 digest 固定，供运维直接定位。
        "docker": runtime,
        "isolation": {
            "mode": mode,
            # 专用节点要求运行中容器只属于沙箱栈；rootless 档位不校验，恒为 True。
            "node_exclusive": runtime.get("node_exclusive", True),
            "foreign_containers": runtime.get("foreign_containers", []),
            "mode_mismatch": runtime.get("mode_mismatch", False),
        },
        "image": settings.sandbox_image,
        "image_pinned": runtime.get("image_pinned", False),
        "network": settings.sandbox_network,
        "security": {
            "env": settings.env,
            "secrets_configured": not security_issues,
            "issues": security_issues,
            # 鉴权口径固定为签名身份头：不接受 Bearer JWT，避免再引入一套令牌密钥。
            "auth": "signed_headers_only",
        },
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


async def publish_execution_audit(
    principal: Principal, execution_id: str, code: str, result: SandboxResult
) -> None:
    """执行证据落审计流：记录主体、镜像、输入/输出哈希与耗时。

    只存哈希不存明文代码与输出：审计需要可核对，但不该把沙箱内容变成长期可读数据。
    """
    if audit_bus is None:
        return
    output = result.stdout + result.stderr
    await publish_audit(
        audit_bus,
        actor_id=principal.user_id,
        actor_role=principal.role,
        action="sandbox.executed",
        target_type="sandbox_execution",
        target_id=execution_id,
        after={
            "image": settings.sandbox_image,
            "input_sha256": hashlib.sha256(code.encode()).hexdigest(),
            "output_sha256": hashlib.sha256(output.encode()).hexdigest(),
            "status": result.status,
            "exit_code": result.exit_code,
            "duration_ms": result.duration_ms,
            "truncated": result.truncated,
            "backend": result.backend,
        },
        tenant=principal.tenant,
    )


@app.post("/run")
async def run_sandbox(
    payload: RunRequest,
    principal: RunnerPrincipalDep,
    execution_id: str = Header(default="", alias="X-Execution-Id"),
) -> dict[str, Any]:
    # 与 harness 的 /sandbox/run 同一权限口径：沙箱执行只对 staff 开放。
    principal.require_staff()
    exec_id = execution_id.strip()
    if not EXECUTION_ID_PATTERN.fullmatch(exec_id):
        raise ApiError("BAD_REQUEST", "X-Execution-Id 缺失或格式非法")
    # 去重键绑定执行主体：别人即使拿到同一个 execution_id 也读不到本次结果。
    owner = owner_scope(principal.tenant, principal.user_id)

    try:
        cached = await dedupe.cached(owner, exec_id)
    except Exception:
        # 去重存储不可用时拒绝执行：宁可沙箱不可用，也不能失去跨实例去重保护。
        logger.exception("读取沙箱执行缓存失败 exec=%s", exec_id)
        return _unavailable("去重存储不可用")
    if cached is not None:
        # 同一执行已完成的复用：故障转移后返回第一次真实执行的结果。
        return cached.to_dict()

    try:
        lock = dedupe.lock(owner, exec_id)
        acquired = await lock.acquire()
    except Exception:
        logger.exception("获取沙箱执行锁失败 exec=%s", exec_id)
        return _unavailable("去重存储不可用")

    if not acquired:
        # 另一台 runner 正在执行同一份代码：短轮询结果，绝不再跑一遍。
        try:
            cached = await dedupe.wait_for_result(owner, exec_id, settings.sandbox_dedupe_wait_seconds)
        except Exception:
            logger.exception("等待沙箱执行结果失败 exec=%s", exec_id)
            cached = None
        if cached is not None:
            return cached.to_dict()
        return _unavailable("同一执行仍在进行中")

    try:
        # 抢到锁后再查一次：上一任可能刚写完结果并释放锁，堵住这个竞态窗口。
        try:
            cached = await dedupe.cached(owner, exec_id)
        except Exception:
            logger.exception("复核沙箱执行缓存失败 exec=%s", exec_id)
            return _unavailable("去重存储不可用")
        if cached is not None:
            return cached.to_dict()

        if not await gate.acquire():
            # 本实例并发已满：释放锁后返回繁忙，由 harness 转移到其他 runner。
            return _unavailable("runner 繁忙，请稍后重试")
        try:
            # 容器启动前先写墓碑：runner 若在执行中途崩溃，锁过期后后续请求看到墓碑
            # 就直接返回 infra_error，不会把可能已发生的执行再跑一遍。
            try:
                started = await dedupe.mark_started(owner, exec_id)
            except Exception:
                logger.exception("写入沙箱执行墓碑失败 exec=%s", exec_id)
                return _unavailable("去重存储不可用")
            if not started:
                return SandboxResult(
                    "infra_error",
                    stderr="同一执行已开始过，为避免重复执行不再重跑",
                    backend="none",
                ).to_dict()

            result = await runner.run(payload.code, language=payload.language, timeout=payload.timeout)
        finally:
            # 无论执行成功、失败还是异常都必须归还并发额度，否则容量会被永久泄漏。
            gate.release()

        if result.status == "unavailable":
            # 容器未启动就没有执行痕迹：清墓碑，允许 harness 换 runner 正常重试。
            try:
                await dedupe.clear_started(owner, exec_id)
            except Exception:
                logger.exception("清理沙箱执行墓碑失败 exec=%s", exec_id)

        if result.status in EXECUTED_STATUSES:
            # 审记与缓存都失败不影响本次返回：最坏情况是丢证据或后续重试多执行一次，
            # 不能反过来阻断业务；缓存可确保同一 execution_id 不会被换 runner 重复执行。
            try:
                await publish_execution_audit(principal, exec_id, payload.code, result)
            except Exception:
                logger.exception("沙箱执行审计投递失败 exec=%s", exec_id)
            try:
                await dedupe.store(owner, exec_id, result)
            except Exception:
                logger.exception("写入沙箱执行结果失败 exec=%s", exec_id)
        return result.to_dict()
    finally:
        await lock.release()
