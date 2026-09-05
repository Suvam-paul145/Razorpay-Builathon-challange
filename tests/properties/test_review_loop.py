"""Tasks 38.5 and 38.6. One review is one decision cycle, and it never buys the case time.

**Where the rest of R30's properties live, and why they are not here.** P63, P64 and P66 are
statements about a case's history under an arbitrary interleaving of review triggers with everything
else the system can do, so they are asserted inside the one machine that models the lifecycle —
``tests/properties/test_lifecycle_machine.py``, extended by task 38.4 with rules for
``sweep_review``, ``attach_event`` and a whole-worker restart, and with three invariants carrying
those three properties. One model of the lifecycle, one place an invariant is checked. Naming a file
after a property and putting a second state machine in it would have meant re-deriving the clock,
the provider, the merchant and the worker, and exploring reviews against a universe where nothing
else was happening — while the failures worth finding are a review racing an expiry and an attach
arriving on a case that reached its cap one step earlier.

So this file holds the three things that machine cannot state:

* **P65 (``pg``).** A uniqueness claim about the *queue*, which needs a second database session and
  a genuinely overlapping transaction. A state machine drives one session at a time by construction,
  so "two concurrent sweeps produce one pending job" is unstateable in it.
* **The index and the restart (``pg``).** That the sweeper's query is served by
  ``ix_recovery_case_due_for_review``, asserted from the query plan of the statement the repository
  actually emits; and that a sweep beginning with an empty queue finds the same due set (R30.C6).
* **The customer trigger (``pg``), added by task 40.3.** R30.C8 driven over the real HTTP surface,
  because the machine cannot obtain a wire token — see the seam comment in
  ``tests/properties/test_lifecycle_machine.py`` for why that is a fact about the credential rather
  than about the machine.

Plus three ``pure``-tier facts that need no database and would be silly to run against one: the
clamp arithmetic that is P63's second clause, the shape of the label tables R26.C14 requires, and
the state list that makes R30.C13's absence from the unresolved grouping structural.
"""

from __future__ import annotations

import threading
import time
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from hypothesis import given, settings
from hypothesis import strategies as st
from sqlalchemy import Engine, event, text
from sqlalchemy.orm import Session, sessionmaker

from revora.api.rendering import (
    CASE_STATE_LABELS,
    SELECTED_ACTION_LABELS,
    WAITING_AND_WATCHING,
    case_state_label,
    selected_action_label,
)
from revora.cases.review import CASE_REVIEW_KIND, enqueue_case_review, sweep_due_reviews
from revora.domain.actions import NULL_ACTIONS, CandidateAction
from revora.domain.enums import CaseState, ReviewTrigger
from revora.domain.transitions import TERMINAL_STATES
from revora.jobs.pipeline import _review_instant
from revora.metrics.unresolved import UNRESOLVED_STATES
from revora.persistence.repositories.cases import RecoveryCaseRepository
from revora.persistence.repositories.session import tenant_transaction
from revora.platform.clock import ManualClock, using_clock
from revora.platform.config import default_configuration
from tests.pg_support import insert_merchant
from tests.strategies.reviews import DRIVABLE_TRIGGERS, review_trigger_sequences

_CONFIG = default_configuration()

# No module-level marker: the two arithmetic tests below need no database and belong in the tier
# that runs on every commit, while the four queue and index tests need a migrated PostgreSQL. A
# module-level `pg` would drag the cheap ones into the slow selection and out of the fast one.


# ---------------------------------------------------------------------------
# `pure` — P63's clamp, and R26.C14's label tables
# ---------------------------------------------------------------------------


