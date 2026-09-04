"""A promise never buys time: Properties 41, 42 and 43.

Task 44.4, ``model`` tier. Three properties, and between them they are the whole of R23's
restraint: the Recovery_Window is unmoved by any promise whatever its date (P41), a scheduled
follow-up is strictly inside that window or there is no follow-up at all (P42), and a case holds no
more promises than its bound permits while a refused submission changes nothing and is still
recorded (P43).

**Why ``model`` and not ``pg``, and what that costs.** The clamp is arithmetic over three instants
and two configured durations, and the interesting behaviour is entirely at its boundaries — the
window end, the safety margin, the lead time and the submission instant. A ``pg`` version of these
properties would need a real case, a real token and a real signal per example, which is two orders
of magnitude fewer examples for the same assertions, and Hypothesis would spend its budget on
fixture setup instead of on the four boundaries. What the tier cannot establish is that the
``INSERT`` succeeds; that is asserted by the ``pg`` tier's own promise tests, and it is the reason
P42's *second* half is asserted here against the constraint's own predicate rather than against a
database: ``follow_up_within_window`` is ``follow_up_at IS NULL OR follow_up_at <
window_end_at_snapshot``, and every plan this file generates is checked against exactly that
expression. A plan that satisfies it is a plan PostgreSQL will accept.

**Three mechanisms appear below, each where it is the strongest available form.**

* *Generated inputs* over :func:`~revora.customer.promises.plan_promise` and
  :func:`~revora.customer.promises.meets_min_lead_time`, through
  :func:`~tests.strategies.customer.promise_dates`, which is boundary-anchored rather than
  uniform. Hypothesis is doing real work: three of the four boundaries are inclusive on one side
  and exclusive on the other, and a uniform generator would find none of them.
* *Declaration reading* — ``SIGNAL_REJECTION_STATUS``, the ``promise_to_pay`` table arguments, the
  ``CATALOGUE`` entries, ``PromiseStatus``. Asserting against the data is what makes the assertion
  fail when somebody changes the system rather than when somebody changes a copy of it.
* *AST inspection*, for the claim that is about what code **cannot** do: nothing on the promise
  path assigns ``window_end_at``. A substring search would fail on the prose — every module on this
  path explains at length that it must not move the window, and several name the column in a
  docstring — so the walk is over names the parser sees. The same reason
  ``test_partial_arrangement.py`` reads the AST.

**What P41's second clause is not asserted against here, and why.** *The case terminates within the
R2.C12 bound* is a statement about the lifecycle machine, which
``tests/properties/test_lifecycle_machine.py`` already drives over generated clock plans. What this
file establishes is the premise that proof depends on: no promise, at any date, produces a
``window_end_at`` other than the one the case opened with — so the termination bound the lifecycle
machine proves against an immutable window is proved against the window a promise leaves behind.
Re-driving the machine here would be a second, weaker copy of that test.

**One reading is recorded here rather than resolved.** R23.C3 computes the Follow_Up_Instant as
"the earlier of" two values, which is inclusive at a tie — when ``promise_date + offset`` equals
``window_end_at - margin`` the two are the same instant and ``min`` returns it either way, so the
tie is not observable in ``follow_up_at``. It *is* observable in ``clamped``, which this file
asserts is ``False`` at the tie: nothing was cut short, because the unclamped instant was already
inside the window. The alternative reading — reporting a tie as clamped — is deliberately not
asserted, because ``clamped`` exists so a merchant can be told *why* a follow-up is earlier than
they expected, and at a tie it is not earlier than anything.
"""

from __future__ import annotations

import ast
import base64
import dataclasses
import inspect
import json
import uuid
from collections.abc import Iterator, Sequence
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Final

import pytest
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st
from sqlalchemy import CheckConstraint, Engine, UniqueConstraint, text
from sqlalchemy.orm import Session, sessionmaker

from revora.customer import promises as promises_module
from revora.customer import signals as signals_module
from revora.customer.promises import (
    PROMISE_ESCALATION_KIND,
    PROMISE_SWEEP_KIND,
    PromisePlan,
    effective_promise_limit,
    follow_up_reached,
    meets_min_lead_time,
    plan_promise,
)
from revora.customer.signals import (
    SIGNAL_REJECTION_STATUS,
    PromiseSubmission,
    SignalOutcome,
    SignalRejection,
)
from revora.domain.actions import ELIGIBILITY, NULL_ACTIONS, CandidateAction
from revora.domain.enums import (
    ActionAvailability,
    CaseState,
    DiagnosisMethod,
    ExclusionReason,
    ExecutionEffectKind,
    IntentState,
    PolicyCheck,
    PolicyVerdict,
    PromiseStatus,
    RiskCause,
    TerminalReason,
)
from revora.domain.keys import execution_key
from revora.domain.money import Minor
from revora.domain.probability import Probability
from revora.estimation.candidates import (
    CandidateFigures,
    PromiseFollowUpFacts,
    build_candidate_set,
)
from revora.execution.engine import ExecutionOutcome, execute_approved_action
from revora.execution.reconcile import reconcile_intents
from revora.optimizer.arithmetic import CandidateInput
from revora.optimizer.selection import Thresholds, select
from revora.persistence.models.customer import (
    MAX_PROMISES_PER_CASE as SCHEMA_MAX_PROMISES_PER_CASE,
)
from revora.persistence.models.customer import PromiseToPay
from revora.platform import crypto
from revora.platform.clock import now
from revora.platform.config import (
    CATALOGUE,
    PROMISE_BOUND_KEYS,
    ConfigurationError,
    ValueKind,
    default_configuration,
)
from revora.platform.crypto import payload_cipher
from revora.platform.secrets import SecretStore, set_secret_store
from revora.policy.engine import evaluate
from revora.policy.input import PolicyInput
from revora.policy.rules import default_rule_set
from revora.providers.payment_link import NotifyMedium, resend_response_id
from tests.fakes.razorpay import (
    OPERATION_CREATE_PAYMENT_LINK,
    FakeRazorpay,
    ProviderBehaviour,
    ResendOutcome,
)
from tests.strategies.crashes import (
    CrashingProvider,
    CrashInjected,
    CrashPlan,
    crash_on_statement,
    resend_crash_plan,
)
from tests.strategies.customer import promise_dates
from tests.strategies.policy import policy_input

CONFIG = default_configuration()
"""The catalogue defaults, with nothing read from a database."""

NOW = datetime(2025, 3, 1, 12, 0, tzinfo=UTC)
"""A fixed submission instant. Fixed rather than generated because every offset below is
*relative* to it, so generating it as well would add a dimension in which nothing varies —
``plan_promise`` reads no clock and compares only differences."""

WINDOW = NOW + CONFIG.RECOVERY_WINDOW_DURATION
"""The ordinary window: one full ``RECOVERY_WINDOW_DURATION`` ahead of the submission."""


def _windows() -> st.SearchStrategy[datetime]:
    """Window ends spanning a comfortable window, a nearly-closed one, and a closed one.

    Three regimes rather than one, because R23.C6 is only reachable in the second and third: a
    window with more than ``PROMISE_WINDOW_SAFETY_MARGIN`` left admits a follow-up somewhere inside
    it however early the promised date, so a generator that only produced fresh windows would leave
    that clause's escalation path unvisited. Expressed as an offset from ``NOW`` so the three
    regimes are named by their relationship to the margin rather than by absolute dates.
    """
    margin_seconds = int(CONFIG.PROMISE_WINDOW_SAFETY_MARGIN.total_seconds())
    return st.one_of(
        # Comfortable: anywhere from a day to the full configured duration ahead.
        st.integers(
            min_value=86_400,
            max_value=int(CONFIG.RECOVERY_WINDOW_DURATION.total_seconds()),
        ).map(lambda s: NOW + timedelta(seconds=s)),
        # Nearly closed: inside the safety margin, either side of it, so R23.C6 fires and its
        # boundary is visited rather than only its interior.
        st.integers(min_value=-60, max_value=margin_seconds + 60)
        .map(lambda s: NOW + timedelta(seconds=s)),
        # Already closed. Reachable: a sweep can be late, and the accepting request reads the case
        # without locking it.
        st.integers(min_value=-604_800, max_value=-1).map(lambda s: NOW + timedelta(seconds=s)),
    )


def _plans() -> st.SearchStrategy[tuple[datetime, datetime, PromisePlan]]:
    """A drawn window, a drawn Promise_Date across every boundary, and the plan they produce.

    The three travel together because every assertion below is about their *relationship* — a plan
    on its own cannot be checked against a window it does not carry the inputs for, and re-deriving
    the inputs from the plan would be asserting the implementation against itself.

    Dates at or before the submission instant are dropped with ``assume``, not because they are
    uninteresting but because they never reach :func:`plan_promise`: ``record_signal`` refuses them
    first, and the function's own contract says so. They are asserted separately, against the
    refusal, in :func:`test_a_date_at_or_before_the_instant_is_refused_before_the_clamp`.
    """

    @st.composite
    def _draw(draw: st.DrawFn) -> tuple[datetime, datetime, PromisePlan]:
        window_end = draw(_windows())
        promise_date = draw(
            promise_dates(relative_to=NOW, window_end_at=window_end, config=CONFIG)
        )
        assume(promise_date > NOW)
        plan = plan_promise(
            promise_date=promise_date,
            instant=NOW,
            window_end_at=window_end,
            follow_up_offset=CONFIG.PROMISE_FOLLOW_UP_OFFSET,
            safety_margin=CONFIG.PROMISE_WINDOW_SAFETY_MARGIN,
        )
        return promise_date, window_end, plan

    return _draw()


# ---------------------------------------------------------------------------
# Property 41: the window is unmoved, for any date
# ---------------------------------------------------------------------------


