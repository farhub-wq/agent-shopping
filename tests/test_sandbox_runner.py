"""sandbox-runner 服务契约测试：鉴权、执行去重、结果映射与 fail-closed 语义。"""

import hashlib

import httpx
import pytest
import requests
from docker.errors import APIError
from sandbox_runner import app as runner_app
from sandbox_runner.runner import DockerRunner, validate_sandbox_isolation
from shop_common.auth import Principal, create_token, signed_identity_headers
from shop_common.config import ServiceSettings, validate_runner_settings
from shop_common.sandbox import SandboxResult

RUNNER_SECRET = "r" * 40
INTERNAL_SECRET = "i" * 40


class _FakeLock:
    def __init__(self) -> None:
        self.released = 0

    async def acquire(self) -> bool:
        return True

    async def release(self) -> None:
        self.released += 1


class _FakeDedupe:
    def __init__(self) -> None:
        self.cached_value: SandboxResult | None = None
        self.store_calls: list[tuple[str, SandboxResult]] = []
        self.lock_obj = _FakeLock()
        self.mark_started_result = True
        self.mark_started_calls: list[str] = []
        self.clear_started_calls: list[str] = []

    async def cached(self, owner: str, execution_id: str) -> SandboxResult | None:
        return self.cached_value

    async def store(self, owner: str, execution_id: str, result: SandboxResult) -> None:
        self.store_calls.append((execution_id, result))

    async def mark_started(self, owner: str, execution_id: str) -> bool:
        self.mark_started_calls.append(execution_id)
        return self.mark_started_result

    async def clear_started(self, owner: str, execution_id: str) -> None:
        self.clear_started_calls.append(execution_id)

    def lock(self, owner: str, execution_id: str) -> _FakeLock:
        return self.lock_obj

    async def wait_for_result(
        self, owner: str, execution_id: str, wait_seconds: float
    ) -> SandboxResult | None:
        return None


@pytest.fixture(autouse=True)
def fake_dedupe(monkeypatch) -> _FakeDedupe:
    # 单测不连真实 Redis：用内存假实现验证去重流程，Redis 层面的读写在 dedupe 单测覆盖。
    fake = _FakeDedupe()
    monkeypatch.setattr(runner_app, "dedupe", fake)
    return fake


@pytest.fixture(autouse=True)
def runner_settings(monkeypatch):
    # runner 默认配置只够本地演示；测试固定为「信任签名头 + 专用密钥」的真实口径。
    monkeypatch.setattr(
        runner_app,
        "settings",
        ServiceSettings(
            _env_file=None,
            env="dev",
            trust_identity_headers=True,
            runner_signing_secret=RUNNER_SECRET,
            internal_signing_secret=INTERNAL_SECRET,
        ),
    )


def _client() -> httpx.AsyncClient:
    # ASGITransport 直接调用 FastAPI 应用，不需要起真实端口。
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=runner_app.app),
        base_url="http://runner.test",
    )


def _headers(role: str, execution_id: str = "exec-1", user: str = "u-1") -> dict[str, str]:
    # runner 只接受专用签名密钥签发的身份头，不再接受 Bearer JWT。
    return {
        **signed_identity_headers(user, role, "acme", secret=RUNNER_SECRET),
        "X-Execution-Id": execution_id,
    }


@pytest.mark.asyncio
async def test_run_rejects_non_staff(monkeypatch):
    called: list[bool] = []

    async def fake_run(code: str, *, language: str = "python", timeout: int | None = None):
        called.append(True)
        return SandboxResult("ok", stdout="42", backend="docker")

    monkeypatch.setattr(runner_app.runner, "run", fake_run)

    async with _client() as client:
        response = await client.post("/run", json={"code": "print(1)"}, headers=_headers("customer"))

    # 客户角色即使能进内网也不允许执行代码；权限由 runner 再做一次校验。
    assert response.status_code == 403
    assert called == []


@pytest.mark.asyncio
async def test_run_rejects_bearer_jwt(monkeypatch):
    called: list[bool] = []

    async def fake_run(code: str, *, language: str = "python", timeout: int | None = None):
        called.append(True)
        return SandboxResult("ok", stdout="42", backend="docker")

    monkeypatch.setattr(runner_app.runner, "run", fake_run)

    async with _client() as client:
        response = await client.post(
            "/run",
            json={"code": "print(1)"},
            headers={
                "Authorization": f"Bearer {create_token(user_id='u-1', role='operator')}",
                "X-Execution-Id": "exec-1",
            },
        )

    # runner 不再接受 Bearer JWT：前端令牌不能替代 runner 专用签名，避免多一套可伪造的身份来源。
    assert response.status_code == 403
    assert called == []


@pytest.mark.asyncio
async def test_run_rejects_internal_signing_secret(monkeypatch):
    called: list[bool] = []

    async def fake_run(code: str, *, language: str = "python", timeout: int | None = None):
        called.append(True)
        return SandboxResult("ok", stdout="42", backend="docker")

    monkeypatch.setattr(runner_app.runner, "run", fake_run)

    # 用业务内网密钥签名：runner 只认自己的专用密钥，泄露内网密钥也不等于能执行代码。
    headers = {
        **signed_identity_headers("u-1", "operator", "acme", secret=INTERNAL_SECRET),
        "X-Execution-Id": "exec-1",
    }
    async with _client() as client:
        response = await client.post("/run", json={"code": "print(1)"}, headers=headers)

    assert response.status_code == 401
    assert called == []


