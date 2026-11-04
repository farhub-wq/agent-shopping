"""售后政策引擎：把「能不能退、要不要人工」从客服脑子里搬到可测试的规则里。

Agent 只能产出 recommendation，最终放行必须走人工或明确的高置信自动策略。
"""

from datetime import datetime, timedelta, timezone
from typing import Any

FRAUD_KEYWORDS = ["空包", "掉包", "少件", "假货", "未收到货却要求退款"]
HIGH_RISK_REASONS = {"no_return_refund", "price_protection"}
AUTO_APPROVE_AMOUNT_LIMIT = 20000  # 200 元
HIGH_AMOUNT_THRESHOLD = 50000  # 500 元
MANUAL_REVIEW_SCORE = 45


def evaluate(
    *,
    order_status: str,
    order_created_at: datetime,
    amount_cents: int,
    reason: str,
    description: str,
    recent_ticket_count: int,
    window_days: int,
) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    if now - order_created_at > timedelta(days=window_days):
        return {
            "decision": "auto_reject",
            "risk_score": 0,
            "risk_reasons": ["超出售后申请窗口"],
            "note": f"订单创建已超过 {window_days} 天，不符合售后政策",
        }
    if order_status not in {"PAID", "SHIPPED", "COMPLETED"}:
        return {
            "decision": "auto_reject",
            "risk_score": 0,
            "risk_reasons": ["订单状态不支持售后"],
            "note": f"当前订单状态 {order_status} 不支持退款",
        }

    score = 0
    reasons: list[str] = []

    if amount_cents > HIGH_AMOUNT_THRESHOLD:
        score += 25
        reasons.append("大额退款")
    if reason in HIGH_RISK_REASONS:
        score += 15
        reasons.append("高风险原因类型")
    if recent_ticket_count >= 3:
        score += 30
        reasons.append("近期售后频次异常")
    hit = [word for word in FRAUD_KEYWORDS if word in description]
    if hit:
        score += 40
        reasons.append(f"描述命中欺诈关键词：{','.join(hit)}")

    if score >= MANUAL_REVIEW_SCORE:
        return {
            "decision": "manual",
            "risk_score": score,
            "risk_reasons": reasons,
            "note": "风险分偏高，转人工审核",
        }

    if (
        amount_cents <= AUTO_APPROVE_AMOUNT_LIMIT
        and reason in {"size_issue", "quality_issue", "wrong_item"}
        and score < 20
    ):
        return {
            "decision": "auto_approve",
            "risk_score": score,
            "risk_reasons": reasons,
            "note": "小额、常规原因且风险分低，自动通过",
        }

    return {
        "decision": "manual",
        "risk_score": score,
        "risk_reasons": reasons or ["默认人工复核"],
        "note": "自动策略未覆盖，转人工审核",
    }
