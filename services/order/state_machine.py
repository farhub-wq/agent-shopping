"""订单状态机：所有状态变更必须经过这张表，服务层不允许直接赋值 status。

电商事故里最常见的一类，是「已退款订单又被发货」——根源就是状态校验散落在各处。
"""

from shop_common.errors import ApiError

TRANSITIONS: dict[str, dict[str, str]] = {
    "CREATED": {"reserve": "RESERVED", "cancel": "CANCELLED"},
    "RESERVED": {"pay_start": "PAYING", "cancel": "CANCELLED"},
    "PAYING": {"pay_success": "PAID", "pay_fail": "RESERVED", "cancel": "CANCELLED"},
    "PAID": {"ship": "SHIPPED", "refund_request": "REFUNDING"},
    "SHIPPED": {"deliver": "COMPLETED", "refund_request": "REFUNDING"},
    "COMPLETED": {"refund_request": "REFUNDING"},
    "REFUNDING": {"refund_complete": "REFUNDED"},
    "CANCELLED": {},
    "REFUNDED": {},
}

TERMINAL_STATES = {"CANCELLED", "REFUNDED"}

STATE_LABELS = {
    "CREATED": "已创建",
    "RESERVED": "库存已锁定",
    "PAYING": "支付中",
    "PAID": "已支付",
    "SHIPPED": "已发货",
    "COMPLETED": "已完成",
    "CANCELLED": "已取消",
    "REFUNDING": "退款处理中",
    "REFUNDED": "已退款",
}


def resolve_target(current: str, event: str, *, refund_previous: str | None = None) -> str:
    if event == "refund_reject" and current == "REFUNDING":
        if refund_previous not in TRANSITIONS:
            raise ApiError("CONFLICT", "退款驳回缺少来源状态")
        return refund_previous
    allowed = TRANSITIONS.get(current, {})
    target = allowed.get(event)
    if target is None:
        raise ApiError(
            "CONFLICT",
            f"订单状态 {STATE_LABELS.get(current, current)} 不允许事件 {event}",
            details={"current": current, "event": event},
        )
    return target
