from pathlib import Path

import pytest

from harness.core.manifest import load_all, load_skill
from harness.core.policy import ToolPolicy, enforce
from shop_common.errors import ApiError

SKILL_ROOT = Path(__file__).parents[1] / "services" / "harness" / "skills"


def test_reviewed_tier_cannot_use_execute_tool():
    policy = ToolPolicy("sandbox_run", permission="execute", risk="medium")
    with pytest.raises(ApiError) as exc:
        enforce("reviewed", policy)
    assert exc.value.code == "FORBIDDEN"


def test_vendor_can_use_execute_but_not_admin_tool():
    policy = ToolPolicy("sandbox_run", permission="execute", risk="medium")
    enforce("vendor", policy)
    admin_policy = ToolPolicy("drop_database", permission="admin", risk="irreversible")
    with pytest.raises(ApiError):
        enforce("vendor", admin_policy)


def test_manifest_cannot_self_escalate():
    """manifest 声明 sandbox，但 community 等级仍然拿不到执行权。"""
    policy = ToolPolicy("sandbox_run", permission="execute")
    with pytest.raises(ApiError):
        enforce("community", policy)


def test_pii_tool_denied_for_reviewed_tier():
    policy = ToolPolicy("order_lookup", permission="read", data_class="pii")
    with pytest.raises(ApiError):
        enforce("reviewed", policy)


def test_bundled_skills_have_expected_trust():
    manifests = {item.name: item for item in load_all(SKILL_ROOT)}
    assert manifests["customer-service"].trust == "vendor"
    assert manifests["ops-aftersales"].trust == "reviewed"


def test_profiling_of_single_manifest():
    manifest = load_skill(SKILL_ROOT / "ops-catalog" / "SKILL.md")
    assert "sandbox_run" in manifest.allowed_tools
