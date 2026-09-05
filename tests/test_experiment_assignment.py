"""Assignment must be deterministic, and the attribution gate must refuse by default.

Two pure surfaces, both tested exhaustively because both are the kind of code where a subtle
error produces plausible output.

**Assignment.** The failure mode is not "wrong arm" — any arm is a valid arm. It is
*instability*: the same case getting different arms on two calls. That destroys the comparison
and is invisible in the result, because nothing downstream records which arm a case was assigned
to twice. So the tests hammer determinism, and they check the ratio is honoured across a large
sample rather than trusting the reduction.

**The attribution gate.** Every test here is an attempt to obtain an attributed claim that is not
earned. Each must be refused. The gate is a conjunction of four conditions and the tests remove
them one at a time, because a conjunction implemented as a disjunction by accident would pass any
test that only ever satisfies everything at once.
"""

from __future__ import annotations

import uuid
from collections import Counter
from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from revora.domain.attribution import RefusalCode, attribution_refusals
from revora.domain.enums import ExperimentGroup, ExperimentLabel, ExperimentState
from revora.experiment.assignment import (
    AllocationRatio,
    assign_group,
    parse_allocation_ratio,
)

pytestmark = pytest.mark.pure


# ---------------------------------------------------------------------------
# The allocation ratio
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "control", "treatment"),
    [
        ("1:1", 1, 1),
        ("1:3", 1, 3),
        (" 2 : 5 ", 2, 5),
        ("0:1", 0, 1),
        ("1:0", 1, 0),
        ("10:90", 10, 90),
    ],
)
def test_valid_ratios_parse(raw: str, control: int, treatment: int) -> None:
    """``0:1`` and ``1:0`` are legitimate: a ramp-up starts near one and a pause sits at the
    other."""
    ratio = parse_allocation_ratio(raw)
    assert (ratio.control, ratio.treatment) == (control, treatment)


@pytest.mark.parametrize(
    "raw",
    ["1", "1:1:1", "", ":", "a:b", "1:", ":1", "0:0", "-1:2", "1.5:1", "1;1"],
)
def test_malformed_ratios_are_refused(raw: str) -> None:
    """Strict parsing, because a permissive parser is a way for a bad configuration value to
    silently become an allocation nobody chose — and arms of the wrong size are not detectable
    from the result.

    ``0:0`` is in the list because it parses as two integers and is still meaningless: it
    allocates nothing to either arm, and the total is the denominator of the reduction.
    """
    with pytest.raises(ValueError):
        parse_allocation_ratio(raw)


# ---------------------------------------------------------------------------
# Determinism — the property that actually matters
# ---------------------------------------------------------------------------


def test_assignment_is_stable_across_repeated_calls() -> None:
    """The same pair always yields the same arm.

    The one failure that would destroy an experiment silently. A sequential or random allocator
    would give a different arm on a retried job, the case would be counted in whichever arm the
    last write recorded, and no figure in the result would look wrong.
    """
    experiment_id = uuid.uuid4()
    ratio = AllocationRatio(control=1, treatment=1)
    for _ in range(20):
        case_id = uuid.uuid4()
        first = assign_group(experiment_id, case_id, ratio)
        assert all(assign_group(experiment_id, case_id, ratio) is first for _ in range(5))


def test_a_uuid_and_its_string_form_assign_identically() -> None:
    """Callers hold ids in both forms and should not have to know which this wants.

    If they diverged, a worker holding a ``UUID`` and one holding a string would assign the same
    case to different arms — the instability above, arriving by a route nobody would suspect.
    """
    experiment_id = uuid.uuid4()
    ratio = AllocationRatio(control=1, treatment=3)
    for _ in range(20):
        case_id = uuid.uuid4()
        assert assign_group(experiment_id, case_id, ratio) is assign_group(
            str(experiment_id), str(case_id), ratio
        )


def test_the_same_case_is_assigned_independently_in_two_experiments() -> None:
    """Keying on the experiment means a control case in one comparison is not forced to be
    control in the next.

    A plain hash of the concatenation would correlate the two, so a case unlucky in one
    experiment would be unlucky in every experiment — which biases every comparison in the same
    direction and is undetectable within any single one.
    """
    ratio = AllocationRatio(control=1, treatment=1)
    cases = [uuid.uuid4() for _ in range(200)]
    left, right = uuid.uuid4(), uuid.uuid4()

    agreements = sum(
        assign_group(left, case, ratio) is assign_group(right, case, ratio) for case in cases
    )
    # Independent assignment agrees about half the time. Anything near 200 or near 0 means the
    # experiment id is not really participating in the digest.
    assert 60 < agreements < 140, (
        f"{agreements}/200 cases got the same arm in two experiments, which suggests the "
        "assignment is not independent across experiments"
    )


