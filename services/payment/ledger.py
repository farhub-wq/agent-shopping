"""复式总账：所有资金变化都必须成对写入借方与贷方，且合计相等。

历史分录只增不改；退款、拒付等反向资金流通过新增反向分录表达，不能修改原交易。
"""

import uuid
from typing import Literal

from shop_common.errors import ApiError
from shop_common.metrics import LEDGER_IMBALANCE
from sqlalchemy.ext.asyncio import AsyncSession

from .models import LedgerEntry, LedgerTransaction

ACCOUNT_CODES = {
    "CASH",  # 已到账资金
    "PROVIDER_RECEIVABLE",  # 渠道待结算应收
    "REVENUE",  # 收入
    "REFUND",  # 退款支出
    "FEE",  # 渠道手续费
    "CHARGEBACK",  # 拒付损失
}

Direction = Literal["debit", "credit"]


def post_transaction(
    session: AsyncSession,
    *,
    txn_type: str,
    ref_type: str,
    ref_id: str,
    entries: list[tuple[str, Direction, int]],
    currency: str = "CNY",
    tenant: str = "default",
) -> LedgerTransaction:
    """写入一笔借贷平衡的总账交易；调用方负责 commit。"""
    if len(entries) < 2:
        raise ApiError("CONFLICT", "总账交易至少需要一借一贷")
    debit = 0
    credit = 0
    for account, direction, amount in entries:
        # 账户与方向都收口在领域层，避免路由层拼出非法科目
        if account not in ACCOUNT_CODES:
            raise ApiError("CONFLICT", f"未知总账科目 {account}")
        if direction not in {"debit", "credit"}:
            raise ApiError("CONFLICT", f"未知记账方向 {direction}")
        if amount <= 0:
            raise ApiError("CONFLICT", "分录金额必须大于 0")
        if direction == "debit":
            debit += amount
        else:
            credit += amount
    if debit != credit:
        # 借贷不平直接拒绝写入，这是资金系统最强的本地不变量
        LEDGER_IMBALANCE.labels("payment").inc()
        raise ApiError("CONFLICT", f"总账借贷不平：debit={debit} credit={credit}")

    txn = LedgerTransaction(
        id=str(uuid.uuid4()),
        tenant_id=tenant,
        type=txn_type,
        ref_type=ref_type,
        ref_id=ref_id,
        currency=currency,
    )
    session.add(txn)
    for account, direction, amount in entries:
        session.add(
            LedgerEntry(
                txn_id=txn.id,
                account_code=account,
                direction=direction,
                amount_cents=amount,
            )
        )
    return txn
