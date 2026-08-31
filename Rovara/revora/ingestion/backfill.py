"""The detection-gap backfill. What stops a webhook outage from being invisible.

Razorpay disables a webhook endpoint after sustained delivery failure. When that happens
Revora does not degrade — it stops detecting anything at all, silently, while continuing to
report healthy numbers, because a system whose only input is webhooks cannot tell "no
failures happened" from "no events arrived". That is the worst failure mode this design has,
and it is not hypothetical: it is documented provider behaviour.

So this job asks the provider directly. It lists payments over a lookback window and ingests
any ``failed`` payment that has no persisted event, through **the same** canonicalization and
detection path a webhook takes. That reuse is the whole design:

* The synthetic ``provider_event_id`` is ``backfill:<payment_id>:<status>``, which lands on
  the same ``UNIQUE (merchant_id, provider_event_id)`` index. A payment that already arrived
  by webhook is skipped by a *different* key, so the dedup index alone cannot catch it —
  which is why the backfill checks for an existing event by payment id first, and why the
  index is still the backstop if that check races.
* Detection runs unchanged, so a backfilled payment is diagnosed, priced and policy-checked
  exactly like a pushed one. A separate path here would be a second way to create a case, and
  therefore a second way to create two.

**The windows overlap deliberately.** The provider does not document whether ``from``/``to``
filter on creation or last update, nor whether the bounds are inclusive. Rather than assume a
clean partition, each run looks back further than the interval between runs, so a payment near
a boundary is seen twice instead of risking being seen never. Seeing it twice is free — the
dedup index makes re-ingestion a no-op — while missing it is exactly the gap being closed.

**What a backfilled case can and cannot do.** It is detected, diagnosed and visible. Whether
it can be *acted* on depends on whether the read carried ``contact``; the design permits
cleartext contact only inside the encrypted raw event store, so the backfill writes the
synthetic payload there and nowhere else. A payment whose read carried no contact produces a
case the execution engine will refuse with ``CONTACT_UNAVAILABLE`` — an audited refusal, not
a silent failure. Detection is the point regardless: a merchant seeing an unactionable case is
strictly better off than not knowing the payment failed.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from revora.domain.payment_event import EventName, PaymentStatus
from revora.ingestion.canonical import CanonicalizationError, canonicalize
from revora.ingestion.service import IngestionOutcome, persist_and_enqueue
from revora.persistence.repositories.cases import WebhookEventRepository
from revora.persistence.repositories.session import tenant_transaction
from revora.platform.clock import now
from revora.platform.config import default_configuration
from revora.platform.logging import get_logger
from revora.providers.classification import Success
from revora.providers.razorpay import MAX_PAYMENTS_PAGE_SIZE, MIN_PAYMENT_WINDOW_TS

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sqlalchemy.orm import Session, sessionmaker

    from revora.platform.config import Configuration
    from revora.providers.classification import PaymentEntity
    from revora.providers.razorpay import PaymentProviderClient

__all__ = [
    "BACKFILL_EVENT_ID_PREFIX",
    "DEFAULT_LOOKBACK",
    "MAX_BACKFILL_PAGES",
    "WINDOW_OVERLAP",
    "BackfillReport",
    "backfill_detection_gap",
    "backfill_event_id",
]

_logger = get_logger(__name__)

BACKFILL_EVENT_ID_PREFIX = "backfill"
"""Namespace on the synthetic event id, so a backfilled event is distinguishable from a
delivered one in the dedup index and in any audit query. An operator asking "what did we only
find because webhooks were down?" gets an answer from a prefix match."""

DEFAULT_LOOKBACK = timedelta(hours=26)
"""How far back a run looks.

Longer than the 24 hours of sustained failure that disables a webhook, so a single run after
the maximum silent outage still covers the whole of it. Two hours of margin rather than
minutes because the run itself may be delayed by the outage that caused it."""

WINDOW_OVERLAP = timedelta(minutes=15)
"""Extra reach past the lookback, absorbing boundary ambiguity.

The provider does not document whether the window filters on creation or update, nor whether
the bounds are inclusive. Overlapping makes both questions moot: a payment near an edge is
seen twice, and twice is free."""

MAX_BACKFILL_PAGES = 200
"""Page ceiling per run, so one call cannot become an unbounded scan. At 100 records a page
this covers 20,000 payments in a window — well past any plausible MVP-scale outage, and a run
that hits the ceiling logs loudly rather than silently truncating."""


