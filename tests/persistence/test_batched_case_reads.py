"""The batched page reads, against the per-case reads they replaced.

The case list used to render a hundred rows with five hundred statements, because every summary row
drew on five other tables and the composition was per row. It now issues five reads for the page.
**The speedup is not what these tests are about.** What they are about is that the batched route and
the per-case route return the same rows and render the same document, because the failure mode of
getting this wrong is not a slow page — it is a list column that quietly disagrees with the detail
page it links to, on a screen whose whole purpose is explaining a decision.

Four claims, and each one names a specific way the batching could have been wrong.

**Same rows.** Every batch read is asserted against its singular counterpart, case by case, on a
universe built to make the choosing matter: cases with several recommendations across cycles,
several active diagnoses across cycles, several attempts, several decisions in one cycle and
decisions filed under a cycle nobody asked for. A batch read that picked the oldest recommendation
instead of the newest would pass a test built on one-row-per-case and fail here.

**Same document.** ``case_summary`` is called both ways for every case and the two dicts asserted
equal. This is the property the endpoint's contract actually rests on, and it is asserted on the
rendered document rather than on the rows so that a field which happens to read a row differently
depending on how it arrived cannot slip through.

**The cycle is still the recommendation's cycle.** The number a case's live policy decisions are
filed under is not ``recovery_case.decision_cycle_count``, and reading the counter instead finds
nothing while raising nothing — it renders a fully evaluated case as having no policy decision. That
mistake has been made three times in this codebase, and moving the derivation into a batch read is
exactly the kind of change that would make it a fourth. So one test pins a case whose counter and
whose recommendation cycle differ, with real decisions under both, and asserts which one is read.

**Nothing crosses a merchant.** Every batched query is asserted to return nothing for another
tenant's case ids and to filter a mixed collection down to the caller's own. A batch read that
forgot its ``merchant_id`` clause is the worst bug available on this surface — it would be invisible
in a single-tenant test database and it would be a cross-tenant disclosure in production — so the
reads here run as the application role with row-level security enforced, and the assertion is made
against a second merchant holding the same shapes rather than against an empty one.

The universe is built with explicit SQL rather than by driving the pipeline, which is the opposite
of what ``tests/api/test_dashboard_reads.py`` does and is deliberate. That tier asserts the read
model against what the system really writes; this one needs shapes the pipeline does not produce on
demand — two active diagnoses, a counter ahead of its recommendation, three decisions in one cycle —
and a test that cannot construct its awkward cases is a test of the easy ones.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import timedelta

import pytest
from sqlalchemy import Engine, event, text
from sqlalchemy.orm import Session, sessionmaker

from revora.api.views import (
    CaseSummaryReads,
    _arrangement_requests,
    _cases_by_id,
    case_summary,
    case_summary_reads,
)
from revora.customer.arrangements import first_arrangement_request
from revora.domain.enums import (
    POLICY_CHECK_ORDER,
    CaseState,
    CustomerSignalKind,
    DelayReason,
    HardStopReason,
    RiskCause,
    TerminalReason,
)
from revora.persistence.models import RecoveryCase
from revora.persistence.repositories.cases import RecoveryCaseRepository
from revora.persistence.repositories.diagnosis import DiagnosisRepository
from revora.persistence.repositories.execution import (
    ExecutionIntentRepository,
    RecoveryOutcomeRepository,
)
from revora.persistence.repositories.policy import PolicyDecisionRepository
from revora.persistence.repositories.recommendations import RecommendationRepository
from revora.persistence.repositories.session import tenant_transaction
from revora.platform.clock import now
from revora.platform.config import default_configuration

pytestmark = pytest.mark.pg

_CONFIG = default_configuration()

_EXPECTED_PAGE_READS = 5
"""How many statements one page of summaries costs, whatever the page holds.

Five, one per table, and the number is asserted rather than described because it is the whole point
of the change: the recommendations, the outcomes, the diagnoses, the attempts and the cycle's policy
decisions. It is not four — the policy read needs a cycle per case and the cycle comes from that
case's recommendation, so the two cannot share a statement without expressing the derivation in SQL.
"""


@dataclass(slots=True)
class _Counted:
    """Statements issued while a :func:`_counting` block was open."""

    statements: list[str] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.statements)


@contextmanager
def _counting(engine: Engine) -> Iterator[_Counted]:
    """Count the statements one block issues, on one engine, and detach afterwards.

    Deliberately **not** ``revora.platform.sqltrace.install``. That attaches its listeners to the
    ``Engine`` class for the life of the process and sets a module flag, so a test using it would
    leave every later test in the run measured — and would leave the tracer's own tests asserting
    against state this file had installed. A listener on one engine, removed on the way out, counts
    the same statements and belongs to the test that asked for it.
    """
    counted = _Counted()

    def _record(conn, cursor, statement, parameters, context, executemany) -> None:  # type: ignore[no-untyped-def]
        counted.statements.append(statement)

    event.listen(engine, "before_cursor_execute", _record)
    try:
        yield counted
    finally:
        event.remove(engine, "before_cursor_execute", _record)


# ---------------------------------------------------------------------------
# Row builders
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Case:
    """One built case and the shape it was built to have, for failure messages."""

    case_id: uuid.UUID
    shape: str


def _case(
    engine: Engine,
    merchant_id: uuid.UUID,
    *,
    shape: str,
    state: CaseState = CaseState.POLICY_CHECK,
    decision_cycle_count: int = 1,
    next_review_at_offset: timedelta | None = None,
    terminal_reason: TerminalReason | None = None,
) -> _Case:
    case_id = uuid.uuid4()
    moment = now()
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO recovery_case (
                    id, merchant_id, state, provider_payment_id, payment_amount, currency,
                    customer_key, customer_contact_masked, detected_at, window_end_at,
                    decision_cycle_count, next_review_at, terminal_reason, created_at
                ) VALUES (
                    :id, :m, :state, :pid, 250000, 'INR', :ck, '******3210',
                    :detected_at, :window_end_at, :cycles, :review, :reason, now()
                )
                """
            ),
            {
                "id": str(case_id),
                "m": str(merchant_id),
                "state": state.value,
                "pid": f"pay_{case_id.hex[:16]}",
                "ck": f"ck-{case_id}",
                "detected_at": moment - timedelta(hours=1),
                "window_end_at": moment + timedelta(hours=167),
                "cycles": decision_cycle_count,
                "review": (
                    None
                    if next_review_at_offset is None
                    else moment + next_review_at_offset
                ),
                "reason": None if terminal_reason is None else terminal_reason.value,
            },
        )
    return _Case(case_id=case_id, shape=shape)


