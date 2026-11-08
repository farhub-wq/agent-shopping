"""sandbox-runner 服务契约测试：鉴权、执行去重、结果映射与 fail-closed 语义。"""

import httpx
import pytest
from sandbox_runner import app as runner_app
from shop_common.auth import create_token
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
    monkeypatch.setattr(runner_app.runner, "docker_available", lambda: True)

    async with _client() as client:
        response = await client.get("/healthz")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["docker_sdk"] is True
    assert response.json()["max_concurrency"] >= 1
