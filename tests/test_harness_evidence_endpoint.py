from unittest.mock import AsyncMock

import pytest
from harness import app as harness_app
from shop_common.auth import Principal


@pytest.mark.asyncio
async def test_evidence_uses_last_run_end_after_approval(monkeypatch):
    trace = [
        {"type": "run.start", "user": "customer-1", "skill": "inventory", "trust": "vendor"},
        {"type": "run.end", "status": "pending_approval", "answer": ""},
        {"type": "approval.granted", "tool": "stock_write"},
        {"type": "run.end", "status": "completed", "answer": "已完成"},
    ]
    monkeypatch.setattr(harness_app, "load_trace", AsyncMock(return_value=trace))

    result = await harness_app.get_evidence(
        "run-1", Principal("customer-1", "customer", "tenant-1")
    )

    assert result["status"] == "completed"
    assert result["trace"] == trace
