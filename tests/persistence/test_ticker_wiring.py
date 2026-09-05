"""The ticker actually produces the sweeps, dedupes the second tick, and reclaims a lease.

Three claims, and each one is about an edge that nothing else in the suite covers.

**The sweeps get produced at all.** ``enqueue_sweep`` was tested nowhere and called from
nothing but a development script, so every periodic sweep in a deployment was inert while the
worker, the queue and each individual sweep handler all passed their own tests. The failure was
in the space between them, and it was silent — no error, no dead-letter, no failed job.

**The second tick enqueues nothing.** That is the dedupe key working, and it has to be asserted
in the affirmative, because ``enqueued=0`` is also what a broken ticker produces. A test that
only checked the first tick would pass against an implementation that enqueued seven jobs every
30 seconds forever.

**A stale lease comes back.** ``reclaim_stale`` was described in two docstrings as the mechanism
that rescues a hard-killed worker's job and had no caller either, so a job stuck ``RUNNING``
stayed that way permanently — holding whichever case it was supposed to advance.

The clock is frozen for all three. Two ticks a millisecond apart are almost always inside one
300-second bucket, and *almost* is not a property: with a real clock this file would fail once
every few hundred thousand runs, at a boundary crossing, and the failure would look like a
broken dedupe key.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session, sessionmaker

from revora.jobs.scheduler import PERIODIC_SWEEP_KINDS
from revora.jobs.ticker import tick
from revora.persistence.models.jobs import PENDING_STATE
from revora.platform import clock
from tests.pg_support import insert_merchant

pytestmark = pytest.mark.pg


@pytest.fixture
def factory(owner_engine: Engine) -> sessionmaker[Session]:
    """A session factory bound to the owner engine, passed into ``tick`` explicitly.

    The ticker takes a factory for the same reason the worker's ``run_once`` does — so a test
    participates in the real arrangement rather than installing a process-wide engine and
    hoping nothing else in the run wanted a different one.
    """
    return sessionmaker(bind=owner_engine, expire_on_commit=False)


@pytest.fixture
def ticked_merchant(owner_engine: Engine) -> tuple[uuid.UUID, str]:
    """A fresh merchant and its slug.

    The slug matters: ``tick`` is called with ``merchant_slug=`` so the pass is restricted to
    this test's own tenant. Without that restriction the ticker would correctly sweep every
    merchant every other test in the session left behind, and "one job per merchant-by-kind"
    would be an assertion about the order tests ran in.
    """
    merchant_id = insert_merchant(owner_engine, display_name="Ticker merchant")
    return merchant_id, f"test-{merchant_id}"


def _pending_kinds(engine: Engine, merchant_id: uuid.UUID) -> list[str]:
    with engine.begin() as connection:
        return [
            str(row[0])
            for row in connection.execute(
                text(
                    "SELECT kind FROM job WHERE merchant_id = :m AND state = :s ORDER BY kind"
                ),
                {"m": str(merchant_id), "s": PENDING_STATE},
            )
        ]


def test_first_tick_enqueues_one_sweep_per_kind_and_second_tick_enqueues_none(
    owner_engine: Engine,
    factory: sessionmaker[Session],
    ticked_merchant: tuple[uuid.UUID, str],
    manual_clock: clock.ManualClock,
) -> None:
    """One job per merchant-by-kind, then nothing — which is the dedupe key, not a failure."""
    merchant_id, slug = ticked_merchant

    first = tick(merchant_slug=slug, factory=factory)

    assert first.merchants == 1, "the slug filter matched no merchant, or matched too many"
    assert first.enqueued == len(PERIODIC_SWEEP_KINDS)
    assert _pending_kinds(owner_engine, merchant_id) == sorted(PERIODIC_SWEEP_KINDS), (
        "a sweep kind was not enqueued, or one was enqueued twice"
    )

    second = tick(merchant_slug=slug, factory=factory)

    assert second.enqueued == 0, (
        "a second tick inside one interval bucket enqueued a duplicate sweep; "
        "the dedupe key is not quantizing"
    )
    assert _pending_kinds(owner_engine, merchant_id) == sorted(PERIODIC_SWEEP_KINDS)


def test_a_tick_in_the_next_bucket_enqueues_the_sweeps_again(
    owner_engine: Engine,
    factory: sessionmaker[Session],
    ticked_merchant: tuple[uuid.UUID, str],
    manual_clock: clock.ManualClock,
) -> None:
    """The other half of the dedupe guarantee, and the half whose absence is invisible.

    A key that never repeats prevents a double-enqueue *and* prevents the sweep ever running
    again once the first one is claimed. Advancing past the longest configured interval and
    claiming the outstanding jobs proves the bucket moves rather than merely being unique.
    """
    merchant_id, slug = ticked_merchant

    tick(merchant_slug=slug, factory=factory)
    # The dedupe index is partial on PENDING, so the previous bucket's jobs have to leave that
    # state before the next bucket's can be inserted — exactly as a worker claiming them does.
    with owner_engine.begin() as connection:
        connection.execute(
            text("UPDATE job SET state = 'DONE' WHERE merchant_id = :m"),
            {"m": str(merchant_id)},
        )

    # Past the longest of the seven intervals (DETECTION_GAP_BACKFILL_INTERVAL, 15 minutes).
    manual_clock.advance(timedelta(hours=2))
    later = tick(merchant_slug=slug, factory=factory)

    assert later.enqueued == len(PERIODIC_SWEEP_KINDS)
    assert _pending_kinds(owner_engine, merchant_id) == sorted(PERIODIC_SWEEP_KINDS)


def test_a_tick_reclaims_a_job_whose_worker_died(
    owner_engine: Engine,
    factory: sessionmaker[Session],
    ticked_merchant: tuple[uuid.UUID, str],
    manual_clock: clock.ManualClock,
) -> None:
    """``reclaim_stale`` has a caller. That is the whole claim, and it had none before.

    The row is what a hard-killed worker leaves behind: ``RUNNING``, with ``locked_by`` set and
    a ``locked_at`` that has stopped moving. Nothing in the worker loop rescues it — a graceful
    stop finishes its job and leaves nothing running, so this state only arises from a kill, and
    only the lease sweep can resolve it.
    """
    merchant_id, slug = ticked_merchant
    abandoned = uuid.uuid4()
    moment = manual_clock.now()

    with owner_engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO job (id, merchant_id, kind, payload, state, run_after,
                                 attempts, max_attempts, locked_by, locked_at, created_at)
                VALUES (:id, :m, 'lifecycle_evaluation', '{}'::jsonb, 'RUNNING', :run_after,
                        1, 5, 'worker-died', :locked_at, now())
                """
            ),
            {
                "id": str(abandoned),
                "m": str(merchant_id),
                "run_after": moment - timedelta(hours=1),
                # An hour of silence, well past DEFAULT_JOB_LEASE's fifteen minutes.
                "locked_at": moment - timedelta(hours=1),
            },
        )

    report = tick(merchant_slug=slug, factory=factory)

    assert report.reclaimed == 1, "the tick did not call the lease sweep"

    with owner_engine.begin() as connection:
        state, locked_by, locked_at = connection.execute(
            text("SELECT state, locked_by, locked_at FROM job WHERE id = :id"),
            {"id": str(abandoned)},
        ).one()

    assert str(state) == PENDING_STATE
    assert locked_by is None and locked_at is None, (
        "the job is claimable again but still carries the dead worker's lease"
    )


