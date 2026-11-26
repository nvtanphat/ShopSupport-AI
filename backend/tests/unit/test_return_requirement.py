"""A return request on a named order needs the domain's eligibility decision before the assistant answers."""

from __future__ import annotations

import pytest
from shopagent.agent.tool_requirements import derive_requirements, unmet_requirements

CUSTOMER_TOOLS = ["get_order", "search_policy", "check_return_eligibility", "propose_return", "propose_cancel_order"]


def _return_keys(text: str, tools: list[str] = CUSTOMER_TOOLS) -> list[str]:
    return [r.key for r in derive_requirements(text, tools) if r.key.startswith("return:")]


@pytest.mark.parametrize(
    "text",
    [
        "Mình muốn trả bộ đồ lót đơn SL-M60001, mua về không thích",
        "Áo len đơn SL-A10002 mặc không vừa, mình muốn trả",
        "Đơn SL-M40002 đôi giày mình không thích nữa, trả được không?",
        "Cho mình trả lại cái quần đơn SL-123456",
        "Đơn SL-123456 đổi trả thế nào?",
    ],
)
def test_a_return_request_on_an_order_requires_the_eligibility_check(text: str) -> None:
    assert len(_return_keys(text)) == 1


@pytest.mark.parametrize(
    "text",
    [
        "Đơn SL-A30001 mình chưa trả tiền, huỷ giúp mình",  # paying, not returning
        "Mình muốn trả góp đơn SL-123456",
        "Trả lời giúp mình đơn SL-123456 giao chưa",
        "Mình muốn trả hàng",  # no order named yet: the assistant asks which one
        "Hủy đơn SL-123456 giúp mình",
    ],
)
def test_other_messages_do_not(text: str) -> None:
    assert _return_keys(text) == []


def test_a_guest_without_the_eligibility_tool_gets_no_such_requirement() -> None:
    assert _return_keys("Mình muốn trả đơn SL-123456", ["lookup_order_guest", "search_policy", "escalate_to_human"]) == []


def _call(tool: str, result: dict, order: str = "SL-123456") -> dict:
    return {"tool": tool, "args": {"order_code": order}, "result": result}


def test_only_a_decision_for_that_order_settles_it() -> None:
    reqs = [r for r in derive_requirements("Mình muốn trả đơn SL-123456", CUSTOMER_TOOLS) if r.key.startswith("return:")]
    order = _call("get_order", {"order_code": "SL-123456", "items": []})
    policy = {"tool": "search_policy", "args": {"query": "đồ lót"}, "result": {"results": [{"clause_id": "POL-RETURN-03"}]}}
    assert unmet_requirements(reqs, [order, policy]) == reqs  # reading the order and the policy is not a decision
    assert unmet_requirements(reqs, [_call("check_return_eligibility", {"eligible": True}, order="SL-999999")]) == reqs

    refused = _call("check_return_eligibility", {"eligible": False, "decision_code": "CATEGORY_EXCLUDED", "clause_id": "POL-RETURN-03"})
    assert unmet_requirements(reqs, [refused]) == []
    ask = _call("check_return_eligibility", {"error_code": "MISSING_SLOT", "slot": "items", "message": "Hỏi khách món nào"})
    assert unmet_requirements(reqs, [ask]) == []  # the slot guard asks the customer: no loop of forced calls
    failed = _call("check_return_eligibility", {"error_code": "DB_ERROR", "message": "lỗi"})
    assert unmet_requirements(reqs, [failed]) == reqs


class _FakeEligibility:
    def __init__(self, verdict: dict) -> None:
        self.verdict, self.calls = verdict, []

    async def ainvoke(self, args: dict) -> dict:
        self.calls.append(args)
        return self.verdict


_MISSING_REASON = {"error_code": "MISSING_SLOT", "slot": "reason_code", "message": "Hãy hỏi khách lý do."}
_ARGS = {"order_code": "SL-400002", "item_ids": ["i1"], "reason_code": "change_of_mind", "reason_quote": "xả kho"}


@pytest.mark.asyncio
async def test_a_missing_reason_carries_the_domain_refusal_of_a_change_of_mind() -> None:
    from shopagent.agent.nodes.agent import _with_change_of_mind_verdict

    tool = _FakeEligibility({"eligible": False, "decision_code": "CATEGORY_EXCLUDED", "clause_id": "POL-RETURN-03", "message": "Hàng xả kho"})
    result = await _with_change_of_mind_verdict(dict(_MISSING_REASON), "check_return_eligibility", dict(_ARGS), tool, 5.0)

    assert result["error_code"] == "MISSING_SLOT" and result["slot"] == "reason_code"  # the customer is still asked
    assert result["if_change_of_mind"]["clause_id"] == "POL-RETURN-03"
    assert tool.calls == [{"order_code": "SL-400002", "item_ids": ["i1"], "reason_code": "change_of_mind"}]


@pytest.mark.asyncio
async def test_nothing_is_added_when_a_change_of_mind_is_allowed_or_another_slot_is_missing() -> None:
    from shopagent.agent.nodes.agent import _with_change_of_mind_verdict

    allowed = _FakeEligibility({"eligible": True})
    assert await _with_change_of_mind_verdict(dict(_MISSING_REASON), "check_return_eligibility", dict(_ARGS), allowed, 5.0) == _MISSING_REASON
    other = {"error_code": "MISSING_SLOT", "slot": "item_ids", "message": "Hỏi món nào"}
    untouched = _FakeEligibility({"eligible": False})
    assert await _with_change_of_mind_verdict(dict(other), "check_return_eligibility", dict(_ARGS), untouched, 5.0) == other
    assert untouched.calls == []