@pytest.mark.model
@settings(max_examples=400)
@given(_plans())
def test_property_41_the_window_end_is_returned_unchanged_for_any_promise_date(
    drawn: tuple[datetime, datetime, PromisePlan],
) -> None:
    """P41. ``window_end_at`` equals its creation value for a promise with **any** date.

    Asserted as identity of the value rather than as "close enough", and against the *drawn* window
    rather than against anything the plan derived, so a clamp that returned ``window_end - margin``
    as the window — the single most plausible mistake in a function whose whole body is
    ``window_end - margin`` — fails here.

    Far-future and far-past dates are in the generated space on purpose. R23 places no upper bound
    on a Promise_Date, and "the window is unmoved" is worth nothing if it is only established for
    dates near the window.
    """
    _promise_date, window_end, plan = drawn
    assert plan.window_end_at == window_end


@pytest.mark.model
def test_property_41_nothing_on_the_promise_path_assigns_the_window_end() -> None:
    """P41's structural half: no module on this path can move ``window_end_at``.

    An AST walk rather than a substring search, and that choice is load-bearing here specifically.
    Every module on the promise path explains at length that it must not extend the window, and
    three of them name the column in prose — so ``"window_end_at" in source`` matches the
    *documentation* of the guarantee and would pass whatever the code did. The parser sees only
    names, so a real assignment is found and a paragraph about assignments is not.

    Reads are permitted and are the point: ``plan_promise`` reads the window, ``record_promise``
    snapshots it, and the escalation's audit record names it. What none of them may do is bind it.
    """
    offenders: list[str] = []
    for module in (promises_module, signals_module):
        tree = ast.parse(inspect.getsource(module))
        for node in ast.walk(tree):
            targets: list[ast.expr] = []
            if isinstance(node, ast.Assign):
                targets = list(node.targets)
            elif isinstance(node, ast.AugAssign | ast.AnnAssign):
                targets = [node.target]
            for target in targets:
                if isinstance(target, ast.Attribute) and target.attr == "window_end_at":
                    offenders.append(f"{module.__name__}:{node.lineno}")
    assert offenders == []


# ---------------------------------------------------------------------------
# Property 42: strictly inside the window, or nothing scheduled
# ---------------------------------------------------------------------------


@pytest.mark.model
@settings(max_examples=400)
@given(_plans())
def test_property_42_either_inside_the_margin_or_escalated_with_nothing_scheduled(
    drawn: tuple[datetime, datetime, PromisePlan],
) -> None:
    """P42. ``follow_up_at <= window_end - margin``, **or** the status is escalated with no
    follow-up.

    The disjunction is asserted as a disjunction rather than as two separate tests, because the
    property is that there is no third case. A plan that was ``RECORDED`` with a null follow-up, or
    escalated with a follow-up, would satisfy neither branch and is exactly what
    ``escalated_schedules_nothing`` exists to make unstorable.
    """
    _promise_date, window_end, plan = drawn
    latest = window_end - CONFIG.PROMISE_WINDOW_SAFETY_MARGIN

    if plan.status is PromiseStatus.BEYOND_WINDOW_ESCALATED:
        assert plan.follow_up_at is None
    else:
        assert plan.status is PromiseStatus.RECORDED
        assert plan.follow_up_at is not None
        assert plan.follow_up_at <= latest


@pytest.mark.model
@settings(max_examples=400)
@given(_plans())
def test_property_42_every_plan_satisfies_the_databases_own_check_expression(
    drawn: tuple[datetime, datetime, PromisePlan],
) -> None:
    """P42's other half, asserted against the constraint's predicate rather than against a database.

    ``follow_up_within_window`` is ``follow_up_at IS NULL OR follow_up_at <
    window_end_at_snapshot``, and this is that expression in Python over the same two values the
    ``INSERT`` would carry. **Strictly** less than, which is the difference between a stored row and
    a 503: the accepting endpoint is reachable without a session, so a plan the ``CHECK`` refuses is
    a well-formed request answered with a driver error.

    Worth having beside the test above even though a positive margin makes it follow: the two would
    come apart if ``PROMISE_WINDOW_SAFETY_MARGIN`` were ever zero, and this is the one that would
    fail. The configuration loader refuses a zero margin, so the pair is belt and braces on the one
    invariant whose violation is customer-visible.
    """
    _promise_date, _window_end, plan = drawn
    assert plan.follow_up_at is None or plan.follow_up_at < plan.window_end_at


@pytest.mark.model
@settings(max_examples=400)
@given(_plans())
def test_property_42_a_scheduled_follow_up_is_never_before_the_submission(
    drawn: tuple[datetime, datetime, PromisePlan],
) -> None:
    """R23.C6, as the other end of the same interval. A scheduled follow-up is in the future.

    A follow-up instant in the past would be claimed by the very next sweep pass, so a promise
    accepted at noon would produce a nudge at 12:05 about a date the customer named for next week —
    which is the message R23 exists to prevent, arriving faster than if the promise had never been
    made.
    """
    _promise_date, _window_end, plan = drawn
    assert plan.follow_up_at is None or plan.follow_up_at >= NOW


@pytest.mark.model
@settings(max_examples=300)
@given(_plans())
def test_the_clamp_takes_the_earlier_of_the_two_and_reports_which(
    drawn: tuple[datetime, datetime, PromisePlan],
) -> None:
    """R23.C3 restated as an equality, and ``clamped`` restated as which argument won.

    The equality is the requirement's own formula, written out again here rather than called: a test
    that asked ``plan_promise`` for the answer and compared it to ``plan_promise`` would agree with
    any implementation. ``clamped`` is asserted as ``False`` at a tie — see the module docstring for
    why that reading was taken.
    """
    promise_date, window_end, plan = drawn
    if plan.status is PromiseStatus.BEYOND_WINDOW_ESCALATED:
        assert plan.clamped is False
        return

    unclamped = promise_date + CONFIG.PROMISE_FOLLOW_UP_OFFSET
    latest = window_end - CONFIG.PROMISE_WINDOW_SAFETY_MARGIN
    assert plan.follow_up_at == min(unclamped, latest)
    assert plan.clamped is (latest < unclamped)


@pytest.mark.model
@settings(max_examples=300)
@given(window_end=_windows())
def test_a_date_at_or_after_the_window_end_always_escalates(window_end: datetime) -> None:
    """R23.C5 at its boundary. ``==`` escalates; the boundary is inclusive.

    Separated from the generated sweep above because the boundary is the whole content of the
    clause, and a test aimed at it says so. Assumes a window still ahead of the submission, so the
    date being tested is a legitimate future one — a window already closed escalates for R23.C6's
    reason instead, which is a different clause reaching the same status.
    """
    assume(window_end > NOW)
    plan = plan_promise(
        promise_date=window_end,
        instant=NOW,
        window_end_at=window_end,
        follow_up_offset=CONFIG.PROMISE_FOLLOW_UP_OFFSET,
        safety_margin=CONFIG.PROMISE_WINDOW_SAFETY_MARGIN,
    )
    assert plan.status is PromiseStatus.BEYOND_WINDOW_ESCALATED
    assert plan.follow_up_at is None
    assert plan.escalates is True


@pytest.mark.model
def test_a_window_with_less_than_the_margin_left_escalates_however_early_the_date() -> None:
    """R23.C6, and the inference behind it stated as a test.

    A window with less room than the safety margin admits no follow-up inside it, so *every*
    promised date escalates — including one an hour from now, which is as accommodating a date as a
    customer can give. That is counter-intuitive enough to be worth a named test: the escalation is
    not about the date being far away, it is about there being nowhere to put the follow-up.
    """
    window_end = NOW + CONFIG.PROMISE_WINDOW_SAFETY_MARGIN - timedelta(seconds=1)
    for ahead in (timedelta(hours=1), timedelta(days=1), timedelta(days=400)):
        plan = plan_promise(
            promise_date=NOW + ahead,
            instant=NOW,
            window_end_at=window_end,
            follow_up_offset=CONFIG.PROMISE_FOLLOW_UP_OFFSET,
            safety_margin=CONFIG.PROMISE_WINDOW_SAFETY_MARGIN,
        )
        assert plan.status is PromiseStatus.BEYOND_WINDOW_ESCALATED, ahead
        assert plan.follow_up_at is None


# ---------------------------------------------------------------------------
# Property 43: the bound holds, and a refusal changes nothing
# ---------------------------------------------------------------------------


@pytest.mark.model
@given(configured=st.integers(min_value=-5, max_value=25))
def test_property_43_the_effective_limit_never_exceeds_the_schemas_own_bound(
    configured: int,
) -> None:
    """P43. The checked bound is never above what ``UNIQUE (merchant_id, case_id)`` will hold.

    The reconciliation of a configurable bound with an index that encodes today's value of it, as a
    property over every configured value including the nonsensical ones. Above the schema's bound it
    is floored — attempting the row would be a constraint violation and a 503 where R23.C7 requires
    a 409. Below it, it is honoured, which is what lets a merchant set 0 and stop accepting promises
    without a migration. Negative is floored to 0, because "fewer than no promises" is not a
    statement about anything.
    """
    config = default_configuration()
    limit = effective_promise_limit(
        type(config)(**{**{f.name: getattr(config, f.name) for f in _fields(config)},
                        "MAX_PROMISES_PER_CASE": configured})
    )
    assert 0 <= limit <= SCHEMA_MAX_PROMISES_PER_CASE
    assert limit == max(0, min(configured, SCHEMA_MAX_PROMISES_PER_CASE))


def _fields(config: object) -> tuple:
    """``dataclasses.fields`` of the configuration, minus the two derived sets.

    A helper rather than an inline comprehension because the two ``frozenset`` fields carry
    defaults and are not accepted positionally, so a rebuild has to name them or omit them — and
    omitting them is correct, since a rebuilt configuration has not defaulted or ignored anything
    new.
    """
    import dataclasses

    return tuple(
        f
        for f in dataclasses.fields(config)  # type: ignore[arg-type]
        if f.name not in {"defaulted_keys", "unrecognized_keys"}
    )


