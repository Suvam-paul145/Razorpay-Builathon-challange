"""The generated world itself: is it the world the scenarios claim it is?

Separate from ``test_scenarios.py`` because these need no database. They are the premises the whole
evidence base rests on, and a premise that only gets checked in the nightly tier is a premise that
breaks on a Tuesday and is discovered on a Friday.

**Every test here checks a claim the scenarios then rely on.** The null scenario's CI gate proves
nothing unless the null scenario's true lift really is zero; the coverage check proves nothing
unless the same seed really does reproduce the same world. So these run in the fast tier, on every
commit.

The decision-path tests at the bottom cover the half of task 27.4 that is about *deciding* rather
than measuring: given a correct estimate of a world where acting hurts, or where the customer was
going to pay anyway, the real optimizer must decline to act. See
:mod:`revora.synthetic.decisions` for why feeding the ground truth in is legitimate there and
circular in the measurement harness.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest

from revora.domain.actions import CandidateAction
from revora.domain.enums import SelectionReason
from revora.domain.failure_taxonomy import REASON_TO_CAUSE
from revora.domain.segments import AmountBand, amount_band_for
from revora.platform.config import default_configuration
from revora.synthetic.decisions import decide, tally_decisions, true_probability
from revora.synthetic.generator import (
    SCALE,
    SCENARIOS,
    ScenarioName,
    generate,
    scenario,
    true_average_lift,
)
from revora.synthetic.harness import TREATED_ACTION, synthetic_uuid

pytestmark = pytest.mark.pure

_NULL_ACTIONS = (CandidateAction.DO_NOTHING, CandidateAction.WAIT)


# ---------------------------------------------------------------------------
# Reproducibility and the counterfactual pair
# ---------------------------------------------------------------------------


def test_the_same_seed_reproduces_the_same_world() -> None:
    """A ``synthetic_run`` row stores only a seed, a scenario and a generator version.

    The claim is that those three reproduce the dataset exactly, so a result can be re-derived
    months later rather than taken on trust. If generation were not reproducible, every stored
    synthetic result would be unfalsifiable.
    """
    first = generate(ScenarioName.POSITIVE, seed=12345, case_count=50)
    second = generate(ScenarioName.POSITIVE, seed=12345, case_count=50)
    third = generate(ScenarioName.POSITIVE, seed=12346, case_count=50)

    assert [case.provider_payment_id for case in first.cases] == [
        case.provider_payment_id for case in second.cases
    ]
    assert [case.uniform_draw for case in first.cases] == [
        case.uniform_draw for case in second.cases
    ]
    assert [case.amount for case in first.cases] == [case.amount for case in second.cases]
    assert [case.uniform_draw for case in first.cases] != [
        case.uniform_draw for case in third.cases
    ], "different seeds produced identical draws"


def test_both_counterfactual_outcomes_come_from_one_draw() -> None:
    """The trick the whole harness rests on.

    One uniform draw per case, compared against both probabilities, so the individual causal effect
    is well defined: a case either recovers regardless, recovers only if treated, or never recovers.
    Two independent draws would give a correct average and no individual truth.

    Asserted structurally: with a positive uplift, ``recovers_if_treated`` must be true wherever
    ``recovers_if_untreated`` is. Treatment can only ever *add* responders, never remove them. With
    independent draws that would fail for roughly a quarter of cases.
    """
    dataset = generate(ScenarioName.POSITIVE, seed=7, case_count=400)
    for case in dataset.cases:
        assert case.p_treated[TREATED_ACTION] >= case.p_natural
        if case.recovers_if_untreated:
            assert case.recovers_if_treated[TREATED_ACTION], (
                "a case that recovers untreated must also recover treated under a positive "
                "uplift; the two outcomes are not sharing one draw"
            )
        assert case.recovers_if_untreated == (case.uniform_draw < case.p_natural)


def test_a_negative_uplift_can_only_remove_responders() -> None:
    """The mirror of the test above, and it catches a sign error the positive one cannot.

    Under a negative uplift, treatment can only take recoveries away. A case that does not recover
    untreated must not recover treated.
    """
    dataset = generate(ScenarioName.NEGATIVE, seed=11, case_count=400)
    for case in dataset.cases:
        assert case.p_treated[TREATED_ACTION] <= case.p_natural
        if not case.recovers_if_untreated:
            assert not case.recovers_if_treated[TREATED_ACTION]


def test_responds_to_is_only_true_where_treatment_flipped_the_outcome() -> None:
    """The individual causal effect, which only exists because both arms share one draw."""
    dataset = generate(ScenarioName.POSITIVE, seed=13, case_count=300)
    responders = [case for case in dataset.cases if case.responds_to(TREATED_ACTION)]
    assert responders, "a scenario with a 0.15 uplift produced no responders at all"
    for case in responders:
        assert not case.recovers_if_untreated
        assert case.recovers_if_treated[TREATED_ACTION]


# ---------------------------------------------------------------------------
# The generated surface matches the real one
# ---------------------------------------------------------------------------


def test_generated_payloads_use_only_verified_error_reasons() -> None:
    """Every reason must map through the real taxonomy.

    An invented reason would diagnose as ``UNKNOWN``, which would collapse every segment to the
    global prior and make the cause-conditioned ground truth meaningless — while the run still
    completed and looked fine.
    """
    for name in SCENARIOS:
        dataset = generate(name, seed=3, case_count=120)
        for case in dataset.cases:
            assert case.error_reason in REASON_TO_CAUSE, (
                f"{case.error_reason!r} is not in the verified taxonomy"
            )
            assert REASON_TO_CAUSE[case.error_reason] is case.cause, (
                f"{case.error_reason!r} maps to {REASON_TO_CAUSE[case.error_reason]}, "
                f"but the generator intended {case.cause}"
            )


def test_amounts_land_in_the_band_they_were_drawn_for() -> None:
    """The band ranges must sit inside the shared banding rule's boundaries.

    Asserted rather than assumed, and the first draft failed it: the ranges were hand-written a
    factor of ten below the real boundaries, so three of the four bands were mislabelled while
    every other test still passed. The ranges are now derived from ``AMOUNT_BAND_BOUNDARIES``,
    which is what makes this test pass for a reason rather than by coincidence.
    """
    dataset = generate(ScenarioName.POSITIVE, seed=5, case_count=400)
    for case in dataset.cases:
        assert amount_band_for(case.amount) is case.amount_band, (
            f"{case.amount} was drawn for {case.amount_band} but bands as "
            f"{amount_band_for(case.amount)}"
        )
    # And every band is actually populated, so segmentation has something to segment.
    assert {case.amount_band for case in dataset.cases} == {
        AmountBand.MICRO,
        AmountBand.SMALL,
        AmountBand.MEDIUM,
        AmountBand.LARGE,
    }


def test_generated_contacts_cannot_reach_a_real_person() -> None:
    """Reserved documentation ranges only. No real PII, ever.

    Synthetic cases are barred from the provider by the import contracts, but a contact that could
    ring a real phone is the kind of thing that survives a refactor of those contracts.
    """
    dataset = generate(ScenarioName.POSITIVE, seed=8, case_count=100)
    for case in dataset.cases:
        entity = case.webhook_payload()["payload"]["payment"]["entity"]  # type: ignore[index]
        assert isinstance(entity, dict)
        assert str(entity["contact"]).startswith("+9190000")
        assert str(entity["email"]).endswith("@example.invalid")


# ---------------------------------------------------------------------------
# Scenario premises
# ---------------------------------------------------------------------------


def test_the_null_scenario_has_a_true_lift_of_exactly_zero() -> None:
    """The premise of the CI gate. If the ground truth is not zero, the gate proves nothing."""
    dataset = generate(ScenarioName.NULL, seed=99, case_count=500)
    for action in CandidateAction:
        assert dataset.true_lift(action) == Decimal("0.0000"), (
            f"the null scenario has a non-zero true lift for {action}"
        )
    for case in dataset.cases:
        assert case.p_treated[TREATED_ACTION] == case.p_natural
        assert case.recovers_if_treated[TREATED_ACTION] == case.recovers_if_untreated


def test_the_high_baseline_scenario_is_above_the_configured_threshold() -> None:
    """The premise of the high-baseline case: these customers were going to pay anyway."""
    threshold = default_configuration().HIGH_BASELINE_THRESHOLD
    dataset = generate(ScenarioName.HIGH_BASELINE, seed=42, case_count=200)
    for case in dataset.cases:
        natural = Decimal(case.p_natural) / Decimal(SCALE)
        assert natural >= threshold, (
            f"natural probability {natural} is below HIGH_BASELINE_THRESHOLD {threshold}"
        )


def test_an_unknown_scenario_is_refused() -> None:
    """A typo must not silently run a different scenario.

    Defaulting here would be the worst possible failure: a mistyped ``"nul"`` running the positive
    scenario would turn the CI gate into a test that always passes.
    """
    with pytest.raises(ValueError, match="unknown scenario"):
        scenario("nul")
    with pytest.raises(ValueError):
        generate("not-a-scenario", seed=1, case_count=10)


def test_a_negative_case_count_is_refused() -> None:
    """Zero cases is a legitimate degenerate run; a negative count is a caller bug."""
    assert generate(ScenarioName.NULL, seed=1, case_count=0).case_count == 0
    with pytest.raises(ValueError, match="negative"):
        generate(ScenarioName.NULL, seed=1, case_count=-1)


def test_true_average_lift_is_zero_over_no_cases() -> None:
    """A degenerate input that the null scenario's ground truth makes reachable."""
    assert true_average_lift([], TREATED_ACTION) == Decimal("0.0000")


