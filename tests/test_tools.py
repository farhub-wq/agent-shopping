import pytest
from harness.core.policy import ToolPolicy
from harness.core.tools import Tool, ToolContext, ToolRegistry, redact, redact_data


def test_redact_masks_phone():
    assert redact("回电 13812345678 谢谢") == "回电 1********** 谢谢"


def test_redact_masks_id_card():
    # 证件号不含「1[3-9]」相邻位，可隔离验证身份证正则本身生效（不被手机号正则抢先截断）。
    assert "110101201001011234" not in redact("证件号 110101201001011234 已核验")


def test_redact_masks_email():
    assert redact("邮箱 zhang@example.com 备用") == "邮箱 ***@*** 备用"


def test_redact_data_recurses_dict_and_list():
    data = {
        "items": ["联系 13812345678", {"note": "邮箱 a@b.com"}],
        "count": 3,
        "ok": True,
        "extra": None,
    }
    masked = redact_data(data)
    assert masked["items"][0] == "联系 1**********"
    assert masked["items"][1]["note"] == "邮箱 ***@***"
    # 标量原样保留：金额、布尔、None 都不是 PII 载体。
    assert masked["count"] == 3
    assert masked["ok"] is True
    assert masked["extra"] is None


def test_redact_data_does_not_mangle_business_identifiers():
    # 订单号（SO 前缀）、SKU 这类业务标识不含手机号特征，不得误伤。
    data = {"order_no": "SO20260101123456", "sku": "SKU-1001"}
    assert redact_data(data) == data


@pytest.mark.asyncio
async def test_execute_redacts_structured_output():
    registry = ToolRegistry()

    async def leak(args, ctx):
        # 模拟 order_lookup 返回的下游订单 payload：PII 藏在嵌套 dict 与自由文本里。
        return {
            "order_no": "SO20260101123456",
            "amount_cents": 9900,
            "receiver": {
                "name": "张三",
                "phone": "13812345678",
                "address": "上海市浦东新区xx路1号，备用电话 13812345678",
            },
            "tags": ["vip", "推荐邮箱 a.b@example.com"],
        }

    registry.register(
        Tool("order_lookup", "d", {"type": "object"}, leak, ToolPolicy("order_lookup", permission="read"))
    )
    ctx = ToolContext(user_id="u1", role="customer", tenant="t", session_id="s", run_id="r")
    result = await registry.execute("order_lookup", {}, ctx, trust="certified")

    assert result.ok
    # 即使最高信任等级，输出也要过脱敏：certified 解决「能不能调」，不豁免「看见什么」。
    assert result.data["order_no"] == "SO20260101123456"
    assert result.data["amount_cents"] == 9900
    assert result.data["receiver"]["name"] == "张三"
    assert "13812345678" not in result.data["receiver"]["phone"]
    assert "13812345678" not in result.data["receiver"]["address"]
    assert "a.b@example.com" not in result.data["tags"][1]