@pytest.mark.model
def test_property_43_the_unique_index_is_what_backstops_the_bound() -> None:
    """P43's structural half, read off the table rather than trusted.

    The application check and the index are two statements of one number, so the index has to
    *exist* for the reconciliation argument to hold at all. Read from
    ``PromiseToPay.__table_args__`` so a migration that dropped it — the legitimate way to
    raise the bound above one — fails here and forces the constant beside it to move in the
    same change.
    """
    unique = [
        arg
        for arg in PromiseToPay.__table_args__
        if isinstance(arg, UniqueConstraint)
        and tuple(column.name for column in arg.columns) == ("merchant_id", "case_id")
    ]
    assert len(unique) == 1
    assert SCHEMA_MAX_PROMISES_PER_CASE == 1


@pytest.mark.model
def test_property_43_a_refused_second_promise_still_records_a_signal() -> None:
    """P43's second half, as the outcome shape R23.C7 requires. 409 *and* a signal id.

    The one place on this surface where a rejection travels beside a persisted signal, and the
    reason it is asserted on the dataclass rather than through a request: what R23.C7 requires is
    that the two coexist, and a test that drove the endpoint would establish that only for whichever
    path it happened to take. Here it is established for the type, so no path can produce a refusal
    that discards the signal.
    """
    import uuid as _uuid

    signal_id = _uuid.uuid4()
    outcome = SignalOutcome(
        signal_id=signal_id, rejection=SignalRejection.PROMISE_ALREADY_RECORDED
    )
    assert outcome.status_code == 409
    assert outcome.accepted is True
    assert outcome.signal_id == signal_id


@pytest.mark.model
def test_the_two_deferred_rejection_rows_are_in_the_status_table() -> None:
    """The two rows the design's table marked deferred, now present, and present as *data*.

    Asserted against ``SIGNAL_REJECTION_STATUS`` rather than by driving the endpoint, because that
    mapping *is* the mechanism: the router reads ``SignalOutcome.status_code``, which reads this
    table, so a new refusal is a member and a row and never a branch. A test that drove the
    endpoint would pass equally well against a router that had grown two ``if``s, which is the
    arrangement this asserts against.

    Every member is covered, not only the two new ones. A member with no row would raise
    ``KeyError`` from ``status_code`` at the moment a customer hit it — a 500 on a path whose whole
    purpose is to answer a refusal cleanly.
    """
    assert SIGNAL_REJECTION_STATUS[SignalRejection.PROMISE_BELOW_MIN_LEAD_TIME] == 422
    assert SIGNAL_REJECTION_STATUS[SignalRejection.PROMISE_ALREADY_RECORDED] == 409
    assert set(SIGNAL_REJECTION_STATUS) == set(SignalRejection)


# ---------------------------------------------------------------------------
# The lead time (R23.C2), and the degenerate guard it does not replace
# ---------------------------------------------------------------------------


@pytest.mark.model
@settings(max_examples=400)
@given(promise_date=promise_dates(relative_to=NOW, window_end_at=WINDOW, config=CONFIG))
def test_the_lead_time_boundary_is_inclusive_at_the_bound(promise_date: datetime) -> None:
    """R23.C2. Refused strictly inside the bound; accepted exactly at it.

    Inclusive at the bound because the bound answers "how soon may a promise be for", and a date
    exactly one lead time away *is* one lead time away. Asserted as an equivalence against the
    comparison rather than as two examples, so the boundary cannot drift by a microsecond in either
    direction without failing.
    """
    accepted = meets_min_lead_time(
        promise_date, instant=NOW, min_lead_time=CONFIG.PROMISE_MIN_LEAD_TIME
    )
    assert accepted is (promise_date >= NOW + CONFIG.PROMISE_MIN_LEAD_TIME)


@pytest.mark.model
def test_a_date_at_or_before_the_instant_is_refused_before_the_clamp() -> None:
    """The degenerate guard, and why it survives the configured one.

    At the default lead time the configured guard subsumes this one entirely. At a configured lead
    time of zero it does not, and this is what still refuses a date the ``promise_date >
    recorded_at`` ``CHECK`` cannot hold. Asserted at zero, because that is the only configuration in
    which the two guards give different answers and therefore the only one in which keeping both is
    justified.
    """
    assert meets_min_lead_time(NOW, instant=NOW, min_lead_time=timedelta(0)) is True
    assert (
        meets_min_lead_time(
            NOW - timedelta(microseconds=1), instant=NOW, min_lead_time=timedelta(0)
        )
        is False
    )


@pytest.mark.model
@given(
    raw=st.sampled_from(
        [
            "2025-03-12T00:00:00Z",
            "2025-03-12T00:00:00+05:30",
            "2025-03-11T18:30:00+00:00",
            "2025-03-12T05:30:00+05:30",
        ]
    )
)
def test_the_received_representation_is_what_arrived_not_what_it_parsed_to(raw: str) -> None:
    """R23.C1. The submitted string is retained beside the UTC instant.

    Two of the four values above denote the *same instant* in different offsets, which is the whole
    reason the column exists: "the customer meant midnight their time" and "the customer meant half
    past six in the evening UTC" are different sentences about one row, and only the retained string
    distinguishes them. R16.C13's terms, applied to a promise.
    """
    submission = PromiseSubmission.model_validate({"promise_date": raw})
    assert submission.received_representation == raw


@pytest.mark.model
def test_the_received_representation_cannot_be_supplied_by_the_caller() -> None:
    """The retained string is the parse's, and a request cannot forge it.

    ``extra="forbid"`` is what makes this true, and it is asserted because the alternative
    implementation — a declared field — would have made the column hold whatever the caller said
    arrived, which is worse than not having the column at all.
    """
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        PromiseSubmission.model_validate(
            {"promise_date": "2025-03-12T00:00:00Z", "received_representation": "forged"}
        )


# ---------------------------------------------------------------------------
# The bounds themselves, and the one whose value the loader constrains
# ---------------------------------------------------------------------------


@pytest.mark.model
def test_every_promise_bound_is_in_the_catalogue_with_the_kind_the_group_declares() -> None:
    """The five bounds R23 needs exist, are of the declared kinds, and are reachable typed.

    ``PROMISE_BOUND_KEYS`` is a mapping rather than a tuple because the five are of mixed kinds, so
    the group's own import-time check cannot be a uniform one — this asserts the mapping agrees with
    the catalogue from the outside as well, and that the accessor exposes every one of them. A bound
    in the catalogue with no accessor attribute is a ``TypeError`` at construction; a bound in this
    group that the migration did not seed is a ``defaulted_keys`` entry the API refuses to start on.
    """
    for key, kind in PROMISE_BOUND_KEYS.items():
        assert CATALOGUE[key].kind is kind
        assert CATALOGUE[key].is_assumption is True
        assert hasattr(CONFIG, key)

    assert CATALOGUE["MAX_PROMISES_PER_CASE"].kind is ValueKind.INTEGER
    assert CONFIG.MAX_PROMISES_PER_CASE == SCHEMA_MAX_PROMISES_PER_CASE


@pytest.mark.model
@given(margin_seconds=st.integers(min_value=-3600, max_value=0))
def test_a_non_positive_safety_margin_is_refused_at_load(margin_seconds: int) -> None:
    """The invariant that makes P42's strictness free rather than checked.

    A margin at or below zero puts the clamped Follow_Up_Instant at or past ``window_end_at``, which
    ``follow_up_within_window`` refuses — so the failure would be a 503 answering a well-formed
    promise on a public endpoint. Refused at configuration load instead, which covers the seed, the
    per-merchant load and the change, because all three go through ``from_values``.
    """
    values = dict(CONFIG.as_raw())
    values["PROMISE_WINDOW_SAFETY_MARGIN"] = str(margin_seconds)
    with pytest.raises(ConfigurationError) as caught:
        type(CONFIG).from_values(values, version="test")
    assert "PROMISE_WINDOW_SAFETY_MARGIN" in str(caught.value)


@pytest.mark.model
def test_the_follow_up_offset_is_not_below_the_cooldown() -> None:
    """A configured ordering nothing enforces, asserted so a change to either is deliberate.

    A follow-up offset below ``COOLDOWN_INTERVAL`` produces a follow-up that becomes due and is then
    refused by policy check 8 for being too soon after the message that carried the promise — which
    reads to an operator as a lost nudge rather than as a configured gap. Not enforced by
    the loader, unlike the safety margin, because the consequence is a *withheld* action
    rather than an unstorable row: the case still terminates inside its bound and no request
    fails. Asserted here so the two defaults cannot drift apart unnoticed.
    """
    assert CONFIG.PROMISE_FOLLOW_UP_OFFSET >= CONFIG.COOLDOWN_INTERVAL


# ---------------------------------------------------------------------------
# The follow-up predicate and the sweep's vocabulary
# ---------------------------------------------------------------------------


@pytest.mark.model
@settings(max_examples=300)
@given(
    drawn=_plans(),
    skew_seconds=st.integers(min_value=-604_800, max_value=604_800),
)
def test_the_follow_up_predicate_agrees_with_the_instant_it_reads(
    drawn: tuple[datetime, datetime, PromisePlan], skew_seconds: int
) -> None:
    """R23.C9's condition, over a clock moved either side of the Follow_Up_Instant.

    ``FOLLOW_UP_SCHEDULED`` answers ``True`` regardless of the clock, and that asymmetry is
    deliberate rather than an oversight: the sweep only moves a promise into that status once the
    instant has passed, so re-deriving the answer from a timestamp would let a clock skew
    *un*-schedule a follow-up the sweep already scheduled. An escalated promise answers ``False``,
    because it has no instant to reach.
    """
    _promise_date, _window_end, plan = drawn
    at = NOW + timedelta(seconds=skew_seconds)

    assert follow_up_reached(plan.status, plan.follow_up_at, instant=at) is (
        plan.follow_up_at is not None and at >= plan.follow_up_at
    )
    assert (
        follow_up_reached(PromiseStatus.FOLLOW_UP_SCHEDULED, plan.follow_up_at, instant=at) is True
    )
    assert (
        follow_up_reached(PromiseStatus.BEYOND_WINDOW_ESCALATED, None, instant=at) is False
    )


