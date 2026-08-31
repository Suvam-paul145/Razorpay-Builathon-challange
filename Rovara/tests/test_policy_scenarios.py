"""Twelve readable scenarios, each constructed to fail exactly one check.

The property tests prove the engine's structural guarantees. These prove it says the right
thing in twelve specific, nameable situations — and they double as executable documentation
of what each check means, which is what somebody reading the policy engine for the first
time actually needs.

Each scenario starts from a fully passing input and breaks exactly one thing. That is the
discipline that makes the assertion meaningful: if a scenario failed two checks, asserting
on the primary reason would be asserting on the ordering rather than on the check.
"""

from __future__ import annotations

import dataclasses
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from revora.domain.actions import CandidateAction
from revora.domain.enums import CaseState, CheckOutcome, PolicyCheck, PolicyVerdict, RiskCause
from revora.domain.money import Minor
from revora.policy.engine import evaluate
from revora.policy.input import PolicyInput
from revora.policy.rules import default_rule_set

pytestmark = pytest.mark.pure

_NOW = datetime(2025, 6, 1, 12, 0, 0, tzinfo=UTC)

RULES = default_rule_set(
    max_recovery_attempts=3,
    max_customer_messages=2,
    cooldown_interval=timedelta(hours=24),
    policy_decision_validity=timedelta(minutes=15),
    risk_reason_codes=frozenset({"payment_risk_check_failed", "compliance_violation"}),
    min_net_value_threshold=Minor(5_000),
    min_incremental_probability=Decimal("0.05"),
)


def passing_input(**overrides: object) -> PolicyInput:
    """An input that passes all twelve checks, with named overrides.

    ``PAYMENT_LINK`` deliberately, not a null action: a null action skips the opt-out,
    consent, message-cap and cooldown checks, so a scenario built on one could not
    demonstrate them failing.
    """
    base = PolicyInput(
        case_id=uuid.uuid4(),
        merchant_id=uuid.uuid4(),
        decision_cycle=1,
        selected_action=CandidateAction.PAYMENT_LINK,
        case_state=CaseState.DECISION_PENDING,
        case_version=3,
        payment_amount=Minor(2_000_000),
        customer_key="ck-scenario",
        verified_payment_captured=False,
        verified_payment_status="failed",
        customer_opted_out=False,
        consent_expires_at=_NOW + timedelta(days=30),
        consent_recorded=True,
        risk_flagged=False,
        diagnosed_cause=RiskCause.INSUFFICIENT_FUNDS,
        human_owner_user_id=None,
        window_end_at=_NOW + timedelta(hours=48),
        executed_action_count=0,
        customer_message_count=0,
        last_outbound_at=None,
        open_intent_exists=False,
        intent_exists_for_key=False,
        evaluated_at=_NOW,
        rules_version=RULES.version_label,
        config_version="2025.01.0-assumption-baseline",
    )
    return dataclasses.replace(base, **overrides)  # type: ignore[arg-type]


def test_the_baseline_input_is_approved() -> None:
    """The control. Without this, a scenario could pass because everything fails."""
    evaluation = evaluate(passing_input(), RULES)
    assert evaluation.verdict is PolicyVerdict.APPROVED
    assert evaluation.primary_reason == "ALL_CHECKS_PASSED"
    assert all(check.passed for check in evaluation.checks)
    assert evaluation.idempotency_key is not None


# ---------------------------------------------------------------------------
# One scenario per check, in order
# ---------------------------------------------------------------------------


def test_1_already_paid_blocks() -> None:
    """The money arrived. Nothing else matters, and nothing else should be the reason."""
    evaluation = evaluate(
        passing_input(verified_payment_captured=True, verified_payment_status="captured"),
        RULES,
    )
    assert evaluation.verdict is PolicyVerdict.BLOCKED
    assert evaluation.primary_reason == PolicyCheck.ALREADY_PAID.value


def test_2_already_terminal_blocks() -> None:
    """A closed case takes no further action."""
    evaluation = evaluate(passing_input(case_state=CaseState.EXPIRED), RULES)
    assert evaluation.verdict is PolicyVerdict.BLOCKED
    assert evaluation.primary_reason == PolicyCheck.ALREADY_TERMINAL.value


def test_3_duplicate_action_blocks_on_an_unresolved_intent() -> None:
    """An intent whose outcome is unknown must not be followed by a second call.

    This is the check that stops one failed payment becoming two customer messages after a
    provider timeout.
    """
    evaluation = evaluate(passing_input(open_intent_exists=True), RULES)
    assert evaluation.verdict is PolicyVerdict.BLOCKED
    assert evaluation.primary_reason == PolicyCheck.DUPLICATE_ACTION.value


def test_3b_duplicate_action_blocks_on_an_existing_key() -> None:
    """The same action at the same attempt ordinal has already been attempted."""
    evaluation = evaluate(passing_input(intent_exists_for_key=True), RULES)
    assert evaluation.verdict is PolicyVerdict.BLOCKED
    assert evaluation.primary_reason == PolicyCheck.DUPLICATE_ACTION.value