def _recommendation(
    engine: Engine,
    merchant_id: uuid.UUID,
    case_id: uuid.UUID,
    *,
    cycle: int,
    action: str = "PAYMENT_LINK",
) -> uuid.UUID:
    """A recommendation and the baseline estimate its ``NOT NULL`` key demands."""
    baseline_id = uuid.uuid4()
    recommendation_id = uuid.uuid4()
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO baseline_estimate (
                    id, merchant_id, case_id, decision_cycle, probability, method,
                    validation_status, created_at
                ) VALUES (
                    :id, :m, :c, :cycle, 0.2500, 'DETERMINISTIC', 'UNVALIDATED_BASELINE', now()
                )
                """
            ),
            {
                "id": str(baseline_id),
                "m": str(merchant_id),
                "c": str(case_id),
                "cycle": cycle,
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO recommendation (
                    id, merchant_id, case_id, baseline_estimate_id, decision_cycle,
                    selected_action, selection_reason, created_at
                ) VALUES (
                    :id, :m, :c, :b, :cycle, :action, 'HIGHEST_NET_VALUE', now()
                )
                """
            ),
            {
                "id": str(recommendation_id),
                "m": str(merchant_id),
                "c": str(case_id),
                "b": str(baseline_id),
                "cycle": cycle,
                "action": action,
            },
        )
    return recommendation_id


def _diagnosis(
    engine: Engine,
    merchant_id: uuid.UUID,
    case_id: uuid.UUID,
    *,
    cycle: int,
    cause: RiskCause,
    is_active: bool = True,
) -> uuid.UUID:
    diagnosis_id = uuid.uuid4()
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO diagnosis (
                    id, merchant_id, case_id, cause, confidence, method, decision_cycle,
                    is_active, created_at
                ) VALUES (
                    :id, :m, :c, :cause, 0.900, 'DETERMINISTIC', :cycle, :active, now()
                )
                """
            ),
            {
                "id": str(diagnosis_id),
                "m": str(merchant_id),
                "c": str(case_id),
                "cause": cause.value,
                "cycle": cycle,
                "active": is_active,
            },
        )
    return diagnosis_id


def _policy_decision(
    engine: Engine,
    merchant_id: uuid.UUID,
    case_id: uuid.UUID,
    *,
    cycle: int,
    verdict: str = "APPROVED",
    reason: str = "ALL_CHECKS_PASSED",
    evaluated_offset: timedelta = timedelta(0),
    checks: int = 0,
) -> uuid.UUID:
    decision_id = uuid.uuid4()
    moment = now() + evaluated_offset
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO policy_decision (
                    id, merchant_id, case_id, verdict, primary_reason, rule_set_version,
                    evaluated_at, expires_at, selected_action, case_state_at_evaluation,
                    decision_cycle, created_at
                ) VALUES (
                    :id, :m, :c, :verdict, :reason, 'v1', :evaluated_at, :expires_at,
                    'PAYMENT_LINK', 'POLICY_CHECK', :cycle, now()
                )
                """
            ),
            {
                "id": str(decision_id),
                "m": str(merchant_id),
                "c": str(case_id),
                "verdict": verdict,
                "reason": reason,
                "evaluated_at": moment,
                "expires_at": moment + timedelta(minutes=15),
                "cycle": cycle,
            },
        )
        for order in range(1, checks + 1):
            connection.execute(
                text(
                    """
                    INSERT INTO policy_check_result (
                        id, merchant_id, policy_decision_id, check_order, check_id,
                        outcome, detail, created_at
                    ) VALUES (:id, :m, :d, :order, :check, 'PASS', :detail, now())
                    """
                ),
                {
                    "id": str(uuid.uuid4()),
                    "m": str(merchant_id),
                    "d": str(decision_id),
                    "order": order,
                    "check": POLICY_CHECK_ORDER[order - 1].value,
                    "detail": f"check {order}",
                },
            )
    return decision_id