@pytest.mark.model
def test_the_promise_table_carries_the_two_checks_the_clamp_relies_on() -> None:
    """The two ``CHECK`` constraints P42 is half of, read off the table.

    Asserted by name rather than by predicate text, because the text is dialect-rendered and would
    make this a test of SQLAlchemy's compiler. The names are what migration ``0008``
    created and what a later migration would have to drop, so a constraint removed shows
    up here.
    """
    # The declared names, not the rendered ones. SQLAlchemy's naming convention prefixes each
    # with ``ck_promise_to_pay_`` at table-construction time, so the assertion is against the
    # suffix — the part a migration writes and a reviewer reads.
    names = {
        str(arg.name)
        for arg in PromiseToPay.__table_args__
        if isinstance(arg, CheckConstraint)
    }
    for declared in (
        "follow_up_within_window",
        "escalated_schedules_nothing",
        "promise_date_after_recording",
        "kept_at_iff_kept",
    ):
        assert any(name.endswith(declared) for name in names), declared


@pytest.mark.model
def test_the_two_job_kinds_are_distinct_and_the_terminal_reason_exists() -> None:
    """The escalation and the sweep are two kinds, and the reason they escalate with is declared.

    Distinct because they are different mechanisms with different schedules — one is enqueued per
    escalated promise by the accepting transaction, the other by the clock — and one kind serving
    both would put a periodic sweep in the queue for every promise. ``PROMISE_SWEEP_KIND`` is in
    ``PERIODIC_SWEEP_KINDS`` and ``PROMISE_ESCALATION_KIND`` is deliberately not; that pairing is
    asserted in ``tests/test_ticker_schedule.py``, which prices every periodic kind.
    """
    from revora.jobs.scheduler import PERIODIC_SWEEP_KINDS

    assert PROMISE_ESCALATION_KIND != PROMISE_SWEEP_KIND
    assert PROMISE_SWEEP_KIND in PERIODIC_SWEEP_KINDS
    assert PROMISE_ESCALATION_KIND not in PERIODIC_SWEEP_KINDS
    assert TerminalReason.PROMISE_BEYOND_RECOVERY_WINDOW.value in {
        reason.value for reason in TerminalReason
    }
    # The status a beyond-window promise holds, and the state its case reaches. Two enumerations,
    # one decision, and naming both here is what makes the pairing reviewable in one place.
    assert PromiseStatus.BEYOND_WINDOW_ESCALATED in set(PromiseStatus)
    assert CaseState.ESCALATED in set(CaseState)


@pytest.mark.model
def test_the_promise_module_is_where_the_bound_and_the_clamp_both_live() -> None:
    """One module owns the promise, and the paths that read it are named.

    A cheap assertion with a specific purpose: ``revora/customer/promises.py`` is the file
    the design names, and a reviewer checking that the clamp is not duplicated needs to know
    there is one. The file's existence and its exports are what a second implementation would
    have to work around.
    """
    path = Path(inspect.getfile(promises_module))
    assert path.name == "promises.py"
    assert path.parent.name == "customer"
    for name in ("plan_promise", "record_promise", "sweep_due_promises"):
        assert name in promises_module.__all__

# ---------------------------------------------------------------------------
# Task 47.4 — the new action's own three properties
# ---------------------------------------------------------------------------
#
# Property 46 and Property 45 are ``model``: one is arithmetic over a candidate set and the
# other is the policy engine, which is a pure function of ``PolicyInput``. Property 44 is
# ``pg``, and it has to be — it is a claim about what survives a process death, and the thing
# that survives is a committed row. Marked per test rather than per module, which is why the
# tier split can live in one file at all.


_ELIGIBLE_CAUSES: Final[tuple[RiskCause, ...]] = tuple(
    cause
    for cause in RiskCause
    if CandidateAction.PROMISE_TO_PAY_FOLLOW_UP in ELIGIBILITY.get(cause, frozenset())
)
"""The causes whose eligibility row admits the follow-up, read from the table rather than
listed. Property 46's third condition is the complement of this tuple, so listing it here by
hand would make the test agree with a copy of the rule instead of with the rule."""

_THRESHOLDS = Thresholds(
    min_net_value=CONFIG.MIN_NET_VALUE_THRESHOLD,
    min_incremental_probability=CONFIG.MIN_INCREMENTAL_PROBABILITY,
    max_cost_to_value_ratio=CONFIG.MAX_COST_TO_VALUE_RATIO,
    high_baseline=CONFIG.HIGH_BASELINE_THRESHOLD,
)

_POLICY_RULES = default_rule_set(
    max_recovery_attempts=CONFIG.MAX_RECOVERY_ATTEMPTS,
    max_customer_messages=CONFIG.MAX_CUSTOMER_MESSAGES,
    cooldown_interval=CONFIG.COOLDOWN_INTERVAL,
    policy_decision_validity=CONFIG.POLICY_DECISION_VALIDITY,
    risk_reason_codes=frozenset({"payment_risk_check_failed"}),
    min_net_value_threshold=CONFIG.MIN_NET_VALUE_THRESHOLD,
    min_incremental_probability=CONFIG.MIN_INCREMENTAL_PROBABILITY,
)
"""The rule set built from the same catalogue defaults every other figure here reads, so a
configured cooldown and the cooldown the property asserts against cannot disagree."""

_BASELINE = Probability(Decimal("0.2000"))
"""Comfortably below ``HIGH_BASELINE_THRESHOLD``, so a selection is decided on value rather
than short-circuited by R7.C6. The follow-up's availability is what varies here, not the
baseline."""


def _inputs(figures: tuple[CandidateFigures, ...]) -> tuple[CandidateInput, ...]:
    """The estimation layer's figures as the optimizer's input struct.

    The same narrowing ``revora.optimizer.service._to_input`` performs on a persisted row,
    without the row. Carrying ``availability`` and ``unavailable_reason`` across is the part
    that matters: the optimizer records the estimation layer's own reason rather than
    re-deriving one, and a test that dropped them would be testing a different pipeline.
    """
    return tuple(
        CandidateInput(
            action=figure.action,
            intervention_probability=figure.intervention_probability,
            financial_cost=figure.financial_cost,
            communication_cost=figure.communication_cost,
            risk_cost=figure.risk_cost,
            customer_cost=figure.customer_cost,
            availability=figure.availability,
            unavailable_reason=figure.unavailable_reason,
        )
        for figure in figures
    )


def _expected_exclusion(
    *, capability: bool, status: PromiseStatus | None, reached: bool
) -> ExclusionReason | None:
    """Property 46's three grounds, in the order R24 and R23.C9 ask them in.

    Written out here rather than imported from
    :func:`revora.estimation.candidates.promise_follow_up_exclusion`, because importing the
    function under test as the oracle is how a property becomes a tautology. The ordering is
    part of the claim: a case with no promise *and* no capability records the capability, so a
    withdrawn capability is never reported as a customer who said nothing.
    """
    if not capability:
        return ExclusionReason.PROVIDER_CAPABILITY_UNVERIFIED
    if status is None or status not in (
        PromiseStatus.RECORDED,
        PromiseStatus.FOLLOW_UP_SCHEDULED,
    ):
        return ExclusionReason.NO_PROMISE_RECORDED
    if not reached:
        return ExclusionReason.PROMISE_DATE_NOT_REACHED
    return None