@pytest.mark.asyncio
async def test_run_requires_execution_id(monkeypatch):
    called: list[bool] = []

    async def fake_run(code: str, *, language: str = "python", timeout: int | None = None):
        called.append(True)
        return SandboxResult("ok", stdout="42", backend="docker")

    monkeypatch.setattr(runner_app.runner, "run", fake_run)

    async with _client() as client:
        headers = _headers("operator")
        headers.pop("X-Execution-Id")
        response = await client.post("/run", json={"code": "print(1)"}, headers=headers)

    # 没有 execution_id 就无法做跨实例去重，必须拒绝而不是退化成可重复执行。
    assert response.status_code == 400
    assert called == []


@pytest.mark.asyncio
async def test_run_returns_execution_result(monkeypatch, fake_dedupe):
    seen: dict[str, object] = {}

    async def fake_run(code: str, *, language: str = "python", timeout: int | None = None):
        seen.update({"code": code, "language": language, "timeout": timeout})
        return SandboxResult("ok", stdout="42", backend="docker", exit_code=0)

    monkeypatch.setattr(runner_app.runner, "run", fake_run)

    async with _client() as client:
        response = await client.post(
            "/run",
            json={"code": "print(42)", "language": "python", "timeout": 5},
            headers=_headers("operator"),
        )

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["stdout"] == "42"
    assert seen == {"code": "print(42)", "language": "python", "timeout": 5}
    # 真正执行完成的结果必须写入共享缓存，供故障转移复用。
    assert len(fake_dedupe.store_calls) == 1
    assert fake_dedupe.lock_obj.released == 1


@pytest.mark.asyncio
async def test_run_reuses_cached_result_without_executing(monkeypatch, fake_dedupe):
    fake_dedupe.cached_value = SandboxResult("ok", stdout="cached", backend="docker")
    called: list[bool] = []

    async def fake_run(code: str, *, language: str = "python", timeout: int | None = None):
        called.append(True)
        return SandboxResult("ok", stdout="fresh", backend="docker")

    monkeypatch.setattr(runner_app.runner, "run", fake_run)

    async with _client() as client:
        response = await client.post("/run", json={"code": "print(1)"}, headers=_headers("operator"))

    # 命中缓存直接复用第一次的真实结果，绝不再执行一遍。
    assert response.status_code == 200
    assert response.json()["stdout"] == "cached"
    assert called == []


@pytest.mark.asyncio
async def test_run_reports_docker_unavailable(monkeypatch, fake_dedupe):
    async def fake_run(code: str, *, language: str = "python", timeout: int | None = None):
        return SandboxResult("unavailable", stderr="docker 不可达", backend="docker")

    monkeypatch.setattr(runner_app.runner, "run", fake_run)

    async with _client() as client:
        response = await client.post("/run", json={"code": "print(1)"}, headers=_headers("operator"))

    # runner 自身可达但 Docker 不可用时，如实返回 unavailable，由调用方 fail-closed。
    assert response.status_code == 200
    assert response.json()["status"] == "unavailable"
    assert response.json()["backend"] == "docker"
    # 没真正执行的结果不能进缓存，否则其他健康 runner 会被假失败挡住。
    assert fake_dedupe.store_calls == []
    assert fake_dedupe.lock_obj.released == 1


@pytest.mark.asyncio
async def test_run_reports_busy_when_queue_full(monkeypatch, fake_dedupe):
    async def no_slot() -> bool:
        return False

    called: list[bool] = []

    async def fake_run(code: str, *, language: str = "python", timeout: int | None = None):
        called.append(True)
        return SandboxResult("ok", stdout="42", backend="docker")

    monkeypatch.setattr(runner_app.gate, "acquire", no_slot)
    monkeypatch.setattr(runner_app.runner, "run", fake_run)

    async with _client() as client:
        response = await client.post("/run", json={"code": "print(1)"}, headers=_headers("operator"))

    # 并发打满时不执行代码，返回 unavailable 让 harness 转移到其他 runner。
    assert response.status_code == 200
    assert response.json()["status"] == "unavailable"
    assert "繁忙" in response.json()["stderr"]
    assert called == []
    assert fake_dedupe.lock_obj.released == 1


@pytest.mark.asyncio
async def test_run_waits_for_inflight_execution(monkeypatch, fake_dedupe):
    async def not_acquired() -> bool:
        return False

    called: list[bool] = []

    async def fake_run(code: str, *, language: str = "python", timeout: int | None = None):
        called.append(True)
        return SandboxResult("ok", stdout="42", backend="docker")

    async def no_result(execution_id: str, wait_seconds: float) -> SandboxResult | None:
        return None

    monkeypatch.setattr(fake_dedupe.lock_obj, "acquire", not_acquired)
    monkeypatch.setattr(fake_dedupe, "wait_for_result", no_result)
    monkeypatch.setattr(runner_app.runner, "run", fake_run)

    async with _client() as client:
        response = await client.post("/run", json={"code": "print(1)"}, headers=_headers("operator"))

    # 另一台 runner 正在执行时，拿不到结果就报「进行中」，绝不重复执行。
    assert response.status_code == 200
    assert response.json()["status"] == "unavailable"
    assert "进行中" in response.json()["stderr"]
    assert called == []


