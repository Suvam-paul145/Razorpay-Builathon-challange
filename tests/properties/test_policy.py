"""Property 2 and R8.C14: the policy verdict is independent of AI output and deterministic.

Property 2 is the central safety claim of the whole system — *AI recommendation is not AI
authority* — and this file is where it stops being a slogan.

The test does what an attacker or a bug would do: takes a decision, replaces every
AI-produced field with arbitrary schema-valid content including prompt-injection text, and
re-evaluates. The verdict, the primary reason and all twelve ordered outcomes must be
byte-identical. They are, and the reason they are is structural rather than defensive:
``PolicyInput`` has no field an AI value could occupy, and ``revora.policy`` cannot import
``revora.reasoning``. This test proves the structure holds; the import contract stops it
being quietly dismantled later.
"""

from __future__ import annotations

import dataclasses
from datetime import timedelta
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from revora.domain.money import Minor
from revora.policy.engine import evaluate
from revora.policy.input import PolicyInput
from revora.policy.rules import default_rule_set
from tests.strategies.policy import ai_field_values, policy_input

pytestmark = pytest.mark.pure

_RULES = default_rule_set(
    max_recovery_attempts=3,
    max_customer_messages=2,
    cooldown_interval=timedelta(hours=24),
    policy_decision_validity=timedelta(minutes=15),
    risk_reason_codes=frozenset({"payment_risk_check_failed", "compliance_violation"}),
    min_net_value_threshold=Minor(5_000),
    min_incremental_probability=Decimal("0.05"),
)


_ASSIGNMENT_REFUSED = (dataclasses.FrozenInstanceError, AttributeError, TypeError)
"""What a refused attribute assignment on a frozen, slotted dataclass may raise.

Three types because the mechanism differs by case and by CPython version:
``FrozenInstanceError`` for a declared field, ``TypeError`` or ``AttributeError`` for a name
the slots layout has no room for. The property under test is that assignment cannot succeed;
pinning the exception type would make the test brittle about something it does not care
about."""


def _fingerprint(evaluation: object) -> tuple[object, ...]:
    """Everything about a decision that must not move: verdict, reason, twelve outcomes."""
    verdict = evaluation.verdict
    reason = evaluation.primary_reason
    checks = evaluation.checks
    return (
        verdict,
        reason,
        tuple((c.order, c.check, c.outcome) for c in checks),
    )


@given(candidate=policy_input(), ai_fields=ai_field_values())
def test_p2_verdict_is_unchanged_by_any_ai_produced_field(
    candidate: PolicyInput, ai_fields: dict[str, object]
) -> None:
    """Feature: Policy_Engine. Property 2 — a policy verdict is independent of AI output.

    The substitution is attempted in the only way it *could* be attempted: by trying to set
    the AI fields on the input. ``PolicyInput`` is frozen and slotted, so every attempt
    raises rather than mutating — and that is the proof. There is no attribute to overwrite
    and no attribute to add, so an AI-produced value cannot reach the engine as data.

    The evaluation is then run again and compared, so even if a future edit made the type
    mutable the verdict comparison would still catch a behaviour change.
    """
    before = evaluate(candidate, _RULES)

    for name, value in ai_fields.items():
        # The exception type is an implementation detail of frozen-plus-slotted
        # dataclasses — CPython raises TypeError for an undeclared name and
        # FrozenInstanceError for a declared one. The property is that the assignment is
        # impossible, not which error announces it, so all three are accepted and the
        # absence of an exception is the failure.
        with pytest.raises(_ASSIGNMENT_REFUSED):
            setattr(candidate, name, value)

    after = evaluate(candidate, _RULES)
    assert _fingerprint(before) == _fingerprint(after)


@given(candidate=policy_input())
def test_p2_policy_input_admits_no_undeclared_attribute(candidate: PolicyInput) -> None:
    """Feature: Policy_Engine. Property 2 — there is nowhere for an AI value to sit.

    Slotted dataclasses have no ``__dict__``, so an attribute that is not declared in the
    class cannot be attached at runtime. That is what closes the last route by which a
    caller could smuggle a model's opinion into an evaluation without editing
    ``revora/policy/input.py`` — a change any reviewer would see.
    """
    assert not hasattr(candidate, "__dict__")
    with pytest.raises(_ASSIGNMENT_REFUSED):
        candidate.ai_recommended_action = "PAYMENT_LINK"  # type: ignore[attr-defined]


@given(candidate=policy_input())
def test_r8c14_identical_inputs_give_identical_decisions(candidate: PolicyInput) -> None:
    """Feature: Policy_Engine. R8.C14 — the engine is a pure function.

    Evaluated three times. A function that read a clock would produce a different
    ``expires_at`` on each call; one that consulted a mutable global or a database could
    change verdict between calls. Neither happens, which is what makes any historical
    decision exactly replayable from its recorded inputs.
    """
    first = evaluate(candidate, _RULES)
    second = evaluate(candidate, _RULES)
    third = evaluate(candidate, _RULES)

    assert first == second == third
    assert first.expires_at == third.expires_at


@given(candidate=policy_input())
def test_all_twelve_checks_are_always_recorded(candidate: PolicyInput) -> None:
    """Feature: Policy_Engine. R8.C2 — twelve outcomes, always, in the fixed order.

    Not "up to twelve". The engine short-circuits nothing, so the record answers "what else
    would have stopped this" as well as "what did" — which is the question a merchant asks
    after changing a bound.
    """
    evaluation = evaluate(candidate, _RULES)
    assert len(evaluation.checks) == 12
    assert [c.order for c in evaluation.checks] == list(range(1, 13))


