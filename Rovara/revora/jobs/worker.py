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

from revora.cases.retention import sweep_customer_data_retention
from revora.cases.review import CASE_REVIEW_KIND, sweep_due_reviews
from revora.cases.sweeper import sweep_expired_cases
from revora.customer.arrangements import PARTIAL_ARRANGEMENT_KIND
from revora.customer.promises import PROMISE_ESCALATION_KIND, PROMISE_SWEEP_KIND
from revora.customer.suppression import CONTACT_SUPPRESSION_KIND
from revora.detection.service import run_detection
from revora.domain.enums import HardStopReason, Provenance, ReviewTrigger
from revora.execution.reconcile import reconcile_intents
from revora.ingestion.backfill import backfill_detection_gap
from revora.ingestion.service import DETECTION_JOB_KIND
from revora.jobs.pipeline import (
    CANDIDATE_JOB_KIND,
    DIAGNOSIS_JOB_KIND,
    EXECUTION_JOB_KIND,
    OPTIMIZER_JOB_KIND,
    OUTCOME_JOB_KIND,
    POLICY_JOB_KIND,
    enqueue_after_detection,
    enqueue_after_recovery_signal,
    handle_candidates,
    handle_contact_suppression,
    handle_diagnosis,
    handle_execution,
    handle_optimizer,
    handle_outcome,
    handle_partial_arrangement,
    handle_policy,
    handle_promise_escalation,
    handle_promise_sweep,
    handle_review,
)
from revora.jobs.queue import ClaimedJob, claim_one, complete, enqueue, fail
from revora.jobs.scheduler import (
    CALIBRATION_REPORT_KIND,
    CUSTOMER_DATA_RETENTION_KIND,
    DETECTION_GAP_BACKFILL_KIND,
    EXECUTION_RECONCILIATION_KIND,
    LIFECYCLE_EVALUATION_KIND,
    PAYMENT_STATE_RECONCILIATION_KIND,
)
from revora.memory.store import observation_writer
from revora.outcome.monitor import sweep_payment_state
from revora.persistence.repositories.config import ConfigurationRepository
from revora.persistence.repositories.jobs import claimable_merchant_ids
from revora.persistence.repositories.session import tenant_transaction, transaction
from revora.platform.clock import now
from revora.platform.logging import correlation_context, get_logger
from revora.providers.razorpay import PaymentProviderClient, RazorpayClient

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


def _handle_detection(
    claimed: ClaimedJob,
    provenance: Provenance,
    synthetic_run_id: uuid.UUID | None,
) -> None:
    """Classify one persisted event, open or attach a case, start the decision pipeline.

    The verdict, the case and the diagnosis job all commit together. A case that exists with
    no job to advance it would sit until the lifecycle sweeper expired it, which is a silent
    way to lose a recovery — so the enqueue shares the transaction that created the case.

    ``provenance`` is ``REAL`` in every deployment and is carried here rather than read off
    the event on purpose — see :func:`revora.detection.service.run_detection`, which explains
    why a label meaning "this money is not real" must not be derivable from request content.
    """
    webhook_event_id = uuid.UUID(str(claimed.payload["webhook_event_id"]))
    with tenant_transaction(claimed.merchant_id) as session:
        config = ConfigurationRepository(session).load(claimed.merchant_id)
        result = run_detection(
            session,
            claimed.merchant_id,
            webhook_event_id,
            config,
            correlation_id=claimed.correlation_id,
            provenance=provenance,
            synthetic_run_id=synthetic_run_id,
        )
        # A failure starts the decision pipeline; a success routes to an outcome observation.
        # Both in detection's own transaction, so a case never exists with no job to advance it.
        enqueue_after_recovery_signal(
            session,
            claimed.merchant_id,
            result,
            correlation_id=claimed.correlation_id,
        )
        enqueue_after_detection(
            session,
            claimed.merchant_id,
            result,
            config,
            correlation_id=claimed.correlation_id,
        )


def _case_id_of(claimed: ClaimedJob) -> uuid.UUID:
    """The case id a pipeline job carries. Payloads hold ids and nothing else."""
    return uuid.UUID(str(claimed.payload["case_id"]))


def _handle_diagnosis(claimed: ClaimedJob) -> None:
    """Determine the risk cause and advance to ``DIAGNOSED``."""
    handle_diagnosis(
        claimed.merchant_id, _case_id_of(claimed), correlation_id=claimed.correlation_id
    )


def _handle_estimation(claimed: ClaimedJob) -> None:
    """Estimate the baseline and price every candidate action."""
    handle_candidates(
        claimed.merchant_id, _case_id_of(claimed), correlation_id=claimed.correlation_id
    )