def test_the_scenario_catalogue_is_complete() -> None:
    """All four mandatory scenarios exist and carry an expectation a reader can check."""
    assert set(SCENARIOS) == {
        ScenarioName.NULL,
        ScenarioName.NEGATIVE,
        ScenarioName.HIGH_BASELINE,
        ScenarioName.POSITIVE,
    }
    for spec in SCENARIOS.values():
        assert spec.expectation.strip(), f"{spec.name} has no stated expectation"
        assert spec.causes, f"{spec.name} generates no causes"


# ---------------------------------------------------------------------------
# The decision path (task 27.4 b and c)
# ---------------------------------------------------------------------------


def test_a_negative_uplift_makes_the_optimizer_decline_to_act_on_every_case() -> None:
    """Task 27.4(b). Acting reduces recovery, so nothing may be selected.

    The failure this catches is an optimizer that reads probability magnitude, or that takes an
    absolute value anywhere in the value chain: either would turn a treatment that *reduces*
    recovery by 0.12 into a candidate that looks worth doing.
    """
    dataset = generate(ScenarioName.NEGATIVE, seed=606, case_count=400)
    tally = tally_decisions(dataset, config=default_configuration())

    assert tally.acted_count == 0, (
        f"the optimizer chose to act on a world where acting reduces recovery: "
        f"{tally.as_document()}"
    )
    assert tally.by_action[CandidateAction.DO_NOTHING.value] == dataset.case_count
    # And the recorded reason has to be the right one. This scenario is what found the bug where
    # it was not: nothing was in contention, so the sentence is "nothing was worth doing", not
    # "doing nothing won the comparison".
    assert tally.reason_share(SelectionReason.NO_POSITIVE_VALUE) == dataset.case_count, (
        tally.as_document()
    )