@pytest.mark.model
@settings(max_examples=400)
@given(
    cause=st.sampled_from(list(RiskCause)),
    status=st.sampled_from([None, *list(PromiseStatus)]),
    offset=st.sampled_from(
        (timedelta(hours=-6), timedelta(seconds=-1), timedelta(0), timedelta(seconds=1))
    ),
    capability=st.booleans(),
    amount=st.sampled_from((200_000, 400_000, 1_000_000)),
)
def test_property_46_every_exclusion_ground_is_recorded_and_the_candidate_retained(
    cause: RiskCause,
    status: PromiseStatus | None,
    offset: timedelta,
    capability: bool,
    amount: int,
) -> None:
    """**Property 46.** The follow-up is absent from selection on each ground, with that
    ground recorded, and retained in the recorded set in every case.

    Four grounds are generated, not three, and the fourth is the one that changes shape.
    ``PROVIDER_CAPABILITY_UNVERIFIED`` (R24.C16), ``NO_PROMISE_RECORDED`` (R24.C2) and
    ``PROMISE_DATE_NOT_REACHED`` (R23.C9) keep the action **in** the figures marked
    ``UNAVAILABLE``; ``CAUSE_NOT_ELIGIBLE`` (R24.C3) keeps it in
    :attr:`~revora.estimation.candidates.CandidateSet.excluded_by_cause` instead, because a
    cause that does not permit an action never gives it figures to be unavailable *with*.
    Both are retention under R6.C9 and neither is a silent drop, which is the whole assertion:
    *why nobody chased this promise* has to be answerable, and it is unanswerable from an
    absence.

    **The retention is asserted twice, at both layers.** Once on the estimation layer's set,
    and once on the optimizer's ``candidates`` tuple after
    :func:`~revora.optimizer.selection.select` has run — because the record a merchant reads is
    written from the second, and an optimizer that filtered unavailable members on the way
    through would leave the estimation layer's retention true and the record empty.

    **R24.C4's five figures are asserted on the excluded member too.** Being in the set is not
    the same as being selectable, and the requirement asks for five figures whenever the action
    is in a candidate set — so an ``UNAVAILABLE`` follow-up still carries a probability, four
    costs and a method for each. A member with a null method would reach the database as a row
    the ``method`` column refuses.

    **The offsets are seconds either side of the instant, not hours.** R23.C9's comparison is
    ``instant >= follow_up_at``, inclusive at the tie, and a generator that sampled in hours
    would never distinguish inclusive from exclusive — the one place this property could be
    wrong and pass.
    """
    instant = NOW
    promise = (
        None
        if status is None
        else PromiseFollowUpFacts(
            status=status,
            # BEYOND_WINDOW_ESCALATED scheduled nothing, and the schema enforces it. Giving it
            # a Follow_Up_Instant here would generate a row the database cannot hold.
            follow_up_at=(
                None
                if status is PromiseStatus.BEYOND_WINDOW_ESCALATED
                else instant + offset
            ),
            instant=instant,
        )
    )
    reached = (
        promise is not None
        and follow_up_reached(promise.status, promise.follow_up_at, instant=instant)
    )
    expected = _expected_exclusion(capability=capability, status=status, reached=reached)

    candidates = build_candidate_set(
        cause,
        baseline=_BASELINE,
        remaining=CONFIG.RECOVERY_WINDOW_DURATION // 2,
        window=CONFIG.RECOVERY_WINDOW_DURATION,
        config=CONFIG,
        promise=promise,
        resend_capability_verified=capability,
    )
    figure = candidates.figure_for(CandidateAction.PROMISE_TO_PAY_FOLLOW_UP)

    if cause not in _ELIGIBLE_CAUSES:
        assert figure is None, (
            f"{cause.value} does not permit the follow-up, yet it was priced; R6.C1 caps "
            "membership at what the eligibility table permits"
        )
        assert CandidateAction.PROMISE_TO_PAY_FOLLOW_UP in candidates.excluded_by_cause, (
            "R24.C3's exclusion was dropped rather than recorded; a merchant asking why "
            "nobody chased this promise gets no answer from an absence"
        )
        return

    assert figure is not None, "R24.C2 retains the excluded candidate in the recorded set"
    for name in (
        "probability_method",
        "financial_cost_method",
        "communication_cost_method",
        "risk_cost_method",
        "customer_cost_method",
    ):
        assert getattr(figure, name) is not None, (
            f"{name} is unset on a retained follow-up; R24.C4 leaves none of the five unset, "
            "and a null method is a row the schema refuses"
        )

    result = select(
        _inputs(candidates.figures),
        baseline=_BASELINE,
        amount=Minor(amount),
        thresholds=_THRESHOLDS,
    )
    evaluated = next(
        item
        for item in result.candidates
        if item.action is CandidateAction.PROMISE_TO_PAY_FOLLOW_UP
    )

    if expected is None:
        assert figure.availability is ActionAvailability.AVAILABLE, (
            "a pending promise past its Follow_Up_Instant on a permitting cause with the "
            "capability verified is the one case where the follow-up competes"
        )
        return

    assert figure.availability is ActionAvailability.UNAVAILABLE
    assert figure.unavailable_reason == expected.value, (
        f"recorded {figure.unavailable_reason} where {expected.value} is the ground; the "
        "three are not interchangeable and collapsing them makes the only question worth "
        "asking about a skipped follow-up unanswerable"
    )
    assert result.selected.action is not CandidateAction.PROMISE_TO_PAY_FOLLOW_UP, (
        "an excluded follow-up was selected anyway"
    )
    assert (
        CandidateAction.PROMISE_TO_PAY_FOLLOW_UP not in result.qualifying_actions
    ), "an excluded follow-up was in the pool that competed"
    assert evaluated.excluded and evaluated.exclusion_reason is expected, (
        "the optimizer re-derived the exclusion instead of carrying the estimation layer's "
        "own reason through"
    )


@pytest.mark.model
@settings(max_examples=200)
@given(
    status=st.sampled_from((PromiseStatus.RECORDED, PromiseStatus.FOLLOW_UP_SCHEDULED)),
    amount=st.sampled_from((200_000, 400_000, 1_000_000)),
)
def test_a_withdrawn_resend_capability_strands_no_promise_holding_case(
    status: PromiseStatus, amount: int
) -> None:
    """Task 47.5. The capability goes and the value model routes around it (R24.C16, R6.C9).

    The degradation path stated as three facts about one candidate set, on a case whose promise
    is pending and due — which is exactly the case that has something to lose:

    1. the follow-up is ``UNAVAILABLE`` with ``PROVIDER_CAPABILITY_UNVERIFIED`` and **retained**;
    2. ``PAYMENT_LINK`` and the two null actions still compete, so the case is still decidable;
    3. a decision is still reached, which is what "no promise-holding case is stranded" means
       at this layer — R24.C15's termination bound is a lifecycle claim and
       ``test_lifecycle_machine.py`` drives it, but a case whose candidate set produced no
       decision would never reach the lifecycle at all.

    The capability *is* verified today, so this is the withdrawal expressed and tested rather
    than only described in a degradation table. ``capability_verified`` defaulting to true is
    what makes it a degradation path rather than a live one.
    """
    promise = PromiseFollowUpFacts(
        status=status, follow_up_at=NOW - timedelta(hours=1), instant=NOW
    )
    candidates = build_candidate_set(
        RiskCause.INSUFFICIENT_FUNDS,
        baseline=_BASELINE,
        remaining=CONFIG.RECOVERY_WINDOW_DURATION // 2,
        window=CONFIG.RECOVERY_WINDOW_DURATION,
        config=CONFIG,
        promise=promise,
        resend_capability_verified=False,
    )

    follow_up = candidates.figure_for(CandidateAction.PROMISE_TO_PAY_FOLLOW_UP)
    assert follow_up is not None, "R6.C9 retains it; the dashboard has to be able to say why"
    assert follow_up.availability is ActionAvailability.UNAVAILABLE
    assert (
        follow_up.unavailable_reason
        == ExclusionReason.PROVIDER_CAPABILITY_UNVERIFIED.value
    )

    result = select(
        _inputs(candidates.figures),
        baseline=_BASELINE,
        amount=Minor(amount),
        thresholds=_THRESHOLDS,
    )
    assert result.selected.action is not CandidateAction.PROMISE_TO_PAY_FOLLOW_UP
    still_competing = {
        item.action for item in result.candidates if not item.excluded
    }
    assert CandidateAction.PAYMENT_LINK in still_competing, (
        "the payment link stopped competing when the resend capability went; the two are "
        "independent capabilities and the degradation table says so"
    )
    assert still_competing >= NULL_ACTIONS, (
        "a null action was excluded, so the case has nothing left it can be decided as"
    )


@pytest.mark.model
@settings(max_examples=200)
@given(
    status=st.sampled_from((PromiseStatus.RECORDED, PromiseStatus.FOLLOW_UP_SCHEDULED)),
    amount=st.sampled_from((200_000, 400_000, 500_000)),
)
def test_a_case_holding_a_live_payment_link_is_offered_no_second_one(
    status: PromiseStatus, amount: int
) -> None:
    """R24.C10 at system scope: no second payable link for one debt, and the follow-up instead.

    **The defect this closes.** A second decision cycle on a case that already held a live payment
    link ranked ``PAYMENT_LINK`` first and created another one — a second live link for the same
    debt, with its own ``reference_id`` and nothing cancelling the first, so the customer could pay
    twice. R24.C10 forbids the second link, and reading that clause only on the follow-up's own
    execution path missed the path that actually produced the duplicate: a *decision* to create a
    link again.

    Both branches of the same generated case are asserted, because the exclusion is only correct if
    it is conditional. With a live link ``PAYMENT_LINK`` is ``UNAVAILABLE`` with
    ``LIVE_PAYMENT_LINK_EXISTS`` and retained; with none it is a plain competing candidate. A case
    whose link is absent — or expired, which under
    :func:`revora.providers.payment_link.clamp_expire_by` means its recovery window has closed —
    must still be able to create one, or a link that went away would strand the case forever. What
    "live" means is not restated in this test or in the estimation layer: it is
    ``ExecutionIntentRepository.live_payment_link``, and
    ``tests/persistence/test_resend_disposition.py`` drives it against real rows.

    **Retention, not removal** (R6.C9, R26.C15). The excluded candidate stays in the estimation
    layer's set *and* in the optimizer's ``candidates`` tuple, carrying all five figures with a
    method on each (R24.C4), because "why did nobody send a link on this cycle" has to be
    answerable and it is unanswerable from an absence.

    **And the consequence, which is the reason the two problems were one problem.**
    ``PAYMENT_LINK`` strictly dominated ``PROMISE_TO_PAY_FOLLOW_UP`` on net value at every amount
    — ``net(PL) - net(FU) = 0.02 x amount + 725``, positive throughout — so a due, pending promise
    on a permitting cause never actually got followed up. Withdrawing the duplicate-link action
    removes that dominance without re-pricing anything: no uplift, cost or threshold moves, and the
    follow-up wins on the figures it always had.

    The amounts stop below ₹5,737.50, and that bound is not this change's business. It is the
    existing ``HUMAN_ESCALATION`` crossover — ``0.10 x amount - 25_000`` overtakes
    ``0.06 x amount - 2_050`` there — so above it the escalation legitimately holds the highest net
    value and selecting the follow-up would be the wrong answer. The same crossover is what
    ``DEMO_ESCALATION_AMOUNT_RANGE`` is placed above deliberately. What this test claims is that the
    follow-up wins *where it should*, not that it wins everywhere.
    """
    promise = PromiseFollowUpFacts(
        status=status, follow_up_at=NOW - timedelta(hours=1), instant=NOW
    )

    def build(*, live: bool) -> tuple[CandidateFigures, ...]:
        return build_candidate_set(
            RiskCause.INSUFFICIENT_FUNDS,
            baseline=_BASELINE,
            remaining=CONFIG.RECOVERY_WINDOW_DURATION // 2,
            window=CONFIG.RECOVERY_WINDOW_DURATION,
            config=CONFIG,
            promise=promise,
            live_payment_link=live,
        ).figures

    # -- no live link: the ordinary case, and the control that keeps the assertion honest ----
    without = build(live=False)
    fresh = next(f for f in without if f.action is CandidateAction.PAYMENT_LINK)
    assert fresh.availability is ActionAvailability.AVAILABLE, (
        "a case holding no live link was refused a payment link; an expired or absent link must "
        "leave the action available or the case is stranded with nothing payable"
    )
    assert fresh.unavailable_reason is None

    # -- holding a live link: excluded on the new ground, retained with every figure ---------
    figures = build(live=True)
    held = next((f for f in figures if f.action is CandidateAction.PAYMENT_LINK), None)
    assert held is not None, (
        "PAYMENT_LINK was dropped from the recorded set rather than retained; R6.C9 and R26.C15 "
        "keep it so a merchant can be told why no link was sent"
    )
    assert held.availability is ActionAvailability.UNAVAILABLE
    assert held.unavailable_reason == ExclusionReason.LIVE_PAYMENT_LINK_EXISTS.value, (
        f"recorded {held.unavailable_reason}; PROVIDER_CAPABILITY_UNVERIFIED would have needed "
        "no migration and is false about a provider that creates links perfectly well"
    )
    for name in (
        "probability_method",
        "financial_cost_method",
        "communication_cost_method",
        "risk_cost_method",
        "customer_cost_method",
    ):
        assert getattr(held, name) is not None, (
            f"{name} is unset on the retained link candidate; R24.C4 leaves none of the five "
            "unset, and a null method is a row the schema refuses"
        )
    assert held.financial_cost == fresh.financial_cost, (
        "the link's priced costs changed with its availability; the exclusion withdraws an "
        "action, it does not re-price one"
    )

    result = select(
        _inputs(figures),
        baseline=_BASELINE,
        amount=Minor(amount),
        thresholds=_THRESHOLDS,
    )
    evaluated = next(
        item for item in result.candidates if item.action is CandidateAction.PAYMENT_LINK
    )
    assert evaluated.excluded
    assert evaluated.exclusion_reason is ExclusionReason.LIVE_PAYMENT_LINK_EXISTS, (
        "the optimizer re-derived the exclusion instead of carrying the estimation layer's own "
        "reason through"
    )
    assert CandidateAction.PAYMENT_LINK not in result.qualifying_actions
    assert result.selected.action is not CandidateAction.PAYMENT_LINK, (
        "a second payable link for one debt was selected anyway"
    )
    assert result.selected.action is CandidateAction.PROMISE_TO_PAY_FOLLOW_UP, (
        f"selected {result.selected.action.value}; with the duplicate link withdrawn the due "
        "promise's follow-up holds the highest net value at every amount, which is what makes "
        "promise_status:MISSED reachable at all"
    )


