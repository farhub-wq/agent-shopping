import json

import httpx
import pytest
from harness.core.sandbox import SandboxManager, SandboxResult


def _enabled_manager(monkeypatch, handler, urls: str = "http://runner.test") -> SandboxManager:
    manager = SandboxManager(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(manager.settings, "sandbox_enabled", True)
    monkeypatch.setattr(manager.settings, "sandbox_runner_urls", urls)
    return manager


async def _no_local(code: str, timeout: int) -> SandboxResult:
    raise AssertionError("启用沙箱时不允许执行本地子进程")


@pytest.mark.asyncio
async def test_local_sandbox_runs_code():
    manager = SandboxManager()
    result = await manager.run("print(6 * 7)")
    assert result.status == "ok"
    assert result.stdout.strip() == "42"


@pytest.mark.asyncio
async def test_local_sandbox_caps_timeout():
    manager = SandboxManager()
    result = await manager.run("import time; time.sleep(5)", timeout=1)
    assert result.status == "timeout"
    assert "1s" in result.stderr


@pytest.mark.asyncio
async def test_sandbox_reports_syntax_error():
    manager = SandboxManager()
    result = await manager.run("def broken(: pass")
    assert result.status == "error"
    assert "SyntaxError" in result.stderr


@pytest.mark.asyncio
async def test_enabled_sandbox_calls_runner(monkeypatch):
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        seen["user"] = request.headers.get("X-User-Id")
        seen["execution_id"] = request.headers.get("X-Execution-Id")
        return httpx.Response(
            200, json={"status": "ok", "stdout": "42", "backend": "docker", "exit_code": 0}
        )

    manager = _enabled_manager(monkeypatch, handler)
    monkeypatch.setattr(manager, "_run_local", _no_local)

    result = await manager.run("print(42)", user_id="ops-1", role="operator", tenant="acme")

    assert result.status == "ok"
    assert result.stdout == "42"
    assert result.backend == "docker"
    assert str(seen["url"]).endswith("/run")
    body = seen["body"]
    assert body["code"] == "print(42)"
    assert body["language"] == "python"
    assert body["timeout"] > 0
    assert seen["user"] == "ops-1"
    # 每次调用必须带 execution_id，runner 依赖它做跨实例去重。
    assert isinstance(seen["execution_id"], str) and seen["execution_id"]


@pytest.mark.asyncio
async def test_enabled_sandbox_fails_closed_when_runner_unreachable(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    manager = _enabled_manager(monkeypatch, handler)
    monkeypatch.setattr(manager, "_run_local", _no_local)

    result = await manager.run("print(1)")

    # runner 不可达必须 fail-closed，不能回退到与宿主同权限的本地进程。
    assert result.status == "unavailable"
    assert result.backend == "remote"


@pytest.mark.asyncio
async def test_enabled_sandbox_maps_runner_5xx_to_unavailable(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="runner down")

    manager = _enabled_manager(monkeypatch, handler)
    monkeypatch.setattr(manager, "_run_local", _no_local)

    result = await manager.run("print(1)")

    assert result.status == "unavailable"
    assert result.backend == "remote"


@pytest.mark.asyncio
async def test_enabled_sandbox_fails_over_to_healthy_runner(monkeypatch):
    calls: list[str] = []
    execution_ids: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.host)
        execution_ids.append(request.headers.get("X-Execution-Id", ""))
        if request.url.host == "bad.test":
            raise httpx.ConnectError("connection refused", request=request)
        return httpx.Response(200, json={"status": "ok", "stdout": "42", "backend": "docker"})

    manager = _enabled_manager(monkeypatch, handler, "http://bad.test,http://good.test")
    monkeypatch.setattr(manager, "_run_local", _no_local)

    result = await manager.run("print(1)")

    # 第一台不可达必须自动换下一台，而不是直接失败。
    assert result.status == "ok"
    assert calls == ["bad.test", "good.test"]
    # 换 runner 只是换执行者：两次请求必须复用同一个 execution_id，供 runner 去重。
    assert execution_ids[0] == execution_ids[1] != ""


@pytest.mark.asyncio
async def test_enabled_sandbox_fails_over_when_runner_busy(monkeypatch):
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.host)
        if request.url.host == "busy.test":
            return httpx.Response(
                200, json={"status": "unavailable", "stderr": "runner 繁忙，请稍后重试"}
            )
        return httpx.Response(200, json={"status": "ok", "stdout": "42", "backend": "docker"})

    manager = _enabled_manager(monkeypatch, handler, "http://busy.test,http://good.test")
    monkeypatch.setattr(manager, "_run_local", _no_local)

    result = await manager.run("print(1)")

    # 单实例打满属于可转移故障：排队超时后应换下一台执行。
    assert result.status == "ok"
    assert calls == ["busy.test", "good.test"]


@pytest.mark.asyncio
async def test_enabled_sandbox_500_does_not_fail_over(monkeypatch):
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.host)
        return httpx.Response(500, text="boom")

    manager = _enabled_manager(monkeypatch, handler, "http://bad.test,http://good.test")
    monkeypatch.setattr(manager, "_run_local", _no_local)

    result = await manager.run("print(1)")

    # 500 可能是容器已执行后响应失败：不允许换实例重试，避免重复执行。
    assert result.status == "unavailable"
    assert calls == ["bad.test"]


@pytest.mark.asyncio
async def test_runner_status_aggregates_all_endpoints(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "bad.test":
            raise httpx.ConnectError("connection refused", request=request)
        return httpx.Response(200, json={"status": "ok", "docker_sdk": True})

    manager = _enabled_manager(monkeypatch, handler, "http://bad.test,http://good.test")

    status = await manager.runner_status()

    assert status["status"] == "ok"
    assert [item["url"] for item in status["endpoints"]] == ["http://bad.test", "http://good.test"]
    assert [item["status"] for item in status["endpoints"]] == ["unavailable", "ok"]