def _intent(
    engine: Engine,
    merchant_id: uuid.UUID,
    case_id: uuid.UUID,
    decision_id: uuid.UUID,
    *,
    ordinal: int,
    state: str = "CONFIRMED",
    action: str = "PAYMENT_LINK",
) -> uuid.UUID:
    intent_id = uuid.uuid4()
    moment = now()
    resolved = state in ("CONFIRMED", "FAILED")
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO execution_intent (
                    id, merchant_id, case_id, policy_decision_id, idempotency_key, action,
                    attempt_ordinal, state, attempt_started_at, resolved_at, created_at
                ) VALUES (
                    :id, :m, :c, :d, :key, :action, :ordinal, :state, :started, :resolved, now()
                )
                """
            ),
            {
                "id": str(intent_id),
                "m": str(merchant_id),
                "c": str(case_id),
                "d": str(decision_id),
                "key": f"idem-{intent_id}",
                "action": action,
                "ordinal": ordinal,
                "state": state,
                "started": moment,
                "resolved": moment if resolved else None,
            },
        )
    return intent_id


def _outcome(
    engine: Engine,
    merchant_id: uuid.UUID,
    case_id: uuid.UUID,
    *,
    amount: int,
    classification: str = "OBSERVED",
) -> uuid.UUID:
    """A verified outcome and the authoritative read its ``NOT NULL`` key demands."""
    read_id = uuid.uuid4()
    outcome_id = uuid.uuid4()
    moment = now()
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO payment_state_read (
                    id, merchant_id, case_id, provider_payment_id, status, amount,
                    captured, read_at, created_at
                ) VALUES (:id, :m, :c, :pid, 'captured', :amount, true, :read_at, now())
                """
            ),
            {
                "id": str(read_id),
                "m": str(merchant_id),
                "c": str(case_id),
                "pid": f"pay_{case_id.hex[:16]}",
                "amount": amount,
                "read_at": moment,
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO recovery_outcome (
                    id, merchant_id, case_id, classification, recovered_amount,
                    recovery_timestamp, verified_by_read_id, created_at
                ) VALUES (:id, :m, :c, :class, :amount, :ts, :read_id, now())
                """
            ),
            {
                "id": str(outcome_id),
                "m": str(merchant_id),
                "c": str(case_id),
                "class": classification,
                "amount": amount,
                "ts": moment,
                "read_id": str(read_id),
            },
        )
    return outcome_id


def _signal(
    engine: Engine,
    merchant_id: uuid.UUID,
    case_id: uuid.UUID,
    *,
    kind: CustomerSignalKind,
    submitted_offset: timedelta,
    note: str | None = None,
    truncated: bool = False,
    delay_reason: DelayReason | None = None,
) -> uuid.UUID:
    """One customer signal.

    ``delay_reason`` is required for a ``DELAY_REASON`` signal and forbidden on a
    ``PROMISE_TO_PAY`` one — both are ``CHECK`` constraints, so the builder passes it through rather
    than filling one in and hiding which kinds carry one.
    """
    signal_id = uuid.uuid4()
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO customer_signal (
                    id, merchant_id, case_id, token_id, kind, delay_reason,
                    delay_reason_note, note_truncated, submitted_at, created_at
                ) VALUES (
                    :id, :m, :c, :token, :kind, :reason, :note, :truncated, :submitted, now()
                )
                """
            ),
            {
                "id": str(signal_id),
                "m": str(merchant_id),
                "c": str(case_id),
                "token": f"tok-{signal_id}",
                "kind": kind.value,
                "reason": None if delay_reason is None else delay_reason.value,
                "note": note,
                "truncated": truncated,
                "submitted": now() + submitted_offset,
            },
        )
    return signal_id


