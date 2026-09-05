"""Task 54.2. What the properties cannot say: every member of four enumerations, by name.

A property quantifies over generated inputs and is strongest at invariants. It is weakest at
**enumeration**. "Every one of the six ``Delay_Reason`` values behaves correctly end to end" is a
claim about six named values, and a property drawing a ``Delay_Reason`` from a strategy passes
while one member is broken — Hypothesis would have to shrink to the one bad member *and* the
property would have to assert something member-specific, which a property asserting a shared
invariant deliberately does not. So the six live here as six functions with six different sets of
expectations, and the same for the four Terminal_Reasons migration ``0015`` added and the two
promise resolutions an authoritative read produces.

**The most valuable lines in this file are the three coverage guards**, not the example tests.
Each walks this module's own test function names and asserts that every member of the enumeration
it guards is named by exactly one of them, against ``len(...)`` of the enumeration itself. A
seventh ``Delay_Reason``, or a seventeenth ``Terminal_Reason``, therefore fails *here* — in the
file whose job is enumeration coverage — rather than silently going untested. They are ``pure``:
they read ``globals()`` and an enumeration and touch nothing else.

**Tiering, per test rather than per module.** The three guards and the mapping-table assertions
need nothing at all. Everything else drives ``record_signal``, a promise resolution or a worker
handler through the real repositories, and those guarantees *are* transaction boundaries — a fake
session that implemented ``FOR UPDATE``, a conditional ``UPDATE ... RETURNING`` and a partial
unique index would be a database with fewer guarantees than the one in the container, so the
end-to-end members are ``pg``.

**What is deliberately absent.** The 429-consumes-a-message case task 54.2 mentions "if not
already asserted there" is asserted there:
``tests/persistence/test_resend_disposition.py::test_a_rate_limited_resend_spends_an_increment_and_returns_to_deciding``
covers the ``FAILED`` intent, the retained ``customer_message_count`` increment and the return to
``DECISION_PENDING``. A second copy would be a second place to update when the disposition
changes, so there is none.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Final

import pytest
from sqlalchemy import Engine, text

from revora.cases.manager import apply_transition
from revora.cases.review import CASE_REVIEW_KIND
from revora.customer.arrangements import PARTIAL_ARRANGEMENT_KIND
from revora.customer.promises import (
    PROMISE_ESCALATION_KIND,
    resolve_kept,
    resolve_missed,
)
from revora.customer.signals import (
    DelayReasonSubmission,
    PartialArrangementSubmission,
    PromiseSubmission,
    SignalOutcome,
    cause_for_delay_reason,
    record_signal,
)
from revora.customer.suppression import CONTACT_SUPPRESSION_KIND, hard_stop_for
from revora.customer.tokens import (
    TokenRejection,
    TokenService,
    VerificationOutcome,
    VerifiedToken,
)
from revora.domain.actions import CandidateAction
from revora.domain.enums import (
    CaseState,
    DelayReason,
    ExecutionEffectKind,
    HardStopReason,
    IntentState,
    PromiseStatus,
    RiskCause,
    TerminalReason,
    TokenRevocationReason,
)
from revora.jobs.pipeline import (
    handle_contact_suppression,
    handle_partial_arrangement,
    handle_promise_escalation,
)
from revora.persistence.repositories.engine import build_engine, dispose_engine, set_engine
from revora.persistence.repositories.session import tenant_transaction
from revora.platform.config import default_configuration
from tests.fakes.customer import installed_signing_secrets
from tests.pg_support import insert_merchant

_CONFIG = default_configuration()

# No module-level marker. The three coverage guards and the two mapping-table tests run on every
# commit; the rest need a migrated PostgreSQL. A module-level marker of either would drag the
# other into the wrong selection.


# ---------------------------------------------------------------------------
# The coverage guards (`pure`)
# ---------------------------------------------------------------------------


def _test_names() -> list[str]:
    """Every test function defined in this module, by name.

    Read from ``globals()`` rather than from a hand-maintained list, because a hand-maintained
    list is a second place to forget the thing this file exists to remember.
    """
    return sorted(name for name in globals() if name.startswith("test_"))


def _named_by(needle: str) -> list[str]:
    """The test functions whose name contains ``needle``, lowercased."""
    return [name for name in _test_names() if needle.lower() in name]


@pytest.mark.pure
def test_every_delay_reason_has_exactly_one_named_end_to_end_test() -> None:
    """One test per ``DelayReason`` member, counted against ``len(DelayReason)``.

    This is the assertion the task is actually asking for. Adding a seventh member to the
    enumeration fails here, naming the member that has no test, instead of passing every property
    in the suite while one stated reason silently records the wrong consequence.

    ``len`` is asserted separately from the per-member walk on purpose: the walk alone would still
    pass if a member were *removed*, and a removed ``Delay_Reason`` is a migration and a wire
    contract change that should not slip through a test file whose subject is the enumeration.
    """
    assert len(DelayReason) == 6, (
        f"DelayReason has {len(DelayReason)} members, not the six R20.C1 declares. If that is "
        "intended, add or remove the named end-to-end test below to match before changing this "
        "number — the count is what makes the coverage claim checkable"
    )
    for reason in DelayReason:
        matches = _named_by(reason.value)
        assert len(matches) == 1, (
            f"{reason.value} is named by {len(matches)} test functions in this module, not one: "
            f"{matches}. Every Delay_Reason needs its own named example test, because a property "
            "drawing one at random passes while a single member is broken"
        )


@pytest.mark.pure
def test_every_customer_stated_terminal_reason_has_exactly_one_named_test() -> None:
    """One test per Terminal_Reason migration ``0015`` added, and the set is derived.

    The four are derived as ``set(TerminalReason) - _TERMINAL_REASONS_BEFORE_0015`` rather than
    listed, so a **seventeenth** member added to the enumeration lands in the derived set, finds no
    test naming it, and fails here. Listing the four directly would have made a new member
    invisible to this file, which is the one failure mode a coverage guard exists to prevent.

    ``recovery_case.terminal_reason`` carries a ``CHECK`` generated from the enumeration, so a new
    member is a migration — and a migration is exactly the moment somebody should be told that a
    case can now end for a reason nothing tests.
    """
    assert len(TerminalReason) == 16, (
        f"TerminalReason has {len(TerminalReason)} members, not the twelve of migration 0001 plus "
        "the four of 0015. A member added here is a migration and needs a named test below"
    )
    customer_stated = set(TerminalReason) - _TERMINAL_REASONS_BEFORE_0015
    assert len(customer_stated) == 4, (
        f"the derived customer-stated set is {sorted(r.value for r in customer_stated)}; four were "
        "expected. Either a member was added to the enumeration or one was removed from the "
        "pre-0015 list above"
    )
    for reason in customer_stated:
        matches = _named_by(reason.value)
        assert len(matches) == 1, (
            f"{reason.value} is named by {len(matches)} test functions, not one: {matches}. Each "
            "of these four is a different sentence about why a case ended and they must not share "
            "a test — R21.C9's whole point is that a dispute and an opt-out are not the same event"
        )


@pytest.mark.pure
def test_every_promise_status_has_an_owner_and_the_two_resolutions_are_tested_here() -> None:
    """All six ``PromiseStatus`` members are accounted for, and ``KEPT``/``MISSED`` are named here.

    The other four are not this file's to test and saying so is the point: ``RECORDED`` and
    ``BEYOND_WINDOW_ESCALATED`` are outcomes of ``plan_promise`` and belong to P41/P42,
    ``FOLLOW_UP_SCHEDULED`` is ``mark_follow_up_scheduled``'s and ``VOIDED`` is
    ``void_for_terminal_state``'s. What the assertion buys is that the union is *total*: a seventh
    status added to the enumeration belongs to nobody, and this is where that shows up.
    """
    assert len(PromiseStatus) == 6
    unaccounted = set(PromiseStatus) - _OWNED_ELSEWHERE - _RESOLUTIONS_TESTED_HERE
    assert set(PromiseStatus) == _OWNED_ELSEWHERE | _RESOLUTIONS_TESTED_HERE, (
        "the promise statuses tested here and those owned elsewhere do not cover PromiseStatus; "
        f"unaccounted for: {sorted(status.value for status in unaccounted)}"
    )
    assert not _OWNED_ELSEWHERE & _RESOLUTIONS_TESTED_HERE
    for status in _RESOLUTIONS_TESTED_HERE:
        matches = _named_by(status.value)
        assert len(matches) == 1, (
            f"{status.value} is named by {len(matches)} test functions, not one: {matches}"
        )


_TERMINAL_REASONS_BEFORE_0015: Final[frozenset[TerminalReason]] = frozenset(
    {
        TerminalReason.RECOVERED_VERIFIED,
        TerminalReason.RECOVERY_WINDOW_ELAPSED,
        TerminalReason.MAX_ATTEMPTS_REACHED,
        TerminalReason.DECISION_CYCLE_LIMIT_REACHED,
        TerminalReason.CUSTOMER_OPTED_OUT,
        TerminalReason.ALREADY_PAID,
        TerminalReason.FRAUD_OR_RISK_FLAG,
        TerminalReason.PAYMENT_STATE_UNVERIFIABLE,
        TerminalReason.EXECUTION_RESULT_UNVERIFIABLE,
        TerminalReason.POLICY_BLOCKED,
        TerminalReason.HUMAN_OWNERSHIP,
        TerminalReason.COMMUNICATION_FAILED,
    }
)
"""The twelve migration ``0001`` created the ``CHECK`` with. Listed so the four this file covers
can be *derived* rather than listed — see the guard above for why that direction matters."""

_OWNED_ELSEWHERE: Final[frozenset[PromiseStatus]] = frozenset(
    {
        PromiseStatus.RECORDED,
        PromiseStatus.BEYOND_WINDOW_ESCALATED,
        PromiseStatus.FOLLOW_UP_SCHEDULED,
        PromiseStatus.VOIDED,
    }
)

_RESOLUTIONS_TESTED_HERE: Final[frozenset[PromiseStatus]] = frozenset(
    {PromiseStatus.KEPT, PromiseStatus.MISSED}
)


# ---------------------------------------------------------------------------
# The mapping tables (`pure`)
# ---------------------------------------------------------------------------


@pytest.mark.pure
def test_the_delay_reason_cause_table_is_total_and_says_what_r20_c5_says() -> None:
    """R20.C5, member by member, with the expected causes written out rather than derived.

    Written out on purpose. Deriving the expectation from ``DELAY_REASON_CAUSE`` would assert that
    the table equals itself; the value of this test is that the six mappings a human agreed to are
    restated somewhere a change has to be made twice, and one of those places is a test that says
    why each row reads as it does.
    """
    expected: Final[dict[DelayReason, RiskCause | None]] = {
        # Two different things to *say*, one thing to conclude: the money is not there.
        DelayReason.SALARY_OR_CASHFLOW_TIMING: RiskCause.INSUFFICIENT_FUNDS,
        DelayReason.AMOUNT_TOO_HIGH_RIGHT_NOW: RiskCause.INSUFFICIENT_FUNDS,
        # The one row where the customer and the provider describe the same event.
        DelayReason.BANK_OR_CARD_PROBLEM: RiskCause.BANK_OR_NETWORK_FAILURE,
        # OTHER names no cause, which is the whole reason OTHER exists (R20.C6).
        DelayReason.OTHER: None,
        # The hard stops are not payment problems, so they sharpen no next decision.
        DelayReason.DISPUTES_THE_CHARGE: None,
        DelayReason.NO_LONGER_WANTS_THE_ORDER: None,
    }
    assert set(expected) == set(DelayReason)
    for reason, cause in expected.items():
        assert cause_for_delay_reason(reason) is cause, (
            f"{reason.value} refines to {cause_for_delay_reason(reason)}, not {cause}. The table "
            "is an [ASSUMPTION] about what a customer meant, and changing one row changes which "
            "Candidate_Action set the next decision cycle is built from"
        )


@pytest.mark.pure
def test_exactly_two_delay_reasons_are_hard_stops() -> None:
    """R21.C1: two of the six end contact, four do not, and no member is unmapped.

    Asserted as a partition of the enumeration rather than as two lookups, because the interesting
    failure is not "a hard stop stopped being one" — it is a *fifth* payment-problem reason
    quietly becoming a hard stop, which would end contact on a case nobody meant to end.
    """
    stops = {reason for reason in DelayReason if hard_stop_for(reason) is not None}
    assert stops == {DelayReason.DISPUTES_THE_CHARGE, DelayReason.NO_LONGER_WANTS_THE_ORDER}
    assert {hard_stop_for(reason) for reason in stops} == set(HardStopReason)
    assert hard_stop_for(None) is None, (
        "two of the three submission shapes carry no Delay_Reason, and the helper accepts None so "
        "they do not each need a guard"
    )


# ---------------------------------------------------------------------------
# `pg` scaffolding
# ---------------------------------------------------------------------------


@pytest.fixture
def installed(migrated_url: str, owner_engine: Engine) -> Iterator[Engine]:
    """The global engine, installed, so the worker handlers and ``apply_transition`` can run.

    Those take a merchant and a case and open their own transaction through the process-wide
    factory — which is what makes them the *worker's* half of a requirement rather than something a
    request could do. Yielding ``owner_engine`` for the read-back assertions keeps the reads on a
    connection this test controls.
    """
    engine = build_engine(migrated_url)
    set_engine(engine)
    try:
        yield owner_engine
    finally:
        dispose_engine()


def _seed_case(
    engine: Engine,
    merchant_id: uuid.UUID,
    *,
    moment: datetime,
    state: CaseState = CaseState.POLICY_CHECK,
    window_end_at: datetime | None = None,
) -> uuid.UUID:
    """One case in a named state. Written directly, because how it got there is not under test."""
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
                    :id, :m, :state, :pid, 249900, 'INR', :ck, :detected, :window_end,
                    :review, 1, now()
                )
                """
            ),
            {
                "id": str(case_id),
                "m": str(merchant_id),
                "state": state.value,
                "pid": f"pay_{case_id.hex[:14]}",
                "ck": f"ck-{case_id}",
                "detected": moment,
                "window_end": window_end_at or moment + _CONFIG.RECOVERY_WINDOW_DURATION,
                "review": (
                    moment + timedelta(hours=12) if state is CaseState.POLICY_CHECK else None
                ),
            },
        )
    return case_id


