import pytest
from harness.core.sandbox import SandboxManager


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