@pytest.mark.parametrize(
    ("control", "treatment", "expected_treatment_share"),
    [(1, 1, 0.50), (1, 3, 0.75), (3, 1, 0.25), (1, 9, 0.90)],
)
def test_the_allocation_ratio_is_honoured_over_a_large_sample(
    control: int, treatment: int, expected_treatment_share: float
) -> None:
    """The integer reduction must actually produce the requested split.

    Two thousand cases, with a tolerance wide enough for sampling noise and narrow enough to
    catch an off-by-one in the comparison or a swapped numerator. The comparison is exact integer
    arithmetic, so a systematic error would show as a consistent bias rather than as noise.

    ``float`` appears here only to express a tolerance in a test assertion — never in the
    arithmetic under test, which is pure integers.
    """
    experiment_id = uuid.uuid4()
    ratio = AllocationRatio(control=control, treatment=treatment)
    counts = Counter(
        assign_group(experiment_id, uuid.uuid4(), ratio) for _ in range(2000)
    )
    observed = counts[ExperimentGroup.TREATMENT] / 2000
    assert abs(observed - expected_treatment_share) < 0.05, (
        f"requested a {treatment}/{control + treatment} treatment share, observed {observed}"
    )


@pytest.mark.parametrize(
    ("control", "treatment", "expected"),
    [(0, 1, ExperimentGroup.TREATMENT), (1, 0, ExperimentGroup.CONTROL)],
)
def test_degenerate_ratios_send_everything_one_way(
    control: int, treatment: int, expected: ExperimentGroup
) -> None:
    """``0:1`` and ``1:0`` are absolute, with no boundary case leaking the other way.

    Worth asserting because the comparison is ``<`` rather than ``<=``, and a boundary error
    would leak a handful of cases into an arm that is supposed to be empty — which would look
    like contamination rather than a bug.
    """
    experiment_id = uuid.uuid4()
    ratio = AllocationRatio(control=control, treatment=treatment)
    assert all(
        assign_group(experiment_id, uuid.uuid4(), ratio) is expected for _ in range(500)
    )


@settings(max_examples=200, deadline=None)
@given(
    experiment_id=st.uuids(),
    case_id=st.uuids(),
    treatment=st.integers(min_value=0, max_value=20),
    control=st.integers(min_value=0, max_value=20),
)
def test_assignment_always_returns_a_valid_arm(
    experiment_id: uuid.UUID, case_id: uuid.UUID, treatment: int, control: int
) -> None:
    """Generated inputs never produce anything but a real arm, and never raise.

    Assignment runs inside the transaction that creates a case. An exception here would roll that
    back and lose a real payment failure, so "never raises for any input" is a correctness
    property rather than robustness theatre.
    """
    if control + treatment == 0:
        with pytest.raises(ValueError):
            AllocationRatio(control=control, treatment=treatment)
        return
    ratio = AllocationRatio(control=control, treatment=treatment)
    assert assign_group(experiment_id, case_id, ratio) in tuple(ExperimentGroup)


# ---------------------------------------------------------------------------
# The attribution gate — every test is an attempt to get an unearned claim
# ---------------------------------------------------------------------------


def _passing_gate_arguments() -> dict[str, object]:
    """A scenario that clears every gate term, so each test can break exactly one.

    The gate takes primitives rather than an ORM row — it lives in ``revora.domain`` so both the
    experiment engine and the metrics engine can apply the identical rule — which is why there is
    no stub object here to build.
    """
    return {
        "state": ExperimentState.COMPLETED.value,
        "required_sample_size_per_group": 100,
        "labels": None,
        "control_cases": 200,
        "treatment_cases": 200,
        "lift_ci_low": Decimal("0.0500"),
        "lift_ci_high": Decimal("0.2500"),
    }


def test_a_fully_qualified_result_permits_attribution() -> None:
    """The control. Without it, every refusal test below would pass against a gate that always
    refuses — which would be safe and useless."""
    assert attribution_refusals(**_passing_gate_arguments()) == ()  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "state",
    [
        ExperimentState.DRAFT.value,
        ExperimentState.ACTIVE.value,
        ExperimentState.ABANDONED.value,
    ],
)
def test_an_incomplete_experiment_refuses_attribution(state: str) -> None:
    """An interim look on a running experiment is not a result.

    ``ACTIVE`` is the important one: an experiment part-way to its sample size will sometimes show
    a significant lift by chance, and reporting it is the classic peeking error.
    """
    arguments = _passing_gate_arguments()
    arguments["state"] = state
    refusals = attribution_refusals(**arguments)  # type: ignore[arg-type]
    assert any(r.code == "EXPERIMENT_NOT_COMPLETED" for r in refusals), refusals