def _handle_optimization(claimed: ClaimedJob) -> None:
    """Rank the candidates and record the recommendation."""
    handle_optimizer(
        claimed.merchant_id, _case_id_of(claimed), correlation_id=claimed.correlation_id
    )


def _handle_policy(claimed: ClaimedJob) -> None:
    """Evaluate the twelve checks and persist the decision."""
    handle_policy(
        claimed.merchant_id, _case_id_of(claimed), correlation_id=claimed.correlation_id
    )


def _handle_lifecycle(claimed: ClaimedJob) -> None:
    """Expire every non-terminal case whose recovery window has closed.

    Supplies the recovery-memory writer as the sweep's terminal callback. The sweeper cannot
    import ``revora.memory`` itself — it sits a layer below — so this is where the two are
    composed, in the top layer that may see both.
    """
    with transaction() as session:
        config = ConfigurationRepository(session).load(claimed.merchant_id)
    sweep_expired_cases(
        claimed.merchant_id,
        on_terminal=observation_writer(config, correlation_id=claimed.correlation_id),
    )


def _handle_contact_suppression(claimed: ClaimedJob) -> None:
    """Apply what a Hard_Stop_Reason causes beyond the request that recorded it (R21.C4-C7).

    The Hard_Stop_Reason travels in the payload rather than being re-derived from the
    ``customer_signal`` row, and rather than being read back off ``contact_suppression``. Payloads
    hold ids and enumeration members here for a reason worth stating: re-deriving it would mean
    this handler deciding *again* which reason applies, and the whole point of R20.C1's typed
    enumeration is that the decision was made once, at the boundary, against a validated
    submission. A second derivation is a second place the mapping can be wrong.

    Absent or unrecognised, the ``HardStopReason`` constructor raises and the job fails into the
    retry path rather than defaulting. There is no safe default: guessing ``DISPUTES_THE_CHARGE``
    for a cancellation would file a fulfilment question as a chargeback risk, and the reverse
    hides a chargeback.
    """
    handle_contact_suppression(
        claimed.merchant_id,
        _case_id_of(claimed),
        hard_stop_reason=HardStopReason(str(claimed.payload["hard_stop_reason"])),
        correlation_id=claimed.correlation_id,
    )


def _handle_partial_arrangement(claimed: ClaimedJob) -> None:
    """Escalate a case whose customer asked to pay differently (R22.C2).

    The signal identifier is required rather than defaulted, and an absent or malformed one fails
    the job into the retry path. There is no safe substitute: the escalation's audit record names
    the signal that caused it, and a record that named the wrong signal — or none — would leave a
    terminal case whose stated cause could not be checked against what the customer actually
    submitted.

    Nothing here resolves a provider client, and the handler's signature is where that is visible:
    an arrangement request cancels nothing and expires nothing (R22.C8), so there is no external
    call for this path to make.
    """
    handle_partial_arrangement(
        claimed.merchant_id,
        _case_id_of(claimed),
        signal_id=uuid.UUID(str(claimed.payload["signal_id"])),
        correlation_id=claimed.correlation_id,
    )


def _handle_promise_escalation(claimed: ClaimedJob) -> None:
    """Escalate a case whose customer promised a date the Recovery_Window cannot reach (R23.C5).

    The promise identifier and the signal identifier are both required rather than defaulted, and
    an absent or malformed one fails the job into the retry path. There is no safe substitute for
    either: the escalation's audit record names the promise whose date caused it and the signal the
    promise came from, and a record naming the wrong one — or none — would leave a terminal case
    whose stated cause could not be checked against what the customer actually submitted.

    Nothing here resolves a provider client, and the handler's signature is where that is visible:
    this escalation cancels nothing and expires nothing, so there is no external call for the path
    to make. That matters more on this path than on the arrangement's — the customer said they
    *will* pay, so a live payment link left live is how the money still arrives.
    """
    handle_promise_escalation(
        claimed.merchant_id,
        _case_id_of(claimed),
        promise_id=uuid.UUID(str(claimed.payload["promise_id"])),
        signal_id=uuid.UUID(str(claimed.payload["signal_id"])),
        correlation_id=claimed.correlation_id,
    )


def _handle_promise_sweep(claimed: ClaimedJob) -> None:
    """Evaluate every promise whose Follow_Up_Instant has been reached (R23.C13).

    A pure sweep, unlike ``CASE_REVIEW_KIND``: it carries no ``case_id`` shape, because a single
    promise's follow-up is not enqueued as a job of its own. Making the follow-up eligible is a
    status change on the promise row, and *deciding* to send it is the ordinary decision cycle the
    sweep enqueues — so there is nothing per-case for this kind to dispatch.

    Needs no terminal callback and no provider client. It voids promises and schedules follow-ups;
    the one transition it could imply — a case whose window elapsed — is deliberately left to the
    lifecycle sweep, so two sweeps are not both writers of ``EXPIRED``.
    """
    handle_promise_sweep(claimed.merchant_id, correlation_id=claimed.correlation_id)