def test_a_high_baseline_makes_the_optimizer_decline_and_say_why() -> None:
    """Task 27.4(c) and P17. Not "nothing was worth doing" but "they were going to pay anyway".

    Both halves matter. Selecting a null action is the behaviour; recording
    ``HIGH_BASELINE_NO_INTERVENTION`` is what tells a merchant which of the two very different
    reasons applied, and a merchant who cannot tell them apart will conclude Revora is broken.
    """
    dataset = generate(ScenarioName.HIGH_BASELINE, seed=707, case_count=400)
    config = default_configuration()
    tally = tally_decisions(dataset, config=config)

    assert tally.acted_count == 0, tally.as_document()
    assert tally.reason_share(SelectionReason.HIGH_BASELINE_NO_INTERVENTION) == (
        dataset.case_count
    ), tally.as_document()

    # And per case, so the reason is not merely the modal one.
    for case in dataset.cases:
        result = decide(case, config=config)
        assert result.selection_reason is SelectionReason.HIGH_BASELINE_NO_INTERVENTION
        assert result.selected.action in _NULL_ACTIONS


def test_a_real_uplift_makes_the_optimizer_act_so_the_two_tests_above_are_not_vacuous() -> None:
    """The control. Without it, both tests above would pass against an optimizer that never acts.

    That optimizer would be perfectly safe and completely useless, and it is the failure mode a
    suite full of "must not act" assertions is least able to see.
    """
    dataset = generate(ScenarioName.POSITIVE, seed=808, case_count=400)
    tally = tally_decisions(dataset, config=default_configuration())

    assert tally.acted_count > 0, (
        f"a planted 0.15 uplift produced no action at all: {tally.as_document()}"
    )
    assert tally.by_action[CandidateAction.PAYMENT_LINK.value] > 0
    # Every acting case is a real comparison won on net value.
    assert tally.reason_share(SelectionReason.HIGHEST_NET_VALUE) == tally.acted_count
    # The rest are the small amounts, where a 0.15 uplift is still not worth 10 rupees of
    # customer cost. That is the correct answer and it carries the correct reason, which is what
    # makes the mixed tally more informative than a uniform one.
    assert tally.reason_share(SelectionReason.NO_POSITIVE_VALUE) == (
        dataset.case_count - tally.acted_count
    )
    assert set(tally.by_reason) == {
        SelectionReason.HIGHEST_NET_VALUE.value,
        SelectionReason.NO_POSITIVE_VALUE.value,
    }, tally.as_document()


