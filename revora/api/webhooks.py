"""The inbound webhook route. The only unauthenticated endpoint in the system.

``POST /webhooks/razorpay/{merchant_slug}``. The handler is deliberately thin: it
reads the raw body, resolves the merchant, loads that merchant's configuration, and
hands everything to ``ingestion.ingest_webhook``, which owns the verify → canonicalize
→ dedup → enqueue sequence. The route's own responsibilities are the three things
that must happen at the HTTP boundary and nowhere else.

**Read the exact bytes.** ``await request.body()`` returns the raw body, and those
bytes are what the signature is verified against. No Pydantic model binds the body,
because binding it would re-serialize it and a re-serialized JSON string does not
match the provider's HMAC (the Signature Verification finding).

**Disclose nothing on failure.** Every non-success answer is a bare status code. An
unknown merchant slug is answered exactly like a bad signature — 401, no body —
because an unauthenticated endpoint must not be an oracle for which slugs exist.

**Answer, don't work.** No detection runs on this path. The handler does at most one
insert and one enqueue, then returns inside the acknowledgement budget; everything
after that is a job.

**A store that cannot be reached answers 503, not 500** (R16.C3). The distinction matters
because the whole recovery path for an ingest failure is the provider redelivering, and a
retry is only useful against a transient fault. See the handler for why the catch is narrow.

**The blocking work runs in a worker thread.** The handler has to be ``async def`` — it awaits
``request.body()`` to get the exact bytes — but everything after that is synchronous SQLAlchemy, and
doing it inline holds the event loop for the whole of three round trips. Under any concurrency that
makes the acknowledgement budget a function of how many other webhooks are in flight, which is the
one thing ``INGEST_ACK_TIMEOUT`` is supposed not to be. ``run_in_threadpool`` moves it off the loop.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Request, Response
from sqlalchemy.exc import InterfaceError, OperationalError
from starlette.concurrency import run_in_threadpool

from revora.ingestion import (
    EVENT_ID_HEADER,
    SIGNATURE_HEADER,
    IngestionOutcome,
    IngestionResult,
    ingest_webhook,
)
from revora.persistence.repositories.config import ConfigurationRepository
from revora.persistence.repositories.session import tenant_transaction, transaction
from revora.persistence.repositories.tenancy import merchant_by_slug
from revora.platform.logging import correlation_context, get_logger

__all__ = ["router"]

router = APIRouter(tags=["webhooks"])

_logger = get_logger(__name__)


@router.post("/webhooks/razorpay/{merchant_slug}")
async def razorpay_webhook(merchant_slug: str, request: Request) -> Response:
    """Receive, verify, and durably record one Razorpay webhook delivery."""
    body = await request.body()
    signature = request.headers.get(SIGNATURE_HEADER)
    event_id = request.headers.get(EVENT_ID_HEADER)

    with correlation_context() as correlation:
        correlation_id = uuid.UUID(correlation)
        try:
            result = await run_in_threadpool(
                _ingest, merchant_slug, body, signature, event_id, correlation_id
            )
        except (OperationalError, InterfaceError):
            # R16.C3: the store is unreachable, so answer 503 and persist nothing. Nothing was
            # persisted by construction — every write on this path is inside a transaction that
            # could not commit — so the only thing to get right is the status code.
            #
            # 503 rather than 500, and the two are not interchangeable here even though both are
            # 5xx. Revora depends on the provider redelivering (R16.C3 marks that dependency an
            # [ASSUMPTION]), and a retry only helps against a transient condition. 503 says "come
            # back"; 500 says "this request is broken", and an operator seeing 500 goes looking for
            # a bug in a payload that was fine.
            #
            # Narrow on purpose. ``OperationalError`` and ``InterfaceError`` are the
            # connection-level failures — server unreachable, connection dropped mid-statement.
            # A bad-SQL defect is deliberately *not* caught: it will fail identically on every
            # redelivery, and dressing it as 503 would have the provider retry a deterministic
            # failure until it gave up and then drop the event silently.
            _logger.error("webhook ingest failed: persistence unavailable")
            return Response(status_code=503)

    if result is None:
        return Response(status_code=401)
    if result.outcome is IngestionOutcome.ACCEPTED:
        _logger.info("webhook accepted", webhook_event_id=str(result.webhook_event_id))
    return Response(status_code=result.http_status)


def _ingest(
    merchant_slug: str,
    body: bytes,
    signature: str | None,
    event_id: str | None,
    correlation_id: uuid.UUID,
) -> IngestionResult | None:
    """The blocking half: resolve the merchant, load configuration, ingest.

    ``None`` means the slug is unknown, which the caller answers 401 — the same answer as a bad
    signature, because an unauthenticated endpoint must not be an oracle for which slugs exist.

    Runs in a worker thread. Every call inside is synchronous, and the correlation id is passed
    explicitly rather than read from the ambient context because a thread does not inherit the
    caller's ``ContextVar`` binding — a detail that would otherwise show up as audit records with
    unrelated correlation ids, which is the hardest kind of trail bug to notice.
    """
    # Resolve the merchant in its own transaction. Extract the id and slug while the row is live —
    # the commit expires ORM attributes, so nothing downstream touches a detached instance.
    with transaction() as session:
        merchant = merchant_by_slug(session, merchant_slug)
        if merchant is None:
            _logger.warning("webhook for unknown merchant slug")
            return None
        merchant_id = merchant.id
        resolved_slug = str(merchant.slug)

    with tenant_transaction(merchant_id) as session:
        config = ConfigurationRepository(session).load(merchant_id)

    return ingest_webhook(
        merchant_id,
        resolved_slug,
        body=body,
        provided_signature=signature,
        provider_event_id=event_id,
        config=config,
        correlation_id=correlation_id,
    )