@pytest.mark.asyncio
async def test_healthz_exposes_runtime_state(monkeypatch):
    monkeypatch.setattr(
        runner_app.runner,
        "runtime_info",
        lambda: {
            "docker_sdk": True,
            "reachable": True,
            "endpoint": "unix:///run/rootless/docker.sock",
            "rootless": True,
            "runtime": "runsc",
            "runtime_ok": True,
            "image": "sha256:" + "c" * 64,
            "image_pinned": True,
            "insecure": False,
        },
    )

    async with _client() as client:
        response = await client.get("/healthz")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["docker_sdk"] is True
    # rootless 端点信息必须透出，供运维判断隔离档位是否退化
    assert response.json()["docker"]["rootless"] is True
    assert response.json()["image_pinned"] is True
    assert response.json()["max_concurrency"] >= 1


@pytest.mark.asyncio
async def test_healthz_reports_insecure_docker_endpoint(monkeypatch):
    monkeypatch.setattr(
        runner_app.runner,
        "runtime_info",
        lambda: {
            "docker_sdk": True,
            "reachable": True,
            "endpoint": "unix:///var/run/docker.sock",
            "rootless": False,
            "runtime": "runc",
            "runtime_ok": True,
            "insecure": True,
        },
    )

    async with _client() as client:
        response = await client.get("/healthz")

    # 非 dev 连到 root 守护进程等于隔离退化，必须用 503 让探测直接判死
    assert response.status_code == 503
    assert response.json()["status"] == "unavailable"


@pytest.mark.asyncio
async def test_healthz_returns_503_when_image_not_pinned(monkeypatch):
    monkeypatch.setattr(
        runner_app.runner,
        "runtime_info",
        lambda: {
            "docker_sdk": True,
            "reachable": True,
            "endpoint": "unix:///run/rootless/docker.sock",
            "rootless": True,
            "runtime": "runsc",
            "runtime_ok": True,
            "image": "shop-agent-sandbox:latest",
            "image_pinned": False,
            "insecure": False,
            "insecure_image": True,
        },
    )

    async with _client() as client:
        response = await client.get("/healthz")

    # 生产用 tag 镜像等于执行环境可被静默替换
    assert response.status_code == 503
    assert response.json()["status"] == "unavailable"
    assert response.json()["image_pinned"] is False


@pytest.mark.asyncio
async def test_healthz_returns_503_when_docker_unreachable(monkeypatch):
    monkeypatch.setattr(
        runner_app.runner,
        "runtime_info",
        lambda: {
            "docker_sdk": True,
            "reachable": False,
            "endpoint": "sdk-default",
            "error": "DockerException",
        },
    )

    async with _client() as client:
        response = await client.get("/healthz")

    assert response.status_code == 503
    assert response.json()["status"] == "unavailable"


@pytest.mark.asyncio
async def test_healthz_returns_503_when_runtime_not_registered(monkeypatch):
    monkeypatch.setattr(
        runner_app.runner,
        "runtime_info",
        lambda: {
            "docker_sdk": True,
            "reachable": True,
            "endpoint": "unix:///run/rootless/docker.sock",
            "rootless": True,
            "runtime": "runsc",
            "runtime_ok": False,
            "insecure": False,
        },
    )

    async with _client() as client:
        response = await client.get("/healthz")

    # 配了 runsc 但宿主没注册，执行必然失败，健康检查不能报 ok
    assert response.status_code == 503
    assert response.json()["status"] == "unavailable"


@pytest.mark.asyncio
async def test_run_scopes_cached_result_to_principal(monkeypatch, fake_dedupe):
    stored: dict[tuple[str, str], SandboxResult] = {}
    executed: list[str] = []

    async def fake_cached(owner: str, execution_id: str) -> SandboxResult | None:
        return stored.get((owner, execution_id))

    async def fake_store(owner: str, execution_id: str, result: SandboxResult) -> None:
        stored[(owner, execution_id)] = result

    async def fake_run(code: str, *, language: str = "python", timeout: int | None = None):
        executed.append(code)
        return SandboxResult("ok", stdout="secret", backend="docker")

    monkeypatch.setattr(fake_dedupe, "cached", fake_cached)
    monkeypatch.setattr(fake_dedupe, "store", fake_store)
    monkeypatch.setattr(runner_app.runner, "run", fake_run)

    async with _client() as client:
        await client.post("/run", json={"code": "print(1)"}, headers=_headers("operator", user="u-1"))
        # 另一个 staff 拿到同一个 execution_id：键绑定主体，必须重新执行而不是读到他人结果
        other = await client.post(
            "/run", json={"code": "print(1)"}, headers=_headers("operator", user="u-2")
        )
        repeat = await client.post(
            "/run", json={"code": "print(1)"}, headers=_headers("operator", user="u-1")
        )

    assert other.json()["stdout"] == "secret"
    assert executed == ["print(1)", "print(1)"]
    # 同一主体重复请求命中自己的缓存，不再执行
    assert repeat.json()["stdout"] == "secret"
    assert len(stored) == 2


@pytest.mark.asyncio
async def test_execution_audit_records_hashes(monkeypatch):
    captured: dict[str, object] = {}

    class _Bus:
        async def publish(self, topic, event_type, payload, *, tenant="default"):
            captured.update(
                {"topic": topic, "type": event_type, "payload": payload, "tenant": tenant}
            )

    monkeypatch.setattr(runner_app, "audit_bus", _Bus())
    monkeypatch.setattr(
        runner_app,
        "settings",
        ServiceSettings(_env_file=None, env="dev", sandbox_image="sha256:" + "a" * 64),
    )

    await runner_app.publish_execution_audit(
        Principal("u-1", "operator", "acme"),
        "exec-1",
        "print(1)",
        SandboxResult("ok", stdout="1", backend="docker", duration_ms=12),
    )

    payload = captured["payload"]
    # 审计只落哈希：既保留可核对性，也不把代码与输出变成长期可读数据
    assert captured["type"] == "audit.sandbox.executed"
    assert captured["tenant"] == "acme"
    assert payload["actor_id"] == "u-1"
    assert payload["target_id"] == "exec-1"
    after = payload["after"]
    assert after["input_sha256"] == hashlib.sha256(b"print(1)").hexdigest()
    assert after["output_sha256"] == hashlib.sha256(b"1").hexdigest()
    assert after["image"] == "sha256:" + "a" * 64


