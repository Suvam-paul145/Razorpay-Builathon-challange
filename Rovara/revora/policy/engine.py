"""``evaluate`` — the only thing in Revora that can authorize an external effect.

A pure function. No I/O, no clock read, no randomness, no logging, no database. Everything
it needs arrives in a :class:`~revora.policy.input.PolicyInput` and a
:class:`~revora.policy.rules.RuleSet`, and the same pair always yields the same decision
(R8.C14). That purity is not stylistic: it is what makes "identical inputs, identical
decision" literally true, what makes any historical decision exactly replayable from its
recorded inputs, and what lets Property 2 be tested by substituting every AI-produced
field and re-evaluating in microseconds.

**The order of the twelve checks is load-bearing.** They run in a fixed sequence and the
verdict comes from the *lowest-ordered* non-pass. Absolute prohibitions come first so that
an expensive or case-specific check can never end up being the recorded reason a paid or
opted-out customer was contacted:

1. ``ALREADY_PAID`` — the money arrived. Nothing else matters.
2. ``ALREADY_TERMINAL`` — the case is closed.
3. ``DUPLICATE_ACTION`` — an unresolved intent already exists.
4. ``FRAUD_OR_RISK`` — a human decides, not us.
5. ``CUSTOMER_OPTED_OUT`` — **before every bound**, deliberately. A bug in an attempt
   counter must not be able to leak a message to somebody who asked us to stop.
6. ``CONSENT_MISSING``
7. ``HUMAN_OWNERSHIP`` — a human owner suspends automation entirely.
8. ``WINDOW_EXPIRED``
9. ``MAX_ATTEMPTS_REACHED``
10. ``MAX_MESSAGES_REACHED``
11. ``COOLDOWN_ACTIVE`` — the only check that may defer rather than block.
12. ``ACTION_NOT_ELIGIBLE``

**There is no assume-fine branch.** Any check returning ``UNAVAILABLE`` yields
``BLOCKED`` with ``POLICY_INPUT_UNAVAILABLE`` (R8.C17). A policy engine that treated an
unreadable input as a passed check would be a policy engine that approves on missing data,
and the specific failure that produces is contacting a customer whose opt-out record
happened to be unreachable.

**All twelve outcomes are always recorded**, including the ones after the first failure.
Not "up to twelve". The engine short-circuits nothing: it evaluates every check and takes
the verdict from the lowest-ordered failure, so the stored record answers "what else would
have stopped this" as well as "what did". That question matters when a merchant changes a
bound and wants to know whether a case would now proceed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from revora.domain.actions import CandidateAction, is_customer_visible
from revora.domain.enums import (
    POLICY_CHECK_ORDER,
    POLICY_INPUT_UNAVAILABLE,
    CheckOutcome,
    PolicyCheck,
    PolicyVerdict,
    RiskCause,
)
from revora.domain.keys import execution_key
from revora.domain.transitions import is_terminal
from revora.policy.input import PolicyInput
from revora.policy.rules import RuleSet

__all__ = ["CheckResult", "PolicyEvaluation", "evaluate", "idempotency_key_for"]


@dataclass(frozen=True, slots=True)
class CheckResult:
    """One check's outcome and its position in the fixed order.

    ``detail`` is a short token, never prose and never a customer identifier. It is
    written to ``policy_check_result.detail``, which is masked at write time, but the
    cheaper guarantee is not to put anything sensitive there in the first place.
    """

    check: PolicyCheck
    order: int
    outcome: CheckOutcome
    detail: str | None = None

    @property
    def passed(self) -> bool:
        return self.outcome is CheckOutcome.PASS


@dataclass(frozen=True, slots=True)
class PolicyEvaluation:
    """The decision, its reason, and all twelve ordered check outcomes.

    ``expires_at`` is set for every verdict, not just ``APPROVED``, so a stored decision
    always has the validity window it was made under. Execution refuses an ``APPROVED``
    decision older than that window because the world it was evaluated against has had
    time to change — the customer may have paid in the meantime.
    """

    verdict: PolicyVerdict
    primary_reason: str
    checks: tuple[CheckResult, ...]
    selected_action: CandidateAction
    case_state_at_evaluation: str
    rules_version: str
    evaluated_at: datetime
    expires_at: datetime
    idempotency_key: str | None = None
    earliest_permitted_at: datetime | None = None
    """Set only on a ``DEFERRED`` verdict: when the cooldown will have elapsed. Stored so
    the scheduler does not re-derive it and possibly disagree with the decision that
    produced it."""

    @property
    def approved(self) -> bool:
        return self.verdict is PolicyVerdict.APPROVED

    @property
    def failed_checks(self) -> tuple[CheckResult, ...]:
        return tuple(check for check in self.checks if not check.passed)


def idempotency_key_for(
    *, case_id: object, action: CandidateAction, attempt_ordinal: int
) -> str:
    """The deterministic execution key: ``(case_id, action, attempt_ordinal)``.

    Derived rather than random, which is the whole basis of exactly-once execution. A
    retried execution recomputes the same key, so the provider's ``reference_id`` is the
    same, so a fetch-by-reference answers "does this effect already exist" authoritatively
    instead of a second call creating a second payment link.

    Minted here, at decision time rather than at execution time, so the key is recorded on
    the authorization itself — an approval that did not name the effect it authorizes could
    not be matched to that effect afterwards.

    The construction is :func:`revora.domain.keys.execution_key`, and it is deliberately
    *not* duplicated here. The same string is also the provider's ``reference_id``, built
    by ``revora.providers.payment_link``; this package cannot import that one (the
    ``policy-isolation`` contract forbids it, because the engine's purity is what makes
    Property 2 checkable), so the shared construction lives in ``domain`` where both can
    reach it. Two copies of this format would let the ``Idempotency_Key`` and the
    ``reference_id`` drift apart, and the failure that produces is a duplicate payment
    link that nothing detects.
    """
    return execution_key(case_id, action.value, attempt_ordinal)


def evaluate(candidate: PolicyInput, rules: RuleSet) -> PolicyEvaluation:
    """Run the twelve checks in order and return the decision. Pure.

    Args:
        candidate: every fact that may be considered. See
            :class:`~revora.policy.input.PolicyInput` — an AI-produced value has nowhere
            to sit in it.
        rules: the versioned rule set to judge against.

    Returns:
        A :class:`PolicyEvaluation` carrying the verdict, the primary reason, and all
        twelve ordered outcomes. Never raises: every input combination has a verdict,
        because a policy engine that could raise would be a policy engine whose failure
        mode is an unhandled exception in the path that authorizes money movement.
    """
    checks: list[CheckResult] = []
    for order, check in enumerate(POLICY_CHECK_ORDER, start=1):
        checks.append(_run_check(check, order, candidate, rules))
    ordered = tuple(checks)

    # Any unreadable input blocks, whatever else passed. Checked before the ordinary
    # failure scan so an UNAVAILABLE never loses to a lower-ordered FAIL — both stop the
    # action, but they call for different responses and the record must say which.
    unavailable = next(
        (c for c in ordered if c.outcome is CheckOutcome.UNAVAILABLE), None
    )
    if unavailable is not None:
        return _decision(
            candidate,
            rules,
            PolicyVerdict.BLOCKED,
            POLICY_INPUT_UNAVAILABLE,
            ordered,
        )

    failure = next((c for c in ordered if not c.passed), None)
    if failure is None:
        return _decision(
            candidate,
            rules,
            PolicyVerdict.APPROVED,
            "ALL_CHECKS_PASSED",
            ordered,
            idempotency_key=idempotency_key_for(
                case_id=candidate.case_id,
                action=candidate.selected_action,
                attempt_ordinal=candidate.executed_action_count + 1,
            ),
        )

    # Cooldown is the only check that defers rather than blocks: the action is legal, it
    # is merely early. Everything else that fails is a refusal.
    if failure.check is PolicyCheck.COOLDOWN_ACTIVE:
        earliest = _cooldown_expiry(candidate, rules)
        if earliest is not None and earliest >= candidate.window_end_at:
            # The cooldown would not elapse until after the window closes, so deferring
            # would schedule an action that can never legally run. Refuse now and say
            # why, rather than parking it to expire silently.
            return _decision(
                candidate,
                rules,
                PolicyVerdict.BLOCKED,
                PolicyCheck.WINDOW_EXPIRED.value,
                ordered,
            )
        return _decision(
            candidate,
            rules,
            PolicyVerdict.DEFERRED,
            failure.check.value,
            ordered,
            earliest_permitted_at=earliest,
        )

    # A risk signal is escalated to a human rather than merely refused: somebody has to
    # look at it, and a silent block would leave the case sitting with no owner.
    if failure.check is PolicyCheck.FRAUD_OR_RISK:
        return _decision(
            candidate, rules, PolicyVerdict.ESCALATE, failure.check.value, ordered
        )

    return _decision(
        candidate, rules, PolicyVerdict.BLOCKED, failure.check.value, ordered
    )


# ---------------------------------------------------------------------------
# The twelve checks
# ---------------------------------------------------------------------------


def _run_check(
    check: PolicyCheck, order: int, candidate: PolicyInput, rules: RuleSet
) -> CheckResult:
    """Dispatch one check. Every branch returns; there is no default pass."""
    match check:
        case PolicyCheck.ALREADY_PAID:
            return _already_paid(order, candidate)
        case PolicyCheck.ALREADY_TERMINAL:
            return _already_terminal(order, candidate)
        case PolicyCheck.DUPLICATE_ACTION:
            return _duplicate_action(order, candidate)
        case PolicyCheck.FRAUD_OR_RISK:
            return _fraud_or_risk(order, candidate)
        case PolicyCheck.CUSTOMER_OPTED_OUT:
            return _opted_out(order, candidate)
        case PolicyCheck.CONSENT_MISSING:
            return _consent(order, candidate, rules)
        case PolicyCheck.HUMAN_OWNERSHIP:
            return _human_ownership(order, candidate)
        case PolicyCheck.WINDOW_EXPIRED:
            return _window(order, candidate)
        case PolicyCheck.MAX_ATTEMPTS_REACHED:
            return _max_attempts(order, candidate, rules)
        case PolicyCheck.MAX_MESSAGES_REACHED:
            return _max_messages(order, candidate, rules)
        case PolicyCheck.COOLDOWN_ACTIVE:
            return _cooldown(order, candidate, rules)
        case PolicyCheck.ACTION_NOT_ELIGIBLE:
            return _eligibility(order, candidate, rules)


def _already_paid(order: int, candidate: PolicyInput) -> CheckResult:
    """1. The payment is already captured, so there is nothing to recover.

    ``verified_payment_captured is None`` is read as a pass rather than as
    ``UNAVAILABLE``, and this is the one place absence is treated as a negative. The
    justification: a case is only ever opened from a ``failed`` payment event, so on a
    fresh case the absence of a capture read is genuine evidence that no capture has been
    observed rather than a gap in what we could read. Treating it as ``UNAVAILABLE`` would
    block every case at its first decision, which would make the system inert.
    """
    if candidate.verified_payment_captured is True:
        return CheckResult(
            PolicyCheck.ALREADY_PAID, order, CheckOutcome.FAIL, "verified_captured"
        )
    return CheckResult(PolicyCheck.ALREADY_PAID, order, CheckOutcome.PASS)


def _already_terminal(order: int, candidate: PolicyInput) -> CheckResult:
    """2. A closed case takes no further action.

    Read from ``domain.transitions.TERMINAL_STATES`` rather than from a list here, so the
    check and the state machine cannot disagree about what "closed" means.
    """
    if is_terminal(candidate.case_state):
        return CheckResult(
            PolicyCheck.ALREADY_TERMINAL, order, CheckOutcome.FAIL, candidate.case_state.value
        )
    return CheckResult(PolicyCheck.ALREADY_TERMINAL, order, CheckOutcome.PASS)


def _duplicate_action(order: int, candidate: PolicyInput) -> CheckResult:
    """3. An unresolved intent already exists, so a second call risks a duplicate effect.

    Two conditions, both fatal. An intent for this exact idempotency key means this action
    at this ordinal was already attempted. An open ``ATTEMPTED`` or ``UNCERTAIN`` intent
    for the case means *something* was attempted and its outcome is unknown — and while an
    outcome is unknown, issuing another external call is how one failed payment becomes
    two customer messages.
    """
    if candidate.intent_exists_for_key:
        return CheckResult(
            PolicyCheck.DUPLICATE_ACTION, order, CheckOutcome.FAIL, "intent_exists_for_key"
        )
    if candidate.open_intent_exists:
        return CheckResult(
            PolicyCheck.DUPLICATE_ACTION, order, CheckOutcome.FAIL, "unresolved_intent"
        )
    return CheckResult(PolicyCheck.DUPLICATE_ACTION, order, CheckOutcome.PASS)


def _fraud_or_risk(order: int, candidate: PolicyInput) -> CheckResult:
    """4. A risk signal blocks automation and escalates to a human.

    Derived from the recorded diagnosis cause and the case's ``risk_flagged`` column, not
    from a provider flag field — no such field exists, which is why the fraud condition is
    a configured reason set resolved upstream in the diagnosis layer.

    A missing diagnosis is ``UNAVAILABLE``, not a pass: without a cause we cannot say the
    payment was *not* declined for risk, and approving an action on that basis is the
    trade this system does not make.
    """
    if candidate.diagnosed_cause is None:
        return CheckResult(
            PolicyCheck.FRAUD_OR_RISK, order, CheckOutcome.UNAVAILABLE, "no_active_diagnosis"
        )
    if candidate.risk_flagged:
        return CheckResult(
            PolicyCheck.FRAUD_OR_RISK, order, CheckOutcome.FAIL, "risk_flagged"
        )
    if candidate.diagnosed_cause is RiskCause.FRAUD_OR_RISK_SIGNAL:
        return CheckResult(
            PolicyCheck.FRAUD_OR_RISK, order, CheckOutcome.FAIL, "fraud_or_risk_signal"
        )
    return CheckResult(PolicyCheck.FRAUD_OR_RISK, order, CheckOutcome.PASS)


def _opted_out(order: int, candidate: PolicyInput) -> CheckResult:
    """5. The customer asked not to be contacted. Checked before every bound.

    Fifth of twelve, ahead of the window and all three counters, and that ordering is a
    safety property rather than an optimization: a bug in an attempt counter can then
    never be the reason an opted-out customer receives a message, because the opt-out
    check has already refused.

    The opt-out applies to *customer-visible* actions. ``DO_NOTHING``, ``WAIT`` and
    ``HUMAN_ESCALATION`` are unaffected — an opt-out is a statement about being contacted,
    not about being helped, and blocking escalation would leave a customer who opted out
    of messages also unable to have a human look at their case.

    An unreadable consent record is ``UNAVAILABLE``. This is the single most important
    ``UNAVAILABLE`` in the engine.
    """
    if not _is_customer_visible_for(candidate):
        return CheckResult(
            PolicyCheck.CUSTOMER_OPTED_OUT, order, CheckOutcome.PASS, "not_customer_visible"
        )
    if candidate.customer_opted_out is None and candidate.consent_recorded:
        return CheckResult(
            PolicyCheck.CUSTOMER_OPTED_OUT, order, CheckOutcome.UNAVAILABLE, "consent_unreadable"
        )
    if candidate.customer_opted_out is True:
        return CheckResult(
            PolicyCheck.CUSTOMER_OPTED_OUT, order, CheckOutcome.FAIL, "opted_out"
        )
    return CheckResult(PolicyCheck.CUSTOMER_OPTED_OUT, order, CheckOutcome.PASS)


def _consent(order: int, candidate: PolicyInput, rules: RuleSet) -> CheckResult:
    """6. Consent must exist and must not have lapsed, for a customer-visible action.

    A missing record fails rather than being treated as ``UNAVAILABLE``: absence of
    consent is a fact about the world, not a gap in our reading of it, and the correct
    response to "we never recorded permission" is to refuse.
    """
    if not _is_customer_visible_for(candidate):
        return CheckResult(
            PolicyCheck.CONSENT_MISSING, order, CheckOutcome.PASS, "not_customer_visible"
        )
    if not candidate.consent_recorded:
        return CheckResult(
            PolicyCheck.CONSENT_MISSING, order, CheckOutcome.FAIL, "no_consent_record"
        )
    if candidate.consent_expired:
        return CheckResult(
            PolicyCheck.CONSENT_MISSING, order, CheckOutcome.FAIL, "consent_expired"
        )
    return CheckResult(PolicyCheck.CONSENT_MISSING, order, CheckOutcome.PASS)


def _human_ownership(order: int, candidate: PolicyInput) -> CheckResult:
    """7. A human owner suspends automated action entirely.

    Not just customer-visible action — everything. Once a person has taken a case, an
    automated decision arriving alongside their work is worse than no automation at all,
    because they cannot tell which of them acted.
    """
    if candidate.human_owner_user_id is not None:
        return CheckResult(
            PolicyCheck.HUMAN_OWNERSHIP, order, CheckOutcome.FAIL, "human_owner_assigned"
        )
    return CheckResult(PolicyCheck.HUMAN_OWNERSHIP, order, CheckOutcome.PASS)


def _window(order: int, candidate: PolicyInput) -> CheckResult:
    """8. The recovery window must still be open.

    Compared against the *persisted* window end, never against a recomputation from the
    current ``RECOVERY_WINDOW_DURATION``. Changing the bound must not retroactively reopen
    or expire a live case.
    """
    if candidate.window_expired:
        return CheckResult(
            PolicyCheck.WINDOW_EXPIRED, order, CheckOutcome.FAIL, "window_elapsed"
        )
    return CheckResult(PolicyCheck.WINDOW_EXPIRED, order, CheckOutcome.PASS)


def _max_attempts(order: int, candidate: PolicyInput, rules: RuleSet) -> CheckResult:
    """9. The executed-action cap.

    A null action does not consume an attempt, so the cap does not apply to it. Otherwise
    a case at its cap could not even record a decision to stop.
    """
    if candidate.selected_action in rules.null_actions:
        return CheckResult(
            PolicyCheck.MAX_ATTEMPTS_REACHED, order, CheckOutcome.PASS, "null_action"
        )
    if candidate.executed_action_count >= rules.max_recovery_attempts:
        return CheckResult(
            PolicyCheck.MAX_ATTEMPTS_REACHED,
            order,
            CheckOutcome.FAIL,
            f"count={candidate.executed_action_count}",
        )
    return CheckResult(PolicyCheck.MAX_ATTEMPTS_REACHED, order, CheckOutcome.PASS)


def _max_messages(order: int, candidate: PolicyInput, rules: RuleSet) -> CheckResult:
    """10. The customer-message cap, applied only to customer-visible actions."""
    if not _is_customer_visible_for(candidate):
        return CheckResult(
            PolicyCheck.MAX_MESSAGES_REACHED, order, CheckOutcome.PASS, "not_customer_visible"
        )
    if candidate.customer_message_count >= rules.max_customer_messages:
        return CheckResult(
            PolicyCheck.MAX_MESSAGES_REACHED,
            order,
            CheckOutcome.FAIL,
            f"count={candidate.customer_message_count}",
        )
    return CheckResult(PolicyCheck.MAX_MESSAGES_REACHED, order, CheckOutcome.PASS)


def _cooldown(order: int, candidate: PolicyInput, rules: RuleSet) -> CheckResult:
    """11. The minimum gap between outbound actions. The only check that may defer.

    Measured from ``last_outbound_at``, which is set at the transition into ``EXECUTING``
    *before* the provider request. So a crash mid-call still costs the cooldown, which is
    the conservative direction: the alternative is a crash loop that issues a call, fails
    to record it, and immediately issues another.

    A null action is exempt — it sends nothing, so there is nothing to space out.
    """
    if candidate.selected_action in rules.null_actions:
        return CheckResult(
            PolicyCheck.COOLDOWN_ACTIVE, order, CheckOutcome.PASS, "null_action"
        )
    if candidate.last_outbound_at is None:
        return CheckResult(
            PolicyCheck.COOLDOWN_ACTIVE, order, CheckOutcome.PASS, "no_prior_outbound"
        )
    if candidate.evaluated_at < candidate.last_outbound_at + rules.cooldown_interval:
        return CheckResult(
            PolicyCheck.COOLDOWN_ACTIVE, order, CheckOutcome.FAIL, "cooldown_active"
        )
    return CheckResult(PolicyCheck.COOLDOWN_ACTIVE, order, CheckOutcome.PASS)


def _eligibility(order: int, candidate: PolicyInput, rules: RuleSet) -> CheckResult:
    """12. The action must be eligible for the diagnosed cause and executable at all.

    Last of the twelve because it is the most case-specific: it is the check most likely
    to be the *only* reason an action is refused, and putting it last means a genuinely
    prohibited action is always refused for the prohibition rather than for eligibility.
    """
    if candidate.diagnosed_cause is None:
        return CheckResult(
            PolicyCheck.ACTION_NOT_ELIGIBLE, order, CheckOutcome.UNAVAILABLE, "no_diagnosis"
        )
    if not rules.is_executable(candidate.selected_action):
        return CheckResult(
            PolicyCheck.ACTION_NOT_ELIGIBLE, order, CheckOutcome.FAIL, "not_executable"
        )
    if not rules.permits_action_for_cause(
        candidate.selected_action, candidate.diagnosed_cause
    ):
        return CheckResult(
            PolicyCheck.ACTION_NOT_ELIGIBLE, order, CheckOutcome.FAIL, "cause_not_eligible"
        )
    return CheckResult(PolicyCheck.ACTION_NOT_ELIGIBLE, order, CheckOutcome.PASS)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _is_customer_visible_for(candidate: PolicyInput) -> bool:
    """Whether the action under evaluation reaches the customer.

    Read from ``domain.actions`` rather than hard-coded, so the opt-out, consent and
    message-cap checks all agree on which actions the customer perceives.
    """
    return is_customer_visible(candidate.selected_action)


def _cooldown_expiry(candidate: PolicyInput, rules: RuleSet) -> datetime | None:
    """When the cooldown will have elapsed, for a deferred decision."""
    if candidate.last_outbound_at is None:
        return None
    return candidate.last_outbound_at + rules.cooldown_interval


def _decision(
    candidate: PolicyInput,
    rules: RuleSet,
    verdict: PolicyVerdict,
    primary_reason: str,
    checks: tuple[CheckResult, ...],
    *,
    idempotency_key: str | None = None,
    earliest_permitted_at: datetime | None = None,
) -> PolicyEvaluation:
    """Assemble the evaluation. The one place a decision object is built."""
    return PolicyEvaluation(
        verdict=verdict,
        primary_reason=primary_reason,
        checks=checks,
        selected_action=candidate.selected_action,
        case_state_at_evaluation=candidate.case_state.value,
        rules_version=rules.version_label,
        evaluated_at=candidate.evaluated_at,
        expires_at=candidate.evaluated_at + rules.policy_decision_validity,
        idempotency_key=idempotency_key,
        earliest_permitted_at=earliest_permitted_at,
    )