def backfill_event_id(payment_id: str, status: str) -> str:
    """The synthetic ``provider_event_id`` for a backfilled payment.

    ``backfill:<payment_id>:<status>`` exactly as specified. The status is part of the key on
    purpose: a payment observed ``failed`` and later observed ``captured`` are two distinct
    facts about it, and collapsing them would make the second unrecordable.
    """
    return f"{BACKFILL_EVENT_ID_PREFIX}:{payment_id}:{status}"


@dataclass(slots=True)
class BackfillReport:
    """What one run found and did. Every count is a number an operator would ask for."""

    window_start: datetime
    window_end: datetime
    pages_read: int = 0
    payments_seen: int = 0
    failed_seen: int = 0
    already_present: int = 0
    ingested: int = 0
    duplicates: int = 0
    unparseable: int = 0
    read_failures: int = 0
    truncated: bool = False
    ingested_payment_ids: list[str] = field(default_factory=list)

    @property
    def gap_closed(self) -> int:
        """How many failed payments Revora would otherwise never have known about.

        The number that justifies this job existing. If it is persistently non-zero, webhook
        delivery is broken and the backfill is the only thing detecting anything.
        """
        return self.ingested


def backfill_detection_gap(
    merchant_id: uuid.UUID,
    *,
    provider: PaymentProviderClient,
    factory: sessionmaker[Session] | None = None,
    config: Configuration | None = None,
    lookback: timedelta = DEFAULT_LOOKBACK,
    correlation_id: uuid.UUID | None = None,
    moment: datetime | None = None,
) -> BackfillReport:
    """List provider payments over a lookback window and ingest the failures we missed.

    Idempotent by construction. Running it twice over the same window ingests nothing the
    second time, because both the pre-check by payment id and the dedup index reject the
    repeat — so a scheduler that double-fires, or an operator who runs it manually during an
    incident, costs nothing but API calls.

    Args:
        lookback: how far back to look. Widened by :data:`WINDOW_OVERLAP` internally.
    """
    configuration = config or default_configuration()
    correlation = correlation_id or uuid.uuid4()
    end = moment or now()
    start = end - lookback - WINDOW_OVERLAP

    from_ts = max(int(start.timestamp()), MIN_PAYMENT_WINDOW_TS)
    to_ts = int(end.timestamp())

    report = BackfillReport(window_start=start, window_end=end)
    skip = 0

    while report.pages_read < MAX_BACKFILL_PAGES:
        result = provider.list_payments(
            from_ts=from_ts, to_ts=to_ts, count=MAX_PAYMENTS_PAGE_SIZE, skip=skip
        )
        if not isinstance(result, Success):
            # A read failure is not an empty window. Stop and report it; the next scheduled
            # run covers the same period because the windows overlap, so nothing is lost by
            # giving up here — whereas treating it as "nothing to do" would leave the gap
            # open while reporting success.
            report.read_failures += 1
            _logger.warning(
                "backfill listing read failed",
                merchant_id=str(merchant_id),
                skip=skip,
                classification=type(result).__name__,
            )
            break

        report.pages_read += 1
        page = result.entity
        report.payments_seen += len(page.payments)

        for payment in page.failed_payments():
            report.failed_seen += 1
            _ingest_one(
                merchant_id,
                payment,
                report=report,
                config=configuration,
                correlation_id=correlation,
                factory=factory,
                moment=end,
            )

        if len(page.payments) < MAX_PAYMENTS_PAGE_SIZE:
            break
        skip += MAX_PAYMENTS_PAGE_SIZE
    else:
        report.truncated = True
        _logger.error(
            "backfill hit its page ceiling; the window was not fully scanned",
            merchant_id=str(merchant_id),
            pages=report.pages_read,
        )

    if report.ingested:
        _logger.warning(
            "backfill closed a detection gap",
            merchant_id=str(merchant_id),
            ingested=report.ingested,
            failed_seen=report.failed_seen,
        )
    return report