@pytest.mark.asyncio
async def test_run_audits_real_execution(monkeypatch, fake_dedupe):
    calls: list[tuple[str, str, str]] = []

    async def fake_audit(principal, execution_id: str, code: str, result: SandboxResult) -> None:
        calls.append((principal.user_id, execution_id, code))

    async def fake_run(code: str, *, language: str = "python", timeout: int | None = None):
        return SandboxResult("ok", stdout="42", backend="docker")

    monkeypatch.setattr(runner_app, "publish_execution_audit", fake_audit)
    monkeypatch.setattr(runner_app.runner, "run", fake_run)

    async with _client() as client:
        await client.post("/run", json={"code": "print(1)"}, headers=_headers("operator"))

    # 真正跑过容器的执行必须留审计记录，且归属到发起该次执行的主体
    assert calls == [("u-1", "exec-1", "print(1)")]


@pytest.mark.asyncio
async def test_run_audits_infra_error_and_caches_it(monkeypatch, fake_dedupe):
    audited: list[SandboxResult] = []

    async def fake_audit(principal, execution_id: str, code: str, result: SandboxResult) -> None:
        audited.append(result)

    async def fake_run(code: str, *, language: str = "python", timeout: int | None = None):
        return SandboxResult("infra_error", stderr="APIError: daemon down", backend="docker")

    monkeypatch.setattr(runner_app, "publish_execution_audit", fake_audit)
    monkeypatch.setattr(runner_app.runner, "run", fake_run)

    async with _client() as client:
        response = await client.post(
            "/run", json={"code": "print(1)"}, headers=_headers("operator")
        )

    # 已发生执行尝试要留审计证据；同时写缓存，保证同一 execution_id 重试不会换 runner 重复执行。
    assert response.json()["status"] == "infra_error"
    assert len(audited) == 1
    assert [(exec_id, r.status) for exec_id, r in fake_dedupe.store_calls] == [
        ("exec-1", "infra_error")
    ]
    # 已启动过的执行不能清墓碑，否则崩溃窗口内仍可能被重跑。
    assert fake_dedupe.clear_started_calls == []


@pytest.mark.asyncio
async def test_run_skips_execution_when_attempt_tombstone_exists(monkeypatch, fake_dedupe):
    called: list[bool] = []

    async def fake_run(code: str, *, language: str = "python", timeout: int | None = None):
        called.append(True)
        return SandboxResult("ok", stdout="42", backend="docker")

    monkeypatch.setattr(runner_app.runner, "run", fake_run)
    fake_dedupe.mark_started_result = False

    async with _client() as client:
        response = await client.post(
            "/run", json={"code": "print(1)"}, headers=_headers("operator")
        )

    # 前一次尝试可能已经执行过：直接返回 infra_error，绝不能再开容器。
    assert response.json()["status"] == "infra_error"
    assert called == []
    assert fake_dedupe.store_calls == []


@pytest.mark.asyncio
async def test_run_clears_tombstone_when_runner_did_not_execute(monkeypatch, fake_dedupe):
    async def fake_run(code: str, *, language: str = "python", timeout: int | None = None):
        return SandboxResult("unavailable", stderr="docker down", backend="docker")

    monkeypatch.setattr(runner_app.runner, "run", fake_run)

    async with _client() as client:
        response = await client.post(
            "/run", json={"code": "print(1)"}, headers=_headers("operator")
        )

    # 容器没有启动就没有执行痕迹：清墓碑，允许 harness 正常故障转移。
    assert response.json()["status"] == "unavailable"
    assert fake_dedupe.clear_started_calls == ["exec-1"]
    assert fake_dedupe.store_calls == []


def test_prod_rejects_default_docker_socket():
    # 生产未显式配置端点：隐式落到默认 root socket，必须拒绝启动
    with pytest.raises(RuntimeError):
        validate_sandbox_isolation(
            ServiceSettings(env="prod", sandbox_docker_host="", sandbox_runtime="runsc")
        )


def test_prod_rejects_explicit_root_socket():
    with pytest.raises(RuntimeError):
        validate_sandbox_isolation(
            ServiceSettings(
                env="prod",
                sandbox_docker_host="unix:///var/run/docker.sock",
                sandbox_runtime="runsc",
            )
        )


def test_prod_rejects_missing_gvisor_runtime():
    # rootless 只解决「被攻破不等于宿主 root」，共享内核 CVE 面仍需 gVisor 兜底
    with pytest.raises(RuntimeError):
        validate_sandbox_isolation(
            ServiceSettings(
                env="prod",
                sandbox_docker_host="unix:///run/rootless/docker.sock",
                sandbox_runtime="",
            )
        )


def test_prod_accepts_rootless_with_gvisor():
    # rootless dockerd + runsc + digest 镜像三者同时成立，才认为生产隔离档位达标
    validate_sandbox_isolation(
        ServiceSettings(
            env="prod",
            sandbox_docker_host="unix:///run/rootless/docker.sock",
            sandbox_runtime="runsc",
            sandbox_image="shop-agent-sandbox@sha256:" + "a" * 64,
        )
    )