def _approvable(**overrides: object) -> PolicyInput:
    """A ``PolicyInput`` for a follow-up on which all twelve checks pass.

    Exists so the two claims below are not vacuous. A property that only ever sees blocked
    inputs would assert "APPROVED implies the cooldown elapsed" over an empty set, and the
    generated strategy alone cannot promise otherwise — it draws unreadable consent and absent
    diagnoses on purpose. This is the passing case written down once, and every deviation from
    it below is a single ``replace``.
    """
    base = PolicyInput(
        case_id=uuid.uuid4(),
        merchant_id=uuid.uuid4(),
        decision_cycle=2,
        selected_action=CandidateAction.PROMISE_TO_PAY_FOLLOW_UP,
        case_state=CaseState.DECISION_PENDING,
        case_version=4,
        payment_amount=Minor(400_000),
        customer_key=f"ck-{uuid.uuid4()}",
        verified_payment_captured=False,
        verified_payment_status="failed",
        customer_opted_out=False,
        contact_suppressed=False,
        consent_expires_at=None,
        consent_recorded=True,
        risk_flagged=False,
        diagnosed_cause=RiskCause.INSUFFICIENT_FUNDS,
        human_owner_user_id=None,
        window_end_at=NOW + timedelta(hours=48),
        executed_action_count=1,
        customer_message_count=1,
        last_outbound_at=NOW - CONFIG.COOLDOWN_INTERVAL,
        open_intent_exists=False,
        intent_exists_for_key=False,
        evaluated_at=NOW,
        rules_version=_POLICY_RULES.version_label,
        config_version=CONFIG.version,
    )
    return dataclasses.replace(base, **overrides)  # type: ignore[arg-type]


@pytest.mark.model
@settings(max_examples=400)
@given(candidate=policy_input(action=CandidateAction.PROMISE_TO_PAY_FOLLOW_UP))
def test_property_45_an_approved_follow_up_is_spaced_and_inside_the_window(
    candidate: PolicyInput,
) -> None:
    """**Property 45.** No approved follow-up is closer than ``COOLDOWN_INTERVAL`` to the last
    outbound action, and none is approved outside the Recovery_Window (R24.C8, R24.C15).

    **Asserted over the authorization rather than over a pair of timestamps, and that is the
    stronger form.** P45 is a claim about two *confirmed* executions, and a confirmed execution
    exists only downstream of an ``APPROVED`` Policy_Decision — the transition into
    ``EXECUTING`` is the only edge that sets ``last_outbound_at``, and it is reachable only
    from ``ACTION_SCHEDULED``, which is reachable only from an approval. So "every approval is
    spaced" implies "every pair of confirmed actions is spaced", and it implies it for every
    input the engine can be given rather than for the interleavings a driver happened to
    produce. The converse does not hold: a test over observed pairs passes on a system that
    approves an unspaced action which then fails at the provider.

    The window half is the same argument. ``window_expired`` is check 8 and it compares against
    the persisted window end, which R2.C5 makes immutable — so an approved action is inside the
    window it was approved against, and no promise can have moved that boundary.

    R24.C8's deferral is asserted here too, because the requirement's substitution is what
    makes the cooldown bound *hold* rather than merely refuse: ``DEFERRED`` carries the instant
    the cooldown elapses, and that instant is inside the window — where it would not be, the
    verdict is ``BLOCKED`` with ``WINDOW_EXPIRED`` instead, which
    :func:`test_a_cooldown_reaching_past_the_window_blocks_instead_of_deferring` pins as an
    example because it needs every other check to pass to be reachable at all.
    """
    evaluation = evaluate(candidate, _POLICY_RULES)

    if evaluation.approved:
        assert candidate.evaluated_at < candidate.window_end_at, (
            "approved outside the Recovery_Window; check 8 compares against the persisted "
            "window end and R2.C5 makes it immutable"
        )
        if candidate.last_outbound_at is not None:
            gap = candidate.evaluated_at - candidate.last_outbound_at
            assert gap >= CONFIG.COOLDOWN_INTERVAL, (
                f"approved {gap} after the last outbound action, inside a "
                f"{CONFIG.COOLDOWN_INTERVAL} cooldown"
            )

    if evaluation.verdict is PolicyVerdict.DEFERRED:
        assert evaluation.primary_reason == PolicyCheck.COOLDOWN_ACTIVE.value, (
            "the cooldown is the only check permitted to defer; every other refusal is a "
            "block, and a deferral for another reason parks a case nothing will pick up"
        )
        assert candidate.last_outbound_at is not None
        expected = candidate.last_outbound_at + CONFIG.COOLDOWN_INTERVAL
        assert evaluation.earliest_permitted_at == expected, (
            "the deferred instant is not the last outbound action plus the cooldown; the "
            "scheduler reads this rather than re-deriving it, so a disagreement here is a "
            "message sent early"
        )
        assert expected < candidate.window_end_at, (
            "deferred to an instant at or after the window end, where R24.C8 substitutes "
            "BLOCKED/WINDOW_EXPIRED — a deferral there parks an action that can never run"
        )


@pytest.mark.model
def test_an_approvable_follow_up_is_actually_approved() -> None:
    """The non-vacuity check for Property 45, and it is not decoration.

    Every assertion above is guarded by ``if evaluation.approved``. If nothing the engine can
    be given is ever approved for this action — a plausible outcome, since it was in
    ``UNAVAILABLE_IN_MVP`` until R24 and check 12 refuses a non-executable action — the whole
    property passes over the empty set. This is the one input that must come out ``APPROVED``,
    and it fails loudly the day the action stops being authorizable.
    """
    evaluation = evaluate(_approvable(), _POLICY_RULES)
    assert evaluation.approved, (
        f"a follow-up with all twelve checks satisfiable was {evaluation.verdict.value} for "
        f"{evaluation.primary_reason}; Property 45 would be vacuous"
    )
    assert evaluation.selected_action is CandidateAction.PROMISE_TO_PAY_FOLLOW_UP


@pytest.mark.model
def test_a_cooldown_reaching_past_the_window_blocks_instead_of_deferring() -> None:
    """R24.C8's substitution, applied unchanged to the new action.

    Two inputs differing in one field. With room after the cooldown the verdict is ``DEFERRED``
    and carries the instant; with the window closing first it is ``BLOCKED`` with
    ``WINDOW_EXPIRED``, and **not** ``COOLDOWN_ACTIVE`` — the honest reason is the one that will
    still be true when the deferral would have elapsed. An example rather than a property
    because reaching the eleventh check requires the ten before it to pass, which a generator
    reaches rarely and this states exactly once.
    """
    inside_cooldown = _approvable(last_outbound_at=NOW - timedelta(hours=1))
    deferred = evaluate(inside_cooldown, _POLICY_RULES)
    assert deferred.verdict is PolicyVerdict.DEFERRED
    assert deferred.primary_reason == PolicyCheck.COOLDOWN_ACTIVE.value
    assert deferred.earliest_permitted_at == inside_cooldown.last_outbound_at + (
        CONFIG.COOLDOWN_INTERVAL
    )

    closing = dataclasses.replace(
        inside_cooldown,
        window_end_at=NOW + timedelta(hours=2),
    )
    assert closing.last_outbound_at is not None
    assert closing.last_outbound_at + CONFIG.COOLDOWN_INTERVAL >= closing.window_end_at
    blocked = evaluate(closing, _POLICY_RULES)
    assert blocked.verdict is PolicyVerdict.BLOCKED
    assert blocked.primary_reason == PolicyCheck.WINDOW_EXPIRED.value, (
        "deferred into a closed window; the case would sit waiting for a cooldown to elapse "
        "on an action the window had already made illegal"
    )
    assert blocked.earliest_permitted_at is None, (
        "a blocked decision carries an earliest permitted instant, which reads as though the "
        "action becomes permitted later"
    )