def _mint(merchant_id: uuid.UUID, case_id: uuid.UUID, *, moment: datetime) -> str:
    """Mint through the real service and return the wire token, so a test holds what a request
    would hold."""
    with installed_signing_secrets(1), tenant_transaction(merchant_id) as session:
        outcome = TokenService.on_session(session, _CONFIG).mint(
            merchant_id,
            case_id=case_id,
            window_end_at=moment + _CONFIG.RECOVERY_WINDOW_DURATION,
            approved_action=CandidateAction.PAYMENT_LINK,
            moment=moment,
        )
    assert outcome.token is not None and outcome.token.wire_token is not None
    return outcome.token.wire_token


def _verify(merchant_id: uuid.UUID, wire_token: str, *, moment: datetime) -> VerifiedToken:
    verified = _verification(merchant_id, wire_token, moment=moment)
    assert verified.token is not None, f"the token was refused: {verified.rejection}"
    return verified.token


def _verification(
    merchant_id: uuid.UUID, wire_token: str, *, moment: datetime
) -> VerificationOutcome:
    """The full outcome, for the one test that wants the rejection rather than the token."""
    with installed_signing_secrets(1), tenant_transaction(merchant_id) as session:
        return TokenService.on_session(session, _CONFIG).verify(
            merchant_id, wire_token, moment=moment
        )