def test_4_fraud_or_risk_escalates_rather_than_blocking_silently() -> None:
    """A risk signal needs a human, so it escalates. A silent block would orphan the case."""
    evaluation = evaluate(
        passing_input(diagnosed_cause=RiskCause.FRAUD_OR_RISK_SIGNAL), RULES
    )
    assert evaluation.verdict is PolicyVerdict.ESCALATE
    assert evaluation.primary_reason == PolicyCheck.FRAUD_OR_RISK.value


def test_4b_risk_flagged_on_the_case_also_escalates() -> None:
    """The flag and the diagnosed cause are two routes to the same conclusion."""
    evaluation = evaluate(passing_input(risk_flagged=True), RULES)
    assert evaluation.verdict is PolicyVerdict.ESCALATE
    assert evaluation.primary_reason == PolicyCheck.FRAUD_OR_RISK.value


def test_5_opted_out_blocks_and_is_checked_before_every_bound() -> None:
    """The customer asked us to stop.

    The second assertion is the important one: the input also breaks all three bounds, and
    the reason is still the opt-out. That ordering is why a bug in an attempt counter can
    never be the reason an opted-out customer gets a message.
    """
    evaluation = evaluate(
        passing_input(
            customer_opted_out=True,
            executed_action_count=99,
            customer_message_count=99,
            window_end_at=_NOW - timedelta(hours=1),
        ),
        RULES,
    )
    assert evaluation.verdict is PolicyVerdict.BLOCKED
    assert evaluation.primary_reason == PolicyCheck.CUSTOMER_OPTED_OUT.value


def test_6_missing_consent_blocks() -> None:
    """No recorded permission is a fact about the world, not a gap in our reading of it."""
    evaluation = evaluate(
        passing_input(consent_recorded=False, customer_opted_out=None), RULES
    )
    assert evaluation.verdict is PolicyVerdict.BLOCKED
    assert evaluation.primary_reason == PolicyCheck.CONSENT_MISSING.value


def test_6b_expired_consent_blocks() -> None:
    """Consent is not perpetual. A lapsed record is closer to no record than to a live one."""
    evaluation = evaluate(
        passing_input(consent_expires_at=_NOW - timedelta(days=1)), RULES
    )
    assert evaluation.verdict is PolicyVerdict.BLOCKED
    assert evaluation.primary_reason == PolicyCheck.CONSENT_MISSING.value


def test_7_human_ownership_suspends_all_automation() -> None:
    """Once a person owns the case, an automated action alongside their work is worse than
    none — they cannot tell which of them acted."""
    evaluation = evaluate(passing_input(human_owner_user_id=uuid.uuid4()), RULES)
    assert evaluation.verdict is PolicyVerdict.BLOCKED
    assert evaluation.primary_reason == PolicyCheck.HUMAN_OWNERSHIP.value


def test_8_expired_window_blocks() -> None:
    """The recovery window has closed."""
    evaluation = evaluate(passing_input(window_end_at=_NOW - timedelta(seconds=1)), RULES)
    assert evaluation.verdict is PolicyVerdict.BLOCKED
    assert evaluation.primary_reason == PolicyCheck.WINDOW_EXPIRED.value


def test_8b_the_window_boundary_is_inclusive_of_expiry() -> None:
    """A window ending exactly now is closed, not open.

    The boundary matters: an off-by-one here is an action taken microseconds outside the
    window a merchant configured.
    """
    evaluation = evaluate(passing_input(window_end_at=_NOW), RULES)
    assert evaluation.primary_reason == PolicyCheck.WINDOW_EXPIRED.value


def test_9_max_attempts_blocks() -> None:
    """The executed-action cap, at exactly the cap rather than past it."""
    evaluation = evaluate(passing_input(executed_action_count=3), RULES)
    assert evaluation.verdict is PolicyVerdict.BLOCKED
    assert evaluation.primary_reason == PolicyCheck.MAX_ATTEMPTS_REACHED.value


def test_10_max_messages_blocks() -> None:
    """The customer-message cap, which only customer-visible actions consume."""
    evaluation = evaluate(passing_input(customer_message_count=2), RULES)
    assert evaluation.verdict is PolicyVerdict.BLOCKED
    assert evaluation.primary_reason == PolicyCheck.MAX_MESSAGES_REACHED.value


def test_11_cooldown_defers_rather_than_blocking() -> None:
    """The action is legal, it is merely early — so the verdict is ``DEFERRED``.

    The only check that defers. It carries the instant the cooldown elapses so the scheduler
    does not re-derive it and possibly disagree with the decision that produced it.
    """
    last_outbound = _NOW - timedelta(hours=1)
    evaluation = evaluate(passing_input(last_outbound_at=last_outbound), RULES)
    assert evaluation.verdict is PolicyVerdict.DEFERRED
    assert evaluation.primary_reason == PolicyCheck.COOLDOWN_ACTIVE.value
    assert evaluation.earliest_permitted_at == last_outbound + timedelta(hours=24)
    assert evaluation.idempotency_key is None