@pytest.mark.pure
@given(
    window_offset=st.timedeltas(
        min_value=timedelta(seconds=-3600), max_value=timedelta(days=30)
    ),
    interval=st.timedeltas(min_value=timedelta(seconds=0), max_value=timedelta(days=14)),
)
@settings(max_examples=500)
def test_p63_a_review_is_never_scheduled_past_the_window(
    window_offset: timedelta, interval: timedelta
) -> None:
    """**Property 63, second clause, as arithmetic.** The clamp cannot produce a late review.

    ``_review_instant`` is ``min(moment + interval, window_end_at)``, and the whole of R30's
    termination argument leans on it: the review loop is only bounded in wall-clock terms because
    ``window_end_at`` is immutable and no review is ever scheduled past it. The database's
    ``review_within_window`` CHECK refuses a violation and the lifecycle machine's P63 invariant
    asserts it over real rows — this is the same claim about the function itself, over inputs no
    live case would produce.

    The generated window offset reaches **backwards** on purpose. A window that has already closed
    is not a hypothetical: the optimizer runs in one job and the policy job that records its
    selection runs later, so a case whose window elapsed in between reaches the clamp with
    ``window_end_at < moment``. The function must answer ``None`` there rather than an instant in
    the past — an instant in the past means the sweeper finds the case due on its very next pass,
    spends a decision cycle on an evaluation whose window check refuses every candidate, and does it
    again. That is not a late review; it is a busy loop that leaves audit records.

    Two clauses, and the second is the one the CHECK constraint cannot express: whatever comes back
    is at or before the window end, **and** it is either ``None`` or strictly in the future.
    """
    moment = datetime(2025, 6, 1, tzinfo=UTC)
    window_end_at = moment + window_offset

    review_at = _review_instant(
        moment=moment, window_end_at=window_end_at, interval=interval
    )

    if review_at is None:
        assert min(moment + interval, window_end_at) <= moment, (
            "the clamp declined to schedule a review at an instant that is genuinely in the "
            "future; a case that chose restraint would then rest at POLICY_CHECK invisible to the "
            "sweeper's index predicate, which is the defect R30 exists to fix"
        )
        return

    assert review_at <= window_end_at, (
        f"a review was scheduled for {review_at}, after the window closes at {window_end_at}. "
        "Every termination bound in the system is measured against the window, so a review outside "
        "it is a decision cycle spent on a case the lifecycle sweep has already ended"
    )
    assert review_at > moment, (
        f"a review was scheduled for {review_at}, at or before the selection instant {moment}. "
        "The sweeper's predicate is `next_review_at <= now`, so this is due immediately and "
        "every pass will find it again"
    )


@pytest.mark.pure
def test_r26_c14_two_null_actions_share_one_label_and_three_endings_do_not() -> None:
    """R26.C14, as the shape of the two label tables (task 38.6).

    The requirement asks for three things at once and each is a separate assertion here:

    1. ``DO_NOTHING`` and ``WAIT`` render under **one shared** label conveying that the case is
       waiting and observing rather than stopped. To a merchant they are one situation, and a
       separate label for each invites a guess at a difference with no operational consequence.
    2. ``STOPPED``, ``BLOCKED`` and ``EXPIRED`` render under **three distinct** labels. They are the
       same money and three different problems, and the merchant's next action differs in each.
    3. Every member of both enumerations has a label, so the wire never carries a machine-generated
       word nobody chose. Totality is what forces that decision into the commit that adds an action.

    And the reason the tables are asserted rather than the rendered output: the constraint task 38.6
    works under is that the shared label must not be composed in the browser from two enum values.
    A client that mapped ``DO_NOTHING`` and ``WAIT`` onto one string would be a second vocabulary,
    free to drift from this one, and the drift would be towards rendering restraint as an ending.
    Testing the server-side table is testing what makes the browser's version impossible.
    """
    assert set(SELECTED_ACTION_LABELS) == {action.value for action in CandidateAction}, (
        "the action label table is not total; a candidate action with no label would reach the "
        "dashboard under a word derived from its member name rather than one somebody chose"
    )
    assert set(CASE_STATE_LABELS) == {state.value for state in CaseState}

    null_labels = {selected_action_label(action.value) for action in NULL_ACTIONS}
    assert null_labels == {WAITING_AND_WATCHING}, (
        f"the Null_Actions carry {sorted(null_labels)} rather than one shared label. R26.C14 wants "
        "one, because 'we looked and decided to leave this customer alone for now' is one situation"
    )

    ending_labels = [
        case_state_label(state.value)
        for state in (CaseState.STOPPED, CaseState.BLOCKED, CaseState.EXPIRED)
    ]
    assert len(set(ending_labels)) == 3, (
        f"the three Terminal_States R26.C14 names share a label: {ending_labels}. They are the "
        "same money and three different problems — stopped means the attempts ran out, blocked "
        "means policy refused, expired means the window closed with nothing having worked"
    )

    # R30.C13's client-side half, checked where the words are chosen. `POLICY_CHECK` must not read
    # as an ending, and it must not borrow a Terminal_State's label — the state a case rests in
    # when it chose restraint is the one state that must never be worded as a conclusion.
    waiting_state_label = case_state_label(CaseState.POLICY_CHECK.value)
    assert waiting_state_label not in {
        case_state_label(state.value) for state in TERMINAL_STATES
    }, (
        f"POLICY_CHECK is labelled {waiting_state_label!r}, which is also a Terminal_State's "
        "label. A case that chose restraint would be indistinguishable from one that ended"
    )