def _live_case_with_token(
    engine: Engine,
    *,
    display_name: str,
    moment: datetime,
    state: CaseState = CaseState.POLICY_CHECK,
    window_end_at: datetime | None = None,
) -> tuple[uuid.UUID, uuid.UUID, VerifiedToken]:
    """A merchant, a case in a live state, and a verified token against it."""
    merchant_id = insert_merchant(engine, display_name=display_name)
    case_id = _seed_case(
        engine, merchant_id, moment=moment, state=state, window_end_at=window_end_at
    )
    token = _verify(merchant_id, _mint(merchant_id, case_id, moment=moment), moment=moment)
    return merchant_id, case_id, token


def _submit(
    merchant_id: uuid.UUID,
    token: VerifiedToken,
    submission: DelayReasonSubmission | PromiseSubmission | PartialArrangementSubmission,
    *,
    moment: datetime,
) -> SignalOutcome:
    with tenant_transaction(merchant_id) as session:
        return record_signal(
            session,
            _CONFIG,
            token,
            submission,
            correlation_id=uuid.uuid4(),
            moment=moment,
        )


def _signal_rows(engine: Engine, case_id: uuid.UUID) -> list[dict[str, object]]:
    with engine.connect() as connection:
        return [
            dict(row)
            for row in connection.execute(
                text(
                    "SELECT kind, delay_reason, delay_reason_note FROM customer_signal "
                    "WHERE case_id = :c ORDER BY submitted_at"
                ),
                {"c": str(case_id)},
            )
            .mappings()
            .all()
        ]


def _job_kinds(engine: Engine, case_id: uuid.UUID) -> set[str]:
    with engine.connect() as connection:
        return {
            str(row[0])
            for row in connection.execute(
                text("SELECT kind FROM job WHERE case_id = :c"), {"c": str(case_id)}
            ).all()
        }