@given(candidate=policy_input())
def test_any_unavailable_input_blocks_and_never_approves(candidate: PolicyInput) -> None:
    """Feature: Policy_Engine. R8.C17 — there is no assume-fine branch.

    An unreadable input yields ``BLOCKED`` with ``POLICY_INPUT_UNAVAILABLE``. The failure
    this prevents is concrete: an opt-out record that happened to be unreachable being read
    as "not opted out", and a customer who asked us to stop being contacted anyway.
    """
    from revora.domain.enums import CheckOutcome, PolicyVerdict

    evaluation = evaluate(candidate, _RULES)
    unavailable = [c for c in evaluation.checks if c.outcome is CheckOutcome.UNAVAILABLE]
    if unavailable:
        assert evaluation.verdict is PolicyVerdict.BLOCKED
        assert evaluation.primary_reason == "POLICY_INPUT_UNAVAILABLE"
    assert not (unavailable and evaluation.verdict is PolicyVerdict.APPROVED)


@given(candidate=policy_input())
def test_primary_reason_is_the_lowest_ordered_failure(candidate: PolicyInput) -> None:
    """Feature: Policy_Engine. R8.C2 — the verdict comes from the lowest-ordered non-pass.

    Which is what guarantees an expensive or case-specific check can never be the recorded
    reason a paid or opted-out customer was contacted: those checks are ordered first, so
    if they fail they are the reason.

    **With one substitution the requirements mandate, and it is not a loophole.** R8.C8 and
    R24.C8 require a ``COOLDOWN_ACTIVE`` failure whose earliest permitted execution instant
    falls at or after the Recovery_Window end to be reported as ``WINDOW_EXPIRED`` and
    ``BLOCKED`` rather than as ``DEFERRED``, because deferring would schedule an action that
    can never legally run and the case would expire with "waiting for cooldown" as its last
    recorded word. So the lowest-ordered failure still *decides*; what it is *called*
    changes in exactly one predictable case.

    The expected reason is derived from the candidate and the rule set here rather than read
    off the evaluation, which is what keeps the property sharp: it still fails if the engine
    reports a higher-ordered check, and it fails if the engine substitutes ``WINDOW_EXPIRED``
    when the cooldown *would* have elapsed inside the window — the two ways a verdict can be
    mis-ordered. Weakening this to "the reason is one of the failures" would catch neither.
    """
    from revora.domain.enums import CheckOutcome, PolicyCheck, PolicyVerdict

    evaluation = evaluate(candidate, _RULES)
    if any(c.outcome is CheckOutcome.UNAVAILABLE for c in evaluation.checks):
        return
    failures = [c for c in evaluation.checks if not c.passed]
    if not failures:
        assert evaluation.verdict is PolicyVerdict.APPROVED
        return

    deciding = failures[0].check
    expected = deciding.value
    if deciding is PolicyCheck.COOLDOWN_ACTIVE and candidate.last_outbound_at is not None:
        earliest = candidate.last_outbound_at + _RULES.cooldown_interval
        if earliest >= candidate.window_end_at:
            expected = PolicyCheck.WINDOW_EXPIRED.value

    assert evaluation.primary_reason == expected, (
        f"lowest-ordered failure {deciding.value} produced {evaluation.primary_reason}"
    )
    if expected != deciding.value:
        # The substitution is a refusal, not a deferral, and it carries no schedule. A
        # WINDOW_EXPIRED that still parked an earliest-permitted instant would be the same
        # silent expiry under a different name.
        assert evaluation.verdict is PolicyVerdict.BLOCKED
        assert evaluation.earliest_permitted_at is None


@given(candidate=policy_input())
def test_only_approved_carries_an_idempotency_key(candidate: PolicyInput) -> None:
    """Feature: Policy_Engine. R8.C15 — a key is minted only where an effect is permitted.

    A blocked or deferred decision carries no idempotency key, because a key is the name of
    an external effect and no effect is authorized. That is what stops a refused decision
    being reused later as if it had been an approval.
    """
    from revora.domain.enums import PolicyVerdict

    evaluation = evaluate(candidate, _RULES)
    if evaluation.verdict is PolicyVerdict.APPROVED:
        assert evaluation.idempotency_key is not None
        assert len(evaluation.idempotency_key) <= 40
    else:
        assert evaluation.idempotency_key is None


@given(
    candidate=policy_input(),
    extra_attempts=st.integers(min_value=0, max_value=10),
)
def test_a_stricter_rule_set_never_approves_more(
    candidate: PolicyInput, extra_attempts: int
) -> None:
    """Feature: Policy_Engine. Monotonicity — tightening a bound cannot permit more.

    Not in the requirements as a numbered criterion, but it is the property that makes a
    merchant's configuration meaningful: lowering the attempt cap must never turn a refusal
    into an approval. A bug that inverted a comparison would show up here and nowhere else.
    """
    from revora.domain.enums import PolicyVerdict

    loose = dataclasses.replace(_RULES, max_recovery_attempts=3 + extra_attempts)
    strict = dataclasses.replace(_RULES, max_recovery_attempts=0)

    if evaluate(candidate, strict).verdict is PolicyVerdict.APPROVED:
        assert evaluate(candidate, loose).verdict is PolicyVerdict.APPROVED
