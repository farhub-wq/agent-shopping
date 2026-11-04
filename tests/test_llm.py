import json

import pytest

from harness.core.llm import DeterministicPlanner

SPECS = [
    {"name": "catalog_search"},
    {"name": "inventory_check"},
    {"name": "order_lookup"},
    {"name": "after_sales_draft"},
]


@pytest.mark.asyncio
async def test_planner_routes_stock_question_to_inventory_tool():
    planner = DeterministicPlanner()
    decision = await planner.decide("system", [{"role": "user", "content": "SKU-1001 还有货吗？"}], SPECS)
    assert decision.kind == "tool"
    assert decision.tool == "inventory_check"
    assert decision.args["sku"] == "SKU-1001"


@pytest.mark.asyncio
async def test_planner_routes_refund_intent():
    planner = DeterministicPlanner()
    decision = await planner.decide(
        "system", [{"role": "user", "content": "订单 SO20260101123456 想申请退款，质量有问题"}], SPECS
    )
    assert decision.tool == "after_sales_draft"
    assert decision.args["reason"] == "quality_issue"
    assert decision.args["order_id"] == "SO20260101123456"


@pytest.mark.asyncio
async def test_planner_finishes_after_tool_observation():
    planner = DeterministicPlanner()
    observation = json.dumps({"status": "ok", "data": {"sku": "SKU-1001", "available": 12}})
    decision = await planner.decide(
        "system",
        [
            {"role": "user", "content": "SKU-1001 还有货吗？"},
            {"role": "assistant", "content": None},
            {"role": "tool", "tool_call_id": "call_1", "content": observation},
        ],
        SPECS,
    )
    assert decision.kind == "final"
    assert "SKU-1001" in decision.content


@pytest.mark.asyncio
async def test_planner_does_not_invent_when_no_tool_matches():
    planner = DeterministicPlanner()
    decision = await planner.decide("system", [{"role": "user", "content": "今天天气怎么样"}], [])
    assert decision.kind == "final"
    assert "库存" in decision.content