@pytest.mark.pure
def test_r30_c13_the_unresolved_grouping_scans_only_terminal_states() -> None:
    """R30.C13's absence from the grouping is structural, and this is the structure.

    The requirement says a case actively waiting for a review appears in **no** ended grouping and
    **no** Terminal_State grouping. The unresolved-revenue view is the grouping in question, and the
    honest way to establish the absence is to show it cannot be present: ``unresolved_groups``
    selects on ``state IN (UNRESOLVED_STATES)``, and every member of that tuple is a Terminal_State,
    so a case at ``POLICY_CHECK`` was never in the scanned set rather than filtered out of it.

    Worth asserting rather than assuming, because the two facts are separable. Somebody adding a
    sixth "unresolved" state — ``POLICY_CHECK`` being the obvious candidate, since a case waiting
    there *is* unresolved money in the ordinary sense of the words — would satisfy every other test
    in the suite and put a case Revora is still working into a grouping headed by how cases ended.
    The counterpart over live rows is the lifecycle machine's P66 invariant, which compares the
    grouping's own total against the number of cases actually in one of these states.
    """
    assert set(UNRESOLVED_STATES) <= TERMINAL_STATES, (
        f"the unresolved grouping scans {sorted(state.value for state in UNRESOLVED_STATES)}, "
        "which includes a non-terminal state. R14.C10 groups by how a case *ended*, so a "
        "non-terminal case in that scan is money reported as lost while it is still being worked"
    )
    assert CaseState.POLICY_CHECK not in UNRESOLVED_STATES
    assert CaseState.RECOVERED not in UNRESOLVED_STATES, (
        "RECOVERED is money that came back; this grouping is the money that did not"
    )


# ---------------------------------------------------------------------------
# `pg` — fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def factory(owner_engine: Engine) -> sessionmaker[Session]:
    """Sessions bound to the owner engine.

    The owner rather than ``revora_app`` because these tests write rows directly and read
    ``pg_stat_activity``; row-level security is a different property with its own tests, and running
    it here would make a lock-wait diagnosis a permissions diagnosis.
    """
    return sessionmaker(bind=owner_engine, expire_on_commit=False)