# ---------------------------------------------------------------------------
# Property 44 — exactly-once for the new action, across a process death
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True, slots=True)
class _FollowUpScenario:
    """One example's universe: a case that has already sent a link, and is now following up."""

    merchant_id: uuid.UUID
    case_id: uuid.UUID
    idempotency_key: str
    payment_link_id: str


class _Resolver:
    """A secret resolver over a fixed mapping. No environment, no file."""

    def __init__(self, values: dict[str, str]) -> None:
        self._values = values

    def get(self, name: str) -> str | None:
        return self._values.get(name)


@pytest.fixture
def installed_secrets() -> Iterator[None]:
    """A payload key, a customer-key secret and a token signing secret.

    Requested rather than autouse, because everything above this line is ``model`` tier and
    reads no secret at all — installing one for the whole module would reset the crypto cache
    four hundred times per property for no reason.

    All three are needed and none is optional. The resend branch never decrypts a contact, but
    the case row still carries an encrypted source event the seed has to write, and R18.C1's
    token is minted inside the same transaction for every customer-visible action — the
    follow-up included. A missing signing secret abandons the execution with
    ``TOKEN_ISSUE_FAILED``, and every "at most one call" assertion below would then pass having
    made zero calls.

    Fixed test keys, never the ones from ``.env``: this writes ciphertext into a database that
    outlives the run.
    """
    resolver = _Resolver(
        {
            "REVORA_PAYLOAD_ENCRYPTION_KEYS": "1:" + base64.b64encode(b"P" * 32).decode(),
            "REVORA_CUSTOMER_KEY_SECRET": base64.b64encode(b"3" * 32).decode(),
            "REVORA_CUSTOMER_TOKEN_SIGNING_SECRETS": "1:"
            + base64.b64encode(b"3" * 32).decode(),
        }
    )
    previous = set_secret_store(SecretStore(resolver))
    crypto.reset_cached_material()
    try:
        yield
    finally:
        set_secret_store(previous)
        crypto.reset_cached_material()


@pytest.fixture
def factory(owner_engine: Engine) -> sessionmaker[Session]:
    """Sessions on the migrated database. Session-scoped engine, so no per-example setup."""
    return sessionmaker(bind=owner_engine, expire_on_commit=False)


def _seed_follow_up(engine: Engine) -> _FollowUpScenario:
    """A case standing where a promise follow-up is actually executable.

    Every row is load-bearing, and the two that are not in
    ``test_exactly_once.py``'s equivalent are the point of this seed:

    * **a ``CONFIRMED`` ``PAYMENT_LINK_CREATE`` intent carrying a ``plink_`` id.** This is what
      ``_live_link_target`` reads, and it is the difference between R24.C10's branch and
      R24.C11's. Without it the follow-up would fall back to *creating* a link and the property
      would be testing the create path again under a new name.
    * **counters at one, and ``last_outbound_at`` a cooldown ago.** The follow-up is the second
      customer-visible action of this case's life, because there is no promise to follow up on
      until Revora has reached the customer once. So ``attempt_ordinal`` is 2, and the
      idempotency key is derived from that — seeding the counters at zero would mint a key for
      an attempt that could not exist.

    The first decision is marked consumed by the link's intent. That matters for the reason
    ``latest_approved_unconsumed`` exists: two approved decisions on one case, and the engine
    must act on the newer one.
    """
    merchant_id = uuid.uuid4()
    case_id = uuid.uuid4()
    link_decision_id = uuid.uuid4()
    link_intent_id = uuid.uuid4()
    follow_up_decision_id = uuid.uuid4()
    event_id = uuid.uuid4()
    moment = now()
    ordinal = 2
    key = execution_key(
        case_id, CandidateAction.PROMISE_TO_PAY_FOLLOW_UP.value, ordinal
    )
    payment_link_id = f"plink_{case_id.hex[:14]}"
    customer_key = f"ck-{case_id}"

    encrypted = payload_cipher().encrypt(
        json.dumps(
            {
                "event": "payment.failed",
                "payload": {
                    "payment": {
                        "entity": {
                            "id": f"pay_{case_id.hex[:14]}",
                            "entity": "payment",
                            "status": "failed",
                            "contact": "+919000090000",
                            "email": "p44-test@example.com",
                        }
                    }
                },
            }
        ).encode()
    )

    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO merchant (id, slug, display_name, default_currency, state,
                                      reporting_timezone, created_at)
                VALUES (:id, :slug, 'P44 merchant', 'INR', 'ACTIVE', 'UTC', now())
                """
            ),
            {"id": str(merchant_id), "slug": f"p44-{merchant_id}"},
        )
        connection.execute(
            text(
                """
                INSERT INTO webhook_event (
                    id, merchant_id, provider_event_id, event_name,
                    raw_payload_ciphertext, raw_payload_nonce, key_version,
                    canonical, correlation_id, signature_verified, received_at, created_at
                ) VALUES (
                    :id, :merchant_id, :provider_event_id, 'payment.failed',
                    :ciphertext, :nonce, :key_version,
                    '{}'::jsonb, :correlation_id, true, :received_at, now()
                )
                """
            ),
            {
                "id": str(event_id),
                "merchant_id": str(merchant_id),
                "provider_event_id": f"evt_{case_id.hex[:16]}",
                "ciphertext": encrypted.ciphertext,
                "nonce": encrypted.nonce,
                "key_version": encrypted.key_version,
                "correlation_id": str(uuid.uuid4()),
                "received_at": moment,
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO recovery_case (
                    id, merchant_id, state, provider_payment_id, payment_amount,
                    currency, customer_key, source_event_id, detected_at, window_end_at,
                    executed_action_count, customer_message_count, decision_cycle_count,
                    last_outbound_at, created_at
                ) VALUES (
                    :id, :merchant_id, :state, :payment_id, 400000,
                    'INR', :customer_key, :source_event_id, :detected_at, :window_end_at,
                    1, 1, 2, :last_outbound_at, now()
                )
                """
            ),
            {
                "id": str(case_id),
                "merchant_id": str(merchant_id),
                "state": CaseState.ACTION_SCHEDULED.value,
                "payment_id": f"pay_{case_id.hex[:14]}",
                "customer_key": customer_key,
                "source_event_id": str(event_id),
                "detected_at": moment - timedelta(hours=72),
                "window_end_at": moment + timedelta(hours=72),
                "last_outbound_at": moment - CONFIG.COOLDOWN_INTERVAL - timedelta(hours=1),
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO customer_consent (
                    id, merchant_id, customer_key, opted_out, source, effective_at,
                    created_at
                ) VALUES (
                    gen_random_uuid(), :merchant_id, :customer_key, false, 'test',
                    :effective_at, now()
                )
                """
            ),
            {
                "merchant_id": str(merchant_id),
                "customer_key": customer_key,
                "effective_at": moment - timedelta(days=3),
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO diagnosis (
                    id, merchant_id, case_id, cause, confidence, method, decision_cycle,
                    is_active, substituted_to_unknown, created_at
                ) VALUES (
                    gen_random_uuid(), :merchant_id, :case_id, :cause, 0.90,
                    :method, 2, true, false, now()
                )
                """
            ),
            {
                "merchant_id": str(merchant_id),
                "case_id": str(case_id),
                "cause": RiskCause.INSUFFICIENT_FUNDS.value,
                "method": DiagnosisMethod.DETERMINISTIC.value,
            },
        )
        for decision_id, action, cycle in (
            (link_decision_id, CandidateAction.PAYMENT_LINK, 1),
            (follow_up_decision_id, CandidateAction.PROMISE_TO_PAY_FOLLOW_UP, 2),
        ):
            connection.execute(
                text(
                    """
                    INSERT INTO policy_decision (
                        id, merchant_id, case_id, verdict, primary_reason, rule_set_version,
                        evaluated_at, expires_at, selected_action, case_state_at_evaluation,
                        decision_cycle, idempotency_key, created_at
                    ) VALUES (
                        :id, :merchant_id, :case_id, 'APPROVED', 'ALL_CHECKS_PASSED', 'v1',
                        :evaluated_at, :expires_at, :action, :state, :cycle, :key, now()
                    )
                    """
                ),
                {
                    "id": str(decision_id),
                    "merchant_id": str(merchant_id),
                    "case_id": str(case_id),
                    "evaluated_at": moment - (timedelta(hours=72) if cycle == 1 else timedelta()),
                    "expires_at": (
                        moment - timedelta(hours=71)
                        if cycle == 1
                        else moment + timedelta(minutes=15)
                    ),
                    "action": action.value,
                    "state": CaseState.ACTION_SCHEDULED.value,
                    "cycle": cycle,
                    "key": (
                        execution_key(case_id, CandidateAction.PAYMENT_LINK.value, 1)
                        if cycle == 1
                        else key
                    ),
                },
            )
        connection.execute(
            text(
                """
                INSERT INTO execution_intent (
                    id, merchant_id, case_id, policy_decision_id, idempotency_key, action,
                    attempt_ordinal, state, attempt_started_at, resolved_at,
                    provider_response_id, provider_short_url, counter_applied, effect_kind,
                    created_at
                ) VALUES (
                    :id, :merchant_id, :case_id, :decision_id, :key, :action,
                    1, :state, :started_at, :resolved_at,
                    :link_id, :short_url, true, :effect_kind, now()
                )
                """
            ),
            {
                "id": str(link_intent_id),
                "merchant_id": str(merchant_id),
                "case_id": str(case_id),
                "decision_id": str(link_decision_id),
                "key": execution_key(case_id, CandidateAction.PAYMENT_LINK.value, 1),
                "action": CandidateAction.PAYMENT_LINK.value,
                "state": IntentState.CONFIRMED.value,
                "started_at": moment - timedelta(hours=72),
                "resolved_at": moment - timedelta(hours=72),
                "link_id": payment_link_id,
                "short_url": f"https://fake.invalid/plink/{case_id.hex[:8]}",
                "effect_kind": ExecutionEffectKind.PAYMENT_LINK_CREATE.value,
            },
        )
        connection.execute(
            text(
                "UPDATE policy_decision SET consumed_by_intent_id = :intent_id WHERE id = :id"
            ),
            {"intent_id": str(link_intent_id), "id": str(link_decision_id)},
        )

    return _FollowUpScenario(merchant_id, case_id, key, payment_link_id)