@pytest.mark.parametrize(
    ("control_cases", "treatment_cases"),
    [(99, 200), (200, 99), (99, 99), (0, 200)],
)
def test_an_underpowered_experiment_refuses_attribution(
    control_cases: int, treatment_cases: int
) -> None:
    """Both arms must reach the threshold computed at definition time.

    One arm short is enough to refuse. A comparison with 200 treatment cases against 99 control
    cases is not half-powered — the smaller arm dominates the variance, and the interval it
    produces is wider than the sample size suggests.
    """
    arguments = _passing_gate_arguments()
    arguments["control_cases"] = control_cases
    arguments["treatment_cases"] = treatment_cases
    refusals = attribution_refusals(**arguments)  # type: ignore[arg-type]
    assert any(r.code == "BELOW_REQUIRED_SAMPLE_SIZE" for r in refusals), refusals


@pytest.mark.parametrize(
    ("low", "high"),
    [("-0.0100", "0.2000"), ("0.0000", "0.2000"), ("-0.2000", "0.0000"), ("-0.1", "0.1")],
)
def test_an_interval_containing_zero_refuses_attribution(low: str, high: str) -> None:
    """Zero inside the interval means the effect is not established.

    The boundary cases are included on purpose: an interval whose bound is *exactly* zero
    contains zero. Implementing this with a strict inequality would let ``[0.0000, 0.2000]``
    through, which claims an effect from data consistent with none.
    """
    arguments = _passing_gate_arguments()
    arguments["lift_ci_low"] = Decimal(low)
    arguments["lift_ci_high"] = Decimal(high)
    refusals = attribution_refusals(**arguments)  # type: ignore[arg-type]
    assert any(
        r.code == ExperimentLabel.CAUSALITY_NOT_ESTABLISHED.value for r in refusals
    ), refusals


def test_an_interval_entirely_below_zero_refuses_attribution() -> None:
    """Excluding zero on the wrong side is a finding, not an attribution.

    The treatment reduced recovery. That must be reported — and it must not be reported as
    incremental revenue, which is what would happen if the gate only checked "excludes zero" and
    something downstream took the absolute value.
    """
    arguments = _passing_gate_arguments()
    arguments["lift_ci_low"] = Decimal("-0.2500")
    arguments["lift_ci_high"] = Decimal("-0.0500")
    refusals = attribution_refusals(**arguments)  # type: ignore[arg-type]
    assert any(r.code == "LIFT_INTERVAL_BELOW_ZERO" for r in refusals), refusals


def test_a_missing_interval_refuses_attribution() -> None:
    """No interval means no claim. ``None`` must not be treated as "wide enough"."""
    arguments = _passing_gate_arguments()
    arguments["lift_ci_low"] = None
    arguments["lift_ci_high"] = None
    refusals = attribution_refusals(**arguments)  # type: ignore[arg-type]
    assert any(r.code == "NO_LIFT_INTERVAL" for r in refusals), refusals


@pytest.mark.parametrize(
    "label",
    [
        ExperimentLabel.UNDERPOWERED.value,
        ExperimentLabel.INVALIDATED.value,
        ExperimentLabel.SYNTHETIC.value,
    ],
)
def test_a_disqualifying_label_refuses_attribution(label: str) -> None:
    """Three labels each block a claim on their own, even with perfect data.

    ``SYNTHETIC`` is the one that matters for the demo: a synthetic experiment can show a
    beautiful lift, and reporting it as recovered revenue would be circular — the lift was put
    there by the generator.
    """
    arguments = _passing_gate_arguments()
    arguments["labels"] = [label]
    refusals = attribution_refusals(**arguments)  # type: ignore[arg-type]
    assert any(r.code == "DISQUALIFYING_LABEL" for r in refusals), refusals


def test_non_blocking_labels_do_not_refuse_attribution() -> None:
    """``CONTAMINATED`` and ``EXPLORATORY`` are recorded but do not disqualify by themselves.

    Contamination is handled by *excluding* the affected cases from the arm counts, so the
    remaining comparison is still valid — the count is reported alongside so a reader can judge.
    Treating the label as fatal would discard a usable experiment; ignoring the exclusion would
    keep a bad one.
    """
    arguments = _passing_gate_arguments()
    arguments["labels"] = [ExperimentLabel.CONTAMINATED.value]
    refusals = attribution_refusals(**arguments)  # type: ignore[arg-type]
    assert refusals == (), refusals


def test_every_failing_term_is_reported_not_just_the_first() -> None:
    """An operator needs the whole list, because the responses differ.

    "Wait for more data" and "your freeze is broken" are different actions, and a gate that
    short-circuited on the first failure would send someone to wait for data that will never
    help.
    """
    refusals = attribution_refusals(
        state=ExperimentState.ACTIVE.value,
        required_sample_size_per_group=1000,
        labels=[ExperimentLabel.INVALIDATED.value],
        control_cases=10,
        treatment_cases=10,
        lift_ci_low=Decimal("-0.3000"),
        lift_ci_high=Decimal("0.5000"),
    )
    codes = {refusal.code for refusal in refusals}
    assert codes == {
        RefusalCode.NOT_COMPLETED,
        RefusalCode.BELOW_SAMPLE_SIZE,
        RefusalCode.CONTAINS_ZERO,
        RefusalCode.DISQUALIFYING_LABEL,
    }, codes
