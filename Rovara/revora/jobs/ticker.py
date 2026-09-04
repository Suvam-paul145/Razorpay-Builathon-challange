"""The clock. Enqueues every periodic sweep for every merchant, and reclaims dead leases.

The worker moves work off the request path and the scheduler builds the dedupe-keyed sweep
jobs, but until this module existed **nothing called the scheduler in a deployment**.
``revora.jobs.scheduler.enqueue_sweep`` had exactly one caller in the repository and it was
``scripts/dev_tick.py``, a development script run by hand. There was no cron, no sidecar and no
scheduler service. The consequence was not a degraded system, it was seven inert guarantees:
cases were never expired, execution intents were never reconciled, payment state was never
re-read, detection gaps were never backfilled, customer contact data was never redacted, and a
case that chose restraint was never reviewed. The worker's claim, dispatch and complete path was
correct and complete the whole time. Only the producer was missing.

``revora.jobs.queue.reclaim_stale`` had the same problem and a narrower blast radius. Two
docstrings describe it as the mechanism that returns a dead worker's ``RUNNING`` job to
``PENDING``, and it had no caller either — so a hard-killed worker's job stayed ``RUNNING``
forever, holding a case at whatever state that job was supposed to advance it past. It is
called here, once per tick, because the lease sweep and the schedule are the same kind of thing:
work that must happen on a clock whether or not any particular job ran.

**Why a sidecar — a third process role — and not the two alternatives.**

* *A loop inside the worker process* is the least new infrastructure, and it is wrong for one
  reason that does not go away: it runs N times across N worker replicas. Every tick would be
  N ticks. The dedupe key makes that *safe* — see below — but it makes the schedule's cost
  scale with worker count and it makes "did the tick happen" unanswerable without knowing
  which replica you are reading.
* *A cron entry* is the least code and the hardest to observe. It lives outside the image, it
  logs somewhere the application does not, and a cron that stopped firing looks exactly like a
  system with nothing to do.
* *A sidecar* gives the schedule exactly one owner. That is the property worth paying a third
  entrypoint for, and the reason is asymmetric: every enqueue is dedupe-keyed by kind and
  interval bucket, so a **double** tick is harmless by construction — the second enqueue
  collides with the first and returns ``None``. A **missing** tick has no such signal. It
  produces no error, no dead-letter and no failed job; it produces a sweep that simply did not
  run. So the arrangement should minimise the chance of a missing tick and may be relaxed about
  duplicate ones, and one owner with one log line per tick is that arrangement.

One image still, not a third dependency graph — see :mod:`revora.platform.role`. This module
imports strictly less than the worker does and reaches no provider.

**``enqueued=0`` is the dedupe working, not a failure.** The tick interval is deliberately
shorter than the shortest sweep interval, so most ticks find every bucket already occupied and
enqueue nothing. That is the design. A tick that enqueued seven jobs every time would mean the
bucket arithmetic had stopped quantizing, which is the actual failure mode and the opposite of
what a zero means. The log line says so in words, because a zero next to the word "enqueued" is
the kind of thing somebody fixes.

**Every sweep kind must have a configured interval, and an unpriced kind raises.** The
development script this replaced mapped four kinds by hard-coded string literal and gave
everything else a 300-second fallback, so renaming a kind silently demoted it to five minutes
instead of failing. :func:`_bucket_seconds` keys on the constants imported from
``revora.jobs.scheduler`` and raises :class:`UnscheduledSweepKindError` on a kind it cannot
price. Migration ``0014`` seeded the three intervals that did not exist, so all seven are
configured and the fallback is gone rather than merely unused.
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Final

from revora.jobs.queue import reclaim_stale
from revora.jobs.scheduler import (
    CALIBRATION_REPORT_KIND,
    CASE_REVIEW_KIND,
    CUSTOMER_DATA_RETENTION_KIND,
    DETECTION_GAP_BACKFILL_KIND,
    EXECUTION_RECONCILIATION_KIND,
    LIFECYCLE_EVALUATION_KIND,
    PAYMENT_STATE_RECONCILIATION_KIND,
    PERIODIC_SWEEP_KINDS,
    PROMISE_SWEEP_KIND,
    enqueue_sweep,
)
from revora.persistence.repositories.config import ConfigurationRepository
from revora.persistence.repositories.session import tenant_transaction, transaction
from revora.persistence.repositories.tenancy import schedulable_merchants
from revora.platform.config import Configuration
from revora.platform.logging import get_logger

try:  # pragma: no cover - typing convenience only
    from sqlalchemy.orm import Session as _Session
    from sqlalchemy.orm import sessionmaker
except ImportError:  # pragma: no cover
    sessionmaker = object  # type: ignore[assignment,misc]
    _Session = object  # type: ignore[assignment,misc]

__all__ = [
    "DEFAULT_JOB_LEASE",
    "DEFAULT_TICK_SECONDS",
    "TickReport",
    "UnscheduledSweepKindError",
    "run_forever",
    "tick",
]

_logger = get_logger(__name__)

DEFAULT_TICK_SECONDS: Final[float] = 30.0
"""How often the loop ticks, in seconds.

