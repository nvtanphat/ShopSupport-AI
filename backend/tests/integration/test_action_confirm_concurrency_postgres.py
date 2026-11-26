"""Confirming one proposal several times at once, on a real PostgreSQL server.

A double click, two open tabs or a client retry send the same `POST /actions/{id}/confirm` twice. Exactly one of
them may carry out the action: one cancellation with one refund, or one return request. The others must be refused
with a conflict, not run a second time. SQLite serialises writes, so only PostgreSQL shows a race.

Runs only when TEST_POSTGRES_URL points at a disposable PostgreSQL database; otherwise it is reported as skipped.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from httpx import ASGITransport, AsyncClient
from shopagent.api.app import app
from shopagent.api.deps import get_db_session
from shopagent.core.clock import Clock, get_clock, set_global_clock
from shopagent.core.principal import Principal
from shopagent.core.security import create_access_token
from shopagent.domain.seeder import generate_seed_data
from shopagent.domain.services.cancellations import CancellationService
from shopagent.domain.services.returns import ReturnService
from shopagent.infra.db.repositories.order_repo import OrderRepository
from shopagent.infra.db.tables import (
    ApprovalRequestModel,
    Base,
    OrderModel,
    RefundModel,
    ReturnRequestModel,
    StaffUserModel,
)
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

POSTGRES_URL = os.environ.get("TEST_POSTGRES_URL", "")

pytestmark = pytest.mark.skipif(not POSTGRES_URL, reason="TEST_POSTGRES_URL not set: PostgreSQL concurrency tests not run")

NOW = datetime(2026, 11, 11, 9, 0, tzinfo=ZoneInfo("Asia/Ho_Chi_Minh"))
CUSTOMER1 = uuid.UUID(int=1001)
PARALLEL = 6


def _async_url(url: str) -> str:
    return url.replace("postgresql://", "postgresql+psycopg://", 1) if url.startswith("postgresql://") else url


@pytest.fixture
async def shop():  # type: ignore[no-untyped-def]
    previous = get_clock()
    set_global_clock(Clock(fixed_now=NOW))
    schema = f"confirm_{uuid.uuid4().hex[:8]}"
    admin = create_async_engine(_async_url(POSTGRES_URL))
    async with admin.begin() as conn:
        await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_async_engine(_async_url(POSTGRES_URL), connect_args={"options": f"-csearch_path={schema}"}, pool_size=PARALLEL + 2)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with factory() as session:
        await generate_seed_data(session, now=NOW, num_customers=10, num_products=10, num_orders=40)

    async def override():  # type: ignore[no-untyped-def]
        async with factory() as session:
            yield session

    app.dependency_overrides[get_db_session] = override
    token = create_access_token({"sub": str(CUSTOMER1), "email": "customer1@example.com", "role": "customer"})
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers={"Authorization": f"Bearer {token}"}) as client:
        yield client, factory
    app.dependency_overrides.clear()
    await engine.dispose()
    async with admin.begin() as conn:
        await conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
    await admin.dispose()
    set_global_clock(previous)


async def _confirm_at_once(client: AsyncClient, card: dict) -> list[int]:
    body = {"preview_hash": card["preview_hash"]}
    responses = await asyncio.gather(*(client.post(f"/actions/{card['action_id']}/confirm", json=body) for _ in range(PARALLEL)))
    return sorted(r.status_code for r in responses)


@pytest.mark.asyncio
async def test_a_cancellation_confirmed_at_once_runs_once_with_one_refund(shop) -> None:  # type: ignore[no-untyped-def]
    client, factory = shop
    async with factory() as session:
        card = await CancellationService(session).propose_cancellation(Principal.customer(CUSTOMER1), uuid.uuid4(), "SL-DEMO02")
        await session.commit()

    codes = await _confirm_at_once(client, card)

    assert codes == [200] + [409] * (PARALLEL - 1)
    async with factory() as session:
        order = (await session.execute(select(OrderModel).where(OrderModel.code == "SL-DEMO02"))).scalar_one()
        refunds = (await session.execute(select(func.count()).select_from(RefundModel).where(RefundModel.order_id == order.id))).scalar_one()
    assert order.status == "cancelled"
    assert refunds == 1


@pytest.mark.asyncio
async def test_a_return_confirmed_at_once_opens_one_return_request(shop) -> None:  # type: ignore[no-untyped-def]
    client, factory = shop
    async with factory() as session:
        order = await OrderRepository(session).get_by_code("SL-DEMO06")
        items = [{"item_id": str(order.items[0].id), "qty": 1}]
        card = await ReturnService(session).propose_return(
            principal=Principal.customer(CUSTOMER1), conversation_id=uuid.uuid4(), order_code="SL-DEMO06", items=items, reason_code="change_of_mind"
        )
        await session.commit()

    codes = await _confirm_at_once(client, card)

    assert codes == [200] + [409] * (PARALLEL - 1)
    async with factory() as session:
        returns = (await session.execute(select(func.count()).select_from(ReturnRequestModel).where(ReturnRequestModel.order_id == order.id))).scalar_one()
    assert returns == 1


@pytest.mark.asyncio
async def test_two_proposals_for_one_order_confirmed_at_once_cancel_and_refund_once(shop) -> None:  # type: ignore[no-untyped-def]
    client, factory = shop
    async with factory() as session:  # the same cancellation proposed in two conversations: two separate cards
        cards = [
            await CancellationService(session).propose_cancellation(Principal.customer(CUSTOMER1), uuid.uuid4(), "SL-DEMO02")
            for _ in range(2)
        ]
        await session.commit()
    assert cards[0]["action_id"] != cards[1]["action_id"]

    responses = await asyncio.gather(
        *(client.post(f"/actions/{c['action_id']}/confirm", json={"preview_hash": c["preview_hash"]}) for c in cards)
    )

    assert sorted(r.status_code for r in responses) == [200, 409]
    async with factory() as session:
        order = (await session.execute(select(OrderModel).where(OrderModel.code == "SL-DEMO02"))).scalar_one()
        refunds = (await session.execute(select(func.count()).select_from(RefundModel).where(RefundModel.order_id == order.id))).scalar_one()
    assert order.status == "cancelled"
    assert refunds == 1


@pytest.mark.asyncio
async def test_two_staff_deciding_one_cancellation_at_once_apply_one_decision(shop) -> None:  # type: ignore[no-untyped-def]
    client, factory = shop
    async with factory() as session:  # SL-DEMO03 is packing: the customer's confirmation asks staff to approve
        card = await CancellationService(session).propose_cancellation(Principal.customer(CUSTOMER1), uuid.uuid4(), "SL-DEMO03")
        await session.commit()
    assert (await client.post(f"/actions/{card['action_id']}/confirm", json={"preview_hash": card["preview_hash"]})).status_code == 200
    async with factory() as session:
        approval_id = (await session.execute(select(ApprovalRequestModel.id).where(ApprovalRequestModel.subject_type == "cancellation"))).scalar_one()
        staff_ids = list((await session.execute(select(StaffUserModel.id).order_by(StaffUserModel.email).limit(2))).scalars())
    assert len(staff_ids) == 2

    def staff(decision: str) -> Any:
        sub = staff_ids[0] if decision == "approve" else staff_ids[1]
        token = create_access_token({"sub": str(sub), "email": "staff@shop.vn", "role": "supervisor"})
        return client.post(
            f"/staff/approvals/{approval_id}/decision",
            json={"decision": decision, "note": "kiểm tra đồng thời"},
            headers={"Authorization": f"Bearer {token}"},
        )

    responses = await asyncio.gather(staff("approve"), staff("reject"))

    assert sorted(r.status_code for r in responses) == [200, 409]
    async with factory() as session:
        order = (await session.execute(select(OrderModel).where(OrderModel.code == "SL-DEMO03"))).scalar_one()
        refunds = (await session.execute(select(func.count()).select_from(RefundModel).where(RefundModel.order_id == order.id))).scalar_one()
    approved = next(r for r in responses if r.status_code == 200).json()["status"] == "approved"
    assert (order.status, refunds) == (("cancelled", 1) if approved else ("packing", 0))
