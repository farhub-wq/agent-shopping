"""待审批运行列表：仅运营可见，返回脱敏摘要。"""

import json

import pytest
from harness import app as harness_app
from shop_common.auth import Principal
from shop_common.errors import ApiError


class _FakeRedis:
    def __init__(self, data: dict[str, str], ttl: int = 600) -> None:
        self._data = data
        self._ttl = ttl

    def scan_iter(self, match: str | None = None, count: int | None = None):
        async def _iter():
            for key in self._data:
                yield key

        return _iter()

    async def get(self, key: str) -> str | None:
        return self._data.get(key)

    async def ttl(self, key: str) -> int:
        return self._ttl


@pytest.fixture
def fake_redis(monkeypatch):
    snapshot = {
        "skill": "aftersales",
        "tool": "order_cancel",
        "reason": "用户要求取消",
        "args": {"order_id": "SO-1", "phone": "13812345678"},
        "ctx": {"user_id": "u1", "role": "customer"},
        "limits": {"token_budget": 1000},
    }
    redis = _FakeRedis(
        {
            "shop:harness:pending:run-a": json.dumps(snapshot),
            "shop:harness:pending:run-bad": "{not-json",
        }
    )
    monkeypatch.setattr(harness_app, "redis", redis)
    return redis


@pytest.mark.asyncio
async def test_pending_runs_are_staff_only(fake_redis):
    with pytest.raises(ApiError) as exc:
        await harness_app.list_pending_runs(Principal("u1", "customer", "default"))
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_pending_runs_return_redacted_summaries(fake_redis):
    result = await harness_app.list_pending_runs(Principal("staff", "operator", "default"))

    # 非法 JSON 的快照被跳过，不应导致整个列表 500。
    assert [item["run_id"] for item in result["items"]] == ["run-a"]
    item = result["items"][0]
    assert item["tool"] == "order_cancel"
    assert item["delegated"] is True
    assert item["expires_in_seconds"] == 600
    # 手机号在摘要里必须脱敏，审批人只看结构不看完整 PII。
    assert item["args_preview"]["phone"] != "13812345678"