def _handle_case_review(claimed: ClaimedJob) -> None:
    """One kind, two shapes of work: the periodic sweep, or one case's review.

    Distinguished by whether the payload carries a ``case_id``. They share a job kind because
    they are one mechanism — the sweep exists only to enqueue the reviews — and splitting them
    would put a second kind in ``PERIODIC_SWEEP_KINDS`` that no clock ever enqueues, which is
    the shape of a sweep everybody assumes is running.

    The sweep needs no terminal callback, unlike the lifecycle sweep: it transitions nothing.
    A review's own terminal path lives in :func:`revora.jobs.pipeline.handle_review`, which
    can reach ``revora.memory`` directly from this layer.
    """
    raw_case_id = claimed.payload.get("case_id")
    if raw_case_id is None:
        sweep_due_reviews(claimed.merchant_id, correlation_id=claimed.correlation_id)
        return

    raw_trigger = claimed.payload.get("review_trigger")
    trigger = (
        ReviewTrigger.SCHEDULED_REVIEW
        if raw_trigger is None
        else ReviewTrigger(str(raw_trigger))
    )
    handle_review(
        claimed.merchant_id,
        uuid.UUID(str(raw_case_id)),
        trigger=trigger,
        correlation_id=claimed.correlation_id,
    )


def _handle_customer_data_retention(claimed: ClaimedJob) -> None:
    """Redact contact data past ``CUSTOMER_DATA_RETENTION``, re-enqueuing while a backlog remains.

    R17.C11 gives a 24-hour window after the bound elapses. A merchant with a large backlog cannot
    meet that if each sweep does one batch and then waits for the next tick, so a sweep that reports
    ``more_remaining`` enqueues its own successor immediately. The dedupe key uses the batch index
    rather than the time bucket, so the follow-up is not swallowed as a duplicate of the tick that
    scheduled the first one.
    """
    with transaction() as session:
        config = ConfigurationRepository(session).load(claimed.merchant_id)

    batch = int(claimed.payload.get("batch", 0))
    report = sweep_customer_data_retention(
        claimed.merchant_id, config=config, correlation_id=claimed.correlation_id
    )
    if not report.more_remaining:
        return

    with tenant_transaction(claimed.merchant_id) as session:
        enqueue(
            session,
            claimed.merchant_id,
            kind=CUSTOMER_DATA_RETENTION_KIND,
            payload={"batch": batch + 1},
            run_after=now(),
            dedupe_key=(
                f"{CUSTOMER_DATA_RETENTION_KIND}:{claimed.merchant_id}:continue:{batch + 1}"
            ),
            correlation_id=claimed.correlation_id,
        )


def _handle_not_yet_implemented(claimed: ClaimedJob) -> None:
    """A registered no-op for a sweep whose owner has not been built yet.

    Completing rather than failing keeps a scheduled sweep from dead-lettering before
    its owning task exists. Its owner replaces this handler when built.
    """
    _logger.debug("sweep handler not yet implemented; completing as no-op", kind=claimed.kind)


_provider_lock = threading.Lock()
_shared_provider: PaymentProviderClient | None = None


def shared_provider() -> PaymentProviderClient:
    """The process-wide Razorpay client, built on first use.

    One client per process, not one per job. The connection pool is the whole point — a client
    per sweep would pay a TLS handshake on every reconciliation read — and the client is
    documented thread-safe because its only mutable state is a semaphore.

    Built lazily rather than at import so a worker that never reaches a provider-touching
    sweep never resolves a credential, and so importing this module in a test does not require
    Razorpay keys to exist.
    """
    global _shared_provider
    with _provider_lock:
        if _shared_provider is None:
            _shared_provider = RazorpayClient()
        return _shared_provider


def _handle_execution(claimed: ClaimedJob, provider: PaymentProviderClient) -> None:
    """Execute the case's approved action at most once. The only handler with an effect.

    Idempotent by the engine's reservation, not by this handler: a redelivered job, a restarted
    worker and a retried attempt all reach the same intent and the second one refuses.
    """
    handle_execution(
        claimed.merchant_id,
        _case_id_of(claimed),
        provider=provider,
        correlation_id=claimed.correlation_id,
    )


def _handle_outcome(claimed: ClaimedJob, provider: PaymentProviderClient) -> None:
    """Read the provider and decide whether the case recovered.

    ``signal_status`` rides in the payload when a webhook prompted this. It is used for conflict
    detection only — the recovery decision comes from the read, which is the whole of R10.C1.
    """
    claimed_status = claimed.payload.get("signal_status")
    handle_outcome(
        claimed.merchant_id,
        _case_id_of(claimed),
        provider=provider,
        signal_status=None if claimed_status is None else str(claimed_status),
        correlation_id=claimed.correlation_id,
    )