def test_do_nothing_is_priced_at_the_baseline_so_its_increment_is_exactly_zero() -> None:
    """P19's neutrality, checked on generated cases rather than on constructed ones.

    ``DO_NOTHING``'s probability *is* the baseline, so its incremental probability is exactly zero
    and its net value exactly zero. If it were instead priced at the treated probability, rounding
    alone would give the null action a small positive increment and the optimizer would find a
    reason to do nothing that looked like a reason to act.
    """
    config = default_configuration()
    for case in generate(ScenarioName.POSITIVE, seed=909, case_count=100).cases:
        result = decide(case, config=config)
        do_nothing = next(
            item for item in result.candidates if item.action is CandidateAction.DO_NOTHING
        )
        assert do_nothing.incremental_probability.value == Decimal("0.0000")
        assert int(do_nothing.net_recovery_value) == 0


def test_run_ids_are_derived_from_the_seed_rather_than_drawn() -> None:
    """The arm assignment digests the experiment id and the case id, so those cannot be random.

    With ``uuid4`` ids a stored run reproduced the same *world* and then split it into different
    arms, which means the stored measured lift could not be re-derived — and a result nobody can
    re-derive is a result nobody can disagree with.

    The merchant is part of the key, so the guarantee is "reproducible for the merchant it ran
    against". Asserted here rather than left implied, because the alternative reading — universally
    reproducible — is the one somebody would rely on, and it would collide on a primary key the
    first time two merchants used the same seed.
    """
    merchant = uuid.uuid4()
    other = uuid.uuid4()

    assert synthetic_uuid(merchant, ScenarioName.NULL, 7, "case", 3) == synthetic_uuid(
        merchant, ScenarioName.NULL, 7, "case", 3
    )
    # Every component of the key changes the result.
    assert synthetic_uuid(merchant, ScenarioName.NULL, 7, "case", 3) != synthetic_uuid(
        merchant, ScenarioName.NULL, 7, "case", 4
    )
    assert synthetic_uuid(merchant, ScenarioName.NULL, 7, "case", 3) != synthetic_uuid(
        merchant, ScenarioName.NULL, 8, "case", 3
    )
    assert synthetic_uuid(merchant, ScenarioName.NULL, 7, "case", 3) != synthetic_uuid(
        merchant, ScenarioName.POSITIVE, 7, "case", 3
    )
    assert synthetic_uuid(merchant, ScenarioName.NULL, 7, "case", 3) != synthetic_uuid(
        merchant, ScenarioName.NULL, 7, "experiment", 3
    )
    assert synthetic_uuid(merchant, ScenarioName.NULL, 7, "case", 3) != synthetic_uuid(
        other, ScenarioName.NULL, 7, "case", 3
    ), "two merchants on the same seed would collide on recovery_case.id"


def test_probabilities_convert_to_the_four_places_the_optimizer_stores() -> None:
    """Six digits of generator resolution, four places of optimizer contract, half-up between."""
    assert true_probability(0).value == Decimal("0.0000")
    assert true_probability(SCALE).value == Decimal("1.0000")
    assert true_probability(250_000).value == Decimal("0.2500")
    assert true_probability(250_050).value == Decimal("0.2501")
    assert true_probability(250_049).value == Decimal("0.2500")