def test_prod_accepts_local_image_id():
    # 本地构建的镜像用镜像 ID 固定内容，同样属于内容寻址
    validate_sandbox_isolation(
        ServiceSettings(
            env="prod",
            sandbox_docker_host="unix:///run/rootless/docker.sock",
            sandbox_runtime="runsc",
            sandbox_image="sha256:" + "b" * 64,
        )
    )


def test_prod_rejects_tagged_image():
    # tag（含 :latest）可被覆盖，等于执行环境可被静默替换
    with pytest.raises(RuntimeError):
        validate_sandbox_isolation(
            ServiceSettings(
                env="prod",
                sandbox_docker_host="unix:///run/rootless/docker.sock",
                sandbox_runtime="runsc",
                sandbox_image="shop-agent-sandbox:latest",
            )
        )


def test_dev_allows_default_socket_and_runtime():
    # 本地开发保留 Docker Desktop / DOCKER_HOST 与缺省 runc 的行为
    validate_sandbox_isolation(
        ServiceSettings(env="dev", sandbox_docker_host="", sandbox_runtime="")
    )


class _FakeContainer:
    def __init__(self) -> None:
        self.wait_timeout: int | None = None
        self.removed = False

    def start(self) -> None:
        pass

    def wait(self, timeout: int | None = None) -> dict[str, int]:
        self.wait_timeout = timeout
        return {"StatusCode": 0}

    def logs(self, stdout: bool = True, stderr: bool = False) -> bytes:
        return b"ok" if stdout and not stderr else b""

    def remove(self, force: bool = False) -> None:
        self.removed = True


class _FakeContainerRef:
    """专用节点独占检查用：只需要 name 与容器标签。"""

    def __init__(self, name: str, labels: dict[str, str] | None) -> None:
        self.name = name
        self.attrs = {"Config": {"Labels": labels}}


class _FakeContainers:
    def __init__(self, container_factory=_FakeContainer) -> None:
        self.create_kwargs: dict[str, object] = {}
        self.last_container = None
        self.listed: list[_FakeContainerRef] = []
        self._container_factory = container_factory

    def create(self, image: str, **kwargs: object) -> _FakeContainer:
        self.create_kwargs = {"image": image, **kwargs}
        self.last_container = self._container_factory()
        return self.last_container

    def list(self) -> list[_FakeContainerRef]:
        return self.listed


class _FakeDockerClient:
    def __init__(self, container_factory=_FakeContainer) -> None:
        self.containers = _FakeContainers(container_factory)

    def close(self) -> None:
        pass


def _runner_with(settings: ServiceSettings, client: _FakeDockerClient, monkeypatch) -> DockerRunner:
    runner = DockerRunner()
    monkeypatch.setattr(runner, "settings", settings)
    monkeypatch.setattr(runner, "_client", lambda: client)
    return runner


def test_run_passes_gvisor_runtime_to_container(monkeypatch):
    client = _FakeDockerClient()
    runner = _runner_with(ServiceSettings(env="dev", sandbox_runtime="runsc"), client, monkeypatch)

    result = runner._run_blocking("print(1)", 5)

    assert result.status == "ok"
    # 生产必须真正以 runsc 创建容器，而不是只在校验里写配置
    assert client.containers.create_kwargs["runtime"] == "runsc"


def test_run_omits_runtime_when_not_configured(monkeypatch):
    client = _FakeDockerClient()
    runner = _runner_with(ServiceSettings(env="dev", sandbox_runtime=""), client, monkeypatch)

    runner._run_blocking("print(1)", 5)

    # 留空时不能传空 runtime，否则本地默认 runc 会被覆盖成非法值
    assert "runtime" not in client.containers.create_kwargs


def test_run_caps_container_log_size(monkeypatch):
    client = _FakeDockerClient()
    runner = _runner_with(ServiceSettings(env="dev"), client, monkeypatch)

    runner._run_blocking("print(1)", 5)

    log_config = client.containers.create_kwargs["log_config"]
    assert isinstance(log_config, dict)
    # stdout 落在宿主 json-file，只读根文件系统挡不住刷屏，必须单容器封顶
    assert log_config["Config"] == {"max-size": "1m", "max-file": "1"}


def test_run_applies_container_hardening(monkeypatch):
    client = _FakeDockerClient()
    runner = _runner_with(ServiceSettings(_env_file=None, env="dev"), client, monkeypatch)

    runner._run_blocking("print(1)", 5)

    kwargs = client.containers.create_kwargs
    # 显式丢弃全部 capability，不依赖镜像默认能力集
    assert kwargs["cap_drop"] == ["ALL"]
    # /dev/shm 默认 64MB，必须单独压下；nofile 防止句柄耗尽宿主配额
    assert kwargs["shm_size"] == 16 * 1024 * 1024
    ulimits = kwargs["ulimits"]
    assert [(u.name, u.soft, u.hard) for u in ulimits] == [("nofile", 1024, 1024)]
    # 不限制 swap 时脚本可在 mem_limit 之外继续占用宿主内存，必须显式禁用
    assert kwargs["memswap_limit"] == kwargs["mem_limit"]


class _WaitTimeoutContainer(_FakeContainer):
    def wait(self, timeout: int | None = None) -> dict[str, int]:
        # docker-py 在 wait 超时抛 requests.ReadTimeout，而不是 docker 自己的异常类型
        raise requests.exceptions.ReadTimeout("read timed out")


