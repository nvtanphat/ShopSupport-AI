"""Refunds of cancelled orders reach the customer through the outbox, as the worker runs it (OutboxDispatcher)."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from shopagent.core.clock import Clock, get_clock, set_global_clock
from shopagent.core.principal import Principal, PrincipalRole
from shopagent.domain.seeder import generate_seed_data
from shopagent.domain.services.actions import ActionService
from shopagent.domain.services.approvals import ApprovalService
from shopagent.domain.services.cancellations import CancellationService
from shopagent.infra.db.tables import (
    ApprovalRequestModel,
    Base,
    OrderModel,
    OutboxEventModel,
    RefundModel,
)
from shopagent.infra.outbox.dispatcher import OutboxDispatcher
from shopagent.infra.outbox.handlers import MAX_PAYOUT_ATTEMPTS
from shopagent.infra.payments.simulated import get_simulated_gateway
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

NOW = datetime(2026, 11, 11, 9, 0, tzinfo=ZoneInfo("Asia/Ho_Chi_Minh"))
CUSTOMER1 = Principal.customer(uuid.UUID(int=1001))
AGENT = Principal.staff(staff_id=uuid.uuid4(), role=PrincipalRole.STAFF_AGENT)


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
    yield factory
    await engine.dispose()
    set_global_clock(previous)


async def _confirm_cancellation(factory: Any, order_code: str) -> None:
    async with factory() as session:
        card = await CancellationService(session).propose_cancellation(CUSTOMER1, uuid.uuid4(), order_code)
        await session.commit()
        await ActionService(session).confirm_action(CUSTOMER1, uuid.UUID(card["action_id"]), card["preview_hash"])
        await session.commit()


async def _drain(factory: Any) -> None:
    dispatcher = OutboxDispatcher(session_factory=factory)
    for _ in range(10):
        if await dispatcher.run_once() == 0:
            return
    raise AssertionError("the outbox did not drain")


async def _order_and_refunds(factory: Any, order_code: str) -> tuple[OrderModel, list[RefundModel]]:
    async with factory() as session:
        order = (await session.execute(select(OrderModel).where(OrderModel.code == order_code))).scalar_one()
        refunds = list((await session.execute(select(RefundModel).where(RefundModel.order_id == order.id))).scalars())
    return order, refunds


@pytest.mark.asyncio
async def test_a_self_service_cancellation_of_a_paid_order_is_refunded(shop) -> None:  # type: ignore[no-untyped-def]
    await _confirm_cancellation(shop, "SL-DEMO02")
    await _drain(shop)

    order, refunds = await _order_and_refunds(shop, "SL-DEMO02")
    assert order.status == "cancelled"
    assert [(r.status, r.amount) for r in refunds] == [("succeeded", order.total)]
    assert order.payment_status == "refunded"


@pytest.mark.asyncio
async def test_a_cancellation_approved_by_staff_is_refunded(shop) -> None:  # type: ignore[no-untyped-def]
    await _confirm_cancellation(shop, "SL-DEMO03")  # packing: the customer's confirmation asks staff to approve
    async with shop() as session:
        approval = (
            await session.execute(select(ApprovalRequestModel).where(ApprovalRequestModel.subject_type == "cancellation"))
        ).scalar_one()
        await ApprovalService(session).decide_approval(AGENT, approval.id, "approved")
        await session.commit()
    await _drain(shop)

    order, refunds = await _order_and_refunds(shop, "SL-DEMO03")
    assert order.status == "cancelled"
    assert [(r.status, r.amount) for r in refunds] == [("succeeded", order.total)]
    assert order.payment_status == "refunded"


@pytest.mark.asyncio
async def test_a_rejected_cancellation_keeps_the_order_and_the_payment(shop) -> None:  # type: ignore[no-untyped-def]
    await _confirm_cancellation(shop, "SL-DEMO03")
    async with shop() as session:
        approval = (await session.execute(select(ApprovalRequestModel))).scalars().first()
        assert approval is not None
        await ApprovalService(session).decide_approval(AGENT, approval.id, "rejected")
        await session.commit()
    await _drain(shop)

    order, refunds = await _order_and_refunds(shop, "SL-DEMO03")
    assert order.status == "packing" and order.payment_status == "paid" and refunds == []


@pytest.mark.asyncio
async def test_events_without_a_handler_are_drained_not_replayed(shop) -> None:  # type: ignore[no-untyped-def]
    async with shop() as session:
        for n in range(3):
            session.add(
                OutboxEventModel(
                    id=uuid.uuid4(), type="ticket.created", payload={}, status="pending",
                    idempotency_key=f"no-handler-{n}", next_attempt_at=get_clock().now() - timedelta(minutes=1),
                )
            )
        await session.commit()
    dispatcher = OutboxDispatcher(session_factory=shop)

    assert await dispatcher.run_once() == 3
    assert await dispatcher.run_once() == 0  # stored as published, so the worker does not spin on them


@pytest.mark.asyncio
async def test_a_refused_payout_is_tried_again_and_then_paid(shop) -> None:  # type: ignore[no-untyped-def]
    gateway = get_simulated_gateway()
    gateway.inject_failure_next("Simulated gateway timeout")
    try:
        await _confirm_cancellation(shop, "SL-DEMO02")
        await _drain(shop)
        order, refunds = await _order_and_refunds(shop, "SL-DEMO02")
        assert [r.status for r in refunds] == ["failed"] and order.payment_status == "paid"

        get_clock().set_fixed_now(NOW + timedelta(minutes=5))  # past the retry delay
        await _drain(shop)
    finally:
        gateway.clear_failure()
        get_clock().set_fixed_now(NOW)

    order, refunds = await _order_and_refunds(shop, "SL-DEMO02")
    assert [(r.status, r.attempts) for r in refunds] == [("succeeded", 2)]
    assert order.payment_status == "refunded"


@pytest.mark.asyncio
async def test_a_payout_refused_every_time_stops_after_the_last_attempt(shop, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
    gateway = get_simulated_gateway()
    real_refund = gateway.refund

    async def always_refuse(**kwargs: Any) -> Any:
        gateway.inject_failure_next("Simulated gateway outage")
        return await real_refund(**kwargs)

    monkeypatch.setattr(gateway, "refund", always_refuse)
    try:
        await _confirm_cancellation(shop, "SL-DEMO02")
        for minutes in (0, 10, 30, 60, 120):
            get_clock().set_fixed_now(NOW + timedelta(minutes=minutes))
            await _drain(shop)
    finally:
        gateway.clear_failure()
        get_clock().set_fixed_now(NOW)

    order, refunds = await _order_and_refunds(shop, "SL-DEMO02")
    assert [(r.status, r.attempts) for r in refunds] == [("failed", MAX_PAYOUT_ATTEMPTS)]
    assert order.payment_status == "paid"
    async with shop() as session:
        waiting = (await session.execute(select(OutboxEventModel).where(OutboxEventModel.status == "pending"))).scalars().all()
    assert waiting == []  # no attempt is left queued