def test_a_lease_that_has_not_expired_is_left_alone(
    owner_engine: Engine,
    factory: sessionmaker[Session],
    ticked_merchant: tuple[uuid.UUID, str],
    manual_clock: clock.ManualClock,
) -> None:
    """A live worker's job is not taken away from it.

    The dangerous direction of the lease bound. Reclaiming late only delays a job whose worker
    is gone; reclaiming early hands a running job to a second worker, which is safe only because
    handlers are idempotent and pays for the work — including any provider read — twice.
    """
    merchant_id, slug = ticked_merchant
    live = uuid.uuid4()
    moment = manual_clock.now()

    with owner_engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO job (id, merchant_id, kind, payload, state, run_after,
                                 attempts, max_attempts, locked_by, locked_at, created_at)
                VALUES (:id, :m, 'lifecycle_evaluation', '{}'::jsonb, 'RUNNING', :run_after,
                        1, 5, 'worker-alive', :locked_at, now())
                """
            ),
            {
                "id": str(live),
                "m": str(merchant_id),
                "run_after": moment - timedelta(minutes=2),
                "locked_at": moment - timedelta(minutes=2),
            },
        )

    report = tick(merchant_slug=slug, factory=factory)

    assert report.reclaimed == 0

    with owner_engine.begin() as connection:
        state = connection.execute(
            text("SELECT state FROM job WHERE id = :id"), {"id": str(live)}
        ).scalar_one()
    assert str(state) == "RUNNING"
