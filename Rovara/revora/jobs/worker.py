"""The worker loop: claim, bind context, dispatch by kind, complete or reschedule.

One loop, in the same image as the API (ADR-1), selected by ``REVORA_ROLE=worker``.
Each pass asks which merchants have work due — the one deliberately cross-tenant read
in the system, returning ids and nothing else — then, for each, claims and processes
jobs one at a time until that merchant's queue is drained.

Every claimed job runs with two ambient facts set: the correlation id, inherited from
the job payload so the audit trail of the async work joins the inbound event that
scheduled it (R11.C7); and the merchant binding, set by the handler's
``tenant_transaction`` so row-level security applies. Dispatch is by ``kind`` through a
static registry — a job whose kind has no handler is a bug, and it fails and
eventually dead-letters rather than being silently dropped.

Handlers are idempotent, so completion is at-least-once: on success the job is marked
done, on any exception it is rescheduled with backoff or dead-lettered past the cap. A
graceful stop is checked between jobs, so a clean shutdown never interrupts a job
mid-flight; a hard kill leaves the job ``RUNNING`` and the lease sweep reclaims it.
"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Callable
from typing import Final

from revora.cases.sweeper import sweep_expired_cases
from revora.detection.service import run_detection
from revora.ingestion.service import DETECTION_JOB_KIND
from revora.jobs.queue import ClaimedJob, claim_one, complete, fail
from revora.jobs.scheduler import (
    CALIBRATION_REPORT_KIND,
    DETECTION_GAP_BACKFILL_KIND,
    EXECUTION_RECONCILIATION_KIND,
    LIFECYCLE_EVALUATION_KIND,
    PAYMENT_STATE_RECONCILIATION_KIND,
)
from revora.persistence.repositories.config import ConfigurationRepository
from revora.persistence.repositories.jobs import claimable_merchant_ids
from revora.persistence.repositories.session import tenant_transaction, transaction
from revora.platform.clock import now
from revora.platform.logging import correlation_context, get_logger

try:  # pragma: no cover - typing convenience only
    from sqlalchemy.orm import Session as _Session
    from sqlalchemy.orm import sessionmaker
except ImportError:  # pragma: no cover
    sessionmaker = object  # type: ignore[assignment,misc]
    _Session = object  # type: ignore[assignment,misc]

__all__ = ["Handler", "build_registry", "run_forever", "run_once"]

_logger = get_logger(__name__)

Handler = Callable[[ClaimedJob], None]

_DEFAULT_MERCHANT_SCAN_LIMIT: Final[int] = 100
"""How many merchants one pass considers. Bounds the work per tick; the next pass
picks up any it did not reach."""


def _handle_detection(claimed: ClaimedJob) -> None:
    """Classify one persisted event and open or attach a case, in one transaction."""
    webhook_event_id = uuid.UUID(str(claimed.payload["webhook_event_id"]))
    with tenant_transaction(claimed.merchant_id) as session:
        config = ConfigurationRepository(session).load(claimed.merchant_id)
        run_detection(
            session,
            claimed.merchant_id,
            webhook_event_id,
            config,
            correlation_id=claimed.correlation_id,
        )


def _handle_lifecycle(claimed: ClaimedJob) -> None:
    """Expire every non-terminal case whose recovery window has closed."""
    sweep_expired_cases(claimed.merchant_id)


def _handle_not_yet_implemented(claimed: ClaimedJob) -> None:
    """A registered no-op for a sweep whose owner has not been built yet.

    Completing rather than failing keeps a scheduled sweep from dead-lettering before
    its owning task exists. Its owner replaces this handler when built.
    """
    _logger.debug("sweep handler not yet implemented; completing as no-op", kind=claimed.kind)


def build_registry() -> dict[str, Handler]:
    """The kind-to-handler map. One place, so a job kind cannot be dispatched two ways."""
    return {
        DETECTION_JOB_KIND: _handle_detection,
        LIFECYCLE_EVALUATION_KIND: _handle_lifecycle,
        EXECUTION_RECONCILIATION_KIND: _handle_not_yet_implemented,
        PAYMENT_STATE_RECONCILIATION_KIND: _handle_not_yet_implemented,
        DETECTION_GAP_BACKFILL_KIND: _handle_not_yet_implemented,
        CALIBRATION_REPORT_KIND: _handle_not_yet_implemented,
    }


def run_once(
    worker_id: str,
    *,
    registry: dict[str, Handler] | None = None,
    merchant_scan_limit: int = _DEFAULT_MERCHANT_SCAN_LIMIT,
    factory: sessionmaker[_Session] | None = None,
    stop: threading.Event | None = None,
) -> int:
    """One pass over all merchants with due work. Returns the number of jobs processed.

    Testable in isolation: a test enqueues jobs, calls this once, and asserts on the
    results, without a running loop.
    """
    handlers = registry if registry is not None else build_registry()
    moment = now()
    with transaction(factory) as session:
        merchant_ids = list(
            claimable_merchant_ids(session, now=moment, limit=merchant_scan_limit)
        )

    processed = 0
    for merchant_id in merchant_ids:
        while stop is None or not stop.is_set():
            claimed = claim_one(merchant_id, worker_id=worker_id, factory=factory)
            if claimed is None:
                break
            _process(claimed, handlers, factory=factory)
            processed += 1
    return processed


def _process(
    claimed: ClaimedJob,
    handlers: dict[str, Handler],
    *,
    factory: sessionmaker[_Session] | None,
) -> None:
    correlation = str(claimed.correlation_id) if claimed.correlation_id is not None else None
    with correlation_context(correlation):
        handler = handlers.get(claimed.kind)
        if handler is None:
            fail(
                claimed,
                error_class="UnknownJobKind",
                error_detail=f"no handler for kind {claimed.kind!r}",
                factory=factory,
            )
            return
        try:
            handler(claimed)
        except Exception as exc:
            _logger.exception("job handler failed", job_kind=claimed.kind)
            fail(claimed, error_class=type(exc).__name__, error_detail=str(exc), factory=factory)
        else:
            complete(claimed, factory=factory)


def run_forever(
    worker_id: str,
    *,
    poll_interval_seconds: float = 1.0,
    stop: threading.Event | None = None,
    factory: sessionmaker[_Session] | None = None,
) -> None:  # pragma: no cover - exercised by the process, not the unit tests
    """Poll the queue until stopped. The worker-role process entry point.

    Checks the stop event between passes and between jobs, so a graceful shutdown
    finishes the current job and then exits, leaving nothing ``RUNNING`` for the lease
    sweep to reclaim. A hard kill is handled by that sweep instead.
    """
    stop = stop or threading.Event()
    registry = build_registry()
    _logger.info("worker started", worker_id=worker_id)
    while not stop.is_set():
        try:
            processed = run_once(worker_id, registry=registry, factory=factory, stop=stop)
        except Exception:
            _logger.exception("worker pass failed")
            processed = 0
        if processed == 0:
            stop.wait(poll_interval_seconds)
    _logger.info("worker stopped", worker_id=worker_id)