def _seed_case_due_for_review(
    engine: Engine,
    merchant_id: uuid.UUID,
    *,
    moment: datetime,
    cycles: int = 0,
    review_offset: timedelta = timedelta(hours=-1),
    state: CaseState = CaseState.POLICY_CHECK,
) -> uuid.UUID:
    """A case resting at ``POLICY_CHECK`` with a review instant already due.

    Written directly rather than driven through the pipeline, and that choice is the right one for
    *these* tests specifically. What is under test here is the sweeper's query, the job table's
    partial unique index and the behaviour of two overlapping transactions — none of which cares how
    the row came to look like this, and all of which is easier to reason about when the row's four
    relevant columns are stated in one place. The claim that the *pipeline* produces such a row is
    the lifecycle machine's, driven end to end through a signed webhook.

    ``review_offset`` defaults to an hour in the past so the case is due immediately.
    """
    case_id = uuid.uuid4()
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO recovery_case (
                    id, merchant_id, state, provider_payment_id, payment_amount, currency,
                    customer_key, detected_at, window_end_at, next_review_at,
                    decision_cycle_count, created_at
                ) VALUES (
                    :id, :merchant_id, :state, :payment_id, 5000, 'INR',
                    :customer_key, :detected_at, :window_end, :review_at, :cycles, now()
                )
                """
            ),
            {
                "id": str(case_id),
                "merchant_id": str(merchant_id),
                "state": state.value,
                "payment_id": f"pay_{case_id.hex[:14]}",
                "customer_key": f"ck-{case_id}",
                "detected_at": moment,
                "window_end": moment + _CONFIG.RECOVERY_WINDOW_DURATION,
                "review_at": moment + review_offset,
                "cycles": cycles,
            },
        )
    return case_id


def _pending_reviews(engine: Engine, merchant_id: uuid.UUID, case_id: uuid.UUID) -> int:
    """How many unclaimed review jobs exist for one case.

    Unclaimed only, because ``one_pending_job_per_dedupe_key`` is partial on pending rows: a claimed
    job has released the key, and counting it would report the mechanism working correctly as a
    violation.
    """
    with engine.begin() as connection:
        return int(
            connection.execute(
                text(
                    "SELECT count(*) FROM job WHERE merchant_id = :m AND kind = :k "
                    "AND case_id = :c AND state = 'PENDING'"
                ),
                {"m": str(merchant_id), "k": CASE_REVIEW_KIND, "c": str(case_id)},
            ).scalar_one()
        )


# ---------------------------------------------------------------------------
# `pg` — Property 65
# ---------------------------------------------------------------------------


@pytest.mark.pg
@given(triggers=review_trigger_sequences())
@settings(max_examples=25, deadline=None)
def test_p65_no_trigger_produces_a_second_cycle_while_one_is_unapplied(
    owner_engine: Engine, triggers: tuple[ReviewTrigger, ...]
) -> None:
    """**Property 65**, clauses one and three. Any number of triggers, at most one queued cycle.

    R30.C9 is worded to cover every Review_Trigger at once — *no second decision cycle for a case
    that already holds an unapplied enqueued one, irrespective of the Review_Trigger* — so the
    generated input is a sequence of **kinds** rather than a sequence of instants. A test that drove
    one kind repeatedly would establish the sweep is idempotent against itself and say nothing about
    the case the requirement is actually written for: a trigger of one kind arriving while another
    kind's cycle is still pending. See ``tests/strategies/reviews.py``.

    Driven through :func:`revora.cases.review.enqueue_case_review`, which is not a shortcut past the
    triggers — it is the single entry point all three of them share, and sharing it is exactly what
    makes R30.C9 hold "irrespective of the Review_Trigger" through one mechanism rather than three.
    The detection service's route into it is driven end to end through a signed webhook by the
    lifecycle machine's ``attach_event`` rule; what is under test here is the mechanism itself.

    Three assertions, and the third is the one that stops the property being satisfied by a
    mechanism that is simply broken: after the pending job is claimed the key is free again, so a
    later legitimate review **can** be enqueued. P65 forbids a duplicate cycle, not all future
    reviews — an implementation that refused forever would satisfy the first two clauses and quietly
    stop reviewing every case that was ever reviewed once.

    The session factory is built here rather than taken from the ``factory`` fixture. A
    function-scoped fixture is set up once for the whole test function, so every generated example
    would share it — and each example needs its own universe anyway, for the reason
    ``tests/pg_support.py`` gives about ``insert_merchant``: this property is about a unique
    constraint scoped by ``merchant_id``, so a shared merchant would let one example's rows collide
    with another's and report a failure that says nothing about the property.
    """
    factory = sessionmaker(bind=owner_engine, expire_on_commit=False)
    clock = ManualClock()
    with using_clock(clock):
        merchant_id = insert_merchant(owner_engine, display_name="Review dedupe")
        case_id = _seed_case_due_for_review(owner_engine, merchant_id, moment=clock.now())

        accepted = 0
        for trigger in triggers:
            with tenant_transaction(merchant_id, factory) as session:
                job_id = enqueue_case_review(
                    session, merchant_id, case_id, trigger=trigger
                )
            if job_id is not None:
                accepted += 1
            assert _pending_reviews(owner_engine, case_id=case_id, merchant_id=merchant_id) <= 1, (
                f"after triggers {[t.value for t in triggers[: accepted + 1]]} the case holds more "
                "than one pending review. Two queued cycles for one case is two decision cycles "
                "spent on one question, and the cap is the only thing bounding how many exist"
            )

        assert accepted == 1, (
            f"{accepted} of {len(triggers)} triggers were accepted. Exactly one should be: the "
            "first creates the pending job and every later one collides with "
            "`one_pending_job_per_dedupe_key`, whatever kind of trigger it was"
        )

        # Claim it, the way the worker does, which frees the dedupe key.
        with owner_engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE job SET state = 'RUNNING' WHERE merchant_id = :m AND kind = :k "
                    "AND case_id = :c AND state = 'PENDING'"
                ),
                {"m": str(merchant_id), "k": CASE_REVIEW_KIND, "c": str(case_id)},
            )

        with tenant_transaction(merchant_id, factory) as session:
            later = enqueue_case_review(
                session, merchant_id, case_id, trigger=ReviewTrigger.SCHEDULED_REVIEW
            )
        assert later is not None, (
            "no review could be enqueued after the pending one was claimed. P65 forbids a "
            "duplicate cycle, not every future review — an implementation that refused forever "
            "would stop reviewing every case it had ever reviewed once"
        )


@pytest.mark.pg
def test_p65_repeated_sweep_passes_over_an_unchanged_case_enqueue_one_cycle(
    owner_engine: Engine,
    factory: sessionmaker[Session],
) -> None:
    """**Property 65**, clause one through the sweeper rather than through the enqueue.

    R30.C9's second half is specific about the input: *at most one enqueued decision cycle from any
    number of Review_Sweeper passes over one Recovery_Case whose persisted fields are unchanged
    between those passes*. So this runs the sweep several times over a case nothing touches in
    between, and asserts that only the first pass produces work.

    Worth having beside the generated test above, because the sweep does more than enqueue: it reads
    a due set, releases it, and re-checks each case under its row lock. An implementation that
    re-checked the *state* and forgot that a review might already be pending would pass the
    generated test — which calls the enqueue directly — and fail this one.
    """
    clock = ManualClock()
    with using_clock(clock):
        merchant_id = insert_merchant(owner_engine, display_name="Review sweep repeat")
        case_id = _seed_case_due_for_review(owner_engine, merchant_id, moment=clock.now())

        counts = [sweep_due_reviews(merchant_id, factory=factory) for _ in range(4)]

        assert counts == [1, 0, 0, 0], (
            f"four sweeps over an unchanged case enqueued {counts}; R30.C9 allows one in total"
        )
        assert _pending_reviews(owner_engine, merchant_id, case_id) == 1


@pytest.mark.pg
def test_p65_two_concurrent_sweeps_against_one_case_produce_one_pending_job(
    owner_engine: Engine, factory: sessionmaker[Session]
) -> None:
    """**Property 65**, clause two, with genuine concurrency rather than two sequential calls.

    The sequential case is already covered above and it is the easy one — the second call reads a
    committed row and declines. What R30.C9 actually has to survive is two enqueues *in flight at
    the same time*, because that is what two worker replicas produce, and it is the case a
    read-then-write guard cannot handle: both read "nothing pending", both write, and the case gets
    two decision cycles.

    So this test earns the word "concurrent". The first transaction inserts and **does not commit**.
    A second thread then opens its own session and issues the same insert, which blocks on
    ``one_pending_job_per_dedupe_key`` — the index row is uncommitted, so PostgreSQL makes the
    second inserter wait rather than guess. That the overlap is real is **observed, not assumed**:
    the main thread polls ``pg_stat_activity`` until the second backend waits on a lock, and fails
    the test if it never does. Sleeping and hoping would leave the test passing for the wrong reason
    on a fast machine — the second insert having simply run after the first committed, which is the
    sequential case wearing a thread.

    When the first transaction commits, the second's ``ON CONFLICT DO NOTHING`` resolves against the
    now-visible row and returns no id. One job exists. The mechanism is the index, not a check in
    application code, which is the whole reason it holds across processes.
    """
    clock = ManualClock()
    with using_clock(clock):
        merchant_id = insert_merchant(owner_engine, display_name="Review concurrency")
        case_id = _seed_case_due_for_review(owner_engine, merchant_id, moment=clock.now())

        second_result: list[uuid.UUID | None] = []
        second_failure: list[BaseException] = []
        inserted = threading.Event()

        def contend() -> None:
            inserted.wait(timeout=15)
            try:
                with tenant_transaction(merchant_id, factory) as session:
                    second_result.append(
                        enqueue_case_review(
                            session,
                            merchant_id,
                            case_id,
                            trigger=ReviewTrigger.EVENT_ATTACHED,
                        )
                    )
            except BaseException as exc:
                second_failure.append(exc)

        contender = threading.Thread(target=contend, name="second-sweep", daemon=True)
        contender.start()
        try:
            with tenant_transaction(merchant_id, factory) as session:
                first = enqueue_case_review(
                    session, merchant_id, case_id, trigger=ReviewTrigger.SCHEDULED_REVIEW
                )
                assert first is not None
                session.flush()
                inserted.set()
                assert _wait_for_a_blocked_backend(owner_engine), (
                    "the second enqueue never blocked on a lock, so the two transactions did not "
                    "overlap and this test is the sequential case with extra machinery. Either the "
                    "partial unique index is gone or the second insert ran after the first "
                    "committed — both make the assertions below meaningless"
                )
            contender.join(timeout=20)
        finally:
            contender.join(timeout=5)

        assert not second_failure, f"the contending enqueue raised: {second_failure[0]!r}"
        assert not contender.is_alive(), (
            "the contending transaction never finished; it is still waiting on a lock the first "
            "transaction has released, which means the conflict was not resolved by the index"
        )
        assert second_result == [None], (
            f"the concurrent enqueue returned {second_result}; a second id means two decision "
            "cycles were queued for one case by two overlapping transactions, which is exactly "
            "what a read-then-write guard cannot prevent and the partial unique index can"
        )
        assert _pending_reviews(owner_engine, merchant_id, case_id) == 1


def _wait_for_a_blocked_backend(engine: Engine, *, timeout: float = 10.0) -> bool:
    """Poll until some backend on this database is waiting on a lock. Proof of overlap.

    A third connection, because the two contending ones are busy — one holds an open transaction and
    the other is blocked inside a statement. ``wait_event_type = 'Lock'`` is the observable form of
    "PostgreSQL has made this backend wait", which is the only thing that distinguishes a genuine
    overlap from two statements that happened to run in sequence.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with engine.connect() as connection:
            waiting = connection.execute(
                text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE datname = current_database() AND wait_event_type = 'Lock'"
                )
            ).scalar_one()
        if int(waiting) > 0:
            return True
        time.sleep(0.05)
    return False


