"""上下文预算：token 粗估、窗口占用比例与压缩阶段判定。

口径与既有成本护栏一致（字符数 // 3），只用于「是否接近窗口上限」的提示，
不参与计费。窗口是会话预算而非模型最大窗口，避免接入超长窗口模型后单轮成本失控。
"""

from __future__ import annotations

import json
from typing import Any

from shop_common.config import get_settings

# 中文字符约 1.5-2 字符/token，取 3 作为保守上界：宁可高估，提前提醒换窗口。
CHARS_PER_TOKEN = 3


def estimate_tokens(text: str | None) -> int:
    if not text:
        return 0
    return max(len(text) // CHARS_PER_TOKEN, 1)


def estimate_messages(messages: list[dict[str, Any]]) -> int:
    total = 0
    for message in messages:
        content = message.get("content")
        if isinstance(content, str):
            total += estimate_tokens(content)
        elif content is not None:
            total += estimate_tokens(json.dumps(content, ensure_ascii=False))
        # tool_calls / tool_call_id / role 等结构字段也占 token，按固定开销计入。
        total += 4
    return total


def context_window() -> int:
    return max(int(getattr(get_settings(), "agent_context_window_default", 32000)), 4096)


def output_reserve() -> int:
    return max(int(getattr(get_settings(), "agent_output_token_reserve", 1024)), 0)


def working_token_budget() -> int:
    """工作记忆可用的 token 额度：窗口 × 提醒比例 - 输出预留。

    system prompt 与工具 schema 由调用方再扣除，保证「装满工作记忆」时整体
    占用正好落在 warn 比例附近，80% 提醒才有实际意义。
    """
    settings = get_settings()
    ratio = float(getattr(settings, "agent_context_warn_ratio", 0.8))
    return max(int(context_window() * ratio) - output_reserve(), 512)


def stage_for(ratio: float) -> str:
    settings = get_settings()
    if ratio >= float(getattr(settings, "agent_context_hard_ratio", 0.95)):
        return "hard"
    if ratio >= float(getattr(settings, "agent_context_split_ratio", 0.9)):
        return "split"
    if ratio >= float(getattr(settings, "agent_context_warn_ratio", 0.8)):
        return "warn"
    return "ok"


def build_context_usage(
    *,
    system: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    omitted_messages: int = 0,
) -> dict[str, Any]:
    """计算一次请求的上下文占用，返回前端渲染所需的最小字段集。"""
    limit = context_window()
    tool_tokens = estimate_tokens(json.dumps(tools, ensure_ascii=False)) if tools else 0
    used = estimate_tokens(system) + estimate_messages(messages) + tool_tokens + output_reserve()
    ratio = round(used / limit, 4)
    stage = stage_for(ratio)
    # 已经开始省略较早对话，说明上下文装填到了预算边界；即使比例略低于阈值，
    # 也应提示换窗口，否则用户会在「无感丢历史」的状态下继续对话。
    if omitted_messages and stage == "ok":
        stage = "warn"
    return {
        "used_tokens": used,
        "limit": limit,
        "ratio": ratio,
        "stage": stage,
        "reserve_tokens": output_reserve(),
        "omitted_messages": omitted_messages,
    }


def _truncate_text(value: Any, limit: int = 200) -> Any:
    if not isinstance(value, str):
        return value
    return value[:limit] + ("…" if len(value) > limit else "")


def _truncate_value(value: Any, *, depth: int = 0) -> Any:
    """浅层保留结构、深层降级为省略号，避免嵌套大对象撑爆结果。"""
    if isinstance(value, str):
        return _truncate_text(value)
    if isinstance(value, dict):
        if depth >= 2:
            return {str(key): "…" for key in list(value)[:5]}
        items = list(value.items())[:20]
        out = {str(key): _truncate_value(item, depth=depth + 1) for key, item in items}
        if len(value) > 20:
            out["_omitted_keys"] = len(value) - 20
        return out
    if isinstance(value, (list, tuple)):
        out = [_truncate_value(item, depth=depth + 1) for item in value[:10]]
        if len(value) > 10:
            out.append(f"…共 {len(value)} 条")
        return out
    return value


def mask_tool_result(
    payload: dict[str, Any], *, max_tokens: int | None = None
) -> tuple[str, dict[str, Any]]:
    """把过长的工具结果压成短占位符（observation masking）。

    返回 (进 prompt 的 JSON 文本, 审计元信息)。`tool_call_id` 在消息结构上独立保留，
    所以模型仍能把结果和调用配对；status / message / 关键 data 字段不丢，
    完整结果仍在运行轨迹里可追溯。
    """
    raw = json.dumps(payload, ensure_ascii=False)
    original = estimate_tokens(raw)
    limit = max_tokens or int(getattr(get_settings(), "agent_tool_result_max_tokens", 400))
    if original <= limit:
        return raw, {"masked": False, "original_tokens": original, "masked_tokens": original}

    masked: dict[str, Any] = {
        "status": payload.get("status"),
        "message": _truncate_text(payload.get("message")),
        "duration_ms": payload.get("duration_ms"),
        "data": _truncate_value(payload.get("data")),
        "_masked": True,
        "_original_tokens": original,
    }
    text = json.dumps(masked, ensure_ascii=False)
    if estimate_tokens(text) > limit:
        # 兜底：压缩后的结构仍超限时，把 data 再降级为单行截断文本。
        masked["data"] = json.dumps(masked.get("data"), ensure_ascii=False)[: max(limit, 16) * CHARS_PER_TOKEN]
        masked["_note"] = "工具结果过长，已截断；完整结果见运行轨迹"
        text = json.dumps(masked, ensure_ascii=False)
    return text, {
        "masked": True,
        "original_tokens": original,
        "masked_tokens": estimate_tokens(text),
    }