def test_11b_cooldown_beyond_the_window_blocks_instead_of_deferring() -> None:
    """Deferring past the window would schedule an action that can never legally run.

    So it is refused now, with the window as the reason, rather than parked to expire
    silently — which would leave a case looking scheduled when nothing would ever happen.
    """
    evaluation = evaluate(
        passing_input(
            last_outbound_at=_NOW - timedelta(hours=1),
            window_end_at=_NOW + timedelta(hours=2),
        ),
        RULES,
    )
    assert evaluation.verdict is PolicyVerdict.BLOCKED
    assert evaluation.primary_reason == PolicyCheck.WINDOW_EXPIRED.value


def test_12_action_not_eligible_for_the_cause_blocks() -> None:
    """``UNKNOWN`` permits nothing customer-visible, so a substituted diagnosis is
    conservative rather than permissive."""
    evaluation = evaluate(passing_input(diagnosed_cause=RiskCause.UNKNOWN), RULES)
    assert evaluation.verdict is PolicyVerdict.BLOCKED
    assert evaluation.primary_reason == PolicyCheck.ACTION_NOT_ELIGIBLE.value


def test_12b_an_mvp_unavailable_action_is_not_executable() -> None:
    """``RETRY`` has no verified provider capability, so it cannot be authorized."""
    evaluation = evaluate(
        passing_input(
            selected_action=CandidateAction.RETRY,
            diagnosed_cause=RiskCause.BANK_OR_NETWORK_FAILURE,
        ),
        RULES,
    )
    assert evaluation.verdict is PolicyVerdict.BLOCKED
    assert evaluation.primary_reason == PolicyCheck.ACTION_NOT_ELIGIBLE.value


# ---------------------------------------------------------------------------
# Ordering, unavailability, and the null actions
# ---------------------------------------------------------------------------


def test_several_failures_report_the_lowest_ordered_reason() -> None:
    """Five checks fail; the reason is the first of them in the fixed order."""
    evaluation = evaluate(
        passing_input(
            verified_payment_captured=True,
            case_state=CaseState.STOPPED,
            open_intent_exists=True,
            customer_opted_out=True,
            executed_action_count=99,
        ),
        RULES,
    )
    assert evaluation.primary_reason == PolicyCheck.ALREADY_PAID.value
    assert len(evaluation.failed_checks) >= 4


def test_a_missing_diagnosis_is_unavailable_and_blocks() -> None:
    """R8.C17 — no assume-fine branch. Without a cause we cannot say the payment was *not*
    declined for risk, so the engine refuses rather than guessing."""
    evaluation = evaluate(passing_input(diagnosed_cause=None), RULES)
    assert evaluation.verdict is PolicyVerdict.BLOCKED
    assert evaluation.primary_reason == "POLICY_INPUT_UNAVAILABLE"
    assert any(c.outcome is CheckOutcome.UNAVAILABLE for c in evaluation.checks)


def test_an_unreadable_consent_record_is_unavailable_and_blocks() -> None:
    """The single most important ``UNAVAILABLE`` in the engine: a consent row that exists
    but could not be read must never be treated as "not opted out"."""
    evaluation = evaluate(
        passing_input(consent_recorded=True, customer_opted_out=None), RULES
    )
    assert evaluation.verdict is PolicyVerdict.BLOCKED
    assert evaluation.primary_reason == "POLICY_INPUT_UNAVAILABLE"


def test_do_nothing_is_approved_even_at_every_bound() -> None:
    """A null action is authorizable when everything else is exhausted.

    This is what lets Revora record a decision to stop rather than falling silent. A
    merchant who cannot see why nothing happened will conclude the system is broken.
    """
    evaluation = evaluate(
        passing_input(
            selected_action=CandidateAction.DO_NOTHING,
            diagnosed_cause=RiskCause.UNKNOWN,
            executed_action_count=99,
            customer_message_count=99,
            customer_opted_out=True,
            last_outbound_at=_NOW,
        ),
        RULES,
    )
    assert evaluation.verdict is PolicyVerdict.APPROVED


def test_an_opt_out_does_not_block_human_escalation() -> None:
    """An opt-out is a statement about being contacted, not about being helped.

    Blocking escalation would leave a customer who opted out of messages also unable to have
    a person look at their case.
    """
    evaluation = evaluate(
        passing_input(
            selected_action=CandidateAction.HUMAN_ESCALATION,
            customer_opted_out=True,
            consent_recorded=False,
            diagnosed_cause=RiskCause.UNKNOWN,
        ),
        RULES,
    )
    assert evaluation.verdict is PolicyVerdict.APPROVED