# ---------------------------------------------------------------------------
# `pg` — the index, and the restart
# ---------------------------------------------------------------------------


@pytest.mark.pg
def test_the_sweepers_query_is_served_by_its_partial_index(owner_engine: Engine) -> None:
    """The due-set query uses ``ix_recovery_case_due_for_review`` (task 38.5).

    The index exists for this one query and its predicate is
    ``state = 'POLICY_CHECK' AND next_review_at IS NOT NULL``. A partial index is only usable when
    the planner can prove the query implies the predicate, so this assertion is really about
    agreement between two things written in different files: the ``where`` clause in
    ``RecoveryCaseRepository.list_due_for_review`` and the ``postgresql_where`` on the index in
    ``revora/persistence/models/cases.py``. Drop the ``next_review_at IS NOT NULL`` term from the
    query — redundant, since ``next_review_at <= now`` already excludes nulls — and the query still
    returns the right rows while the index stops being usable. Nothing else in the suite would
    notice, and the symptom in production is a sequential scan of every case a merchant has ever
    had, once every ``REVIEW_SWEEP_INTERVAL``.

    **The statement is captured, not retyped.** A hand-written copy of the query in this test would
    be a claim about the copy: it would keep passing while the repository's real ``where`` clause
    drifted away from the index. So a ``before_cursor_execute`` listener records the exact SQL and
    parameters the repository emits, and the plan is taken for that.

    ``enable_seqscan = off`` for the ``EXPLAIN`` only. On a table holding a handful of rows the
    planner will scan sequentially whichever indexes exist, because it is genuinely cheaper — so
    without this the test would assert that PostgreSQL costs small tables correctly rather than
    that the index covers the query. Turning it off asks the question this test means to ask: *is
    this index available for this query at all?* The answer is a plan naming it, or a plan naming
    no index at all.
    """
    clock = ManualClock()
    with using_clock(clock):
        merchant_id = insert_merchant(owner_engine, display_name="Review index")
        _seed_case_due_for_review(owner_engine, merchant_id, moment=clock.now())

        captured: list[tuple[str, object]] = []

        def record(
            conn: object,
            cursor: object,
            statement: str,
            parameters: object,
            context: object,
            executemany: bool,
        ) -> None:
            if "next_review_at" in statement and statement.lstrip().upper().startswith("SELECT"):
                captured.append((statement, parameters))

        event.listen(owner_engine, "before_cursor_execute", record)
        try:
            with tenant_transaction(merchant_id, sessionmaker(bind=owner_engine)) as session:
                RecoveryCaseRepository(session).list_due_for_review(
                    merchant_id,
                    now=clock.now(),
                    max_decision_cycles=_CONFIG.MAX_RECOVERY_ATTEMPTS,
                    limit=50,
                )
        finally:
            event.remove(owner_engine, "before_cursor_execute", record)

        assert len(captured) == 1, (
            f"expected to capture exactly one due-set query, captured {len(captured)}. The "
            "assertion below is about the statement the repository emits, so capturing the wrong "
            "number of them means it is about something else"
        )
        statement, parameters = captured[0]

        with owner_engine.begin() as connection:
            connection.exec_driver_sql("SET LOCAL enable_seqscan = off")
            plan = "\n".join(
                str(row[0])
                for row in connection.exec_driver_sql(f"EXPLAIN {statement}", parameters)  # type: ignore[arg-type]
            )

        assert "ix_recovery_case_due_for_review" in plan, (
            "the Review_Sweeper's due-set query is not served by its partial index. The plan was:\n"
            f"{plan}\n"
            "Either the query no longer implies the index predicate "
            "(state = 'POLICY_CHECK' AND next_review_at IS NOT NULL) or the index is gone. In "
            "production this is a sequential scan of every case the merchant has ever had, once "
            "every REVIEW_SWEEP_INTERVAL"
        )