def _case_row(engine: Engine, case_id: uuid.UUID) -> dict[str, object]:
    with engine.connect() as connection:
        return dict(
            connection.execute(
                text(
                    "SELECT state, terminal_reason, version, window_end_at, payment_amount, "
                    "executed_action_count, customer_message_count FROM recovery_case "
                    "WHERE id = :c"
                ),
                {"c": str(case_id)},
            )
            .mappings()
            .one()
        )


def _suppression_reasons(engine: Engine, merchant_id: uuid.UUID) -> list[str]:
    with engine.connect() as connection:
        return [
            str(row[0])
            for row in connection.execute(
                text(
                    "SELECT hard_stop_reason FROM contact_suppression WHERE merchant_id = :m"
                ),
                {"m": str(merchant_id)},
            ).all()
        ]


def _token_revocations(engine: Engine, case_id: uuid.UUID) -> list[str | None]:
    with engine.connect() as connection:
        return [
            None if row[0] is None else str(row[0])
            for row in connection.execute(
                text(
                    "SELECT revocation_reason FROM customer_access_token WHERE case_id = :c"
                ),
                {"c": str(case_id)},
            ).all()
        ]


def _promise_row(engine: Engine, case_id: uuid.UUID) -> dict[str, object]:
    with engine.connect() as connection:
        return dict(
            connection.execute(
                text(
                    "SELECT id, status, kept_at, missed_at, seconds_promise_to_payment, "
                    "promise_date, customer_signal_id FROM promise_to_pay WHERE case_id = :c"
                ),
                {"c": str(case_id)},
            )
            .mappings()
            .one()
        )


def _assert_accepted_delay_reason(
    engine: Engine,
    case_id: uuid.UUID,
    outcome: SignalOutcome,
    reason: DelayReason,
    *,
    before: dict[str, object],
) -> None:
    """What every accepted Delay_Reason has in common, asserted once.

    The per-member tests below assert what is *different* about their member. This holds the
    shared part — the row is there, it carries the reason, and R19.C9's five prohibitions left the
    case exactly where it was — so a member-specific failure reads as a member-specific failure
    instead of drowning in six copies of the same four assertions.
    """
    assert outcome.accepted, f"{reason.value} was refused: {outcome.rejection}"
    rows = _signal_rows(engine, case_id)
    assert len(rows) == 1
    assert rows[0]["kind"] == "DELAY_REASON"
    assert rows[0]["delay_reason"] == reason.value
    after = _case_row(engine, case_id)
    assert after == before, (
        f"submitting {reason.value} moved the case: {before} -> {after}. R19.C9 permits the "
        "accepting request no state transition and no counter movement, and R20.C9 names "
        "window_end_at and the two counters specifically"
    )


# ---------------------------------------------------------------------------
# `pg` — the six Delay_Reason values, end to end, one test each
# ---------------------------------------------------------------------------


@pytest.mark.pg
def test_salary_or_cashflow_timing_records_insufficient_funds_and_ends_nothing(
    installed: Engine,
) -> None:
    """The commonest stated reason. Refines the cause and leaves everything else alone.

    ``INSUFFICIENT_FUNDS`` is shared with ``AMOUNT_TOO_HIGH_RIGHT_NOW`` and the two are kept
    apart as *reasons* — the collapse happens only at the cause, which is why both tests exist.
    """
    moment = datetime.now(UTC)
    merchant_id, case_id, token = _live_case_with_token(
        installed, display_name="Delay reason salary", moment=moment
    )
    before = _case_row(installed, case_id)

    outcome = _submit(
        merchant_id,
        token,
        DelayReasonSubmission(
            delay_reason=DelayReason.SALARY_OR_CASHFLOW_TIMING, note="paid on the 5th"
        ),
        moment=moment,
    )

    _assert_accepted_delay_reason(
        installed, case_id, outcome, DelayReason.SALARY_OR_CASHFLOW_TIMING, before=before
    )
    assert (
        cause_for_delay_reason(DelayReason.SALARY_OR_CASHFLOW_TIMING)
        is RiskCause.INSUFFICIENT_FUNDS
    )
    assert outcome.suppression is None, "a cash-flow explanation is not an objection to the debt"
    assert _suppression_reasons(installed, merchant_id) == []
    assert _token_revocations(installed, case_id) == [None]
    assert _job_kinds(installed, case_id) == {CASE_REVIEW_KIND}, (
        "a case resting at POLICY_CHECK gains exactly one queued decision cycle (R30.C8) and "
        "nothing else"
    )
    assert _signal_rows(installed, case_id)[0]["delay_reason_note"] == "paid on the 5th", (
        "the note is retained on customer_signal, where the retention sweep can reach it"
    )


@pytest.mark.pg
def test_bank_or_card_problem_records_a_bank_or_network_failure(installed: Engine) -> None:
    """The one row where the customer and the provider describe the same event from two sides.

    Worth its own test rather than a parameter, because this is the only stated reason whose cause
    a provider error code could also have produced — so it is the only one where the recorded
    ``Diagnosis_Evidence_Source`` is what tells a reviewer whether a stranger or the network said
    so.
    """
    moment = datetime.now(UTC)
    merchant_id, case_id, token = _live_case_with_token(
        installed, display_name="Delay reason bank", moment=moment
    )
    before = _case_row(installed, case_id)

    outcome = _submit(
        merchant_id,
        token,
        DelayReasonSubmission(delay_reason=DelayReason.BANK_OR_CARD_PROBLEM),
        moment=moment,
    )

    _assert_accepted_delay_reason(
        installed, case_id, outcome, DelayReason.BANK_OR_CARD_PROBLEM, before=before
    )
    assert (
        cause_for_delay_reason(DelayReason.BANK_OR_CARD_PROBLEM)
        is RiskCause.BANK_OR_NETWORK_FAILURE
    )
    assert _suppression_reasons(installed, merchant_id) == []
    assert _signal_rows(installed, case_id)[0]["delay_reason_note"] is None, (
        "the note is optional and an absent one is null rather than an empty string"
    )


