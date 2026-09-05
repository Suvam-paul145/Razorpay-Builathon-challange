"""Customer signals in Recovery_Memory, and the rules that keep them out of the baseline.

Task 48's pure half. Four claims are checked here and each one fails silently in production if
it is wrong, which is the whole reason they are tested without a database:

**A signal cannot become a segment dimension.** R25.C3 requires the Delay_Reason and the
Promise_Status to be selectable as distinct segments, so they are flat string keys in the same
JSONB document the estimator probes by containment. That is one edit away from a disaster: adding
either name to :data:`~revora.domain.segments.FEATURE_KEYS` would extend every backoff level at
once, every historical observation would stop matching its own segment, and every baseline in
the system would silently collapse toward the global prior. Nothing raises. So the separation is
asserted from both directions — the keys are not in ``FEATURE_KEYS``, and no probe
``backoff_candidates`` produces names them.

**A responded-to case is not a no-intervention observation.** R25.C4. The classification is a
pure function of three inputs and every combination of them is cheap to check, including the one
that motivated the clause: zero *confirmed* actions, a control-arm assignment, and a submission
that arrived after an attempt nobody ever resolved.

**Only one intervention status may train the baseline.** Checked exhaustively over the
enumeration rather than for the three members that exist today, so a fourth added later is
excluded by default. A deny-list would have admitted it by default, and the resulting baseline
would look better-evidenced while being contaminated.

**Every unobserved Delay_Reason is named.** R25.C8's point is the zero counts, and the tuple is
derived from the enumeration so a reason added next month appears in it without an edit here. A
hardcoded vocabulary would omit it, and the omission would look exactly like coverage.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import cast

import pytest

from revora.customer.arrangements import FEATURE_PARTIAL_ARRANGEMENT, ArrangementRequest
from revora.domain.actions import CandidateAction
from revora.domain.enums import (
    DelayReason,
    DiagnosisEvidenceSource,
    ExperimentGroup,
    IntentState,
    InterventionStatus,
    PromiseStatus,
    Provenance,
    RiskCause,
)
from revora.domain.money import Minor
from revora.domain.segments import FEATURE_KEYS, SegmentFeatures, backoff_candidates
from revora.estimation.baseline import (
    TRAINING_LABEL_STATUS,
    usable_as_training_label,
)
from revora.memory.store import (
    FEATURE_CUSTOMER_SIGNALS,
    FEATURE_DELAY_REASON,
    FEATURE_PROMISE_STATUS,
    SIGNAL_SEGMENT_KEYS,
    CustomerSignalFacts,
    _first_action_instant,
    _observation_provenance,
    _signal_features,
    classify_intervention_status,
)
from revora.memory.versions import NOT_RECORDED, _unobserved
from revora.metrics.engine import SignalCohortCounts
from revora.optimizer.service import CauseProvenance

_MOMENT = datetime.fromisoformat("2026-04-02T09:30:00+00:00")
_SIGNAL_ID = uuid.UUID("11111111-2222-3333-4444-555555555555")


# ---------------------------------------------------------------------------
# Fakes, kept minimal on purpose
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Intent:
    """The two ``execution_intent`` columns :func:`_first_action_instant` reads.

    A stand-in rather than the ORM class, so the rule is testable with no database and so this
    file states the complete set of columns the "was there a Revora action" question depends on.
    Two, and if a third is ever needed the failure shows up here as a missing attribute.
    """

    state: str
    attempt_started_at: datetime


@dataclass(frozen=True, slots=True)
class _Case:
    """The one ``recovery_case`` column :func:`_observation_provenance` reads."""

    provenance: str | None


def _facts(**overrides: object) -> CustomerSignalFacts:
    """A :class:`CustomerSignalFacts` with everything absent, plus the overrides.

    Written as a helper because the class has ten fields and nine of them are ``None`` in most
    of these tests; spelling all ten out per test would bury the one field each test is about.
    """
    base: dict[str, object] = {
        "signal_count": 0,
        "delay_reason": None,
        "promise_status": None,
        "promise_seconds_to_payment": None,
        "arrangement": None,
        "contact_suppressed": False,
        "any_synthetic": False,
        "signal_after_action": False,
        "first_action_at": None,
        "latest_signal_at": None,
    }
    base.update(overrides)
    return CustomerSignalFacts(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# R25.C3 — selectable, and not a segment dimension
# ---------------------------------------------------------------------------


@pytest.mark.pure
def test_the_signal_keys_are_selectable_and_are_not_segment_dimensions() -> None:
    """Both halves of R25.C3, and the second is the one that protects every stored baseline.

    Selectable means flat and string-valued: ``features @> '{"delay_reason": "..."}'`` matches a
    string and matches nothing nested, so a value a merchant wants to segment on has to be
    stored the way the five segment features are stored.

    Not a segment dimension means absent from ``FEATURE_KEYS``. ``LEVEL_FEATURES`` truncates that
    one ordered tuple, so a sixth entry joins every backoff level at once and every observation
    written before the edit stops matching its own segment. Nothing raises when that happens —
    containment simply returns fewer rows, every level falls through, and every estimate becomes
    the global prior while the dashboard reports it as an estimate. This assertion is the only
    thing standing between that and a silent regression.
    """
    for key in SIGNAL_SEGMENT_KEYS:
        assert key not in FEATURE_KEYS, (
            f"{key!r} became a segment feature. LEVEL_FEATURES truncates FEATURE_KEYS, so every "
            "backoff level would gain it at once and every historical observation would stop "
            "matching its own segment"
        )

    features = SegmentFeatures.derive(
        risk_cause=RiskCause.INSUFFICIENT_FUNDS,
        amount=Minor(250_000),
        payment_method="card",
        executed_action_count=0,
        error_source="issuer",
    )
    for _level, _segment_id, subset in backoff_candidates(features):
        assert not set(subset).intersection(SIGNAL_SEGMENT_KEYS), (
            "a backoff probe now names a Customer_Signal key, so what a customer said has "
            "become part of how the baseline segments its own history"
        )

    written = _signal_features(
        _facts(
            signal_count=2,
            delay_reason=DelayReason.SALARY_OR_CASHFLOW_TIMING,
            promise_status=PromiseStatus.KEPT,
            latest_signal_at=_MOMENT,
        )
    )
    assert written[FEATURE_DELAY_REASON] == DelayReason.SALARY_OR_CASHFLOW_TIMING.value
    assert written[FEATURE_PROMISE_STATUS] == PromiseStatus.KEPT.value
    assert all(isinstance(written[key], str) for key in SIGNAL_SEGMENT_KEYS), (
        "a signal segment key holds a non-string value, which a containment probe built from "
        "string values cannot match — so the value is stored and unselectable, which is the one "
        "outcome R25.C3 forbids"
    )


@pytest.mark.pure
def test_the_nested_document_holds_everything_that_is_not_selectable() -> None:
    """R25.C1's remaining fields, nested so no probe can reach them.

    The counts, the interval and the two indications are facts about what happened rather than
    segments anybody selects on, and putting them in the flat namespace would give the estimator
    four more keys it must be trusted never to name. Nesting makes that structural: a containment
    probe built from string values cannot match an object at all.
    """
    facts = _facts(
        signal_count=3,
        delay_reason=DelayReason.AMOUNT_TOO_HIGH_RIGHT_NOW,
        promise_status=PromiseStatus.KEPT,
        promise_seconds_to_payment=86_400,
        arrangement=ArrangementRequest(
            signal_id=_SIGNAL_ID,
            requested_at=_MOMENT,
            note=None,
            note_truncated=False,
            note_redacted_at=None,
        ),
        contact_suppressed=True,
        signal_after_action=True,
        first_action_at=_MOMENT - timedelta(hours=2),
        latest_signal_at=_MOMENT,
    )
    written = _signal_features(facts)
    assert set(written) == {
        FEATURE_DELAY_REASON,
        FEATURE_PROMISE_STATUS,
        FEATURE_CUSTOMER_SIGNALS,
        FEATURE_PARTIAL_ARRANGEMENT,
    }

    body = written[FEATURE_CUSTOMER_SIGNALS]
    assert isinstance(body, dict), (
        "the Customer_Signal document became a flat value, so a probe naming this key would "
        "match it and it would be a segment dimension in all but name"
    )
    assert body["signal_count"] == 3
    assert body["seconds_promise_to_payment"] == 86_400
    assert body["arrangement_requested"] is True
    assert body["contact_suppressed"] is True
    assert body["signal_after_revora_action"] is True
    assert body["provenance"] == Provenance.REAL.value


@pytest.mark.pure
def test_a_case_that_said_nothing_carries_no_signal_keys() -> None:
    """The absent form, which is what keeps every pre-existing observation unchanged.

    A present-but-null key is a key a containment probe can match on, so "the customer said
    nothing" has to be the absence of a key rather than a null value under one. This is also what
    keeps the pg assertion of the exact five-key feature set on an untouched case honest instead
    of widened.
    """
    assert _signal_features(_facts()) == {}


@pytest.mark.pure
def test_a_suppression_alone_is_content_even_with_no_signal_on_this_case() -> None:
    """R25.C1 asks whether a suppression *covers* the case, which R21.C8 makes scope-wide.

    A case can inherit a suppression from a sibling case of the same customer and order without
    holding a single Customer_Signal of its own. An observation that recorded nothing for it
    would describe a case Revora was free to chase when it was not.
    """
    facts = _facts(contact_suppressed=True)
    assert facts.has_content is True
    written = _signal_features(facts)
    assert set(written) == {FEATURE_CUSTOMER_SIGNALS}
    body = written[FEATURE_CUSTOMER_SIGNALS]
    assert isinstance(body, dict)
    assert body["contact_suppressed"] is True
    assert body["signal_count"] == 0


# ---------------------------------------------------------------------------
# R25.C4 — the training label
# ---------------------------------------------------------------------------


@pytest.mark.pure
def test_a_post_action_signal_disqualifies_a_control_case_from_the_training_set() -> None:
    """R25.C4, and the case it exists for is the third assertion.

    Zero confirmed actions plus a control-arm assignment used to be sufficient for
    ``NO_INTERVENTION_CONFIRMED``. It is not, because an intent stranded in ``ATTEMPTED`` or
    ``UNCERTAIN`` has no confirmation and may perfectly well have reached a person — and if that
    person then submitted a Delay_Reason, their own submission is the evidence it did. A case
    like that entering the baseline's training set biases the baseline downward, which flatters
    every incremental claim built on it.

    The first two assertions restate the pre-existing rule unchanged, because R25.C4 must not
    have moved it: a control case with no action and no post-action signal is still the one
    usable label, and a confirmed action still disqualifies.
    """
    assert (
        classify_intervention_status(confirmed_actions=0, group=ExperimentGroup.CONTROL)
        is InterventionStatus.NO_INTERVENTION_CONFIRMED
    )
    assert (
        classify_intervention_status(confirmed_actions=1, group=ExperimentGroup.CONTROL)
        is InterventionStatus.REVORA_INTERVENED
    )
    assert (
        classify_intervention_status(
            confirmed_actions=0,
            group=ExperimentGroup.CONTROL,
            signal_after_action=True,
        )
        is InterventionStatus.REVORA_INTERVENED
    ), (
        "a customer who responded to something Revora sent was accepted as a no-intervention "
        "training label; the baseline would learn the recovery rate of answered messages and "
        "report it as the rate with no intervention at all"
    )
    # A signal that arrived *before* any action, or on a case with no action at all, changes
    # nothing. The clause is about responses, not about submissions.
    assert (
        classify_intervention_status(
            confirmed_actions=0,
            group=ExperimentGroup.CONTROL,
            signal_after_action=False,
        )
        is InterventionStatus.NO_INTERVENTION_CONFIRMED
    )


@pytest.mark.pure
def test_only_one_intervention_status_may_train_the_baseline() -> None:
    """R5.C6 and R25.C4's shared consequence, checked over the whole enumeration.

    Exhaustive rather than three-membered on purpose. The dangerous edit is a *fifth* status
    describing some newly observable kind of intervention: under a deny-list it would join the
    training set the day it was declared, and the contamination would be invisible because the
    resulting baseline would simply look better-evidenced.
    """
    admitted = [
        status for status in InterventionStatus if usable_as_training_label(status)
    ]
    assert admitted == [TRAINING_LABEL_STATUS], (
        f"more than one intervention status is usable as a training label: {admitted}"
    )
    assert TRAINING_LABEL_STATUS is InterventionStatus.NO_INTERVENTION_CONFIRMED
    # The string form, because the value arrives from a TEXT column.
    assert usable_as_training_label(TRAINING_LABEL_STATUS.value) is True
    assert usable_as_training_label(InterventionStatus.REVORA_INTERVENED.value) is False
    # A label a later build wrote and this one has never heard of must not train anything, and
    # must not take an estimation run down either.
    assert usable_as_training_label("SOME_FUTURE_STATUS") is False


@pytest.mark.pure
def test_a_failed_intent_is_not_a_revora_action() -> None:
    """Which intent states count as "a Revora action" for R25.C4, and why ``FAILED`` does not.

    ``FAILED`` is the one state that means definitely nothing landed — a connect-phase failure or
    a parseable provider rejection — so a signal arriving after one cannot have been a response
    to it, and treating it as one would discard a genuinely untreated observation from a training
    set that is already labelled as too small.

    The other three all admit that a customer may have received something, and the earliest of
    them is the instant used, because R25.C4 asks about *a* Revora action rather than the last
    one.
    """
    base = datetime.fromisoformat("2026-04-02T08:00:00+00:00")
    assert (
        _first_action_instant(
            [
                cast(
                    "object",
                    _Intent(state=IntentState.FAILED.value, attempt_started_at=base),
                )
            ]  # type: ignore[arg-type]
        )
        is None
    ), "a failed intent was treated as a Revora action, so an untreated case lost its label"

    intents = [
        _Intent(state=IntentState.FAILED.value, attempt_started_at=base),
        _Intent(
            state=IntentState.UNCERTAIN.value,
            attempt_started_at=base + timedelta(hours=1),
        ),
        _Intent(
            state=IntentState.CONFIRMED.value,
            attempt_started_at=base + timedelta(hours=2),
        ),
    ]
    assert _first_action_instant(intents) == base + timedelta(hours=1), (  # type: ignore[arg-type]
        "the earliest non-failed attempt is not the instant used, so a signal submitted between "
        "the first attempt and a later one would not count as a response to it"
    )
    for state in (IntentState.ATTEMPTED, IntentState.UNCERTAIN, IntentState.CONFIRMED):
        assert (
            _first_action_instant(
                [_Intent(state=state.value, attempt_started_at=base)]  # type: ignore[arg-type]
            )
            == base
        ), f"{state.value} stopped counting as a Revora action"


# ---------------------------------------------------------------------------
# R25.C2 — provenance
# ---------------------------------------------------------------------------


@pytest.mark.pure
def test_a_synthetic_signal_widens_the_observation_provenance() -> None:
    """R25.C2 applying R15.C2 unchanged: one synthetic contributor is enough.

    And never the reverse. There is no input to this function that narrows a synthetic case back
    to ``REAL``, which is what makes the claim a property of the function rather than of the data
    it happens to have been given.
    """
    real_case = cast("object", _Case(provenance=Provenance.REAL.value))
    synthetic_case = cast("object", _Case(provenance=Provenance.SYNTHETIC.value))

    assert (
        _observation_provenance(real_case, _facts())  # type: ignore[arg-type]
        == Provenance.REAL.value
    )
    assert (
        _observation_provenance(  # type: ignore[arg-type]
            real_case, _facts(signal_count=1, any_synthetic=True)
        )
        == Provenance.SYNTHETIC.value
    ), (
        "a generated Customer_Signal contributed to an observation still labelled REAL, and that "
        "label is the whole basis on which a figure may claim to describe real money"
    )
    assert (
        _observation_provenance(  # type: ignore[arg-type]
            synthetic_case, _facts(signal_count=1, any_synthetic=False)
        )
        == Provenance.SYNTHETIC.value
    ), "a real signal narrowed a synthetic case back to REAL"
    assert (
        _observation_provenance(  # type: ignore[arg-type]
            cast("object", _Case(provenance=None)), _facts()
        )
        == Provenance.REAL.value
    )


# ---------------------------------------------------------------------------
# R25.C8 — the zero counts
# ---------------------------------------------------------------------------


@pytest.mark.pure
def test_every_unobserved_delay_reason_is_named() -> None:
    """R25.C8's actual requirement, which is about the values that are *absent*.

    Derived from the enumeration rather than from a written-out list, so a Delay_Reason added
    next month appears in the tuple on its first report with no edit to the reporting module. A
    hardcoded vocabulary would omit it, and the omission would look exactly like coverage — an
    approver would be told nothing is missing about a population nothing has been observed for.
    """
    observed = {
        DelayReason.SALARY_OR_CASHFLOW_TIMING.value: 12,
        DelayReason.OTHER.value: 3,
    }
    missing = _unobserved(DelayReason, observed)
    assert missing == tuple(
        sorted(
            member.value
            for member in DelayReason
            if member.value not in observed
        )
    )
    assert DelayReason.DISPUTES_THE_CHARGE.value in missing
    assert DelayReason.SALARY_OR_CASHFLOW_TIMING.value not in missing

    # A value present with a zero count is unobserved too. A grouping cannot normally produce
    # one, but a caller filtering a mapping could, and "the key exists" is not evidence.
    assert DelayReason.OTHER.value in _unobserved(
        DelayReason, {DelayReason.OTHER.value: 0}
    )

    # Nothing observed at all names every member, which is a fresh deployment's honest report.
    assert _unobserved(DelayReason, {}) == tuple(
        sorted(member.value for member in DelayReason)
    )
    assert _unobserved(CandidateAction, {}) == tuple(
        sorted(member.value for member in CandidateAction)
    )


@pytest.mark.pure
def test_a_null_column_value_groups_under_a_named_bucket() -> None:
    """Why ``NOT_RECORDED`` exists rather than the row being dropped.

    Every mapping in the composition report has to sum to the observation count, because a reader
    checking that sum is doing exactly the right thing. Dropping nulls gives five mappings whose
    sums disagree with the total by five different amounts, and reconciling that is the work the
    report exists to save.
    """
    assert NOT_RECORDED == "NOT_RECORDED"
    assert NOT_RECORDED not in {member.value for member in CandidateAction}, (
        "the null bucket collides with a real action value, so a case that chose nothing and a "
        "case that chose that action are now counted together"
    )
    assert NOT_RECORDED not in {member.value for member in DelayReason}


# ---------------------------------------------------------------------------
# R25.C11 and R25.C5 — the two reports
# ---------------------------------------------------------------------------


@pytest.mark.pure
def test_the_cohort_counts_are_counts_and_carry_no_rates() -> None:
    """R25.C11 reports counts. A signal rate would have the wrong denominator.

    Dividing a customer's choice to speak by a cohort size gives a figure that moves when
    detection volume moves, which answers no question anybody asks. The counts sit beside
    ``case_count`` in the report and any ratio is the reader's to form deliberately.

    The default construction is all-zero rather than sentinel-valued, and that is correct here
    and not elsewhere: "no case in this cohort held a Delay_Reason" is a measurement, unlike an
    unestablished incremental revenue figure.
    """
    empty = SignalCohortCounts()
    document = empty.as_document()
    assert document == {
        "by_delay_reason": {},
        "by_promise_status": {},
        "arrangement_request_count": 0,
        "contact_suppression_count": 0,
        "signalling_case_count": 0,
    }
    assert not any("rate" in key for key in document), (
        "a rate entered the Customer_Signal cohort counts; its denominator is a detection "
        "volume, so the figure would move for reasons that have nothing to do with customers"
    )

    populated = SignalCohortCounts(
        by_delay_reason={DelayReason.BANK_OR_CARD_PROBLEM.value: 4},
        by_promise_status={PromiseStatus.MISSED.value: 2},
        arrangement_request_count=1,
        contact_suppression_count=1,
        signalling_case_count=6,
    )
    assert populated.as_document()["by_delay_reason"] == {
        DelayReason.BANK_OR_CARD_PROBLEM.value: 4
    }
    assert sum(populated.by_delay_reason.values()) <= populated.signalling_case_count, (
        "the per-reason counts exceed the number of cases that submitted anything, which means "
        "one case is being counted under two reasons and the column no longer adds up"
    )


@pytest.mark.pure
def test_the_recommendation_names_the_signal_that_produced_its_cause() -> None:
    """R25.C5's second half, in the form a reviewer queries it.

    ``cause_signal_id`` sits at the top level of the audit record's ``decision`` document so the
    join from a recommendation to the submission that shaped its candidate set is
    ``decision->>'cause_signal_id'`` rather than a path expression.

    The three-state ``cause_refined_by_customer`` is carried through unflattened: ``None`` means
    no reason was submitted, ``False`` means one was and named no cause (R20.C6), ``True`` means
    it refined the cause. Collapsing the first two would lose the fact a merchant asking why the
    second contact repeated the first actually needs.
    """
    stated = CauseProvenance(
        risk_cause=RiskCause.INSUFFICIENT_FUNDS,
        evidence_source=DiagnosisEvidenceSource.CUSTOMER_STATED_REASON.value,
        signal_id=str(_SIGNAL_ID),
        delay_reason=DelayReason.SALARY_OR_CASHFLOW_TIMING.value,
        cause_refined=True,
    )
    document = stated.as_document()
    assert document["cause_signal_id"] == str(_SIGNAL_ID)
    assert document["candidate_set_risk_cause"] == RiskCause.INSUFFICIENT_FUNDS.value
    assert document["customer_stated_cause"] is True
    assert document["cause_refined_by_customer"] is True

    provider = CauseProvenance(
        risk_cause=RiskCause.BANK_OR_NETWORK_FAILURE,
        evidence_source=DiagnosisEvidenceSource.PROVIDER_ERROR_CODE.value,
        signal_id=None,
        delay_reason=None,
        cause_refined=None,
    )
    provider_document = provider.as_document()
    assert provider_document["cause_signal_id"] is None
    assert provider_document["customer_stated_cause"] is False
    assert provider_document["cause_refined_by_customer"] is None, (
        "'no reason was submitted' and 'a reason was submitted and refined nothing' have been "
        "collapsed into one value, and only the second explains a repeated contact"
    )

    absent = CauseProvenance(None, None, None, None, None)
    assert absent.as_document()["candidate_set_risk_cause"] is None


@pytest.mark.pure
def test_a_signal_cannot_move_a_case_out_of_the_control_arm() -> None:
    """R25.C10, and the reason it needs no code is worth asserting rather than assuming.

    The control-arm decision is made in one place, at the execution boundary, and it reads the
    ``experiment_assignment`` row and nothing else. So "a Customer_Signal leaves a Control_Group
    case in Control_Group" is a fact about what that function can see, not a rule somebody
    applied — and the recommendation stands and is recorded because the suppression happens
    *after* the optimizer and the policy engine have both already run, which is what makes a
    control case a counterfactual rather than a gap.

    Checked by reading the function's source for the vocabulary it must not contain. A source
    assertion is a weak instrument in general; here it is the right one, because the claim is
    precisely "this function consults no signal", and any behavioural test would have to
    construct the absence of an influence rather than its presence.
    """
    import ast
    import inspect
    import textwrap

    from revora.execution import engine as execution_engine

    # ``dedent`` rather than ``cleandoc``: the latter strips indentation from every line after
    # the first, which is right for a docstring and turns a function body into a syntax error.
    source = textwrap.dedent(inspect.getsource(execution_engine._is_control_arm))
    tree = ast.parse(source)
    names = {
        node.id if isinstance(node, ast.Name) else node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Name | ast.Attribute)
    }
    forbidden = {
        "CustomerSignalRepository",
        "PromiseToPayRepository",
        "ContactSuppressionRepository",
        "latest_delay_reason",
        "delay_reason",
        "promise_status",
        "signal_count",
    }
    intruders = sorted(names.intersection(forbidden))
    assert intruders == [], (
        f"the control-arm decision now reads a Customer_Signal: {intruders}. A signal that can "
        "move a case out of Control_Group contaminates the arm, and a contaminated control case "
        "is excluded from the comparison — so the cost is a silently smaller experiment"
    )


@pytest.mark.pure
def test_the_experiment_freeze_is_keyed_on_the_experiment_and_not_on_a_signal() -> None:
    """R25.C9: an ``ACTIVE`` experiment applies the versions frozen at definition time.

    Signals recorded during the experiment cannot change which model versions a case is
    estimated with, because the freeze is read by experiment id from
    ``experiment_version_freeze`` and the four frozen components are a fixed list. A signal
    that could move a frozen version would make two cases in one arm estimated by two different
    models, and the resulting lift would be a measurement of the deployment schedule.
    """
    from revora.memory.versions import (
        COMPONENT_BASELINE_MODEL,
        COMPONENT_BASELINE_WORKFLOW,
        COMPONENT_CANDIDATE_PRIORS,
        COMPONENT_POLICY_RULE_SET,
        FROZEN_COMPONENTS,
    )

    assert set(FROZEN_COMPONENTS) == {
        COMPONENT_BASELINE_WORKFLOW,
        COMPONENT_POLICY_RULE_SET,
        COMPONENT_BASELINE_MODEL,
        COMPONENT_CANDIDATE_PRIORS,
    }
    assert not any(
        token in component
        for component in FROZEN_COMPONENTS
        for token in ("signal", "delay_reason", "promise")
    ), (
        "a Customer_Signal-derived component entered the experiment freeze, so what customers "
        "said during an experiment would decide which model versions that experiment applied"
    )


@pytest.mark.pure
def test_the_estimate_records_which_status_its_labels_came_from() -> None:
    """The recorded estimate names its label population (R5.C6, R25.C4).

    Recorded rather than left as a fact about the code, because a calibration report recomputed
    later has to know what population the counts described — and ``observations`` alone does not
    say whether an intervened case was among them.
    """
    from revora.domain.enums import EstimationMethod, ValidationStatus
    from revora.domain.probability import Probability
    from revora.domain.segments import SegmentLevel
    from revora.estimation.baseline import BaselineFigures, SelectedSegment
    from revora.estimation.beta import UNIFORM_PRIOR
    from revora.persistence.repositories.estimates import SegmentCounts

    features = SegmentFeatures.derive(
        risk_cause=RiskCause.INSUFFICIENT_FUNDS,
        amount=Minor(250_000),
        payment_method="card",
        executed_action_count=0,
        error_source="issuer",
    )
    counts = SegmentCounts(
        observations=40,
        recoveries=10,
        synthetic_contributions=0,
        unknown_intervention=5,
        resolved_control=40,
    )
    figures = BaselineFigures(
        probability=Probability(Decimal("0.268")),
        interval=None,
        posterior=UNIFORM_PRIOR.posterior(successes=10, trials=40),
        features=features,
        segment=SelectedSegment(
            level=SegmentLevel.FULL,
            segment_id="L1:x",
            counts=counts,
            sample_size_satisfied=True,
            levels_examined=1,
        ),
        method=EstimationMethod.PRIOR_FALLBACK,
        provenance=Provenance.REAL,
        validation_status=ValidationStatus.CALIBRATION_UNVERIFIED,
        training_snapshot_id="snap",
    )
    document = figures.feature_document()
    assert document["training_label_status"] == TRAINING_LABEL_STATUS.value
    assert not set(document).intersection(SIGNAL_SEGMENT_KEYS), (
        "a Customer_Signal key entered a baseline estimate's feature document, so what a "
        "customer said is now part of how an estimate identifies its own segment"
    )