Shorter than the shortest configured sweep interval (300 seconds, shared by lifecycle
evaluation and the review sweep) and by a wide margin. Deliberately over-ticking: a tick that
lands inside an occupied bucket costs one insert that hits ``one_pending_job_per_dedupe_key``
and returns ``None``, while a tick interval close to the sweep interval would make the
*phase* of the loop decide whether a sweep ran on time. Ten cheap attempts per bucket buys a
schedule whose accuracy does not depend on when the process happened to start."""

DEFAULT_JOB_LEASE: Final[timedelta] = timedelta(minutes=15)
"""How long a ``RUNNING`` job may hold its lease before it is presumed abandoned.

**Not a configured bound, and deliberately not made into one here.** There is no
``JOB_LEASE`` row in the catalogue, and inventing one alongside the three sweep intervals
migration ``0014`` seeds would be putting a fourth number into a versioned table on this
change's way past. It is also not the kind of number R15.C6 is about: a merchant has a
judgement about how often their customers' data is swept, and no judgement whatsoever about
how long to wait before concluding that a container is dead.

Fifteen minutes, and the direction of the error is what picks the value. Reclaiming **early**
is the dangerous side: the job is handed to a second worker while the first is still running
it, which is safe — every handler is idempotent, which is the standing precondition of this
queue — but pays for the work twice, including a second provider read. Reclaiming **late**
only delays a job whose worker is already gone. So the bound sits comfortably above the
longest plausible single-job runtime: a provider call is bounded by ``PROVIDER_CALL_TIMEOUT``
at 15 seconds, and the slowest handler is one batch of the retention sweep. Fifteen minutes is
two orders of magnitude of headroom, which is the right amount when the cheap failure is
"a dead worker's job waits a quarter of an hour"."""


class UnscheduledSweepKindError(RuntimeError):
    """A periodic sweep kind has no configured interval, so it cannot be scheduled.

    Raised rather than defaulted, and this is the whole reason :func:`_bucket_seconds` exists
    as a function with a raise in it instead of a ``dict.get`` with a fallback. A fallback
    turns "somebody added an eighth sweep kind and no interval for it" into "the eighth sweep
    runs every five minutes and nobody chose that", which is indistinguishable from working.
    """


def _bucket_seconds(kind: str, config: Configuration) -> int:
    """The interval a sweep kind is enqueued on, in seconds, from configuration.

    Read from the bounds rather than hard-coded, so changing ``REVIEW_SWEEP_INTERVAL`` in
    ``app_config`` changes how often the review sweep is re-enqueued — without a redeploy,
    which is the point of the bounds being rows.

    **Keyed on the constants, not on string literals.** The development script this was lifted
    from spelled the four kinds it knew about as ``"lifecycle_evaluation"`` and friends, so
    renaming a constant in ``revora.jobs.scheduler`` left this mapping matching nothing and
    silently demoted the renamed kind to a 300-second fallback. Importing the constants makes
    that a ``KeyError`` in the author's editor rather than a schedule nobody chose.

    Raises:
        UnscheduledSweepKindError: if ``kind`` has no configured interval. All eight members
            of ``PERIODIC_SWEEP_KINDS`` have one as of migration ``0016``, which added
            ``PROMISE_SWEEP_INTERVAL`` for the promise sweep; a ninth added
            without a bound stops the ticker instead of being given a default.
    """
    by_kind = {
        LIFECYCLE_EVALUATION_KIND: config.LIFECYCLE_EVALUATION_INTERVAL,
        EXECUTION_RECONCILIATION_KIND: config.EXECUTION_RECONCILIATION_INTERVAL,
        PAYMENT_STATE_RECONCILIATION_KIND: config.PAYMENT_STATE_RECONCILIATION_INTERVAL,
        DETECTION_GAP_BACKFILL_KIND: config.DETECTION_GAP_BACKFILL_INTERVAL,
        CALIBRATION_REPORT_KIND: config.CALIBRATION_REPORT_INTERVAL,
        CUSTOMER_DATA_RETENTION_KIND: config.CUSTOMER_DATA_RETENTION_SWEEP_INTERVAL,
        CASE_REVIEW_KIND: config.REVIEW_SWEEP_INTERVAL,
        PROMISE_SWEEP_KIND: config.PROMISE_SWEEP_INTERVAL,
    }
    interval = by_kind.get(kind)
    if interval is None:
        raise UnscheduledSweepKindError(
            f"sweep kind {kind!r} has no configured interval; every member of "
            f"PERIODIC_SWEEP_KINDS needs one. Add a DURATION_SECONDS bound to the "
            f"catalogue, seed it in a migration, and map it here."
        )
    # A bound configured at zero or below would make the bucket divisor zero and every tick
    # share one bucket forever, which is a sweep that runs once and never again. One second is
    # the smallest divisor that still quantizes; `enqueue_sweep` clamps the same way.
    return max(1, int(interval.total_seconds()))


@dataclass(frozen=True, slots=True)
class TickReport:
    """What one tick did. Returned so a caller can assert on it.

    Three numbers rather than one, because they mean different things and a test needs each:
    ``merchants`` is how many tenants the pass covered, ``enqueued`` is how many sweeps it
    created, and ``reclaimed`` is how many abandoned jobs it returned to ``PENDING``.
    Collapsing them into a total would make a tick that rescued a dead worker's job look like
    a tick that scheduled a sweep, and those call for different responses — the first says a
    container died.

    Frozen, on the same terms as ``ClaimedJob``: it is a snapshot of a pass that has already
    finished, and there is nothing a caller could correctly change about it afterwards.
    """

    merchants: int
    enqueued: int
    reclaimed: int


def tick(
    *,
    merchant_slug: str | None = None,
    kinds: tuple[str, ...] | None = None,
    lease: timedelta = DEFAULT_JOB_LEASE,
    factory: sessionmaker[_Session] | None = None,
) -> TickReport:
    """One pass: every sweep for every merchant, plus the lease sweep.

    Testable in isolation, the way ``revora.jobs.worker.run_once`` is: a test calls this once
    and asserts on the jobs that appeared, without a running loop and without a signal.

    Each merchant's configuration is loaded inside that merchant's own transaction, so a
    merchant that has overridden ``REVIEW_SWEEP_INTERVAL`` is scheduled on its own interval
    rather than on the platform default. That costs one small read per merchant per tick and it
    is the difference between the bounds being configuration and the bounds being decoration.

    A failure for one merchant does not stop the tick. Configuration for one tenant can be
    unloadable — a row of the wrong kind, a value that will not parse — and letting that halt
    the pass would mean one tenant's bad row stopped every other tenant's sweeps. The failure
    is logged with the merchant named and the loop moves on.

    Args:
        merchant_slug: restrict the pass to one merchant. ``None`` — what the sidecar always
            passes — is every real merchant.
        kinds: restrict the pass to these sweep kinds. ``None`` is all of
            ``PERIODIC_SWEEP_KINDS``, which is again what the sidecar always passes.
        lease: how long a ``RUNNING`` job may hold its lease. See :data:`DEFAULT_JOB_LEASE`.
        factory: session factory, for a test that binds its own engine.

    The two filters exist for exactly one caller, ``scripts/dev_tick.py``, which is run by hand
    against a single merchant or a single kind while watching what happens. They are here
    rather than in that script because the alternative was a second copy of the per-merchant
    loop and of the interval mapping — and that copy is what produced the bug this module was
    written to fix, where the script's hard-coded kind strings had drifted from the constants.
    One implementation with two callers is the same trade ``revora.platform.ratelimit`` took
    when it got its second one. The lease sweep is *not* filtered by ``kinds``: it is not a
    sweep kind, and a developer narrowing the pass to one kind is not asking for a dead
    worker's job to stay stuck.

    Raises:
        ValueError: if ``kinds`` names something that is not a periodic sweep kind. Silently
            enqueueing nothing would be indistinguishable from a tick where every bucket was
            already occupied.
    """
    selected = _selected_kinds(kinds)
    merchants = _resolve_merchants(factory)
    if merchant_slug is not None:
        merchants = [pair for pair in merchants if pair[1] == merchant_slug]
        if not merchants:
            _logger.warning("no merchant matched; nothing ticked", merchant_slug=merchant_slug)

    total_enqueued = 0
    total_reclaimed = 0

    for merchant_id, slug in merchants:
        try:
            total_enqueued += _tick_merchant(merchant_id, slug, selected, factory=factory)
            total_reclaimed += reclaim_stale(merchant_id, lease=lease, factory=factory)
        except Exception:
            # Logged with the merchant named, because "the ticker is failing" and "one
            # tenant's configuration will not parse" call for entirely different responses and
            # only the slug distinguishes them.
            _logger.exception("tick failed for merchant", merchant_slug=slug)

    return TickReport(
        merchants=len(merchants), enqueued=total_enqueued, reclaimed=total_reclaimed
    )


def _selected_kinds(kinds: tuple[str, ...] | None) -> tuple[str, ...]:
    """Validate the requested kinds, or return every one of them."""
    if kinds is None:
        return PERIODIC_SWEEP_KINDS
    unknown = tuple(kind for kind in kinds if kind not in PERIODIC_SWEEP_KINDS)
    if unknown:
        raise ValueError(
            f"{unknown} are not periodic sweep kinds; expected a subset of "
            f"{PERIODIC_SWEEP_KINDS}"
        )
    return tuple(kinds)


def _resolve_merchants(
    factory: sessionmaker[_Session] | None,
) -> list[tuple[uuid.UUID, str]]:
    """Every real merchant, in a single untenanted transaction. See ``schedulable_merchants``."""
    with transaction(factory) as session:
        return list(schedulable_merchants(session))


def _tick_merchant(
    merchant_id: uuid.UUID,
    slug: str,
    kinds: tuple[str, ...],
    *,
    factory: sessionmaker[_Session] | None,
) -> int:
    """Enqueue the given periodic sweeps for one merchant. Returns how many were created."""
    with tenant_transaction(merchant_id, factory) as session:
        config = ConfigurationRepository(session).load(merchant_id)

    enqueued: list[str] = []
    for kind in kinds:
        job_id = enqueue_sweep(
            merchant_id,
            kind,
            bucket_seconds=_bucket_seconds(kind, config),
            factory=factory,
        )
        if job_id is not None:
            enqueued.append(kind)

    # Info, not debug: one line per merchant per tick is the whole observability story of this
    # role, and a role whose only failure mode is silence has to say something on every pass.
    # `enqueued=0` is the dedupe key working — the bucket for each kind is already occupied by
    # a pending job — and the message says so rather than leaving a zero to be interpreted.
    _logger.info(
        "tick enqueued sweeps",
        merchant_slug=slug,
        enqueued=len(enqueued),
        considered=len(kinds),
        kinds=",".join(enqueued),
        note="enqueued=0 means every bucket is already pending; that is the dedupe key working",
    )
    return len(enqueued)


def run_forever(
    *,
    tick_seconds: float = DEFAULT_TICK_SECONDS,
    lease: timedelta = DEFAULT_JOB_LEASE,
    stop: threading.Event | None = None,
    factory: sessionmaker[_Session] | None = None,
) -> None:  # pragma: no cover - exercised by the process, not the unit tests
    """Tick until stopped. The ticker-role process entry point.

    The stop event is checked between ticks and waited on instead of slept through, so a
    graceful shutdown finishes the tick it is in and then exits rather than being interrupted
    part way down the merchant list. A tick interrupted mid-list is not corrupting — every
    enqueue is its own transaction and dedupe-keyed — but the merchants after the interruption
    would be skipped for this bucket, and one skipped bucket is exactly the invisible failure
    this role exists to prevent.

    A failed tick is logged and the loop continues. The alternative — exiting on the first
    exception and letting the orchestrator restart the container — loses nothing on a transient
    database blip and turns it into a gap in the schedule.
    """
    stop = stop or threading.Event()
    _logger.info("ticker started", tick_seconds=tick_seconds, lease_seconds=lease.total_seconds())
    while not stop.is_set():
        try:
            tick(lease=lease, factory=factory)
        except Exception:
            _logger.exception("ticker pass failed")
        stop.wait(tick_seconds)
    _logger.info("ticker stopped")