@pytest.mark.pg
def test_amount_too_high_right_now_collapses_to_insufficient_funds_without_becoming_an_arrangement(
    installed: Engine,
) -> None:
    """"That is more than I can pay" is a Delay_Reason, not a Partial_Arrangement_Request.

    The distinction is the reason this member needs its own test: it is the one stated reason a
    reader could mistake for an offer to settle for less, and R22 gives an arrangement request a
    *terminal* consequence. So the assertion that matters is the negative one — no arrangement
    job, no escalation, and the case still live.
    """
    moment = datetime.now(UTC)
    merchant_id, case_id, token = _live_case_with_token(
        installed, display_name="Delay reason amount", moment=moment
    )
    before = _case_row(installed, case_id)

    outcome = _submit(
        merchant_id,
        token,
        DelayReasonSubmission(delay_reason=DelayReason.AMOUNT_TOO_HIGH_RIGHT_NOW),
        moment=moment,
    )

    _assert_accepted_delay_reason(
        installed, case_id, outcome, DelayReason.AMOUNT_TOO_HIGH_RIGHT_NOW, before=before
    )
    assert (
        cause_for_delay_reason(DelayReason.AMOUNT_TOO_HIGH_RIGHT_NOW)
        is RiskCause.INSUFFICIENT_FUNDS
    )
    assert not outcome.arrangement_enqueued, (
        "stating that the amount is too high is not asking to pay differently; R22.C2's "
        "escalation belongs to the partial-arrangement shape alone"
    )
    assert PARTIAL_ARRANGEMENT_KIND not in _job_kinds(installed, case_id)
    assert _case_row(installed, case_id)["terminal_reason"] is None


@pytest.mark.pg
def test_other_records_the_signal_and_deliberately_refines_no_cause(installed: Engine) -> None:
    """R20.C6. ``OTHER`` exists so a customer is not pushed into a reason that is wrong.

    The honest consequence of "we do not know what they meant" is that the recorded Risk_Cause is
    left exactly as it was — so the assertion is that a signal *was* persisted and that it names
    no cause. Those two together are what distinguish "no refinement" from "nothing happened".
    """
    moment = datetime.now(UTC)
    merchant_id, case_id, token = _live_case_with_token(
        installed, display_name="Delay reason other", moment=moment
    )
    before = _case_row(installed, case_id)

    outcome = _submit(
        merchant_id,
        token,
        DelayReasonSubmission(delay_reason=DelayReason.OTHER, note="long story"),
        moment=moment,
    )

    _assert_accepted_delay_reason(
        installed, case_id, outcome, DelayReason.OTHER, before=before
    )
    assert cause_for_delay_reason(DelayReason.OTHER) is None, (
        "OTHER refining to a cause would push a customer whose reason was not anticipated into "
        "one that is wrong, which is the single thing R20.C6 exists to prevent"
    )
    assert _suppression_reasons(installed, merchant_id) == []


@pytest.mark.pg
def test_disputes_the_charge_suppresses_contact_and_revokes_the_token_in_one_transaction(
    installed: Engine,
) -> None:
    """R21.C1 and R21.C10, in the accepting request. The transition is still the worker's.

    Three writes that must be atomic with the signal — the suppression row, the token revocation
    and the queued consequence — and one that must *not* have happened: the case is still at
    ``POLICY_CHECK``, because R19.C9 forbids the accepting request from transitioning it and
    ``handle_contact_suppression`` is what ends it.
    """
    moment = datetime.now(UTC)
    merchant_id, case_id, token = _live_case_with_token(
        installed, display_name="Hard stop dispute", moment=moment
    )
    before = _case_row(installed, case_id)

    outcome = _submit(
        merchant_id,
        token,
        DelayReasonSubmission(delay_reason=DelayReason.DISPUTES_THE_CHARGE),
        moment=moment,
    )

    _assert_accepted_delay_reason(
        installed, case_id, outcome, DelayReason.DISPUTES_THE_CHARGE, before=before
    )
    assert cause_for_delay_reason(DelayReason.DISPUTES_THE_CHARGE) is None, (
        "a dispute is not a payment problem, so it sharpens no next automated decision"
    )
    assert outcome.suppression is not None
    assert outcome.suppression.hard_stop_reason is HardStopReason.DISPUTES_THE_CHARGE
    assert _suppression_reasons(installed, merchant_id) == [
        HardStopReason.DISPUTES_THE_CHARGE.value
    ]
    assert _token_revocations(installed, case_id) == [
        TokenRevocationReason.CONTACT_SUPPRESSED.value
    ], (
        "R21.C10: a customer whose case has been suppressed must not still hold a working link "
        "into it, and the revocation is atomic with the suppression rather than left to the worker"
    )
    kinds = _job_kinds(installed, case_id)
    assert CONTACT_SUPPRESSION_KIND in kinds, (
        "the hard stop queued no consequence, so the case would be suppressed and never escalated"
    )
    assert kinds <= {CASE_REVIEW_KIND, CONTACT_SUPPRESSION_KIND}, (
        f"the accepting request queued {sorted(kinds)}; nothing outbound and nothing else belongs "
        "in that transaction"
    )
    assert _case_row(installed, case_id)["state"] == CaseState.POLICY_CHECK.value