def _ingest_one(
    merchant_id: uuid.UUID,
    payment: PaymentEntity,
    *,
    report: BackfillReport,
    config: Configuration,
    correlation_id: uuid.UUID,
    factory: sessionmaker[Session] | None,
    moment: datetime,
) -> None:
    """Ingest one missed payment, or account for why it was skipped."""
    # Checked by payment id, not by the synthetic event id. The dedup index keys on the
    # event id, and a payment delivered by webhook has a *provider* event id — so the index
    # alone would not recognise the backfill of an already-known payment. This is the check
    # that makes "already arrived by webhook" a no-op; the index remains the backstop for a
    # race between two backfill runs.
    with tenant_transaction(merchant_id, factory) as session:
        if _already_ingested(session, merchant_id, payment.id):
            report.already_present += 1
            return

    body = _synthetic_envelope(payment, moment=moment)

    try:
        canonical_result = canonicalize(
            body, disclosure_length=config.MASK_DISCLOSURE_LENGTH
        )
    except CanonicalizationError as exc:
        # Our own synthesized envelope failed our own canonicalizer. That is a bug here or
        # provider drift, never a merchant problem, so it is logged and counted rather than
        # quarantined against the merchant's record.
        report.unparseable += 1
        _logger.error(
            "backfill synthesized an envelope its own canonicalizer rejected",
            merchant_id=str(merchant_id),
            rule=exc.rule,
        )
        return

    outcome = persist_and_enqueue(
        merchant_id,
        provider_event_id=backfill_event_id(payment.id, payment.status),
        body=body,
        canonical_result=canonical_result,
        config=config,
        correlation_id=correlation_id,
        moment=moment,
        factory=factory,
    )

    if outcome.outcome is IngestionOutcome.ACCEPTED:
        report.ingested += 1
        report.ingested_payment_ids.append(payment.id)
    elif outcome.outcome is IngestionOutcome.DUPLICATE:
        report.duplicates += 1
    else:  # pragma: no cover - credential paths are exercised by ingestion's own tests
        report.unparseable += 1


def _already_ingested(
    session: Session, merchant_id: uuid.UUID, payment_id: str
) -> bool:
    """Whether any persisted event already refers to this payment.

    Reads the canonical column, which every event carries whichever route it arrived by, so
    one query answers for webhooks and earlier backfills alike.
    """
    return WebhookEventRepository(session).has_event_for_payment(merchant_id, payment_id)


def _synthetic_envelope(payment: PaymentEntity, *, moment: datetime) -> bytes:
    """Build a webhook-shaped body from an API read.

    The envelope wrapper is synthetic; every value inside it is the provider's own. That
    distinction is what makes this honest rather than fabrication — the backfill is not
    inventing a payment, it is re-shaping a payment the provider reported so it can travel the
    one code path that knows how to interpret it.

    ``contact`` and ``email`` are included when the read carried them, because the
    canonicalizer derives the non-reversible ``customer_key`` and the masked contact from
    exactly those fields, and ``recovery_case.customer_key`` is ``NOT NULL``. This body is
    encrypted before it is stored — it is the same encrypted raw payload a delivered webhook
    produces, which is the only place the design permits cleartext contact to live.
    """
    entity: dict[str, object] = {
        "id": payment.id,
        "entity": "payment",
        "status": payment.status,
        "amount": int(payment.amount),
        "amount_refunded": int(payment.amount_refunded),
        "captured": bool(payment.captured),
        "created_at": payment.created_at or int(moment.timestamp()),
    }
    for key, value in (
        ("currency", payment.currency),
        ("order_id", payment.order_id),
        ("method", payment.method),
        ("contact", payment.contact),
        ("email", payment.email),
        ("error_code", payment.error_code),
        ("error_description", payment.error_description),
        ("error_reason", payment.error_reason),
        ("error_source", payment.error_source),
        ("error_step", payment.error_step),
    ):
        if value is not None:
            entity[key] = value

    envelope = {
        # The event name a delivered webhook would have carried for this status, so
        # detection applies the same rules to both. Derived from the status rather than
        # hardcoded, so a backfill of a non-failed payment cannot masquerade as a failure.
        "event": _event_name_for(payment.status),
        "created_at": entity["created_at"],
        "payload": {"payment": {"entity": entity}},
    }
    # Separators without spaces and sorted keys: the body is encrypted and stored verbatim,
    # and a stable serialization means two backfills of one payment produce identical bytes.
    return json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode()


def _event_name_for(status: str) -> str:
    """The webhook event name corresponding to a payment status."""
    if status == PaymentStatus.CAPTURED.value:
        return EventName.PAYMENT_CAPTURED.value
    if status == PaymentStatus.AUTHORIZED.value:
        return EventName.PAYMENT_AUTHORIZED.value
    return EventName.PAYMENT_FAILED.value