def _run_resend_plan(
    engine: Engine,
    factory: sessionmaker[Session],
    scenario: _FollowUpScenario,
    plan: CrashPlan,
    fake: FakeRazorpay,
) -> None:
    """The crashing attempt, then the restarts, then the reconciliation runs.

    ``CrashInjected`` is caught here and nowhere else — that is what makes this a restart
    rather than a test failure. It derives from ``BaseException`` so nothing inside the engine
    catches it first.

    The reconciliation runs are expected to be inert, and running them anyway is the assertion.
    A resend intent is *absent* from the sweep's candidate set rather than skipped by it, so a
    plan that reconciles three times must still leave the notify count where it was.
    """
    crashing = CrashingProvider(fake, plan.point)

    with crash_on_statement(engine, plan.point), suppress(CrashInjected):
        execute_approved_action(
            scenario.merchant_id, scenario.case_id, provider=crashing, factory=factory
        )

    for _ in range(plan.restarts):
        with suppress(CrashInjected):  # pragma: no cover - the crash is one-shot
            execute_approved_action(
                scenario.merchant_id, scenario.case_id, provider=fake, factory=factory
            )

    for _ in range(plan.reconciliation_runs):
        reconcile_intents(scenario.merchant_id, provider=fake, factory=factory)


def _follow_up_intents(
    engine: Engine, scenario: _FollowUpScenario
) -> Sequence[dict[str, object]]:
    with engine.begin() as connection:
        rows = connection.execute(
            text(
                """
                SELECT state, effect_kind, provider_response_id, resolved_at, counter_applied
                FROM execution_intent
                WHERE merchant_id = :merchant_id AND idempotency_key = :key
                ORDER BY created_at
                """
            ),
            {"merchant_id": str(scenario.merchant_id), "key": scenario.idempotency_key},
        )
        return [dict(row._mapping) for row in rows]


def _counters(engine: Engine, scenario: _FollowUpScenario) -> dict[str, object]:
    with engine.begin() as connection:
        row = connection.execute(
            text(
                """
                SELECT state, executed_action_count, customer_message_count
                FROM recovery_case WHERE id = :case_id
                """
            ),
            {"case_id": str(scenario.case_id)},
        ).one()
    return dict(row._mapping)


_P44_SETTINGS = settings(
    max_examples=30,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
"""Thirty examples. Each one seeds a merchant, a case, two decisions and an intent, then drives
up to six transactions against a real database — and thirty covers all five crash points
against all six interesting resend outcomes several times over. The root conftest's autouse
clock fixture is function-scoped, which is what the suppression is for; nothing here depends on
per-example fixture setup, because every row is created inside the example body."""


@pytest.mark.pg
@_P44_SETTINGS
@given(plan=resend_crash_plan())
def test_property_44_one_key_one_resend_whatever_the_crash(
    owner_engine: Engine,
    factory: sessionmaker[Session],
    installed_secrets: None,
    plan: CrashPlan,
) -> None:
    """**Property 44.** P3 and P9 extended to ``PROMISE_TO_PAY_FOLLOW_UP`` (R24.C9, C12, C15).

    Five assertions, and the first is different in kind from its ``PAYMENT_LINK`` counterpart.

    1. **At most one ``notify_by`` call for one key, on any sequence of crashes, restarts and
       reconciliation passes.** For a create, "at most one" is *recoverable*: the object can be
       fetched back by ``reference_id``, so a crash after the call is resolved by reading. A
       resend cannot be read back by anything — the response carries a success boolean, no
       notification identifier, and no endpoint reports whether one was sent. So this bound is
       not maintained by reconciliation, it is maintained by never calling twice, and the
       reconciliation passes in the plan are here to prove they change nothing.
    2. **At most one intent row per key**, which is the database-level guarantee behind it. If
       this fails, assertion 1 passed by luck.
    3. **No payment link was created.** R24.C10: a case holding a live link gets that link
       re-notified and no second payable object is minted. Asserted against the fake's own call
       log rather than against the intent, because the intent recording
       ``PAYMENT_LINK_RESEND`` would be equally consistent with a create that also happened.
    4. **A ``CONFIRMED`` intent carries the composed identifier.** Not a provider id — there is
       none — so it must be the ``"<plink>#notify_by:<medium>"`` token, which is deliberately
       not a valid Razorpay id shape so nothing can later feed it to a fetch endpoint.
    5. **Each counter rose by at most one.** The case starts at one executed action and one
       customer message, because a promise exists only after Revora reached the customer once.
       So the bound asserted is two, and a crash that double-counted would tighten
       ``MAX_CUSTOMER_MESSAGES`` silently — which is a customer who stops hearing from us a
       message early, and no error anywhere.

    ``UNCERTAIN`` is the interesting outcome rather than a nuisance one: it is terminal for the
    case and the intent stays unresolved forever, so it is the state under which "no second
    call, ever" is hardest to hold — a later engine pass finds an unresolved intent, and the
    thing that stops it calling is the refusal rather than a read.
    """
    scenario = _seed_follow_up(owner_engine)
    fake = FakeRazorpay(plan.behaviour)

    _run_resend_plan(owner_engine, factory, scenario, plan, fake)

    notifies = fake.notify_call_count_for(scenario.payment_link_id, NotifyMedium.SMS)
    rows = _follow_up_intents(owner_engine, scenario)
    counters = _counters(owner_engine, scenario)

    assert notifies <= 1, (
        f"{notifies} resends for one idempotency key under {plan.point.value}; a customer "
        "would be messaged twice about one promise, and nothing can read back whether the "
        "first one arrived"
    )
    assert len(rows) <= 1, (
        f"{len(rows)} intent rows for one key — the unique constraint did not hold"
    )
    assert not fake.calls_for(OPERATION_CREATE_PAYMENT_LINK), (
        "a payment link was created while following up on a promise; R24.C10 re-notifies the "
        "link the case already holds and mints no second payable object"
    )

    if rows:
        row = rows[0]
        assert row["effect_kind"] == ExecutionEffectKind.PAYMENT_LINK_RESEND.value
        if IntentState(str(row["state"])) is IntentState.CONFIRMED:
            assert row["provider_response_id"] == resend_response_id(
                scenario.payment_link_id, NotifyMedium.SMS
            ), (
                "a confirmed resend carries something other than the composed token; there is "
                "no provider identifier to carry, and a real-looking id here is one a later "
                "reader could feed to a fetch endpoint"
            )
            assert row["resolved_at"] is not None
            assert row["counter_applied"] is True

    assert int(counters["executed_action_count"]) <= 2, (
        f"executed_action_count reached {counters['executed_action_count']} for one further "
        "attempt on a case that had already executed one"
    )
    assert int(counters["customer_message_count"]) <= 2, (
        f"customer_message_count reached {counters['customer_message_count']}; this counter is "
        f"what bounds contact at MAX_CUSTOMER_MESSAGES = {CONFIG.MAX_CUSTOMER_MESSAGES}"
    )


@pytest.mark.pg
def test_the_follow_up_harness_reaches_the_provider_without_a_crash(
    owner_engine: Engine, factory: sessionmaker[Session], installed_secrets: None
) -> None:
    """The non-vacuity check for Property 44, and the reason it is not optional.

    Every assertion above is an upper bound, and an upper bound is satisfied by zero. The seed
    has to satisfy all twelve policy checks re-evaluated against reloaded state, and there is
    deliberately no assume-fine branch — a missing consent row, an absent diagnosis or an
    unresolvable signing secret each abandon the execution before the provider is reached, and
    every bound above then passes having done nothing at all.

    So this drives the uncrashed path and asserts the opposite direction: exactly one resend,
    the intent ``CONFIRMED``, both counters moved by exactly one, and the case waiting for an
    outcome (R24.C12).
    """
    scenario = _seed_follow_up(owner_engine)
    fake = FakeRazorpay(ProviderBehaviour(resend_outcomes=(ResendOutcome.SUCCESS,)))

    attempt = execute_approved_action(
        scenario.merchant_id, scenario.case_id, provider=fake, factory=factory
    )

    assert attempt.outcome is ExecutionOutcome.CONFIRMED, (
        f"the seed did not reach a confirmed resend: {attempt.outcome.value} / {attempt.detail}"
    )
    assert fake.notify_call_count_for(scenario.payment_link_id, NotifyMedium.SMS) == 1
    assert not fake.calls_for(OPERATION_CREATE_PAYMENT_LINK)

    counters = _counters(owner_engine, scenario)
    assert counters["state"] == CaseState.WAITING_FOR_OUTCOME.value, (
        "R24.C12 transitions the case to WAITING_FOR_OUTCOME on a confirmed follow-up"
    )
    assert int(counters["executed_action_count"]) == 2
    assert int(counters["customer_message_count"]) == 2
