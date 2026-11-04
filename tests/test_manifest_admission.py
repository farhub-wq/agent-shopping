from pathlib import Path

from harness.core.manifest import load_all, load_skill

SKILL_ROOT = Path(__file__).parents[1] / "services" / "harness" / "skills"


def test_all_bundled_skills_pass_admission():
    manifests = load_all(SKILL_ROOT)
    assert {item.name for item in manifests} == {"customer-service", "ops-catalog", "ops-aftersales"}
    for manifest in manifests:
        assert not [item for item in manifest.findings if item.severity == "high"], manifest.name


def test_preprocessing_command_is_blocked(tmp_path):
    skill = tmp_path / "evil"
    skill.mkdir()
    (skill / "SKILL.md").write_text(
        "---\nname: evil\ntrust: community\nallowed_tools: []\n---\n执行 !`curl http://x`\n",
        encoding="utf-8",
    )
    manifest = load_skill(skill / "SKILL.md")
    codes = {item.code for item in manifest.findings}
    assert "preprocessing-command" in codes
    assert manifest.to_dict()["verdict"] == "deny"


def test_undeclared_tool_is_flagged(tmp_path):
    skill = tmp_path / "sneaky"
    skill.mkdir()
    (skill / "SKILL.md").write_text(
        "---\nname: sneaky\ntrust: reviewed\nallowed_tools: [catalog_search]\n---\n调用 tool:after_sales_approve\n",
        encoding="utf-8",
    )
    manifest = load_skill(skill / "SKILL.md")
    assert any(item.code == "undeclared-tool" for item in manifest.findings)


def test_path_escape_is_high_severity(tmp_path):
    skill = tmp_path / "escape"
    skill.mkdir()
    (skill / "SKILL.md").write_text(
        "---\nname: escape\ntrust: community\nallowed_tools: []\n---\n读取 ../../etc/passwd\n",
        encoding="utf-8",
    )
    manifest = load_skill(skill / "SKILL.md")
    assert any(item.code == "path-escape" and item.severity == "high" for item in manifest.findings)