class _WaitWrappedTimeoutContainer(_FakeContainer):
    def wait(self, timeout: int | None = None) -> dict[str, int]:
        # urllib3 2.x 会把读超时包装成 ConnectionError，错误文本仍带 read timed out
        raise requests.exceptions.ConnectionError("Read timed out.")


class _WaitConnectionBrokenContainer(_FakeContainer):
    def wait(self, timeout: int | None = None) -> dict[str, int]:
        raise requests.exceptions.ConnectionError("Connection refused")


class _StartBrokenContainer(_FakeContainer):
    def start(self) -> None:
        raise APIError("runtime runsc not found")


class _WaitBrokenContainer(_FakeContainer):
    def wait(self, timeout: int | None = None) -> dict[str, int]:
        raise APIError("daemon down")


def test_wait_timeout_maps_to_timeout(monkeypatch):
    client = _FakeDockerClient(_WaitTimeoutContainer)
    runner = _runner_with(ServiceSettings(_env_file=None, env="dev"), client, monkeypatch)

    result = runner._run_blocking("print(1)", 5)

    # wait 超时是脚本本身的执行结果：不能被误判成 error，也不能被当基础设施故障转移
    assert result.status == "timeout"
    assert "5s" in result.stderr
    # 超时后仍然必须强制回收容器
    assert client.containers.last_container.removed is True


def test_wait_wrapped_read_timeout_maps_to_timeout(monkeypatch):
    client = _FakeDockerClient(_WaitWrappedTimeoutContainer)
    runner = _runner_with(ServiceSettings(_env_file=None, env="dev"), client, monkeypatch)

    result = runner._run_blocking("print(1)", 5)

    # 部署机实测：urllib3 2.x 把读超时包成 ConnectionError，仍必须判成 timeout
    assert result.status == "timeout"
    assert client.containers.last_container.removed is True


def test_wait_connection_error_maps_to_infra_error(monkeypatch):
    client = _FakeDockerClient(_WaitConnectionBrokenContainer)
    runner = _runner_with(ServiceSettings(_env_file=None, env="dev"), client, monkeypatch)

    result = runner._run_blocking("print(1)", 5)

    # 非读超时的连接故障是基础设施问题，不能被脚本结果缓存固化
    assert result.status == "infra_error"


def test_docker_failure_before_start_maps_to_unavailable(monkeypatch):
    client = _FakeDockerClient(_StartBrokenContainer)
    runner = _runner_with(ServiceSettings(_env_file=None, env="dev"), client, monkeypatch)

    result = runner._run_blocking("print(1)", 5)

    # 容器未启动就没有副作用，属于实例级不可用，harness 可以安全换 runner
    assert result.status == "unavailable"


def test_docker_failure_after_start_maps_to_infra_error(monkeypatch):
    client = _FakeDockerClient(_WaitBrokenContainer)
    runner = _runner_with(ServiceSettings(_env_file=None, env="dev"), client, monkeypatch)

    result = runner._run_blocking("print(1)", 5)

    # 启动后的失败可能已经执行了代码：既不能返回 unavailable（会重复执行），
    # 也不能返回 error（会污染脚本错误率并误置 untrusted_output）。
    assert result.status == "infra_error"


@pytest.mark.asyncio
async def test_run_clamps_timeout_to_configured_max(monkeypatch):
    client = _FakeDockerClient()
    runner = _runner_with(
        ServiceSettings(_env_file=None, env="dev", sandbox_timeout_seconds=20), client, monkeypatch
    )

    await runner.run("print(1)", timeout=3600)

    # 配置值是上限：超大 timeout 不能把并发槽长期占住
    assert client.containers.last_container.wait_timeout == 20


@pytest.mark.asyncio
async def test_run_rejects_non_positive_timeout(monkeypatch):
    called: list[bool] = []

    async def fake_run(code: str, *, language: str = "python", timeout: int | None = None):
        called.append(True)
        return SandboxResult("ok", stdout="42", backend="docker")

    monkeypatch.setattr(runner_app.runner, "run", fake_run)

    async with _client() as client:
        response = await client.post(
            "/run", json={"code": "print(1)", "timeout": 0}, headers=_headers("operator")
        )

    # 0/负数超时在入口直接拒绝，不进入执行与缓存流程
    assert response.status_code == 422
    assert called == []


def _prod_sandbox(**overrides) -> ServiceSettings:
    values = {
        "env": "prod",
        "sandbox_docker_host": "unix:///run/rootless/docker.sock",
        "sandbox_runtime": "runsc",
        "sandbox_image": "sha256:" + "a" * 64,
    }
    values.update(overrides)
    return ServiceSettings(_env_file=None, **values)


def test_prod_rejects_network_bridge():
    # 出网等于给沙箱开内网探测与数据外带的通道，生产不允许改回 bridge/host
    with pytest.raises(RuntimeError):
        validate_sandbox_isolation(_prod_sandbox(sandbox_network="bridge"))


def test_prod_rejects_excessive_cpu():
    with pytest.raises(RuntimeError):
        validate_sandbox_isolation(_prod_sandbox(sandbox_cpu=8.0))


def test_prod_rejects_excessive_memory():
    with pytest.raises(RuntimeError):
        validate_sandbox_isolation(_prod_sandbox(sandbox_memory_mb=16384))


def test_dev_allows_relaxed_sandbox_params():
    # 本地开发需要临时放宽网络/资源参数，守卫只在非 dev 生效
    validate_sandbox_isolation(
        ServiceSettings(
            _env_file=None,
            env="dev",
            sandbox_network="bridge",
            sandbox_cpu=8.0,
            sandbox_memory_mb=16384,
        )
    )


