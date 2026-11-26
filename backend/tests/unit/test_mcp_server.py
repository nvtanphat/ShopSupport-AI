"""The MCP server: the agent's customer tools for MCP clients, with no way to change an order without the customer."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from mcp import Client
from shopagent.core.clock import Clock, get_clock, set_global_clock
from shopagent.core.principal import Principal
from shopagent.domain.seeder import generate_seed_data
from shopagent.domain.services.actions import ActionService
from shopagent.infra.db.repositories.order_repo import OrderRepository
from shopagent.infra.db.tables import Base, OrderModel, PendingActionModel, ReturnRequestModel
from shopagent.mcp_server.server import (
    EXPOSED_TOOLS,
    READ_ONLY_TOOLS,
    AuthenticationError,
    authenticate_customer,
    build_server,
    open_conversation,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

NOW = datetime(2026, 11, 11, 9, 0, tzinfo=ZoneInfo("Asia/Ho_Chi_Minh"))
CUSTOMER1 = Principal.customer(uuid.UUID(int=1001))


@pytest.fixture
async def shop():  # type: ignore[no-untyped-def]
    previous = get_clock()
    set_global_clock(Clock(fixed_now=NOW))
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with factory() as session:
        await generate_seed_data(session, now=NOW, num_customers=10, num_products=10, num_orders=40)
    conversation_id = await open_conversation(factory, CUSTOMER1)
    yield factory, build_server(factory, CUSTOMER1, conversation_id)
    await engine.dispose()
    set_global_clock(previous)


async def _orders(factory: Any) -> dict[str, tuple[str, str]]:
    async with factory() as session:
        rows = (await session.execute(select(OrderModel))).scalars().all()
    return {o.code: (o.status, o.payment_status) for o in rows}


async def _return_codes(factory: Any) -> set[str]:
    async with factory() as session:
        return set((await session.execute(select(ReturnRequestModel.code))).scalars().all())


async def _item_ids(factory: Any, order_code: str) -> list[str]:
    async with factory() as session:
        order = await OrderRepository(session).get_by_code(order_code)
        return [str(item.id) for item in order.items]


@pytest.mark.asyncio
async def test_lists_the_customer_tools_and_no_tool_that_confirms(shop) -> None:  # type: ignore[no-untyped-def]
    _, server = shop
    async with Client(server) as client:
        tools = (await client.list_tools()).tools
    names = {t.name for t in tools}
    assert names == EXPOSED_TOOLS
    assert not any("confirm" in n for n in names)
    assert "lookup_order_guest" not in names  # the guest OTP flow belongs to the web app
    for tool in tools:
        assert tool.annotations is not None and tool.annotations.destructive_hint is False
        assert tool.annotations.read_only_hint is (tool.name in READ_ONLY_TOOLS)
        assert tool.input_schema.get("type") == "object"


@pytest.mark.asyncio
async def test_a_cancellation_through_mcp_is_only_a_proposal_until_the_customer_confirms(shop) -> None:  # type: ignore[no-untyped-def]
    factory, server = shop
    before = await _orders(factory)
    async with Client(server) as client:
        result = await client.call_tool("propose_cancel_order", {"order_code": "SL-DEMO02"})
    assert result.is_error is False
    card = result.structured_content
    assert card is not None and card["type"] == "cancel_order" and card["preview_hash"]

    assert await _orders(factory) == before  # nothing changed: only a card exists
    async with factory() as session:
        action = await session.get(PendingActionModel, uuid.UUID(card["action_id"]))
    assert action is not None and action.status == "proposed"

    # The customer's own confirmation, which is what POST /actions/{id}/confirm runs, is what cancels the order.
    async with factory() as session:
        await ActionService(session).confirm_action(CUSTOMER1, uuid.UUID(card["action_id"]), card["preview_hash"])
        await session.commit()
    assert (await _orders(factory))["SL-DEMO02"][0] == "cancelled"


@pytest.mark.asyncio
async def test_no_tool_call_changes_an_order_or_creates_a_return(shop) -> None:  # type: ignore[no-untyped-def]
    factory, server = shop
    before = await _orders(factory)
    returns_before = await _return_codes(factory)
    demo06 = await _item_ids(factory, "SL-DEMO06")
    calls: list[tuple[str, dict[str, Any]]] = [
        ("search_policy", {"query": "hủy đơn đã thanh toán"}),
        ("list_my_orders", {"limit": 10}),
        ("get_order", {"order_code": "SL-DEMO02"}),
        ("track_shipment", {"order_code": "SL-DEMO04"}),
        ("check_return_eligibility", {"order_code": "SL-DEMO06", "item_ids": demo06, "reason_code": "change_of_mind"}),
        ("check_variant_stock", {"sku": "DEMO-JKT-M"}),
        ("get_case_status", {}),
        ("propose_cancel_order", {"order_code": "SL-DEMO01"}),
        ("propose_cancel_order", {"order_code": "SL-DEMO03"}),
        ("propose_return", {"order_code": "SL-DEMO06", "items": [{"item_id": demo06[0], "qty": 1}], "reason_code": "change_of_mind"}),
        ("escalate_to_human", {"reason_code": "customer_request", "summary": "Khách muốn nói chuyện với nhân viên"}),
    ]
    async with Client(server) as client:
        results = {name: await client.call_tool(name, arguments) for name, arguments in calls}
    # Only search_policy fails here: this test database has no policy index.
    assert [name for name, r in results.items() if r.is_error] == ["search_policy"]
    assert {name for name, _ in calls} == EXPOSED_TOOLS  # every tool was called
    assert await _orders(factory) == before
    assert await _return_codes(factory) == returns_before  # propose_return opened no return request
    async with factory() as session:
        proposals = (await session.execute(select(PendingActionModel.type, PendingActionModel.status))).all()
    # Three cards were made (two cancellations, one return), so the calls ran; none was carried out.
    assert sorted(kind for kind, _ in proposals) == ["cancel_order", "cancel_order", "return_request"]
    assert {status for _, status in proposals} == {"proposed"}


@pytest.mark.asyncio
async def test_another_customers_order_is_refused_without_a_proposal(shop) -> None:  # type: ignore[no-untyped-def]
    factory, server = shop
    async with factory() as session:
        other = (
            await session.execute(select(OrderModel.code).where(OrderModel.customer_id != CUSTOMER1.customer_id).limit(1))
        ).scalar_one()
    async with Client(server) as client:
        refused = await client.call_tool("propose_cancel_order", {"order_code": other})
        details = await client.call_tool("get_order", {"order_code": other})
    assert refused.is_error is True and details.is_error is True
    async with factory() as session:
        assert (await session.execute(select(PendingActionModel))).first() is None


@pytest.mark.asyncio
async def test_bad_arguments_and_unknown_tools_are_reported_as_errors(shop) -> None:  # type: ignore[no-untyped-def]
    _, server = shop
    async with Client(server) as client:
        bad = await client.call_tool("get_order", {"order_code": ""})
        unknown = await client.call_tool("confirm_action", {"action_id": str(uuid.uuid4())})
    assert bad.is_error is True and bad.structured_content and bad.structured_content["error_code"] == "INVALID_ARGUMENTS"
    assert unknown.is_error is True and unknown.structured_content and unknown.structured_content["error_code"] == "UNKNOWN_TOOL"


@pytest.mark.asyncio
async def test_only_a_customer_with_the_right_password_gets_a_server(shop) -> None:  # type: ignore[no-untyped-def]
    factory, _ = shop
    principal = await authenticate_customer(factory, "Customer1@example.com ", "pass123")
    assert principal.customer_id == CUSTOMER1.customer_id
    with pytest.raises(AuthenticationError):
        await authenticate_customer(factory, "customer1@example.com", "wrong")
    with pytest.raises(AuthenticationError):
        await authenticate_customer(factory, "nobody@example.com", "pass123")
    for not_a_customer in (Principal.guest(), Principal.staff(staff_id=uuid.uuid4())):
        with pytest.raises(AuthenticationError):
            build_server(factory, not_a_customer, uuid.uuid4())