def _suppression(
    engine: Engine,
    merchant_id: uuid.UUID,
    case_id: uuid.UUID,
    signal_id: uuid.UUID,
    *,
    reason: HardStopReason = HardStopReason.DISPUTES_THE_CHARGE,
) -> uuid.UUID:
    suppression_id = uuid.uuid4()
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO contact_suppression (
                    id, merchant_id, scope_key, origin_case_id, customer_signal_id,
                    hard_stop_reason, suppressed_at, created_at
                ) VALUES (:id, :m, :scope, :c, :s, :reason, now(), now())
                """
            ),
            {
                "id": str(suppression_id),
                "m": str(merchant_id),
                "scope": f"scope-{case_id}",
                "c": str(case_id),
                "s": str(signal_id),
                "reason": reason.value,
            },
        )
    return suppression_id


# ---------------------------------------------------------------------------
# The universe
# ---------------------------------------------------------------------------


def _build_universe(engine: Engine, merchant_id: uuid.UUID) -> list[_Case]:
    """Cases covering every shape the batched reads have to choose between.

    Every entry exists because some plausible batching mistake would pass without it.
    """
    cases: list[_Case] = []

    # Nothing at all. A bundle of absences, which must render as markers rather than as a
    # missing mapping key.
    cases.append(_case(engine, merchant_id, shape="bare", state=CaseState.DETECTED))

    # Several recommendations across cycles, so "latest" is a choice and not the only row.
    # The cycles are inserted out of order on purpose: a batch read that relied on insertion
    # order rather than on the sort keys would pass with them ascending.
    multi = _case(engine, merchant_id, shape="three cycles", decision_cycle_count=3)
    for cycle, action in ((2, "CUSTOMER_MESSAGE"), (1, "WAIT"), (3, "PAYMENT_LINK")):
        _recommendation(engine, merchant_id, multi.case_id, cycle=cycle, action=action)
        _diagnosis(
            engine,
            merchant_id,
            multi.case_id,
            cycle=cycle,
            cause=(
                RiskCause.INSUFFICIENT_FUNDS
                if cycle < 3
                else RiskCause.EXPIRED_PAYMENT_METHOD
            ),
        )
    # Three decisions in the newest cycle — a case can hold several in one cycle, and the
    # summary reports the last of them by evaluation time.
    newest = _policy_decision(
        engine,
        merchant_id,
        multi.case_id,
        cycle=3,
        verdict="DEFERRED",
        reason="COOLDOWN_NOT_ELAPSED",
        evaluated_offset=timedelta(minutes=-30),
        checks=12,
    )
    _policy_decision(
        engine,
        merchant_id,
        multi.case_id,
        cycle=3,
        verdict="BLOCKED",
        reason="CONSENT_MISSING",
        evaluated_offset=timedelta(minutes=-20),
        checks=12,
    )
    _policy_decision(
        engine,
        merchant_id,
        multi.case_id,
        cycle=3,
        verdict="APPROVED",
        evaluated_offset=timedelta(minutes=-10),
        checks=12,
    )
    # And a decision under an older cycle, which must not be read for this case.
    _policy_decision(
        engine, merchant_id, multi.case_id, cycle=1, verdict="APPROVED", checks=12
    )
    for ordinal, state in ((1, "FAILED"), (2, "CONFIRMED"), (3, "ATTEMPTED")):
        _intent(engine, merchant_id, multi.case_id, newest, ordinal=ordinal, state=state)
    cases.append(multi)

    # Recovered, with an outcome and one confirmed attempt.
    recovered = _case(
        engine, merchant_id, shape="recovered", state=CaseState.RECOVERED
    )
    _recommendation(engine, merchant_id, recovered.case_id, cycle=1)
    _diagnosis(
        engine,
        merchant_id,
        recovered.case_id,
        cycle=1,
        cause=RiskCause.INSUFFICIENT_FUNDS,
    )
    decision = _policy_decision(
        engine, merchant_id, recovered.case_id, cycle=1, checks=12
    )
    _intent(engine, merchant_id, recovered.case_id, decision, ordinal=1)
    _outcome(engine, merchant_id, recovered.case_id, amount=250_000)
    cases.append(recovered)

    # Actively waiting: POLICY_CHECK, a future review instant, a counter below the cap. The
    # one shape whose summary carries a `waiting` block, and the block reads the selected
    # action off the recommendation the batch chose.
    waiting = _case(
        engine,
        merchant_id,
        shape="waiting",
        state=CaseState.POLICY_CHECK,
        decision_cycle_count=1,
        next_review_at_offset=timedelta(hours=6),
    )
    _recommendation(engine, merchant_id, waiting.case_id, cycle=1, action="WAIT")
    _diagnosis(engine, merchant_id, waiting.case_id, cycle=1, cause=RiskCause.UNKNOWN)
    _policy_decision(engine, merchant_id, waiting.case_id, cycle=1, checks=12)
    cases.append(waiting)

    # A superseded diagnosis beside an active one on an older cycle: the active read must
    # ignore `is_active = false` however new it is.
    superseded = _case(engine, merchant_id, shape="superseded diagnosis")
    _diagnosis(
        engine,
        merchant_id,
        superseded.case_id,
        cycle=1,
        cause=RiskCause.INSUFFICIENT_FUNDS,
    )
    _diagnosis(
        engine,
        merchant_id,
        superseded.case_id,
        cycle=2,
        cause=RiskCause.EXPIRED_PAYMENT_METHOD,
        is_active=False,
    )
    cases.append(superseded)

    # A recommendation whose cycle is *behind* the case counter, with decisions under both.
    # This is the shape that catches a batch read deriving the cycle from the counter.
    behind = _case(engine, merchant_id, shape="counter ahead", decision_cycle_count=4)
    _recommendation(engine, merchant_id, behind.case_id, cycle=3, action="PAYMENT_LINK")
    _policy_decision(
        engine,
        merchant_id,
        behind.case_id,
        cycle=3,
        verdict="APPROVED",
        checks=12,
    )
    _policy_decision(
        engine,
        merchant_id,
        behind.case_id,
        cycle=4,
        verdict="BLOCKED",
        reason="CONSENT_MISSING",
        checks=12,
    )
    cases.append(behind)

    # Decisions but no recommendation, filed under the counter — the honest fallback, and
    # the one case where reading the counter is correct.
    fallback = _case(engine, merchant_id, shape="no recommendation", decision_cycle_count=2)
    _policy_decision(
        engine,
        merchant_id,
        fallback.case_id,
        cycle=2,
        verdict="BLOCKED",
        reason="CONSENT_MISSING",
        checks=12,
    )
    cases.append(fallback)

    # A decision with a partial evaluation, so `_check_documents` has a NOT_RECORDED row to
    # render and the batched check read has to deliver a short sequence rather than nothing.
    partial = _case(engine, merchant_id, shape="partial checks", decision_cycle_count=1)
    _recommendation(engine, merchant_id, partial.case_id, cycle=1)
    _policy_decision(engine, merchant_id, partial.case_id, cycle=1, checks=5)
    cases.append(partial)

    return cases


@pytest.fixture
def factory(app_engine: Engine) -> sessionmaker[Session]:
    """Sessions on the **application** role, so row-level security is enforced on every read.

    Not the owner role. The owner bypasses RLS, so a batch query that had lost its
    ``merchant_id`` clause would still pass every isolation assertion below — the mistake would
    be invisible in exactly the tier written to catch it.
    """
    return sessionmaker(bind=app_engine, expire_on_commit=False)


@pytest.fixture
def universe(owner_engine: Engine, merchant_id: uuid.UUID) -> list[_Case]:
    return _build_universe(owner_engine, merchant_id)


def _rows(session: Session, merchant_id: uuid.UUID, cases: Sequence[_Case]) -> list[RecoveryCase]:
    """The case rows, in the list endpoint's order, read inside the caller's transaction."""
    repository = RecoveryCaseRepository(session)
    ids = [case.case_id for case in cases]
    statement = (
        repository.scoped(merchant_id)
        .where(RecoveryCase.id.in_(ids))
        .order_by(RecoveryCase.detected_at.desc(), RecoveryCase.id)
    )
    return list(session.execute(statement).scalars())


# ---------------------------------------------------------------------------
# Same rows
# ---------------------------------------------------------------------------


def test_each_batched_read_returns_what_the_per_case_read_returns(
    factory: sessionmaker[Session], merchant_id: uuid.UUID, universe: list[_Case]
) -> None:
    """Every batch read, case by case, against its singular counterpart.

    Compared by row identity rather than by a field, so a batch read that returned the right
    *shape* from the wrong row fails. The comparison covers the absences too: a case with no
    recommendation must be missing from the mapping, not present holding somebody else's.
    """
    shapes = {case.case_id: case.shape for case in universe}
    with tenant_transaction(merchant_id, factory) as session:
        cases = _rows(session, merchant_id, universe)
        ids = [case.id for case in cases]
        assert len(cases) == len(universe)

        recommendations = RecommendationRepository(session).latest_for_cases(merchant_id, ids)
        outcomes = RecoveryOutcomeRepository(session).for_cases(merchant_id, ids)
        diagnoses = DiagnosisRepository(session).active_for_cases(merchant_id, ids)
        intents = ExecutionIntentRepository(session).list_for_cases(merchant_id, ids)

        for case in cases:
            shape = shapes[case.id]
            expected = RecommendationRepository(session).latest_for_case(merchant_id, case.id)
            batched = recommendations.get(case.id)
            assert (None if expected is None else expected.id) == (
                None if batched is None else batched.id
            ), f"latest recommendation differs on the '{shape}' case"

            expected_outcome = RecoveryOutcomeRepository(session).for_case(merchant_id, case.id)
            batched_outcome = outcomes.get(case.id)
            assert (None if expected_outcome is None else expected_outcome.id) == (
                None if batched_outcome is None else batched_outcome.id
            ), f"outcome differs on the '{shape}' case"

            expected_diagnosis = DiagnosisRepository(session).active_for_case(
                merchant_id, case.id
            )
            batched_diagnosis = diagnoses.get(case.id)
            assert (None if expected_diagnosis is None else expected_diagnosis.id) == (
                None if batched_diagnosis is None else batched_diagnosis.id
            ), f"active diagnosis differs on the '{shape}' case"

            assert [row.id for row in intents.get(case.id, ())] == [
                row.id
                for row in ExecutionIntentRepository(session).list_for_case(
                    merchant_id, case.id
                )
            ], f"attempts differ on the '{shape}' case"


def test_the_batched_decision_read_matches_the_per_cycle_read(
    factory: sessionmaker[Session], merchant_id: uuid.UUID, universe: list[_Case]
) -> None:
    """The cycle's decisions, in order, for a hundred cases naming different cycles.

    Asserted as a list of ids rather than a count, because the order is what the summary's
    ``policy_decision`` column reads off — it reports the *last* decision by evaluation time, so a
    batch read that returned the same set in a different order would change a rendered verdict
    without changing a row count.
    """
    shapes = {case.case_id: case.shape for case in universe}
    with tenant_transaction(merchant_id, factory) as session:
        cases = _rows(session, merchant_id, universe)
        reads = case_summary_reads(session, merchant_id, cases)
        repository = PolicyDecisionRepository(session)
        recommendations = RecommendationRepository(session)
        for case in cases:
            recommendation = recommendations.latest_for_case(merchant_id, case.id)
            cycle = (
                int(case.decision_cycle_count)
                if recommendation is None
                else int(recommendation.decision_cycle)
            )
            expected = repository.for_cycle(merchant_id, case.id, cycle)
            assert [row.id for row in reads[case.id].decisions] == [
                row.id for row in expected
            ], f"decisions differ on the '{shapes[case.id]}' case (cycle {cycle})"


def test_the_batched_check_results_match_the_per_decision_read(
    factory: sessionmaker[Session], merchant_id: uuid.UUID, universe: list[_Case]
) -> None:
    """The twelve check rows, batched across every decision of every case.

    Includes a decision with only five recorded checks, because an absent decision key and a
    decision with a short list have to reach ``_check_documents`` as the same thing the singular
    read produces — an empty or partial sequence it renders ``NOT_RECORDED`` from, never a
    shortened list.
    """
    with tenant_transaction(merchant_id, factory) as session:
        repository = PolicyDecisionRepository(session)
        cases = _rows(session, merchant_id, universe)
        decision_ids: list[uuid.UUID] = []
        for case in cases:
            for cycle in range(0, 6):
                decision_ids.extend(
                    row.id for row in repository.for_cycle(merchant_id, case.id, cycle)
                )
        assert decision_ids, "the universe built no policy decisions to batch"

        batched = repository.check_results_for_decisions(merchant_id, decision_ids)
        for decision_id in decision_ids:
            expected = repository.check_results_for(merchant_id, decision_id)
            assert [row.id for row in batched.get(decision_id, ())] == [
                row.id for row in expected
            ], f"check results differ for decision {decision_id}"
            assert [int(row.check_order) for row in batched.get(decision_id, ())] == sorted(
                int(row.check_order) for row in expected
            ), "the batched check rows must stay in evaluation order"


# ---------------------------------------------------------------------------
# Same document
# ---------------------------------------------------------------------------


def test_a_batched_summary_row_is_identical_to_an_unbatched_one(
    factory: sessionmaker[Session], merchant_id: uuid.UUID, universe: list[_Case]
) -> None:
    """The claim the endpoint's contract rests on, asserted on the rendered document.

    Not on the rows. A field that read a row differently depending on how the row arrived would
    pass a row-identity comparison and still change the wire, and the wire is what a browser and a
    merchant see.
    """
    shapes = {case.case_id: case.shape for case in universe}
    with tenant_transaction(merchant_id, factory) as session:
        cases = _rows(session, merchant_id, universe)
        reads = case_summary_reads(session, merchant_id, cases)
        assert set(reads) == {case.id for case in cases}, (
            "every case handed in must get a bundle, including the ones holding nothing — a "
            "missing key would render as an absence nobody asked for"
        )
        for case in cases:
            batched = case_summary(
                session, merchant_id, case, config=_CONFIG, reads=reads[case.id]
            )
            unbatched = case_summary(session, merchant_id, case, config=_CONFIG)
            assert batched == unbatched, (
                f"the '{shapes[case.id]}' case renders differently batched and unbatched"
            )


def test_the_summary_reads_decisions_for_the_recommendations_cycle_not_the_counter(
    factory: sessionmaker[Session], owner_engine: Engine, merchant_id: uuid.UUID
) -> None:
    """The mistake that has been made three times, pinned so batching cannot make it a fourth.

    The case's counter says cycle 4 and its recommendation says cycle 3, and both cycles hold a
    real decision with a different verdict. Reading the counter would report ``BLOCKED`` on a case
    the optimizer approved — a false statement about a decision, arriving through the read model
    rather than through a missing row, and one that raises nothing on the way out.
    """
    case = _case(owner_engine, merchant_id, shape="counter ahead", decision_cycle_count=4)
    _recommendation(owner_engine, merchant_id, case.case_id, cycle=3)
    _policy_decision(
        owner_engine, merchant_id, case.case_id, cycle=3, verdict="APPROVED", checks=12
    )
    _policy_decision(
        owner_engine,
        merchant_id,
        case.case_id,
        cycle=4,
        verdict="BLOCKED",
        reason="CONSENT_MISSING",
        checks=12,
    )

    with tenant_transaction(merchant_id, factory) as session:
        rows = _rows(session, merchant_id, [case])
        reads = case_summary_reads(session, merchant_id, rows)
        bundle = reads[case.case_id]
        assert [int(row.decision_cycle) for row in bundle.decisions] == [3], (
            "the batched read took the case counter's cycle instead of the recommendation's"
        )
        summary = case_summary(session, merchant_id, rows[0], config=_CONFIG, reads=bundle)
        assert summary["policy_decision"] == "APPROVED"


# ---------------------------------------------------------------------------
# Cost
# ---------------------------------------------------------------------------


def test_the_page_reads_do_not_grow_with_the_page(
    factory: sessionmaker[Session],
    owner_engine: Engine,
    app_engine: Engine,
    merchant_id: uuid.UUID,
) -> None:
    """Five statements for one case and five for forty, and none for an empty page.

    The regression guard, and the reason it counts statements rather than measuring time: the
    fan-out this replaced was not slow because a query was slow, it was slow because there were
    five hundred of them. A timing assertion on a fast machine would not have caught it and does
    not catch it coming back.

    The empty case matters separately. An empty page must issue **no** statement at all — five
    queries whose answers are known before they are sent is a cost paid for nothing, and it is the
    shape a naive batch read has by default.
    """
    cases = [
        _case(owner_engine, merchant_id, shape=f"case {index}", decision_cycle_count=1)
        for index in range(40)
    ]
    for case in cases:
        _recommendation(owner_engine, merchant_id, case.case_id, cycle=1)
        _diagnosis(
            owner_engine,
            merchant_id,
            case.case_id,
            cycle=1,
            cause=RiskCause.INSUFFICIENT_FUNDS,
        )
        _policy_decision(owner_engine, merchant_id, case.case_id, cycle=1, checks=12)

    with tenant_transaction(merchant_id, factory) as session:
        rows = _rows(session, merchant_id, cases)
        assert len(rows) == 40

        with _counting(app_engine) as empty:
            assert case_summary_reads(session, merchant_id, []) == {}
        assert len(empty) == 0, (
            f"an empty page issued {len(empty)} statements; it must issue none: {empty.statements}"
        )

        with _counting(app_engine) as one:
            case_summary_reads(session, merchant_id, rows[:1])
        with _counting(app_engine) as forty:
            case_summary_reads(session, merchant_id, rows)

        assert len(one) == _EXPECTED_PAGE_READS, one.statements
        assert len(forty) == _EXPECTED_PAGE_READS, (
            f"forty cases cost {len(forty)} statements against {len(one)} for one; "
            "the per-row fan-out has come back"
        )


# ---------------------------------------------------------------------------
# Tenant isolation
# ---------------------------------------------------------------------------


def test_no_batched_read_crosses_a_merchant(
    factory: sessionmaker[Session],
    owner_engine: Engine,
    merchant_id: uuid.UUID,
    other_merchant_id: uuid.UUID,
) -> None:
    """Every batch read, asked for another tenant's case ids, answers nothing.

    Asserted two ways, because they fail differently. Asked for **only** the other tenant's ids the
    answer is empty — a read with no merchant clause would return the rows. Asked for a **mixed**
    collection the answer holds only this tenant's — a read that filtered by merchant in Python
    after fetching everything would pass the first assertion and leak on the second under a
    different code path.

    The reads run as the application role with row-level security in force, so this covers the
    application filter and the database policy together. Neither is allowed to be the only one that
    works: RLS is the backstop and the ``merchant_id`` argument is the primary control, and a test
    that could not tell which one answered would not notice the primary one going missing.
    """
    mine = _build_universe(owner_engine, merchant_id)
    theirs = _build_universe(owner_engine, other_merchant_id)
    their_ids = [case.case_id for case in theirs]
    mixed = [case.case_id for case in mine] + their_ids

    with tenant_transaction(merchant_id, factory) as session:
        recommendations = RecommendationRepository(session)
        outcomes = RecoveryOutcomeRepository(session)
        diagnoses = DiagnosisRepository(session)
        intents = ExecutionIntentRepository(session)
        policy = PolicyDecisionRepository(session)

        assert recommendations.latest_for_cases(merchant_id, their_ids) == {}
        assert outcomes.for_cases(merchant_id, their_ids) == {}
        assert diagnoses.active_for_cases(merchant_id, their_ids) == {}
        assert intents.list_for_cases(merchant_id, their_ids) == {}
        assert policy.for_cycles(merchant_id, dict.fromkeys(their_ids, 1)) == {}
        assert _cases_by_id(session, merchant_id, their_ids) == {}
        assert _arrangement_requests(session, merchant_id, their_ids) == {}

        my_ids = {case.case_id for case in mine}
        for name, found in (
            ("recommendations", recommendations.latest_for_cases(merchant_id, mixed)),
            ("outcomes", outcomes.for_cases(merchant_id, mixed)),
            ("diagnoses", diagnoses.active_for_cases(merchant_id, mixed)),
            ("intents", intents.list_for_cases(merchant_id, mixed)),
            ("decisions", policy.for_cycles(merchant_id, dict.fromkeys(mixed, 3))),
            ("cases", _cases_by_id(session, merchant_id, mixed)),
        ):
            assert set(found) <= my_ids, (
                f"the batched {name} read returned another merchant's rows from a mixed "
                f"collection: {sorted(set(found) - my_ids)}"
            )
            assert found, f"the batched {name} read found nothing for this merchant either"

        # And the check-result read, which keys on decision ids rather than case ids.
        their_decisions: list[uuid.UUID] = []
        with tenant_transaction(other_merchant_id, factory) as other:
            other_policy = PolicyDecisionRepository(other)
            for case_id in their_ids:
                for cycle in range(0, 6):
                    their_decisions.extend(
                        row.id for row in other_policy.for_cycle(other_merchant_id, case_id, cycle)
                    )
        assert their_decisions, "the other merchant's universe built no decisions"
        assert policy.check_results_for_decisions(merchant_id, their_decisions) == {}


# ---------------------------------------------------------------------------
# The unresolved grouping's two batched reads
# ---------------------------------------------------------------------------


def test_the_batched_case_read_matches_get_row_for_row(
    factory: sessionmaker[Session], merchant_id: uuid.UUID, universe: list[_Case]
) -> None:
    """``_cases_by_id`` against ``MerchantScopedRepository.get``, including a missing id.

    An id with no visible row has to be absent from the mapping rather than present as ``None``,
    because the suppressed-case list branches on exactly that to skip a row it cannot render.
    """
    absent = uuid.uuid4()
    ids = [case.case_id for case in universe] + [absent]
    with tenant_transaction(merchant_id, factory) as session:
        found = _cases_by_id(session, merchant_id, ids)
        repository = RecoveryCaseRepository(session)
        assert absent not in found
        for case_id in ids:
            expected = repository.get(merchant_id, case_id)
            assert (None if expected is None else expected.id) == (
                None if found.get(case_id) is None else found[case_id].id
            )


def test_the_batched_arrangement_request_matches_the_singular_read(
    factory: sessionmaker[Session], owner_engine: Engine, merchant_id: uuid.UUID
) -> None:
    """``_arrangement_requests`` against ``first_arrangement_request``, field for field.

    The batched form builds the same record from the same row, and that duplication is what this
    test exists to hold in place: the two live in different modules, so nothing but an assertion
    stops one of them starting to read a different column.

    **Earliest, not latest**, which is the whole reason the ordering is asserted with three
    submissions rather than one. A repeated arrangement request is the same customer asking the
    same thing again, not a correction, so the instant that matters is the first — and a batch read
    that took the newest would still return *an* arrangement request and would silently reset how
    long the queue says the customer has been waiting.
    """
    case = _case(
        owner_engine,
        merchant_id,
        shape="three requests",
        state=CaseState.ESCALATED,
        terminal_reason=TerminalReason.CUSTOMER_REQUESTED_PARTIAL_ARRANGEMENT,
    )
    # Inserted newest first, so insertion order and submission order disagree.
    for offset, note in (
        (timedelta(minutes=-10), "asking again"),
        (timedelta(minutes=-40), "the first ask"),
        (timedelta(minutes=-25), "and again"),
    ):
        _signal(
            owner_engine,
            merchant_id,
            case.case_id,
            kind=CustomerSignalKind.PARTIAL_ARRANGEMENT_REQUEST,
            submitted_offset=offset,
            note=note,
            truncated=note == "the first ask",
        )
    # A signal of another kind on the same case, which the read must not pick up.
    _signal(
        owner_engine,
        merchant_id,
        case.case_id,
        kind=CustomerSignalKind.PROMISE_TO_PAY,
        submitted_offset=timedelta(minutes=-60),
    )
    # And a case with no request at all.
    without = _case(
        owner_engine,
        merchant_id,
        shape="no request",
        state=CaseState.ESCALATED,
        terminal_reason=TerminalReason.CUSTOMER_REQUESTED_PARTIAL_ARRANGEMENT,
    )

    with tenant_transaction(merchant_id, factory) as session:
        batched = _arrangement_requests(
            session, merchant_id, [case.case_id, without.case_id]
        )
        assert without.case_id not in batched, (
            "a case with no request must be absent from the mapping, matching the singular "
            "read's None"
        )
        expected = first_arrangement_request(session, merchant_id, case.case_id)
        assert expected is not None
        assert batched[case.case_id] == expected
        assert batched[case.case_id].note == "the first ask", (
            "the batched read took a later request than the singular read's earliest"
        )
        assert batched[case.case_id].note_truncated is True


def test_the_suppressed_and_arrangement_lists_render_from_the_batched_reads(
    factory: sessionmaker[Session], owner_engine: Engine, merchant_id: uuid.UUID
) -> None:
    """The unresolved grouping's two breakdowns, built through the batched reads.

    An end-to-end check on the two lists rather than on the reads beneath them, because the reads
    agreeing is necessary and not sufficient — the lists also have to keep pairing each row with
    *its own* case and *its own* request, and an off-by-one in the pairing is invisible at the
    repository level and glaring on the screen.
    """
    from revora.api.views import _arrangement_cases, _suppressed_cases

    suppressed = []
    for index in range(3):
        case = _case(
            owner_engine,
            merchant_id,
            shape=f"suppressed {index}",
            state=CaseState.ESCALATED,
            terminal_reason=TerminalReason.CUSTOMER_DISPUTED_CHARGE,
        )
        signal = _signal(
            owner_engine,
            merchant_id,
            case.case_id,
            kind=CustomerSignalKind.DELAY_REASON,
            submitted_offset=timedelta(minutes=-30),
            delay_reason=DelayReason.DISPUTES_THE_CHARGE,
        )
        _suppression(owner_engine, merchant_id, case.case_id, signal)
        suppressed.append(case)

    arrangements = []
    for index in range(3):
        case = _case(
            owner_engine,
            merchant_id,
            shape=f"arrangement {index}",
            state=CaseState.ESCALATED,
            terminal_reason=TerminalReason.CUSTOMER_REQUESTED_PARTIAL_ARRANGEMENT,
        )
        _signal(
            owner_engine,
            merchant_id,
            case.case_id,
            kind=CustomerSignalKind.PARTIAL_ARRANGEMENT_REQUEST,
            submitted_offset=timedelta(minutes=-20 - index),
            note=f"note {index}",
        )
        arrangements.append(case)

    with tenant_transaction(merchant_id, factory) as session:
        rows = _suppressed_cases(session, merchant_id, currency="INR")
        assert {row["case_id"] for row in rows} == {
            str(case.case_id) for case in suppressed
        }
        for row in rows:
            assert row["hard_stop_reason"] == HardStopReason.DISPUTES_THE_CHARGE.value
            assert row["unresolved_amount"]["minor"] == 250_000  # type: ignore[index]

        listed = _arrangement_cases(session, merchant_id, currency="INR")
        assert {row["case_id"] for row in listed} == {
            str(case.case_id) for case in arrangements
        }
        by_case = {row["case_id"]: row for row in listed}
        for index, case in enumerate(arrangements):
            note = by_case[str(case.case_id)]["note"]
            assert note is not None, "each row must carry its own case's note"
            assert note["text"] == f"note {index}", (  # type: ignore[index]
                "a row was paired with another case's arrangement request"
            )


def test_the_unresolved_lists_do_not_grow_their_reads_with_their_rows(
    factory: sessionmaker[Session],
    owner_engine: Engine,
    app_engine: Engine,
    merchant_id: uuid.UUID,
) -> None:
    """Two reads for the suppressed list and two for the arrangements, whatever the row count.

    The same regression guard as the page's, on the two lists that were the other half of the
    fan-out: one read for the driving rows and one for what each row needs, never one per row.
    """
    from revora.api.views import _arrangement_cases, _suppressed_cases

    for index in range(12):
        case = _case(
            owner_engine,
            merchant_id,
            shape=f"suppressed {index}",
            state=CaseState.ESCALATED,
            terminal_reason=TerminalReason.CUSTOMER_DISPUTED_CHARGE,
        )
        signal = _signal(
            owner_engine,
            merchant_id,
            case.case_id,
            kind=CustomerSignalKind.DELAY_REASON,
            submitted_offset=timedelta(minutes=-30),
            delay_reason=DelayReason.DISPUTES_THE_CHARGE,
        )
        _suppression(owner_engine, merchant_id, case.case_id, signal)

        arrangement = _case(
            owner_engine,
            merchant_id,
            shape=f"arrangement {index}",
            state=CaseState.ESCALATED,
            terminal_reason=TerminalReason.CUSTOMER_REQUESTED_PARTIAL_ARRANGEMENT,
        )
        _signal(
            owner_engine,
            merchant_id,
            arrangement.case_id,
            kind=CustomerSignalKind.PARTIAL_ARRANGEMENT_REQUEST,
            submitted_offset=timedelta(minutes=-20),
            note="note",
        )

    with tenant_transaction(merchant_id, factory) as session:
        with _counting(app_engine) as suppressed:
            rows = _suppressed_cases(session, merchant_id, currency="INR")
        assert len(rows) == 12
        assert len(suppressed) == 2, (
            f"twelve suppressed cases cost {len(suppressed)} statements; the list must be the "
            f"suppressions plus one batched case read: {suppressed.statements}"
        )

        with _counting(app_engine) as listed:
            arrangements = _arrangement_cases(session, merchant_id, currency="INR")
        assert len(arrangements) == 12
        assert len(listed) == 2, (
            f"twelve arrangement requests cost {len(listed)} statements; the list must be the "
            f"cases plus one batched request read: {listed.statements}"
        )


def test_the_reads_bundle_cannot_be_amended_after_it_is_built(
    factory: sessionmaker[Session], merchant_id: uuid.UUID, universe: list[_Case]
) -> None:
    """``CaseSummaryReads`` is frozen, so "the same rows render the same row" is about the data.

    A mutable bundle would make that claim depend on nothing having touched it between the read and
    the render, which is a claim about ordering rather than about data and is not one a test can
    keep true.
    """
    with tenant_transaction(merchant_id, factory) as session:
        cases = _rows(session, merchant_id, universe)
        bundle = case_summary_reads(session, merchant_id, cases)[cases[0].id]
        assert isinstance(bundle, CaseSummaryReads)
        with pytest.raises(AttributeError):
            bundle.recommendation = None  # type: ignore[misc]