@pytest.mark.asyncio
async def test_healthz_returns_503_when_secrets_are_dev_defaults(monkeypatch):
    monkeypatch.setattr(
        runner_app,
        "settings",
        ServiceSettings(_env_file=None, env="prod", trust_identity_headers=True),
    )
    monkeypatch.setattr(
        runner_app.runner,
        "runtime_info",
        lambda: {
            "docker_sdk": True,
            "reachable": True,
            "endpoint": "unix:///run/rootless/docker.sock",
            "rootless": True,
            "runtime": "runsc",
            "runtime_ok": True,
            "image": "sha256:" + "a" * 64,
            "image_pinned": True,
            "insecure": False,
        },
    )

    async with _client() as client:
        response = await client.get("/healthz")

    # 隔离与镜像都达标，但 runner 专用密钥未注入：身份可伪造，必须判死
    assert response.status_code == 503
    assert response.json()["security"]["secrets_configured"] is False
    assert "RUNNER_SIGNING_SECRET" in response.json()["security"]["issues"][0]


def _prod_runner(**overrides) -> ServiceSettings:
    values = {
        "env": "prod",
        "trust_identity_headers": True,
        # 两个密钥必须不同：runner 密钥独立于网关内网密钥
        "runner_signing_secret": RUNNER_SECRET,
        "internal_signing_secret": INTERNAL_SECRET,
        # 受限 ACL 用户：只能读写沙箱键与审计流
        "sandbox_redis_url": "redis://sandbox-runner:" + "p" * 32 + "@redis:6379/0",
        "redis_url": "redis://redis:6379/0",
    }
    values.update(overrides)
    return ServiceSettings(_env_file=None, **values)


def test_prod_runner_rejects_reused_internal_secret():
    # 与网关共用一把钥匙等于没有收敛半径：泄露内网密钥就能执行代码
    with pytest.raises(RuntimeError):
        validate_runner_settings(_prod_runner(runner_signing_secret=INTERNAL_SECRET))


def test_prod_runner_rejects_shared_redis_url():
    # 回落业务共享 REDIS_URL 时 runner 凭据能读写全部业务数据
    with pytest.raises(RuntimeError):
        validate_runner_settings(_prod_runner(sandbox_redis_url=""))


def test_prod_runner_rejects_default_redis_user():
    with pytest.raises(RuntimeError):
        validate_runner_settings(
            _prod_runner(sandbox_redis_url="redis://default:" + "p" * 32 + "@redis:6379/0")
        )


def test_prod_runner_rejects_whitespace_redis_password():
    # ACL 文件按空格分隔字段，含空白的密码会让受限用户加载失败，宁可启动期就拒绝。
    with pytest.raises(RuntimeError):
        validate_runner_settings(
            _prod_runner(
                sandbox_redis_url="redis://sandbox-runner:"
                + "p" * 16
                + "%20"
                + "p" * 8
                + "@redis:6379/0"
            )
        )


def test_prod_runner_rejects_dev_sandbox_redis_password():
    # compose 默认密码公开可见，生产沿用等于 runner 的 ACL 隔离形同虚设。
    with pytest.raises(RuntimeError):
        validate_runner_settings(
            _prod_runner(
                sandbox_redis_url="redis://sandbox-runner:dev-sandbox-redis-password@redis:6379/0"
            )
        )


def test_prod_runner_accepts_dedicated_secret_and_acl_redis():
    validate_runner_settings(_prod_runner())


def test_dev_runner_allows_shared_redis():
    # 本地开发零配置：ACL 与独立密钥只在非 dev 强制
    validate_runner_settings(
        ServiceSettings(
            _env_file=None,
            env="dev",
            runner_signing_secret=RUNNER_SECRET,
            sandbox_redis_url="",
        )
    )


# ---- 专用沙箱节点档位（dedicated）----


def _prod_dedicated(**overrides) -> ServiceSettings:
    # dedicated 档位允许 rootful 默认 socket，但其它生产约束一条不放宽
    values = {
        "env": "prod",
        "sandbox_isolation_mode": "dedicated",
        "sandbox_docker_host": "unix:///var/run/docker.sock",
        "sandbox_runtime": "runsc",
        "sandbox_image": "sha256:" + "a" * 64,
    }
    values.update(overrides)
    return ServiceSettings(_env_file=None, **values)


def test_prod_dedicated_accepts_root_socket():
    # 专用节点允许 rootful socket：安全性由节点独占 + gVisor + digest 镜像承担
    validate_sandbox_isolation(_prod_dedicated())


def test_prod_dedicated_requires_explicit_endpoint():
    # 空端点会回落 SDK 默认，无法证明连的是哪台守护进程
    with pytest.raises(RuntimeError):
        validate_sandbox_isolation(_prod_dedicated(sandbox_docker_host=""))


def test_prod_dedicated_rejects_tcp_endpoint():
    # tcp 端点意味着远程 root 守护进程，专用节点只接受本地 unix socket
    with pytest.raises(RuntimeError):
        validate_sandbox_isolation(
            _prod_dedicated(sandbox_docker_host="tcp://docker.example:2375")
        )


def test_prod_dedicated_still_requires_gvisor():
    # dedicated 只放宽端点来源，不改动 runsc / digest / network / 资源上限
    with pytest.raises(RuntimeError):
        validate_sandbox_isolation(_prod_dedicated(sandbox_runtime="runc"))