@pytest.mark.pg
def test_the_sweeper_finds_its_due_set_after_a_restart_with_an_empty_queue(
    owner_engine: Engine,
) -> None:
    """R30.C6. Every input is a persisted column, so an empty queue costs the sweep nothing.

    The failure this guards against is a sweep that remembers. Anything held in the process — a
    schedule, a set of case ids already visited, a cached due list — is gone after a restart, and a
    sweep that depended on it would silently stop reviewing exactly the cases waiting when the
    process died. Those are the cases Revora already decided to be patient with, so nothing would
    look wrong: no error, no queue depth, no case in a stuck state. They would simply wait out their
    windows and expire.

    So the arrangement is deliberately hostile. The engine is disposed and rebuilt — a fresh
    connection pool, no session, nothing carried over — and the job table is emptied, which is
    stronger than a real restart: a real one leaves pending jobs behind, and this one denies the
    sweep even that. What remains is three columns on ``recovery_case``, which is exactly what
    R30.C6 says the sweep may depend on.

    The eligible set is asserted by identity rather than by count. Three cases are seeded and only
    one is eligible — the others are excluded by their state and by the cycle cap — so a sweep that
    found "one case" for the wrong reason fails here.
    """
    from revora.persistence.repositories.engine import (
        build_engine,
        dispose_engine,
        set_engine,
    )

    clock = ManualClock()
    with using_clock(clock):
        merchant_id = insert_merchant(owner_engine, display_name="Review restart")
        due = _seed_case_due_for_review(owner_engine, merchant_id, moment=clock.now())
        # Excluded by state: nothing outside POLICY_CHECK is waiting on a review.
        _seed_case_due_for_review(
            owner_engine,
            merchant_id,
            moment=clock.now(),
            state=CaseState.DIAGNOSED,
            review_offset=timedelta(hours=-1),
        )
        # Excluded by the cap: R30.C5 stops at MAX_RECOVERY_ATTEMPTS, and queueing this would
        # produce a job whose only possible outcome is a transition to STOPPED.
        capped = _seed_case_due_for_review(
            owner_engine,
            merchant_id,
            moment=clock.now(),
            cycles=_CONFIG.MAX_RECOVERY_ATTEMPTS,
        )

        with owner_engine.begin() as connection:
            connection.execute(
                text("DELETE FROM job WHERE merchant_id = :m"), {"m": str(merchant_id)}
            )

        dispose_engine()
        engine = build_engine(owner_engine.url.render_as_string(hide_password=False))
        set_engine(engine)
        try:
            enqueued = sweep_due_reviews(merchant_id)
        finally:
            dispose_engine()

        assert enqueued == 1, (
            f"a sweep starting with an empty job queue after a restart enqueued {enqueued} "
            "reviews. Its whole input is state, next_review_at and decision_cycle_count on the "
            "case row, so an empty queue must change nothing about what it finds"
        )
        assert _pending_reviews(owner_engine, merchant_id, due) == 1
        assert _pending_reviews(owner_engine, merchant_id, capped) == 0, (
            "a case already at MAX_RECOVERY_ATTEMPTS was enqueued for review. The sweep excludes "
            "it so the queue does not fill with jobs whose only outcome is a transition to STOPPED"
        )


