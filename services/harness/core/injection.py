"""提示注入防线：高置信模式扫描 + 指令性片段中和。

三层用法：
- scan()：用户输入入口扫描，命中即拒（零模型调用、零记忆写入，防投毒）；
- neutralize()：记忆召回文本逐条中和，挡存量污染与其他写入方；
- neutralize_data()：工具结果递归中和，数据照回但指令性片段失效。

定位是「高召回率的第一道墙」而非语义判定：漏判由审批护栏与最小权限兜底，
误伤按 label 观测后收窄模式。编码混淆（base64/零宽字符）不在本层范围。
"""

import re
from typing import Any

# 命中后替换的占位符：让指令性片段对模型失去语义，又不破坏正常数据可读性。
PLACEHOLDER = "[已过滤]"

# 模式库：只收高置信形态，每条带 label 供 trace/指标定位误伤。
# 误伤边界由 tests/test_injection.py 的干净样本钉死，改模式必须同步改样本。
_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # 指令覆盖（英文）：ignore/disregard/forget ... previous/above ... instructions/rules
    (
        "override-instructions-en",
        re.compile(
            r"\b(ignore|disregard|forget|override)\b[^.\n]{0,25}"
            r"\b(previous|prior|above|earlier|system)\b[^.\n]{0,25}"
            r"\b(instructions?|rules?|prompts?|constraints?)\b",
            re.IGNORECASE,
        ),
    ),
    # 指令覆盖（中文）：忽略/无视/抛开 + 之前/以上 + 指令/命令/规则/设定/约束。
    # 注意宾语白名单不含「说的」，避免误伤「忽略之前说的尺寸」这类正常改口。
    (
        "override-instructions-zh",
        re.compile(r"(忽略|无视|抛开|丢弃)(之前|以前|上述|以上|前面)(的)?(指令|指示|命令|要求|规则|设定|约束)"),
    ),
    # 人格劫持：你现在…扮演/成为/切换；或直接指派高权角色；或越狱关键词。
    # 角色名词收白名单（管理员/开发者/超级用户/admin/root），
    # 避免误伤「你现在是机器人还是人工客服」这类询问。
    (
        "persona-hijack",
        re.compile(
            r"(你|您)现在(是|起|必须|需要|改为|变成)[^。.\n]{0,12}(扮演|成为|作为|切换)"
            r"|(你|您)现在是(一个|一名)?(新)?(管理员|开发者|超级用户|admin|root)"
            r"|(越狱模式|开发者模式|无限制模式|\bDAN\b\s*(模式|mode)?|jailbreak)",
            re.IGNORECASE,
        ),
    ),
    # 套取 system prompt（中文）：正向「系统提示词…告诉我/输出」与反向「输出…系统指令」。
    (
        "system-prompt-exfil-zh",
        re.compile(
            r"(系统提示词|系统指令|初始指令|内部指令|运行约束)[^。.\n]{0,12}(告诉我|给我看|输出|打印|重复|展示|是什么)"
            r"|(输出|重复|打印|展示|告诉我)[^。.\n]{0,12}(系统提示词|系统指令|初始指令|内部指令)"
        ),
    ),
    # 套取 system prompt（英文）：双向语序。
    (
        "system-prompt-exfil-en",
        re.compile(
            r"\b(system prompt|your instructions|initial prompt)\b[^.\n]{0,25}"
            r"\b(reveal|show|print|repeat|tell|leak)\b"
            r"|\b(reveal|show|print|repeat|leak)\b[^.\n]{0,25}"
            r"\b(system prompt|your instructions|initial prompt)\b",
            re.IGNORECASE,
        ),
    ),
    # 分隔符逃逸：伪造对话结构标记。
    (
        "delimiter-injection",
        re.compile(r"(<<\s*SYS\s*>>|\[/?INST\]|<\|im_(start|end)\|>|</?system>)", re.IGNORECASE),
    ),
]


def scan(text: str) -> str | None:
    """返回命中的模式 label，未命中返回 None。只取首个命中，够用且便宜。"""
    for label, pattern in _PATTERNS:
        if pattern.search(text):
            return label
    return None


def neutralize(text: str) -> str:
    """把命中的指令性片段替换为占位符：数据保留，指令失效。"""
    for _, pattern in _PATTERNS:
        text = pattern.sub(PLACEHOLDER, text)
    return text


def neutralize_data(data: Any) -> Any:
    """递归中和：与 redact_data 同构，dict 按值、list 按元素、字符串走 neutralize。"""
    if isinstance(data, str):
        return neutralize(data)
    if isinstance(data, dict):
        return {key: neutralize_data(value) for key, value in data.items()}
    if isinstance(data, list):
        return [neutralize_data(item) for item in data]
    return data
