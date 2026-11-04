"""SKILL.md 准入：在技能进入运行时之前，把风险挡在外面。

检查项固定为六个编码，便于 CI 与文档引用：
preprocessing-command / undeclared-tool / unlisted-script / path-escape /
network-egress / dependency-risk。
"""

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

PREPROCESS_PATTERN = re.compile(r"!`[^`]*`")
TOOL_REF_PATTERN = re.compile(r"tool:([a-zA-Z0-9_.\-]+)")
SCRIPT_PATTERN = re.compile(r"scripts?/([a-zA-Z0-9_.\-/]+)")
PATH_ESCAPE_PATTERN = re.compile(r"(\.\./|\.\.\\)")
NETWORK_PATTERN = re.compile(r"(curl|wget|https?://[^\s)]+)")


@dataclass
class Finding:
    code: str
    severity: str
    message: str
    line: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
            "line": self.line,
        }


@dataclass
class SkillManifest:
    name: str
    version: str
    description: str
    trust: str
    allowed_tools: list[str]
    permissions: dict[str, Any]
    max_steps: int
    body: str
    path: str
    content_hash: str = ""
    findings: list[Finding] = field(default_factory=list)

    @property
    def system_prompt(self) -> str:
        return self.body.strip()

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "trust": self.trust,
            "allowed_tools": self.allowed_tools,
            "permissions": self.permissions,
            "max_steps": self.max_steps,
            "path": self.path,
            "content_hash": self.content_hash,
            "findings": [item.to_dict() for item in self.findings],
            "verdict": "deny" if any(f.severity == "high" for f in self.findings) else "allow",
        }


def _split_front_matter(raw: str) -> tuple[dict[str, Any], str, int]:
    if not raw.startswith("---"):
        raise ValueError("SKILL.md 缺少 YAML front matter")
    parts = raw.split("---", 2)
    if len(parts) < 3:
        raise ValueError("SKILL.md front matter 未闭合")
    meta = yaml.safe_load(parts[1]) or {}
    body = parts[2]
    # split 只切两刀，正文里出现的 --- 不会被误当成 front matter 分隔符；
    # offset 记录正文在原文中的起始行号，便于把正文位置映射回原文行号。
    offset = raw[: len(parts[0]) + len(parts[1]) + 6].count("\n")
    return meta, body, offset


def load_skill(path: Path) -> SkillManifest:
    raw = path.read_text(encoding="utf-8")
    meta, body, offset = _split_front_matter(raw)
    manifest = SkillManifest(
        name=str(meta.get("name", path.parent.name)),
        version=str(meta.get("version", "0.0.0")),
        description=str(meta.get("description", "")),
        trust=str(meta.get("trust", "community")),
        allowed_tools=list(meta.get("allowed_tools") or []),
        permissions=dict(meta.get("permissions") or {}),
        max_steps=int(meta.get("max_steps", 6)),
        body=body,
        path=str(path),
    )
    manifest.content_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    # 准入审计在加载时同步完成：风险技能根本进不了运行时，
    # 而不是等到第一次调用才暴露。
    manifest.findings = audit(raw, offset, manifest)
    return manifest


def audit(raw: str, offset: int, manifest: SkillManifest) -> list[Finding]:
    findings: list[Finding] = []
    # 六类检查固定编码；high 会让整个技能 deny，medium 仅提示不阻断。
    # preprocessing-command：!`cmd` 意味着内容在进入模型上下文前就被宿主执行。
    for match in PREPROCESS_PATTERN.finditer(raw):
        findings.append(
            Finding(
                "preprocessing-command",
                "high",
                "技能包含 !`command` 预处理，宿主可能在模型看到内容前执行命令",
                raw[: match.start()].count("\n") + 1,
            )
        )
    for match in TOOL_REF_PATTERN.finditer(raw):
        name = match.group(1)
        # 正文显式引用的工具必须在 allowed_tools 中声明，否则是隐式扩权。
        if name not in manifest.allowed_tools:
            findings.append(
                Finding(
                    "undeclared-tool",
                    "high",
                    f"正文引用了未声明的工具 {name}",
                    raw[: match.start()].count("\n") + 1,
                )
            )
    for match in SCRIPT_PATTERN.finditer(raw):
        script = match.group(1)
        # 引用脚本却没有 sandbox 能力，说明脚本可能被宿主直接执行；
        # 风险定为 medium：不一定利用成功，但需要人工复核。
        if not any(script in tool for tool in manifest.allowed_tools) and "sandbox" not in manifest.allowed_tools:
            findings.append(
                Finding(
                    "unlisted-script",
                    "medium",
                    f"引用了脚本 {script}，但未声明沙箱执行能力",
                    raw[: match.start()].count("\n") + 1,
                )
            )
    for match in PATH_ESCAPE_PATTERN.finditer(raw):
        # 路径逃逸可直接读写技能目录之外的文件，属于高危，直接 deny。
        findings.append(
            Finding(
                "path-escape",
                "high",
                "技能包含路径逃逸片段",
                raw[: match.start()].count("\n") + 1,
            )
        )
    # 声明网络能力却没有 allowlist，等于允许任意出站，无法审计目的地。
    if manifest.permissions.get("network") and not manifest.permissions.get("network_allowlist"):
        findings.append(
            Finding("network-egress", "high", "声明了网络访问但未提供 allowlist")
        )
    # 工具数量过多违反最小权限；不阻断，但提示人工收敛。
    if len(manifest.allowed_tools) > 12:
        findings.append(Finding("dependency-risk", "medium", "允许工具数量过多，最小权限原则未满足"))
    return findings


def load_all(skill_root: Path) -> list[SkillManifest]:
    manifests: list[SkillManifest] = []
    for skill_file in sorted(skill_root.glob("*/SKILL.md")):
        manifests.append(load_skill(skill_file))
    return manifests