# ---------------------------------------------------------------------------
# `pg` — the third trigger, now that it has a producer (task 40.3)
# ---------------------------------------------------------------------------


@pytest.mark.pg
def test_a_customer_signal_enqueues_exactly_one_review_through_the_real_http_surface(
    owner_engine: Engine,
) -> None:
    """**R30.C8** driven end to end: an accepted Customer_Signal enqueues one decision cycle.

    Task 38.5 left a note here saying this test was deliberately absent, because
    ``ReviewTrigger.CUSTOMER_SIGNAL`` had no producer and driving it by inserting a
    ``customer_signal`` row and calling the enqueue directly would have reported coverage of the one
    trigger path with no implementation behind it. Task 40.3 built the producer, so the note becomes
    a test — and it is driven **over HTTP through the mounted customer surface**, with a token
    minted by the real service, because every shortcut past the router skips a control.

    Four claims, and the third is the one that needed the real path.

    * The review is enqueued with the trigger recorded as ``CUSTOMER_SIGNAL``, so R30.C11's audit
      record can name which of the three triggers fired.
    * It goes through the **same** dedupe key as the other two, so five submissions produce one
      pending cycle. That is R30.C9 holding "irrespective of the Review_Trigger" through one
      mechanism rather than three, and it is why 40.3 needed no new idempotency machinery.
    * **No transition and no policy evaluation happen inside the request** (R30.C8's second and
      third clauses): the case is still at ``POLICY_CHECK`` afterwards, with ``next_review_at``
      intact and no ``policy_decision`` row.
    * A signal on a case that is **not** at ``POLICY_CHECK`` enqueues nothing, which is the
      requirement's ``WHEN`` clause rather than an optimisation — a case waiting on an outcome
      already has a cycle in flight, and a second would be two cycles spent on one question.
    """
    from revora.api.app import create_app
    from revora.customer.tokens import TokenService
    from revora.domain.actions import CandidateAction
    from revora.persistence.repositories.engine import (
        build_engine,
        dispose_engine,
        set_engine,
    )
    from tests.fakes.customer import installed_signing_secrets

    merchant_id = insert_merchant(owner_engine, display_name="Customer signal trigger")
    moment = datetime.now(UTC)
    waiting = _seed_case_due_for_review(owner_engine, merchant_id, moment=moment)
    with owner_engine.begin() as connection:
        slug = str(
            connection.execute(
                text("SELECT slug FROM merchant WHERE id = :m"), {"m": str(merchant_id)}
            ).scalar_one()
        )
    executing = uuid.uuid4()
    with owner_engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO recovery_case (
                    id, merchant_id, state, provider_payment_id, payment_amount, currency,
                    customer_key, detected_at, window_end_at, decision_cycle_count, created_at
                ) VALUES (
                    :id, :m, 'WAITING_FOR_OUTCOME', :pid, 249900, 'INR', :ck, :detected,
                    :window_end, 1, now()
                )
                """
            ),
            {
                "id": str(executing),
                "m": str(merchant_id),
                "pid": f"pay_{executing.hex[:14]}",
                "ck": f"ck-{executing}",
                "detected": moment,
                "window_end": moment + timedelta(days=7),
            },
        )

    set_engine(build_engine(owner_engine.url.render_as_string(hide_password=False)))
    try:
        with installed_signing_secrets(1):
            tokens: dict[uuid.UUID, str] = {}
            for case_id in (waiting, executing):
                with tenant_transaction(merchant_id) as session:
                    minted = TokenService.on_session(session, _CONFIG).mint(
                        merchant_id,
                        case_id=case_id,
                        window_end_at=moment + timedelta(days=7),
                        approved_action=CandidateAction.PAYMENT_LINK,
                        moment=moment,
                    )
                assert minted.token is not None and minted.token.wire_token is not None
                tokens[case_id] = minted.token.wire_token

            app = create_app(verify_schema=False, serve_dashboard=False)
            with TestClient(app) as client:
                for case_id in (waiting, executing):
                    for _ in range(5):
                        response = client.post(
                            f"/customer/{slug}/delay-reason",
                            headers={
                                "Authorization": f"Bearer {tokens[case_id]}",
                                "Content-Type": "application/json",
                            },
                            json={"delay_reason": "SALARY_OR_CASHFLOW_TIMING"},
                        )
                        assert response.status_code in {201, 429}, response.text
    finally:
        dispose_engine()

    with owner_engine.connect() as connection:
        jobs = connection.execute(
            text(
                "SELECT case_id, payload->>'review_trigger' AS trigger, state, dedupe_key "
                "FROM job WHERE merchant_id = :m AND kind = :k"
            ),
            {"m": str(merchant_id), "k": CASE_REVIEW_KIND},
        ).all()
        still_waiting = connection.execute(
            text(
                "SELECT state, next_review_at IS NOT NULL FROM recovery_case WHERE id = :c"
            ),
            {"c": str(waiting)},
        ).one()
        decisions = int(
            connection.execute(
                text("SELECT count(*) FROM policy_decision WHERE merchant_id = :m"),
                {"m": str(merchant_id)},
            ).scalar_one()
        )

    assert len(jobs) == 1, (
        f"{len(jobs)} review jobs after five submissions on each of two cases. R30.C9 allows one "
        "for the case at POLICY_CHECK and none for the case waiting on an outcome"
    )
    assert str(jobs[0][0]) == str(waiting), (
        "the review was enqueued for the wrong case, or for the case that was not resting at "
        "POLICY_CHECK — R30.C8's WHEN clause is the state, not the signal"
    )
    assert jobs[0][1] == ReviewTrigger.CUSTOMER_SIGNAL.value, (
        f"the job records the trigger as {jobs[0][1]!r}; R30.C11's audit record has to be able to "
        "name which of the three triggers fired, and it reads this payload"
    )
    assert jobs[0][3] == f"{CASE_REVIEW_KIND}:{waiting}", (
        "the customer trigger used a different dedupe key from the sweeper's, so R30.C9's "
        "'irrespective of the Review_Trigger' would hold through two mechanisms instead of one"
    )
    assert tuple(still_waiting) == ("POLICY_CHECK", True), (
        f"the case is {still_waiting[0]} with next_review_at present={still_waiting[1]}. R30.C8 "
        "forbids a transition inside the accepting request, and the review instant is cleared by "
        "the edge out of POLICY_CHECK rather than by the signal"
    )
    assert decisions == 0, (
        "a policy decision exists, so the accepting request caused an evaluation (R30.C8)"
    )


# The seam this file used to carry is closed. Task 38.5 recorded that
# ``ReviewTrigger.CUSTOMER_SIGNAL`` was generated and filtered out because it had no producer, and
# that when one appeared this file would want a case driven *through* it rather than through the
# shared enqueue. The test above is that case. The assertion below is what will notice the next
# time the enumeration grows past what something can actually drive.
assert DRIVABLE_TRIGGERS == (
    ReviewTrigger.SCHEDULED_REVIEW,
    ReviewTrigger.EVENT_ATTACHED,
    ReviewTrigger.CUSTOMER_SIGNAL,
), (
    "the drivable-trigger list changed. Every member of ReviewTrigger is drivable as of task 40.3, "
    "so a shorter list means a member lost its producer and a longer one means a member was "
    "declared before it had one — and in the second case this file wants a case driven through it "
    "rather than through the shared enqueue"
)
