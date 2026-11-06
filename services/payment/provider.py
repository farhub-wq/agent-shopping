"""支付渠道抽象与 Mock 实现。

真实聚合支付接入时只替换 Provider：支付单创建、回调验签、退款、渠道账单四个能力保持不变。
"""

import hashlib
import hmac
import json
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from shop_common.errors import ApiError

# 回调签名时间窗：超过 5 分钟的回调视为重放，直接拒绝
CALLBACK_MAX_SKEW_SECONDS = 300


@dataclass
class ProviderPayment:
    provider_txn_id: str
    pay_url: str


class PaymentProvider(Protocol):
    name: str

    def create_payment(
        self,
        *,
        payment_id: str,
        order_id: str,
        amount_cents: int,
        currency: str,
        expires_at: str,
        gateway_url: str,
    ) -> ProviderPayment: ...

    def refund(self, *, payment_id: str, refund_id: str, amount_cents: int) -> str: ...

    def verify_callback(
        self, *, raw_body: bytes, headers: Mapping[str, str], secret: str
    ) -> dict[str, Any]: ...

    def statement(self, *, biz_date: str, local_records: list[dict[str, Any]]) -> list[dict[str, Any]]: ...


def callback_signature(secret: str, ts: str, raw_body: bytes) -> str:
    """回调签名算法：HMAC-SHA256(secret, "{ts}.{raw_body}")。"""
    return hmac.new(secret.encode(), f"{ts}.".encode() + raw_body, hashlib.sha256).hexdigest()


class MockProvider:
    """本地沙箱渠道：收银台由 payment 服务自己渲染，回调按真实渠道的方式签名。"""

    name = "mock"

    def create_payment(
        self,
        *,
        payment_id: str,
        order_id: str,
        amount_cents: int,
        currency: str,
        expires_at: str,
        gateway_url: str,
    ) -> ProviderPayment:
        # 渠道流水号用独立 uuid，不复用业务单号，避免渠道侧改单号规则时污染本地数据
        return ProviderPayment(
            provider_txn_id=f"mock_{uuid.uuid4().hex}",
            pay_url=f"{gateway_url.rstrip('/')}/api/payments/cashier/{payment_id}",
        )

    def refund(self, *, payment_id: str, refund_id: str, amount_cents: int) -> str:
        # Mock 渠道同步成功；真实渠道通常是异步回调，届时 refund 只返回受理号。
        # 退款号必须全局唯一：迟到支付票据都是 latepay- 前缀，截断前 8 位会跨订单撞唯一索引。
        return f"mockrefund_{refund_id}"

    def verify_callback(
        self, *, raw_body: bytes, headers: Mapping[str, str], secret: str
    ) -> dict[str, Any]:
        ts = headers.get("x-payment-timestamp", "")
        signature = headers.get("x-payment-signature", "")
        if not ts or not signature:
            raise ApiError("UNAUTHORIZED", "回调缺少签名头")
        try:
            skew = abs(time.time() - int(ts))
        except ValueError as exc:
            raise ApiError("UNAUTHORIZED", "回调时间戳非法") from exc
        if skew > CALLBACK_MAX_SKEW_SECONDS:
            raise ApiError("UNAUTHORIZED", "回调时间戳超出允许窗口")
        expected = callback_signature(secret, ts, raw_body)
        if not hmac.compare_digest(signature, expected):
            raise ApiError("UNAUTHORIZED", "回调签名校验失败")
        try:
            payload = json.loads(raw_body)
        except ValueError as exc:
            raise ApiError("BAD_REQUEST", "回调报文不是合法 JSON") from exc
        if not isinstance(payload, dict):
            raise ApiError("BAD_REQUEST", "回调报文必须是对象")
        return payload

    def statement(self, *, biz_date: str, local_records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        # Mock 渠道没有独立账单，直接回放本地记录；测试通过 monkeypatch 注入差异
        return [{**record, "biz_date": biz_date} for record in local_records]


def get_provider(name: str) -> PaymentProvider:
    if name == "mock":
        return MockProvider()
    # 明确失败而不是静默退回 mock，避免生产误用沙箱渠道
    raise ApiError("CONFLICT", f"不支持的支付渠道 {name}")
