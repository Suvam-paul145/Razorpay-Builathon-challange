"""The scheduler: enqueue the periodic sweeps, each dedupe-keyed so it cannot double.

The worker moves work off the request path; the scheduler is what puts the periodic
work there in the first place. It enqueues, on an interval, the sweeps that keep the
system correct without depending on any single job having run: lifecycle evaluation,
execution reconciliation, payment-state reconciliation, detection-gap backfill, and
the calibration report trigger.

Every enqueue carries a dedupe key built from the sweep kind and the interval bucket,
so two overlapping schedulers — or one that restarts mid-tick — cannot enqueue the
same sweep twice. The key collides against ``one_pending_job_per_dedupe_key`` (pending
jobs only), so once a sweep has been claimed the next tick can enqueue it again.

Only the sweeps whose owners exist are enqueued for real work in this phase.
Lifecycle evaluation is live (task 11.4). The others are declared here with their kind
constants and are filled in by their owning tasks — execution reconciliation (20.5),
payment-state reconciliation (21.3), detection-gap backfill (22.1), calibration report
(15.5). Declaring the kinds here keeps the vocabulary in one place.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Final

from revora.jobs.queue import enqueue
from revora.persistence.repositories.session import tenant_transaction
from revora.platform.clock import now
from revora.platform.logging import get_logger

try:  # pragma: no cover - typing convenience only
    from sqlalchemy.orm import Session, sessionmaker
except ImportError:  # pragma: no cover
    Session = object  # type: ignore[assignment,misc]
    sessionmaker = object  # type: ignore[assignment,misc]

__all__ = [
    "CALIBRATION_REPORT_KIND",
    "DETECTION_GAP_BACKFILL_KIND",
    "EXECUTION_RECONCILIATION_KIND",
    "LIFECYCLE_EVALUATION_KIND",
    "PAYMENT_STATE_RECONCILIATION_KIND",
    "PERIODIC_SWEEP_KINDS",
    "enqueue_lifecycle_sweep",
]

_logger = get_logger(__name__)

LIFECYCLE_EVALUATION_KIND: Final[str] = "lifecycle_evaluation"
EXECUTION_RECONCILIATION_KIND: Final[str] = "execution_reconciliation"
PAYMENT_STATE_RECONCILIATION_KIND: Final[str] = "payment_state_reconciliation"
DETECTION_GAP_BACKFILL_KIND: Final[str] = "detection_gap_backfill"
CALIBRATION_REPORT_KIND: Final[str] = "calibration_report"

PERIODIC_SWEEP_KINDS: Final[tuple[str, ...]] = (
    LIFECYCLE_EVALUATION_KIND,
    EXECUTION_RECONCILIATION_KIND,
    PAYMENT_STATE_RECONCILIATION_KIND,
    DETECTION_GAP_BACKFILL_KIND,
    CALIBRATION_REPORT_KIND,
)
"""Every periodic sweep kind. The worker registers a handler for each; the ones whose
owners do not exist yet are registered as no-ops so a scheduled sweep completes rather
than dead-lettering."""


def enqueue_lifecycle_sweep(
    merchant_id: uuid.UUID,
    *,
    bucket_seconds: int,
    moment: datetime | None = None,
    factory: sessionmaker[Session] | None = None,
) -> uuid.UUID | None:
    """Enqueue one lifecycle-evaluation sweep for a merchant, dedupe-keyed by bucket.

    ``bucket_seconds`` quantizes the current time so a key repeats within one interval
    and changes across intervals — which is what makes the dedupe both prevent a
    double-enqueue this tick and permit a fresh enqueue next tick. Returns the job id,
    or ``None`` if this bucket's sweep is already pending.
    """
    moment = moment or now()
    bucket = int(moment.timestamp()) // max(1, bucket_seconds)
    with tenant_transaction(merchant_id, factory) as session:
        return enqueue(
            session,
            merchant_id,
            kind=LIFECYCLE_EVALUATION_KIND,
            payload={"bucket": bucket},
            run_after=moment,
            dedupe_key=f"{LIFECYCLE_EVALUATION_KIND}:{merchant_id}:{bucket}",
        )