@pytest.mark.pg
def test_no_longer_wants_the_order_suppresses_contact_under_its_own_hard_stop_reason(
    installed: Engine,
) -> None:
    """The second hard stop, and the assertion is that it is *not* the first.

    A cancellation and a dispute suppress contact identically and mean different things: one
    implies fulfilment and refund questions, the other a possible chargeback. They are both a
    person's problem and they are not the same person's, so the recorded Hard_Stop_Reason has to
    be the one the customer chose.
    """
    moment = datetime.now(UTC)
    merchant_id, case_id, token = _live_case_with_token(
        installed, display_name="Hard stop cancel", moment=moment
    )
    before = _case_row(installed, case_id)

    outcome = _submit(
        merchant_id,
        token,
        DelayReasonSubmission(delay_reason=DelayReason.NO_LONGER_WANTS_THE_ORDER),
        moment=moment,
    )

    _assert_accepted_delay_reason(
        installed, case_id, outcome, DelayReason.NO_LONGER_WANTS_THE_ORDER, before=before
    )
    assert outcome.suppression is not None
    assert outcome.suppression.hard_stop_reason is HardStopReason.NO_LONGER_WANTS_THE_ORDER
    assert _suppression_reasons(installed, merchant_id) == [
        HardStopReason.NO_LONGER_WANTS_THE_ORDER.value
    ]
    assert _token_revocations(installed, case_id) == [
        TokenRevocationReason.CONTACT_SUPPRESSED.value
    ]
    assert CONTACT_SUPPRESSION_KIND in _job_kinds(installed, case_id)


# ---------------------------------------------------------------------------
# `pg` — the four Terminal_Reasons migration 0015 added, one test each
# ---------------------------------------------------------------------------


@pytest.mark.pg
def test_a_dispute_ends_the_case_customer_disputed_charge(installed: Engine) -> None:
    """R21.C4. The worker's half: ``ESCALATED`` with ``CUSTOMER_DISPUTED_CHARGE``.

    Not ``CUSTOMER_OPTED_OUT``, and R21.C9 is why: an opt-out withdraws consent to be contacted
    at all, a dispute objects to one debt, and only one of them implies a possible chargeback. A
    merchant reading the ``ESCALATED`` grouping needs to see which happened.

    ``handle_contact_suppression`` is called directly rather than through ``run_once`` because what
    is under test is this handler's effect; draining the queue would also run the decision cycle
    the submission enqueued and make a failure ambiguous about which handler caused it.
    """
    moment = datetime.now(UTC)
    merchant_id, case_id, token = _live_case_with_token(
        installed, display_name="Terminal dispute", moment=moment
    )
    assert _submit(
        merchant_id,
        token,
        DelayReasonSubmission(delay_reason=DelayReason.DISPUTES_THE_CHARGE),
        moment=moment,
    ).accepted

    handle_contact_suppression(
        merchant_id, case_id, hard_stop_reason=HardStopReason.DISPUTES_THE_CHARGE
    )

    row = _case_row(installed, case_id)
    assert row["state"] == CaseState.ESCALATED.value
    assert row["terminal_reason"] == TerminalReason.CUSTOMER_DISPUTED_CHARGE.value, (
        f"a disputed charge ended the case as {row['terminal_reason']}. Collapsing it onto "
        "CUSTOMER_OPTED_OUT would have needed no migration and would have made 'how many "
        "customers disputed a charge' unanswerable"
    )


@pytest.mark.pg
def test_a_cancellation_ends_the_case_customer_cancelled_order(installed: Engine) -> None:
    """R21.C5. The same handler, the other reason, and they must not be interchangeable.

    Both hard stops suppress contact identically, so the only place the difference survives is the
    Terminal_Reason — which is exactly why this is a second named test and not a second assertion
    inside the first.
    """
    moment = datetime.now(UTC)
    merchant_id, case_id, token = _live_case_with_token(
        installed, display_name="Terminal cancel", moment=moment
    )
    assert _submit(
        merchant_id,
        token,
        DelayReasonSubmission(delay_reason=DelayReason.NO_LONGER_WANTS_THE_ORDER),
        moment=moment,
    ).accepted

    handle_contact_suppression(
        merchant_id, case_id, hard_stop_reason=HardStopReason.NO_LONGER_WANTS_THE_ORDER
    )

    row = _case_row(installed, case_id)
    assert row["state"] == CaseState.ESCALATED.value
    assert row["terminal_reason"] == TerminalReason.CUSTOMER_CANCELLED_ORDER.value


@pytest.mark.pg
def test_an_arrangement_request_ends_the_case_customer_requested_partial_arrangement(
    installed: Engine,
) -> None:
    """R22.C2, and R22.C7's three money fields untouched beside it.

    A customer asking to settle for less or in instalments has asked Revora to agree to something
    it is structurally unable to agree to — there is no field on the request, no column, and
    nothing on this path that reads one. So the consequence is the smallest one that is still an
    answer: the case ends ``ESCALATED`` and a person picks it up.

    ``payment_amount``, the currency and ``window_end_at`` are compared before and after, because
    this is the one terminal consequence somebody would expect to move a number.
    """
    moment = datetime.now(UTC)
    merchant_id, case_id, token = _live_case_with_token(
        installed, display_name="Terminal arrangement", moment=moment
    )
    before = _case_row(installed, case_id)

    outcome = _submit(
        merchant_id, token, PartialArrangementSubmission(note="two parts?"), moment=moment
    )
    assert outcome.accepted and outcome.signal_id is not None
    assert outcome.arrangement_enqueued
    assert PARTIAL_ARRANGEMENT_KIND in _job_kinds(installed, case_id)
    assert _case_row(installed, case_id)["state"] == CaseState.POLICY_CHECK.value, (
        "R19.C9: the accepting request queues the escalation and applies none of it"
    )

    handle_partial_arrangement(merchant_id, case_id, signal_id=outcome.signal_id)

    after = _case_row(installed, case_id)
    assert after["state"] == CaseState.ESCALATED.value
    assert (
        after["terminal_reason"]
        == TerminalReason.CUSTOMER_REQUESTED_PARTIAL_ARRANGEMENT.value
    )
    assert after["payment_amount"] == before["payment_amount"]
    assert after["window_end_at"] == before["window_end_at"], (
        "R22.C7: an arrangement request changes no money field and does not move the window"
    )
    assert isinstance(after["payment_amount"], int), (
        "the amount is an integer count of minor units; a float here would be a rounding error "
        "in the figure a person is about to negotiate over"
    )


