"""售后政策引擎：把「能不能退、要不要人工」从客服脑子里搬到可测试的规则里。

Agent 只能产出 recommendation，最终放行必须走人工或明确的高置信自动策略。
"""

from datetime import UTC, datetime, timedelta
from typing import Any

FRAUD_KEYWORDS = ["空包", "掉包", "少件", "假货", "未收到货却要求退款"]
# 这两类原因本身风险更高：无退货退款与价保容易被滥用，命中即加分
HIGH_RISK_REASONS = {"no_return_refund", "price_protection"}
AUTO_APPROVE_AMOUNT_LIMIT = 20000  # 200 元
HIGH_AMOUNT_THRESHOLD = 50000  # 500 元
# 达到该分数强制转人工：宁可多人工复核，也不让高风险请求走自动放款
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
    now = datetime.now(UTC)
    # 超窗与订单状态属于硬性拒绝项，先短路返回，不进入打分，避免给出误导性的低风险分
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

    # 风险分为各维度的可解释加分项，每一项都写入 reasons，便于人工复核时追溯依据
    if amount_cents > HIGH_AMOUNT_THRESHOLD:
        score += 25
        reasons.append("大额退款")
    if reason in HIGH_RISK_REASONS:
        score += 15
        reasons.append("高风险原因类型")
    if recent_ticket_count >= 3:
        score += 30
        reasons.append("近期售后频次异常")
    # 关键词命中权重最高（40 分）：欺诈话术一旦出现，几乎必然推过人工线
    hit = [word for word in FRAUD_KEYWORDS if word in description]
    if hit:
        score += 40
        reasons.append(f"描述命中欺诈关键词：{','.join(hit)}")

    # 分数越线即转人工，优先于下面的自动通过判断：高风险请求不会因金额小而自动放行
    if score >= MANUAL_REVIEW_SCORE:
        return {
            "decision": "manual",
            "risk_score": score,
            "risk_reasons": reasons,
            "note": "风险分偏高，转人工审核",
        }

    # 自动通过条件刻意收紧为三重与：小额 + 常规原因白名单 + 风险分极低（<20），三者缺一即人工
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

    # 兜底一律转人工：政策未明确覆盖的场景不做自动放款，把判断权交回人工
    return {
        "decision": "manual",
        "risk_score": score,
        "risk_reasons": reasons or ["默认人工复核"],
        "note": "自动策略未覆盖，转人工审核",
    }
