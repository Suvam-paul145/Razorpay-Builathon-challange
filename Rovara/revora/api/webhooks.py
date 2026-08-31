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
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Request, Response

from revora.ingestion import (
    EVENT_ID_HEADER,
    SIGNATURE_HEADER,
    IngestionOutcome,
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

        # Resolve the merchant in its own transaction. Extract the id and slug while
        # the row is live — the commit expires ORM attributes, so nothing downstream
        # touches a detached instance.
        with transaction() as session:
            merchant = merchant_by_slug(session, merchant_slug)
            if merchant is None:
                _logger.warning("webhook for unknown merchant slug")
                return Response(status_code=401)
            merchant_id = merchant.id
            resolved_slug = merchant.slug

        with tenant_transaction(merchant_id) as session:
            config = ConfigurationRepository(session).load(merchant_id)

        result = ingest_webhook(
            merchant_id,
            resolved_slug,
            body=body,
            provided_signature=signature,
            provider_event_id=event_id,
            config=config,
            correlation_id=correlation_id,
        )

    if result.outcome is IngestionOutcome.ACCEPTED:
        _logger.info("webhook accepted", webhook_event_id=str(result.webhook_event_id))
    return Response(status_code=result.http_status)