def _handle_execution_reconciliation(
    claimed: ClaimedJob, provider: PaymentProviderClient
) -> None:
    """Resolve unresolved execution intents by reading. Never repeats a create."""
    reconcile_intents(
        claimed.merchant_id, provider=provider, correlation_id=claimed.correlation_id
    )


def _handle_payment_state_reconciliation(
    claimed: ClaimedJob, provider: PaymentProviderClient
) -> None:
    """Re-read every case waiting on an outcome, so none depends on a webhook arriving."""
    sweep_payment_state(
        claimed.merchant_id, provider=provider, correlation_id=claimed.correlation_id
    )


def _handle_detection_gap_backfill(
    claimed: ClaimedJob, provider: PaymentProviderClient
) -> None:
    """List provider payments and ingest failures no webhook delivered.

    The job that stops a disabled webhook from being invisible. Its report is logged at
    warning level when it finds anything, because a non-zero count means detection is running
    on this job alone.
    """
    backfill_detection_gap(
        claimed.merchant_id, provider=provider, correlation_id=claimed.correlation_id
    )


def build_registry(
    *,
    provider: PaymentProviderClient | None = None,
    provenance: Provenance = Provenance.REAL,
    synthetic_run_id: uuid.UUID | None = None,
) -> dict[str, Handler]:
    """The kind-to-handler map. One place, so a job kind cannot be dispatched two ways.

    ``provider`` is injectable so a test can substitute the scriptable fake, and defaults to
    the shared client resolved on first use. The three provider-touching sweeps close over it
    here rather than reaching for a global inside the handler, which keeps "what can make an
    external call" answerable by reading this function.

    ``provenance`` is what a case opened by this registry's detection handler is labelled
    with. ``REAL`` in every deployment; the Demonstration_Loader's worker is the one caller
    that passes ``SYNTHETIC``, which is how R28.C1's label reaches the row without the
    loader writing it. It sits beside ``provider`` deliberately: both are facts about the
    process draining the queue rather than about any request, and keeping them in one
    function keeps "what is substituted in this run" answerable by reading one signature.

    ``synthetic_run_id`` travels with it for the same reason and is subject to the same
    check: a ``SYNTHETIC`` case with no run id cannot be reproduced from a seed, and a run
    id on a ``REAL`` case is a contradiction.

    Raises:
        ValueError: if exactly one of ``provenance is SYNTHETIC`` and ``synthetic_run_id is
            not None`` holds. Refused here rather than left to produce a half-labelled row,
            because both halves of the label are what make a seeded case auditable and the
            failure would only be visible months later, in a figure nobody could re-derive.
    """
    if (provenance is Provenance.SYNTHETIC) != (synthetic_run_id is not None):
        raise ValueError(
            "provenance=SYNTHETIC and synthetic_run_id must be supplied together; got "
            f"provenance={provenance.value} and synthetic_run_id={synthetic_run_id!r}"
        )
    client = provider

    def _resolve() -> PaymentProviderClient:
        return client if client is not None else shared_provider()

    return {
        DETECTION_JOB_KIND: lambda claimed: _handle_detection(
            claimed, provenance, synthetic_run_id
        ),
        DIAGNOSIS_JOB_KIND: _handle_diagnosis,
        CANDIDATE_JOB_KIND: _handle_estimation,
        OPTIMIZER_JOB_KIND: _handle_optimization,
        POLICY_JOB_KIND: _handle_policy,
        EXECUTION_JOB_KIND: lambda claimed: _handle_execution(claimed, _resolve()),
        OUTCOME_JOB_KIND: lambda claimed: _handle_outcome(claimed, _resolve()),
        LIFECYCLE_EVALUATION_KIND: _handle_lifecycle,
        EXECUTION_RECONCILIATION_KIND: lambda claimed: _handle_execution_reconciliation(
            claimed, _resolve()
        ),
        PAYMENT_STATE_RECONCILIATION_KIND: lambda claimed: (
            _handle_payment_state_reconciliation(claimed, _resolve())
        ),
        DETECTION_GAP_BACKFILL_KIND: lambda claimed: _handle_detection_gap_backfill(
            claimed, _resolve()
        ),
        CUSTOMER_DATA_RETENTION_KIND: _handle_customer_data_retention,
        CASE_REVIEW_KIND: _handle_case_review,
        CONTACT_SUPPRESSION_KIND: _handle_contact_suppression,
        PARTIAL_ARRANGEMENT_KIND: _handle_partial_arrangement,
        PROMISE_ESCALATION_KIND: _handle_promise_escalation,
        PROMISE_SWEEP_KIND: _handle_promise_sweep,
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
