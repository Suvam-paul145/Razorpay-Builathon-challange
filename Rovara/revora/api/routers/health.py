"""Liveness, and the webhook health surface a disabled webhook would otherwise hide.

``GET /health`` is unauthenticated and says only whether the process can reach a correctly
migrated database. It deliberately reveals nothing else: a health endpoint that names the schema
revision, the build or the tenant count is a reconnaissance endpoint.

``GET /health/webhook`` requires a session and answers the one operational question no other
surface can. **A disabled webhook is total, silent detection loss.** Revenue at risk simply stops
appearing; every figure on every dashboard stays green because the numerator and the denominator
both go to zero together. The detection-gap backfill job exists to catch this, but a merchant needs
to see it too, and the thing to look at is *time since the last received event* — a figure that is
not a count of anything and therefore cannot be reassuring by accident.

No threshold is asserted here. A merchant with four failures a week and one with four hundred a day
have completely different normal silences, and a hard-coded "unhealthy after an hour" would either
cry wolf for the first or stay quiet for a day for the second. The endpoint reports the interval and
the counts; the interpretation belongs to whoever knows the traffic.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from revora.api.deps import TenantSession
from revora.persistence.models import WebhookEvent
from revora.persistence.repositories.engine import get_engine
from revora.persistence.repositories.schema import (
    EXPECTED_REVISION,
    SchemaRevisionMismatchError,
    verify_schema_revision,
)
from revora.persistence.repositories.session import tenant_transaction
from revora.platform.clock import now
from revora.platform.logging import get_logger

__all__ = ["router"]

_logger = get_logger(__name__)

router = APIRouter(tags=["health"])

_RECENT_WINDOW = timedelta(hours=24)
"""How far back the received-event count looks. A day, because that is also the provider's
redelivery window, so the count and any redelivery of it cover the same span."""


class HealthResponse(BaseModel):
    status: str


@router.get(
    "/health",
    response_model=HealthResponse,
    responses={503: {"description": "The database is unreachable or on the wrong revision."}},
)
def health() -> HealthResponse:
    """Liveness. ``ok`` or 503, and nothing else.

    Verifies the schema revision rather than merely opening a connection. A process serving against
    a schema it was not built for is the failure mode this check exists for — it does not look like
    a schema problem, it looks like a wrong number on a dashboard.
    """
    try:
        verify_schema_revision(get_engine(), expected=EXPECTED_REVISION)
    except (SchemaRevisionMismatchError, SQLAlchemyError, RuntimeError) as exc:
        # The reason is logged, not returned. An unauthenticated caller learning that the schema is
        # two revisions behind learns something about the deployment it has no business knowing.
        _logger.error("health check failed", error=type(exc).__name__)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="unavailable"
        ) from exc
    return HealthResponse(status="ok")


class WebhookHealthResponse(BaseModel):
    """Time since the last event, and the counts that put it in context."""

    last_event_at: str | None
    seconds_since_last_event: int | None
    events_last_24h: int
    verified_events_last_24h: int
    checked_at: str
    detail: str


@router.get("/health/webhook", response_model=WebhookHealthResponse)
def webhook_health(
    current: TenantSession,
) -> WebhookHealthResponse:
    """Time since this merchant's last received webhook event (R13.C7's operational sibling).

    ``None`` for both time fields means no event has *ever* been received, which is a different and
    much louder condition than a long silence: it means the webhook was never wired up, not that it
    stopped.
    """
    checked_at = now()
    since = checked_at - _RECENT_WINDOW

    with tenant_transaction(current.merchant_id) as session:
        newest: datetime | None = session.execute(
            select(func.max(WebhookEvent.received_at)).where(
                WebhookEvent.merchant_id == current.merchant_id
            )
        ).scalar_one_or_none()
        row = session.execute(
            select(
                func.count().label("total"),
                func.count()
                .filter(WebhookEvent.signature_verified)
                .label("verified"),
            )
            .select_from(WebhookEvent)
            .where(
                WebhookEvent.merchant_id == current.merchant_id,
                WebhookEvent.received_at >= since,
            )
        ).one()

    elapsed = None if newest is None else int((checked_at - newest).total_seconds())
    return WebhookHealthResponse(
        last_event_at=None if newest is None else newest.isoformat(),
        seconds_since_last_event=elapsed,
        events_last_24h=int(row.total),
        verified_events_last_24h=int(row.verified),
        checked_at=checked_at.isoformat(),
        detail=(
            "No webhook event has ever been received for this merchant. Detection is entirely "
            "dependent on the backfill job until the webhook is configured."
            if newest is None
            else (
                "A disabled webhook is silent total detection loss: every figure stays green "
                "because nothing arrives to count. Compare the interval below against this "
                "merchant's normal failure rate — no threshold is asserted here, because a "
                "normal silence differs by orders of magnitude between merchants."
            )
        ),
    )