def test_prod_rejects_unknown_isolation_mode():
    # 拼错档位不能静默落回 rootless，否则隔离预期与实际部署不一致
    with pytest.raises(RuntimeError):
        validate_sandbox_isolation(_prod_dedicated(sandbox_isolation_mode="dedicated-node"))


@pytest.mark.asyncio
async def test_healthz_exposes_dedicated_isolation(monkeypatch):
    monkeypatch.setattr(
        runner_app, "settings", _prod_runner(sandbox_isolation_mode="dedicated")
    )
    monkeypatch.setattr(
        runner_app.runner,
        "runtime_info",
        lambda: {
            "docker_sdk": True,
            "reachable": True,
            "endpoint": "unix:///var/run/docker.sock",
            "rootless": False,
            "runtime": "runsc",
            "runtime_ok": True,
            "image": "sha256:" + "a" * 64,
            "image_pinned": True,
            "insecure": False,
            "insecure_image": False,
            "isolation_mode": "dedicated",
            "node_exclusive": True,
            "foreign_containers": [],
            "mode_mismatch": False,
        },
    )

    async with _client() as client:
        response = await client.get("/healthz")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    # 档位与独占检查结果必须透出，运维才能区分「隔离达标」与「配置漂移」
    assert response.json()["isolation"]["mode"] == "dedicated"
    assert response.json()["isolation"]["node_exclusive"] is True
    assert response.json()["isolation"]["foreign_containers"] == []


@pytest.mark.asyncio
async def test_healthz_returns_503_when_node_not_exclusive(monkeypatch):
    monkeypatch.setattr(
        runner_app, "settings", _prod_runner(sandbox_isolation_mode="dedicated")
    )
    monkeypatch.setattr(
        runner_app.runner,
        "runtime_info",
        lambda: {
            "docker_sdk": True,
            "reachable": True,
            "rootless": False,
            "runtime": "runsc",
            "runtime_ok": True,
            "image": "sha256:" + "a" * 64,
            "image_pinned": True,
            "insecure": False,
            "insecure_image": False,
            "isolation_mode": "dedicated",
            "node_exclusive": False,
            "foreign_containers": ["erp-agent"],
            "mode_mismatch": False,
        },
    )

    async with _client() as client:
        response = await client.get("/healthz")

    # 节点混部时 rootful socket 的风险半径不可控，健康检查必须判死
    assert response.status_code == 503
    assert response.json()["isolation"]["foreign_containers"] == ["erp-agent"]


@pytest.mark.asyncio
async def test_healthz_returns_503_when_isolation_mode_mismatches(monkeypatch):
    monkeypatch.setattr(
        runner_app, "settings", _prod_runner(sandbox_isolation_mode="dedicated")
    )
    monkeypatch.setattr(
        runner_app.runner,
        "runtime_info",
        lambda: {
            "docker_sdk": True,
            "reachable": True,
            "rootless": True,
            "runtime": "runsc",
            "runtime_ok": True,
            "image": "sha256:" + "a" * 64,
            "image_pinned": True,
            "insecure": False,
            "insecure_image": False,
            "isolation_mode": "dedicated",
            "node_exclusive": True,
            "foreign_containers": [],
            "mode_mismatch": True,
        },
    )

    async with _client() as client:
        response = await client.get("/healthz")

    # 声明 dedicated 却连 rootless 守护进程：隔离强度低于声明，必须判死
    assert response.status_code == 503
    assert response.json()["isolation"]["mode_mismatch"] is True


def test_dedicated_refuses_execution_on_contaminated_node(monkeypatch):
    client = _FakeDockerClient()
    client.containers.listed = [_FakeContainerRef("erp-agent", None)]
    runner = _runner_with(_prod_dedicated(), client, monkeypatch)

    result = runner._run_blocking("print(1)", 5)

    # harness 不读 compose health，执行边界必须自己拦：混部节点上不创建任何容器
    assert result.status == "unavailable"
    assert "erp-agent" in result.stderr
    assert client.containers.create_kwargs == {}


def test_dedicated_executes_when_node_is_exclusive(monkeypatch):
    client = _FakeDockerClient()
    client.containers.listed = [
        _FakeContainerRef("sandbox-runner", {"shop.sandbox.role": "runner"}),
        _FakeContainerRef("exec-1", {"shop.sandbox.role": "execution"}),
    ]
    runner = _runner_with(_prod_dedicated(), client, monkeypatch)

    result = runner._run_blocking("print(1)", 5)

    # 沙箱栈自身容器（runner / 执行中容器 / infra）不算混部
    assert result.status == "ok"
    assert client.containers.create_kwargs["labels"] == {"shop.sandbox.role": "execution"}


def test_rootless_mode_skips_node_exclusivity_check(monkeypatch):
    client = _FakeDockerClient()
    client.containers.listed = [_FakeContainerRef("erp-agent", None)]
    runner = _runner_with(
        ServiceSettings(_env_file=None, env="dev", sandbox_isolation_mode="rootless"),
        client,
        monkeypatch,
    )

    # rootless 档位的隔离不依赖节点独占，不能因为它白跑失败拦下正常执行
    assert runner._run_blocking("print(1)", 5).status == "ok"


def test_run_labels_execution_container(monkeypatch):
    client = _FakeDockerClient()
    runner = _runner_with(ServiceSettings(_env_file=None, env="dev"), client, monkeypatch)

    runner._run_blocking("print(1)", 5)

    # 执行容器必须带角色标签，否则专用节点独占检查会把它当成业务容器
    assert client.containers.create_kwargs["labels"] == {"shop.sandbox.role": "execution"}
