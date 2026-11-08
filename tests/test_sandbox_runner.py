"""sandbox-runner 服务契约测试：鉴权、执行去重、结果映射与 fail-closed 语义。"""

import httpx
import pytest
from sandbox_runner import app as runner_app
from sandbox_runner.runner import DockerRunner, validate_sandbox_isolation
from shop_common.auth import create_token
from shop_common.config import ServiceSettings
from shop_common.sandbox import SandboxResult


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

    async def cached(self, execution_id: str) -> SandboxResult | None:
        return self.cached_value

    async def store(self, execution_id: str, result: SandboxResult) -> None:
        self.store_calls.append((execution_id, result))

    def lock(self, execution_id: str) -> _FakeLock:
        return self.lock_obj

    async def wait_for_result(self, execution_id: str, wait_seconds: float) -> SandboxResult | None:
        return None


@pytest.fixture(autouse=True)
def fake_dedupe(monkeypatch) -> _FakeDedupe:
    # 单测不连真实 Redis：用内存假实现验证去重流程，Redis 层面的读写在 dedupe 单测覆盖。
    fake = _FakeDedupe()
    monkeypatch.setattr(runner_app, "dedupe", fake)
    return fake


def _client() -> httpx.AsyncClient:
    # ASGITransport 直接调用 FastAPI 应用，不需要起真实端口。
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=runner_app.app),
        base_url="http://runner.test",
    )


def _headers(role: str, execution_id: str = "exec-1") -> dict[str, str]:
    return {
        "Authorization": f"Bearer {create_token(user_id='u-1', role=role)}",
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
    def start(self) -> None:
        pass

    def wait(self, timeout: int | None = None) -> dict[str, int]:
        return {"StatusCode": 0}

    def logs(self, stdout: bool = True, stderr: bool = False) -> bytes:
        return b"ok" if stdout else b""

    def remove(self, force: bool = False) -> None:
        pass


class _FakeContainers:
    def __init__(self) -> None:
        self.create_kwargs: dict[str, object] = {}

    def create(self, image: str, **kwargs: object) -> _FakeContainer:
        self.create_kwargs = {"image": image, **kwargs}
        return _FakeContainer()


class _FakeDockerClient:
    def __init__(self) -> None:
        self.containers = _FakeContainers()

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
    monkeypatch.setattr(runner_app, "settings", ServiceSettings(_env_file=None, env="prod"))
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

    # 隔离与镜像都达标，但密钥仍是开发默认值：身份可伪造，必须判死
    assert response.status_code == 503
    assert response.json()["security"]["secrets_configured"] is False
    assert "JWT_SECRET" in response.json()["security"]["issues"][0]