@pytest.mark.pg
def test_a_promise_past_the_window_ends_the_case_promise_beyond_recovery_window(
    installed: Engine,
) -> None:
    """R23.C5. A Promise_Date at or past ``window_end_at``, which is never extended.

    The window's immutability is what makes this an escalation rather than a reschedule: R2.C5
    means the promise cannot be accommodated, so a person has to decide. The window end is
    compared before and after, because "making room" is the one plausible wrong implementation.

    The promise row is asserted ``BEYOND_WINDOW_ESCALATED`` with no Follow_Up_Instant — nothing is
    scheduled for a date the case cannot reach.
    """
    moment = datetime.now(UTC)
    merchant_id, case_id, token = _live_case_with_token(
        installed, display_name="Terminal promise window", moment=moment
    )
    before = _case_row(installed, case_id)
    beyond = before["window_end_at"]
    assert isinstance(beyond, datetime)

    outcome = _submit(
        merchant_id,
        token,
        PromiseSubmission(promise_date=beyond.replace(tzinfo=UTC) + timedelta(days=10)),
        moment=moment,
    )
    assert outcome.accepted
    promise = _promise_row(installed, case_id)
    assert promise["status"] == PromiseStatus.BEYOND_WINDOW_ESCALATED.value
    assert PROMISE_ESCALATION_KIND in _job_kinds(installed, case_id)

    assert isinstance(promise["id"], uuid.UUID)
    assert isinstance(promise["customer_signal_id"], uuid.UUID)
    handle_promise_escalation(
        merchant_id,
        case_id,
        promise_id=promise["id"],
        signal_id=promise["customer_signal_id"],
    )

    after = _case_row(installed, case_id)
    assert after["state"] == CaseState.ESCALATED.value
    assert after["terminal_reason"] == TerminalReason.PROMISE_BEYOND_RECOVERY_WINDOW.value
    assert after["window_end_at"] == before["window_end_at"], (
        "the escalation moved window_end_at. R23.C4 leaves it unchanged, and R2.C12's "
        "termination bound is measured against it"
    )


# ---------------------------------------------------------------------------
# `pg` — the two promise resolutions an authoritative read produces
# ---------------------------------------------------------------------------


@pytest.mark.pg
def test_a_promise_paid_early_resolves_kept_with_a_negative_integer_interval(
    installed: Engine,
) -> None:
    """R23.C10. ``KEPT``, and the interval is a **signed count of seconds**.

    A customer who pays early produces a negative interval, which is the normal case for a promise
    kept well rather than an error to clamp to zero — so the example is deliberately an early
    payment. Asserted as an ``int`` and not a ``bool``, because ``bool`` is a subclass of ``int``
    and would satisfy every ``isinstance`` check while recording one second.

    The second call asserts idempotence: a second authoritative read of the same capture is not a
    second measurement, which is what the conditional ``UPDATE`` behind ``resolve`` is for.
    """
    moment = datetime.now(UTC)
    merchant_id, case_id, token = _live_case_with_token(
        installed, display_name="Promise kept", moment=moment
    )
    promise_date = (moment + timedelta(days=3)).replace(microsecond=0)
    assert _submit(
        merchant_id, token, PromiseSubmission(promise_date=promise_date), moment=moment
    ).accepted

    paid_at = promise_date - timedelta(hours=6)
    with tenant_transaction(merchant_id) as session:
        seconds = resolve_kept(session, merchant_id, case_id, paid_at=paid_at)

    assert seconds == -6 * 3600, (
        f"the recorded interval is {seconds}; paying six hours early is -21600 seconds. A clamp "
        "to zero would make the promise-kept measurement unable to say a customer paid early"
    )
    assert isinstance(seconds, int) and not isinstance(seconds, bool)

    row = _promise_row(installed, case_id)
    assert row["status"] == PromiseStatus.KEPT.value
    assert row["kept_at"] is not None, (
        "kept_at_iff_kept makes a KEPT status without an instant unstorable, so both move in one "
        "statement or neither does"
    )
    assert row["missed_at"] is None
    assert row["seconds_promise_to_payment"] == -6 * 3600

    with tenant_transaction(merchant_id) as session:
        again = resolve_kept(session, merchant_id, case_id, paid_at=paid_at + timedelta(hours=1))
    assert again is None, (
        "a second authoritative read of the same capture re-measured the interval; the "
        "conditional UPDATE is what makes the resolution idempotent"
    )
    assert _promise_row(installed, case_id)["seconds_promise_to_payment"] == -6 * 3600


@pytest.mark.pg
def test_a_promise_resolves_missed_only_once_a_follow_up_reached_confirmed(
    installed: Engine,
) -> None:
    """R23.C11, and the harder half of it is the negative assertion.

    The clause is **not** "the promised date passed and nothing was paid". Marking a promise
    ``MISSED`` while the follow-up Revora owed the customer was still queued would blame the
    customer for a message Revora had not sent. So the first call — with no confirmed
    ``PROMISE_TO_PAY_FOLLOW_UP`` intent — must move nothing, and only after one reaches
    ``CONFIRMED`` does the promise become ``MISSED``.

    ``CONFIRMED`` and not ``UNCERTAIN``: a resend is re-readable by nothing, so treating an
    unresolved one as a delivered follow-up would make the promise-kept rate a function of the
    provider's silence.
    """
    moment = datetime.now(UTC)
    merchant_id, case_id, token = _live_case_with_token(
        installed, display_name="Promise missed", moment=moment
    )
    promise_date = (moment + timedelta(days=2)).replace(microsecond=0)
    assert _submit(
        merchant_id, token, PromiseSubmission(promise_date=promise_date), moment=moment
    ).accepted

    with tenant_transaction(merchant_id) as session:
        premature = resolve_missed(session, merchant_id, case_id, moment=moment)
    assert premature is False, (
        "the promise was marked MISSED with no confirmed follow-up. That records a customer's "
        "failure where the truth is that Revora had not yet sent the reminder it owed them"
    )
    assert _promise_row(installed, case_id)["status"] == PromiseStatus.RECORDED.value

    _insert_confirmed_follow_up(installed, merchant_id, case_id, moment=moment)

    with tenant_transaction(merchant_id) as session:
        moved = resolve_missed(session, merchant_id, case_id, moment=moment)
    assert moved is True

    row = _promise_row(installed, case_id)
    assert row["status"] == PromiseStatus.MISSED.value
    assert row["missed_at"] is not None, (
        "R23.C11's missed-promise instant is when the read established the miss, not the promised "
        "date — a promise is not missed at the moment it was due"
    )
    assert row["kept_at"] is None
    assert row["seconds_promise_to_payment"] is None


