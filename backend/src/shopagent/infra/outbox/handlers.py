"""Outbox event handlers.

All handlers must be idempotent (at-least-once delivery semantics).
"""

from __future__ import annotations

import logging
import uuid
from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from shopagent.core.clock import get_clock
from shopagent.domain.models.enums import RefundStatus
from shopagent.domain.services.refunds import RefundService
from shopagent.infra.db.repositories.outbox_repo import OutboxRepository
from shopagent.infra.db.repositories.refund_repo import RefundRepository
from shopagent.infra.db.tables import OutboxEventModel
from shopagent.infra.payments.simulated import get_simulated_gateway

logger = logging.getLogger("shopagent.outbox.handlers")


# A payout the gateway refused is tried again later, with a new idempotency key per attempt (attempts is part of it).
MAX_PAYOUT_ATTEMPTS = 3
RETRY_DELAY_SECONDS = 60


async def _disburse(session: AsyncSession, refund_id: uuid.UUID) -> None:
    """Pay out one refund; if the gateway refuses, queue another attempt until MAX_PAYOUT_ATTEMPTS."""
    refund = await RefundService(session).process_disbursement(refund_id, get_simulated_gateway(), actor_id="outbox_worker")
    if refund.status == RefundStatus.FAILED.value and refund.attempts < MAX_PAYOUT_ATTEMPTS:
        await OutboxRepository(session).add_event(
            event_type="refund.disburse",
            payload={"refund_id": str(refund.id), "order_id": str(refund.order_id), "amount": refund.amount},
            idempotency_key=f"disburse_{refund.id}_retry_{refund.attempts}",
            next_attempt_at=get_clock().now() + timedelta(seconds=RETRY_DELAY_SECONDS * 2 ** (refund.attempts - 1)),
        )


async def _refund_in(session: AsyncSession, event: OutboxEventModel, statuses: set[str]) -> uuid.UUID | None:
    """The event's refund when it is in one of `statuses`; anything else was handled already (at-least-once delivery)."""
    refund_id_str = event.payload.get("refund_id") or event.payload.get("subject_id")
    if not refund_id_str:
        return None
    refund = await RefundRepository(session).get_by_id(uuid.UUID(str(refund_id_str)))
    return refund.id if refund and refund.status in statuses else None


async def handle_refund_created(session: AsyncSession, event: OutboxEventModel) -> None:
    """Pay out a refund created without an approval gate."""
    refund_id = await _refund_in(session, event, {RefundStatus.CREATED.value})
    if refund_id:
        await _disburse(session, refund_id)


async def handle_refund_disburse(session: AsyncSession, event: OutboxEventModel) -> None:
    """Pay out an approved refund (a cancelled order that was paid), or try a refused payout again."""
    refund_id = await _refund_in(session, event, {RefundStatus.APPROVED.value, RefundStatus.FAILED.value})
    if refund_id:
        await _disburse(session, refund_id)


async def handle_approval_decided(session: AsyncSession, event: OutboxEventModel) -> None:
    """Pay out a refund that staff approved."""
    if event.payload.get("subject_type") != "refund" or event.payload.get("decision") != "approved":
        return
    refund_id = await _refund_in(session, event, {RefundStatus.APPROVED.value})
    if refund_id:
        await _disburse(session, refund_id)


async def handle_return_created(session: AsyncSession, event: OutboxEventModel) -> None:
    """Handle post-return creation tasks (e.g. shipping labels or customer notification)."""
    return_code = event.payload.get("return_code")
    logger.info("Return request %s created successfully.", return_code)


async def handle_action_confirmed(session: AsyncSession, event: OutboxEventModel) -> None:
    action_id = event.payload.get("action_id")
    logger.info("Action %s confirmed.", action_id)


HANDLERS = {
    "refund.created": handle_refund_created,
    "refund.disburse": handle_refund_disburse,
    "approval.decided": handle_approval_decided,
    "return.created": handle_return_created,
    "action.confirmed": handle_action_confirmed,
}