def _insert_confirmed_follow_up(
    engine: Engine, merchant_id: uuid.UUID, case_id: uuid.UUID, *, moment: datetime
) -> None:
    """A ``CONFIRMED`` ``PROMISE_TO_PAY_FOLLOW_UP`` intent, and the decision it needs.

    Written directly rather than executed, because how the follow-up got confirmed is the
    execution engine's subject and R23.C11 only asks whether one did. ``effect_kind`` is
    ``PAYMENT_LINK_RESEND`` because a follow-up is a resend — which is also why an unresolved one
    is permanently unresolvable and why only ``CONFIRMED`` counts here.
    """
    decision_id = uuid.uuid4()
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO policy_decision (
                    id, merchant_id, case_id, verdict, primary_reason, rule_set_version,
                    evaluated_at, expires_at, selected_action, case_state_at_evaluation,
                    decision_cycle, created_at
                ) VALUES (
                    :id, :m, :c, 'APPROVED', 'ALL_CHECKS_PASSED', 'v1', :at, :expires,
                    :action, 'POLICY_CHECK', 1, now()
                )
                """
            ),
            {
                "id": str(decision_id),
                "m": str(merchant_id),
                "c": str(case_id),
                "at": moment,
                "expires": moment + timedelta(minutes=15),
                "action": CandidateAction.PROMISE_TO_PAY_FOLLOW_UP.value,
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO execution_intent (
                    id, merchant_id, case_id, policy_decision_id, idempotency_key, action,
                    attempt_ordinal, state, effect_kind, attempt_started_at, resolved_at,
                    created_at
                ) VALUES (
                    :id, :m, :c, :d, :key, :action, 1, :state, :effect, :at, :at, now()
                )
                """
            ),
            {
                "id": str(uuid.uuid4()),
                "m": str(merchant_id),
                "c": str(case_id),
                "d": str(decision_id),
                "key": f"idem-{uuid.uuid4()}",
                "action": CandidateAction.PROMISE_TO_PAY_FOLLOW_UP.value,
                "state": IntentState.CONFIRMED.value,
                "effect": ExecutionEffectKind.PAYMENT_LINK_RESEND.value,
                "at": moment,
            },
        )


# ---------------------------------------------------------------------------
# `pg` — a token presented after its case went terminal
# ---------------------------------------------------------------------------


@pytest.mark.pg
def test_a_token_presented_after_its_case_went_terminal_is_revoked_and_answers_410(
    installed: Engine,
) -> None:
    """R18.C8. A case entering a Terminal_State ends the customer's access, in that transaction.

    The token is minted and verified **before** the transition, so the 410 afterwards is a
    revocation and not a token that never worked. That ordering is the whole test: a version that
    seeded a terminal case would also pass against an implementation that merely refused to mint
    for one, which is a different and much weaker guarantee.

    The transition is applied through ``apply_transition`` — the only writer of
    ``recovery_case.state`` — rather than by an ``UPDATE``, because the revocation lives *at* that
    writer. R18.C8 holds for whatever terminal edge somebody adds next precisely because it is
    conditioned on the target state rather than on a list of edges, and an ``UPDATE`` here would
    have tested the list.

    410 rather than 404, per R18.C7: 128 bits of entropy makes enumeration infeasible, and a
    customer holding a dead link needs to be told it is dead rather than shown a status that reads
    as "wrong URL".
    """
    moment = datetime.now(UTC)
    merchant_id = insert_merchant(installed, display_name="Token after terminal")
    case_id = _seed_case(installed, merchant_id, moment=moment)
    wire_token = _mint(merchant_id, case_id, moment=moment)

    before = _verification(merchant_id, wire_token, moment=moment)
    assert before.token is not None, f"the token did not work before the case ended: {before}"
    assert before.status_code == 200

    result = apply_transition(
        merchant_id,
        case_id,
        expected_version=int(_case_row(installed, case_id)["version"]),
        target_state=CaseState.STOPPED,
        reason="task 54.2 — a terminal edge, to end the customer's access",
        actor="test",
        terminal_reason=TerminalReason.MAX_ATTEMPTS_REACHED,
    )
    assert result.applied, f"the terminal transition was refused: {result.outcome}"

    assert _token_revocations(installed, case_id) == [
        TokenRevocationReason.CASE_TERMINAL.value
    ], (
        "the case ended and its token was left live, so a customer can keep writing signals to a "
        "case nobody is working any more. The revocation is atomic with the state change"
    )

    after = _verification(merchant_id, wire_token, moment=moment + timedelta(minutes=1))
    assert after.token is None
    assert after.rejection is TokenRejection.REVOKED, (
        f"a token whose case has ended was refused as {after.rejection}; REVOKED is what happened "
        "and EXPIRED would be a different and untrue sentence about the same row"
    )
    assert after.status_code == 410
