"""Read models for the dashboard. Every figure leaves here already formatted and already labelled.

This module is where a stored row becomes something a browser may render, and it exists as its own
layer rather than inline in the routers for one reason: **the honesty rules are the hard part, and
they belong in one place.** Four of them run through everything below.

**No money leaves as a bare integer.** :func:`revora.api.rendering.money` emits the formatted
string and the minor units together, so a client that renders the string is doing the easy thing
and a client that does arithmetic has to go out of its way (R14.C12).

**An absent value names what is absent and what state the case is in.** Never zero, never a dash.
A case with no recommendation gets a marker saying so and naming ``DETECTED`` or ``BLOCKED``, and
those two mean opposite things — the first is "not yet", the second is "policy stopped it"
(R14.C15).

**A refusal is rendered as fully as an action.** Where the optimizer selected ``DO_NOTHING`` or
``WAIT``, :func:`case_detail` returns the recorded reason *plus* the baseline probability, the
incremental probability, the net value and all three compared thresholds. A refusal shown as an
empty row or a red dot is how a merchant concludes the product is broken when it is working — and
"we decided not to spend your money on this customer" is the single most defensible thing Revora
says.

**Every candidate is returned, excluded ones included** (R6.C9, R7.C8). The case detail *is* the
comparison, not the winner. An excluded action carries its exclusion reason and its figures, so
"a retry was considered and is not available on this account" is visible rather than being an
absence nobody can interpret.

Nothing here computes a figure. Every number is read from a row that some component committed, and
the two derived values that do appear — a cost total and a net value — are integer sums of columns
sitting next to each other. A read model that computed would be a second implementation of the
arithmetic, free to disagree with the stored one.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Final

from revora.api.rendering import (
    HARD_STOP_LABELS,
    MoneyField,
    case_state_label,
    data_unavailable,
    money,
    not_yet_recorded,
    rate,
    selected_action_label,
)
from revora.customer.suppression import scope_key_for_case
from revora.domain.actions import CandidateAction
from revora.domain.enums import (
    POLICY_CHECK_ORDER,
    CaseState,
    EstimationMethod,
    SelectionReason,
)
from revora.domain.failure_taxonomy import EVIDENCE_SOURCE
from revora.metrics.engine import CohortMetrics
from revora.metrics.unresolved import unresolved_groups
from revora.persistence.repositories.audit import AuditRecordRepository
from revora.persistence.repositories.cases import RecoveryCaseRepository
from revora.persistence.repositories.consent import CustomerConsentRepository
from revora.persistence.repositories.customer import (
    ContactSuppressionRepository,
    CustomerSignalRepository,
)
from revora.persistence.repositories.diagnosis import DiagnosisRepository
from revora.persistence.repositories.estimates import BaselineEstimateRepository
from revora.persistence.repositories.execution import (
    ExecutionIntentRepository,
    PaymentStateReadRepository,
    RecoveryOutcomeRepository,
)
from revora.persistence.repositories.experiments import (
    ExperimentResultRepository,
)
from revora.persistence.repositories.policy import PolicyDecisionRepository
from revora.persistence.repositories.recommendations import RecommendationRepository
from revora.platform.clock import now
from revora.timeline.stages import (
    REVIEW_DECISION_KEYS,
    AuditRecordView,
    CaseView,
    FigureView,
    IntentView,
    SignalView,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sqlalchemy.orm import Session

    from revora.persistence.models import (
        AuditRecord,
        BaselineEstimate,
        Diagnosis,
        Experiment,
        ExperimentResult,
        PolicyCheckResult,
        PolicyDecision,
        Recommendation,
        RecommendationCandidate,
        RecoveryCase,
    )
    from revora.platform.config import Configuration

__all__ = [
    "NULL_SELECTION_REASONS",
    "TimelineInputs",
    "audit_document",
    "case_detail",
    "case_summary",
    "experiment_document",
    "metrics_document",
    "timeline_inputs",
    "unresolved_view",
]

NULL_SELECTION_REASONS: frozenset[str] = frozenset(
    {SelectionReason.NO_POSITIVE_VALUE.value, SelectionReason.HIGH_BASELINE_NO_INTERVENTION.value}
)
"""The two reasons that mean Revora deliberately did not act.

Both get the full refusal block. They are not interchangeable and the block says which: one is
"nothing was worth doing", the other is "this customer was going to pay anyway". A merchant shown
the same words for both learns nothing from either."""


def case_summary(
    session: Session,
    merchant_id: uuid.UUID,
    case: RecoveryCase,
    *,
    config: Configuration,
) -> dict[str, object]:
    """One row of the case list (R14.C2, R26.C14, R30.C13).

    Deliberately a per-case read rather than one joined query. The list is bounded by
    ``DASHBOARD_PAGE_SIZE`` — a hundred by default — and a hundred small indexed reads is a
    cost worth paying for a list whose columns come from five tables and where a join would have to
    pick, for each of them, which of several rows is "the" one. That choice is exactly what the
    repository methods below already encode, and duplicating it in SQL is how a list column starts
    disagreeing with the detail page it links to.

    ``config`` is here for one figure: the decision-cycle cap that R30.C13's *actively waiting*
    condition compares against. It is a per-merchant configured bound, so the row cannot answer it
    alone and the caller already holds the accessor.
    """
    currency = str(case.currency)
    recommendation = RecommendationRepository(session).latest_for_case(merchant_id, case.id)
    outcome = RecoveryOutcomeRepository(session).for_case(merchant_id, case.id)
    diagnosis = DiagnosisRepository(session).active_for_case(merchant_id, case.id)
    intents = ExecutionIntentRepository(session).list_for_case(merchant_id, case.id)
    # Same cycle derivation as `case_detail`, for the same reason: the case counter and the cycle a
    # decision was recorded under are not the same number, and a list column that disagreed with the
    # detail page it links to is worse than either being wrong alone.
    decisions = PolicyDecisionRepository(session).for_cycle(
        merchant_id,
        case.id,
        int(recommendation.decision_cycle)
        if recommendation is not None
        else int(case.decision_cycle_count),
    )
    state = str(case.state)
    selected = None if recommendation is None else str(recommendation.selected_action)

    return {
        "case_id": str(case.id),
        "state": state,
        # R26.C14. The label is chosen server-side and the stored member travels beside it, so a
        # reader sees both and the browser composes neither.
        "state_label": case_state_label(state),
        "detected_at": case.detected_at.isoformat(),
        "window_end_at": case.window_end_at.isoformat(),
        "payment_amount": money(int(case.payment_amount), currency=currency),
        "provider_payment_id": str(case.provider_payment_id),
        "customer_contact_masked": case.customer_contact_masked,
        "risk_cause": (
            str(diagnosis.cause) if diagnosis is not None else not_yet_recorded(state, "diagnosis")
        ),
        "selected_action": (
            selected if selected is not None else not_yet_recorded(state, "recommendation")
        ),
        "selected_action_label": (
            None if selected is None else selected_action_label(selected)
        ),
        # R30.C13. Present when the case is genuinely still being worked, absent otherwise — and
        # absent is `None` rather than a block of zeros, on the same terms as every other absence
        # in this module.
        "waiting": _waiting_summary(case, selected, config=config),
        "executed_action": _executed_action_summary(intents, state),
        "policy_decision": (
            str(decisions[-1].verdict)
            if decisions
            else not_yet_recorded(state, "policy decision")
        ),
        "recovered_amount": (
            money(int(outcome.recovered_amount), currency=currency)
            if outcome is not None
            else not_yet_recorded(state, "recovered amount")
        ),
        "outcome_classification": (
            str(outcome.classification)
            if outcome is not None
            else not_yet_recorded(state, "outcome")
        ),
        "human_owner_user_id": (
            None if case.human_owner_user_id is None else str(case.human_owner_user_id)
        ),
        "provenance": str(case.provenance),
    }


def _waiting_summary(
    case: RecoveryCase, selected: str | None, *, config: Configuration
) -> dict[str, object] | None:
    """R30.C13. A case that chose restraint, presented as still being worked.

    Returns ``None`` unless all three of the requirement's conditions hold: the case is at
    ``POLICY_CHECK``, it carries a ``next_review_at`` later than now, and its decision-cycle
    counter is below ``MAX_RECOVERY_ATTEMPTS``. All three are needed and none is redundant. Without
    the state check a terminal case with a stale instant would render as waiting — it cannot happen,
    because :func:`revora.cases.manager.apply_locked_transition` clears the instant on every edge
    out of ``POLICY_CHECK``, and this check is what stops that guarantee being load-bearing *here*.
    Without the instant check a case whose review is overdue would claim a future appointment.
    Without the counter check a case at the cap would claim a review that R30.C10 will refuse.

    **Why this block exists at all.** ``POLICY_CHECK`` with a null selection is the state R30 was
    written about: correctly non-terminal, and until the review loop existed, behaviourally
    identical to abandonment. A list that showed it as a bare state name and an empty "executed"
    cell said nothing about whether anything would ever happen next — so a merchant reading it drew
    the same conclusion the old implementation had actually reached. Naming the instant is what
    turns restraint into a visible appointment.

    Deliberately carries no money. Every figure in the block is a timestamp, an enumeration member
    or a small count, so there is nothing here for a client to do arithmetic on.
    """
    if CaseState(case.state) is not CaseState.POLICY_CHECK:
        return None
    review_at = case.next_review_at
    if review_at is None or review_at <= now():
        return None
    cycles = int(case.decision_cycle_count)
    cap = int(config.MAX_RECOVERY_ATTEMPTS)
    if cycles >= cap:
        return None
    return {
        "next_review_at": review_at.isoformat(),
        "selected_action": selected,
        "selected_action_label": (
            None if selected is None else selected_action_label(selected)
        ),
        "decision_cycle_count": cycles,
        "max_recovery_attempts": cap,
        "detail": (
            "Revora decided not to act this cycle and will look at this case again at the "
            "instant above. The case has not ended."
        ),
    }


def _executed_action_summary(intents: Sequence[object], state: str) -> object:
    """The most recent confirmed action, or a marker.

    "No executed action" is not a failure and must not render as one: on a ``RECOVERED`` case it
    means the customer paid without being contacted, which is the outcome the product most wants
    and the one a naive display would show as a blank.
    """
    confirmed = [intent for intent in intents if str(getattr(intent, "state", "")) == "CONFIRMED"]
    if not confirmed:
        return not_yet_recorded(state, "executed action")
    latest = confirmed[-1]
    return str(getattr(latest, "action", ""))


def case_detail(
    session: Session,
    merchant_id: uuid.UUID,
    case: RecoveryCase,
    *,
    config: Configuration,
) -> dict[str, object]:
    """The whole decision trail for one case (R14.C3 through C6, R14.C14, R11.C5).

    Everything a merchant would need to answer "why did Revora do that", in the order the pipeline
    produced it: what failed, what it was diagnosed as, what the baseline said, every candidate
    that was priced, which one was selected and why, what policy decided across all twelve checks,
    what was actually executed, and what came back.
    """
    currency = str(case.currency)
    state = str(case.state)

    diagnosis = DiagnosisRepository(session).active_for_case(merchant_id, case.id)
    baseline = BaselineEstimateRepository(session).latest_for_case(merchant_id, case.id)
    recommendation = RecommendationRepository(session).latest_for_case(merchant_id, case.id)
    # The cycle to read decisions for comes from the recommendation when there is one, not from the
    # case counter. They diverge: the counter is incremented when a cycle *starts*, and the
    # recommendation and its policy decision are written under the cycle the optimizer ran in. Using
    # the counter showed a fully evaluated case as having no policy decision — a false absence
    # arriving through the read model rather than through a missing row, which is exactly the
    # failure R14.C15 exists to prevent, one level up.
    cycle = (
        int(recommendation.decision_cycle)
        if recommendation is not None
        else int(case.decision_cycle_count)
    )
    decisions = PolicyDecisionRepository(session).for_cycle(merchant_id, case.id, cycle)
    intents = ExecutionIntentRepository(session).list_for_case(merchant_id, case.id)
    reads = PaymentStateReadRepository(session).list_for_case(merchant_id, case.id)
    outcome = RecoveryOutcomeRepository(session).for_case(merchant_id, case.id)
    consent = CustomerConsentRepository(session).for_customer(
        merchant_id, str(case.customer_key)
    )

    return {
        "case": case_summary(session, merchant_id, case, config=config),
        "counters": {
            "executed_action_count": int(case.executed_action_count),
            "customer_message_count": int(case.customer_message_count),
            "decision_cycle_count": cycle,
            "max_recovery_attempts": int(config.MAX_RECOVERY_ATTEMPTS),
            "max_customer_messages": int(config.MAX_CUSTOMER_MESSAGES),
            "last_outbound_at": (
                None if case.last_outbound_at is None else case.last_outbound_at.isoformat()
            ),
        },
        "diagnosis": (
            {
                "cause": str(diagnosis.cause),
                "confidence": str(diagnosis.confidence),
                "method": str(diagnosis.method),
                "evidence": diagnosis.evidence,
                "substituted_to_unknown": bool(diagnosis.substituted_to_unknown),
                "decision_cycle": int(diagnosis.decision_cycle),
                "recorded_at": diagnosis.created_at.isoformat(),
                # Named explicitly so the dashboard can state it rather than implying it by the
                # absence of an AI badge. R3.C1's claim is that the deterministic path needs no
                # model at all, and a surface that only shows AI when present cannot express
                # "this decision involved none".
                "ai_involved": diagnosis.ai_invocation_id is not None,
            }
            if diagnosis is not None
            else not_yet_recorded(state, "diagnosis")
        ),
        "baseline": (
            {
                "probability": str(baseline.probability),
                "interval": (
                    None
                    if baseline.ci_low is None
                    else f"[{baseline.ci_low}, {baseline.ci_high}]"
                ),
                "uncertainty_available": bool(baseline.uncertainty_available),
                "segment_id": baseline.segment_id,
                "method": str(baseline.method),
                "validation_status": str(baseline.validation_status),
                "provenance": str(baseline.provenance),
            }
            if baseline is not None
            else not_yet_recorded(state, "baseline estimate")
        ),
        "recommendation": _recommendation_document(
            session,
            merchant_id,
            recommendation,
            baseline_probability=None if baseline is None else str(baseline.probability),
            currency=currency,
            state=state,
            config=config,
        ),
        "policy_decisions": [
            _policy_document(session, merchant_id, decision) for decision in decisions
        ]
        or not_yet_recorded(state, "policy decision"),
        "executed_actions": [
            {
                "intent_id": str(intent.id),
                "action": str(intent.action),
                "attempt_ordinal": int(intent.attempt_ordinal),
                "state": str(intent.state),
                "attempt_started_at": intent.attempt_started_at.isoformat(),
                "resolved_at": (
                    None if intent.resolved_at is None else intent.resolved_at.isoformat()
                ),
                "provider_failure_code": intent.provider_failure_code,
                # A bearer capability: whoever holds the URL can pay. Shown here because the
                # dashboard is authenticated, and never written to a log or an audit record.
                "provider_short_url": intent.provider_short_url,
                "is_post_payment": bool(intent.is_post_payment),
                "idempotency_key": str(intent.idempotency_key),
            }
            for intent in intents
        ]
        or not_yet_recorded(state, "executed action"),
        "authoritative_reads": [
            {
                "read_at": read.read_at.isoformat(),
                "status": str(read.status),
                "captured": bool(read.captured),
                "amount": money(int(read.amount), currency=currency),
                "amount_refunded": money(int(read.amount_refunded), currency=currency),
                "attempt_no": int(read.attempt_no),
            }
            for read in reads
        ]
        or not_yet_recorded(state, "authoritative provider read"),
        "outcome": (
            {
                "classification": str(outcome.classification),
                "recovered_amount": money(int(outcome.recovered_amount), currency=currency),
                "recovery_timestamp": outcome.recovery_timestamp.isoformat(),
                "seconds_to_recovery": outcome.seconds_to_recovery,
                # The read that verified it. A recovery with no backing read cannot exist —
                # the column is NOT NULL — and surfacing the id is what makes the figure
                # checkable rather than merely asserted.
                "verified_by_read_id": str(outcome.verified_by_read_id),
                "reconciled_from_terminal_state": outcome.reconciled_from_terminal_state,
            }
            if outcome is not None
            else not_yet_recorded(state, "verified outcome")
        ),
        "consent": (
            {
                "opted_out": bool(consent.opted_out),
                "source": consent.source,
                "effective_at": consent.effective_at.isoformat(),
                "consent_expires_at": (
                    None
                    if consent.consent_expires_at is None
                    else consent.consent_expires_at.isoformat()
                ),
            }
            if consent is not None
            else not_yet_recorded(state, "consent record")
        ),
        # R21.C11's case-level half, and it sits beside ``consent`` on purpose. R21.C9's whole
        # point is that a Contact_Suppression and a customer-wide opt-out are different
        # statements, and the surface where a merchant is most likely to conflate them is the one
        # that shows only one of the two. Adjacent, both always present, so "objected to this
        # debt" and "asked not to be contacted at all" are visibly separate facts.
        "contact_suppression": _suppression_document(session, merchant_id, case),
        "terminal_reason": case.terminal_reason,
    }


def _suppression_document(
    session: Session, merchant_id: uuid.UUID, case: RecoveryCase
) -> dict[str, object] | None:
    """The Contact_Suppression covering this case's scope, released or not. ``None`` if none.

    Uses :meth:`~revora.persistence.repositories.customer.ContactSuppressionRepository.for_scope`
    rather than ``in_force``, and the difference is the whole reason both reads exist. The policy
    hot path must not see a released suppression — a released one is history and contact is
    permitted again. A *reader* must see it: "this case was suppressed and Kavya lifted it on
    Tuesday" is exactly what a merchant looking at a case needs, and a view that only showed
    unreleased rows would present a released case as one that had never objected at all.

    ``released_by_user_id`` is surfaced because R21.C2 makes a release name a person, and a named
    person nobody can read is an accountability record that discharges nobody.

    The scope key is included. It is a digest, so it discloses no customer identifier, and it is
    the join a merchant needs to ask "what else is suppressed for this customer and this order" —
    which is R21.C9's second clause, the one about being able to *distinguish* an objection to one
    debt from a withdrawal of consent.
    """
    suppression = ContactSuppressionRepository(session).for_scope(
        merchant_id, scope_key_for_case(case)
    )
    if suppression is None:
        return None
    return {
        "scope_key": suppression.scope_key,
        "hard_stop_reason": str(suppression.hard_stop_reason),
        "hard_stop_label": HARD_STOP_LABELS.get(
            str(suppression.hard_stop_reason), str(suppression.hard_stop_reason)
        ),
        "suppressed_at": suppression.suppressed_at.isoformat(),
        "origin_case_id": str(suppression.origin_case_id),
        # True when the objection was recorded against a *different* case in the same scope, which
        # is R21.C8 being visible rather than merely working: this case is blocked because of
        # something the customer said about another payment on the same order.
        "inherited": str(suppression.origin_case_id) != str(case.id),
        "released_at": (
            None
            if suppression.released_at is None
            else suppression.released_at.isoformat()
        ),
        "released_by_user_id": (
            None
            if suppression.released_by_user_id is None
            else str(suppression.released_by_user_id)
        ),
        "release_config_version": suppression.release_config_version,
        "in_force": suppression.released_at is None,
    }


def _recommendation_document(
    session: Session,
    merchant_id: uuid.UUID,
    recommendation: Recommendation | None,
    *,
    baseline_probability: str | None,
    currency: str,
    state: str,
    config: Configuration,
) -> dict[str, object]:
    """The recommendation, every candidate, and the refusal block where nothing was selected."""
    if recommendation is None:
        return not_yet_recorded(state, "recommendation")

    candidates = RecommendationRepository(session).candidates_for(
        merchant_id, recommendation.id
    )
    reason = str(recommendation.selection_reason)
    selected = str(recommendation.selected_action)
    selected_candidate = next(
        (item for item in candidates if str(item.action) == selected), None
    )

    document: dict[str, object] = {
        "recommendation_id": str(recommendation.id),
        "decision_cycle": int(recommendation.decision_cycle),
        "selected_action": selected,
        "selection_reason": reason,
        "divergence_reason": recommendation.divergence_reason,
        "substituted_risk_cause": recommendation.substituted_risk_cause,
        "substitution_reason": recommendation.substitution_reason,
        # Prose, and labelled as prose. No figure is derived from it and the dashboard must
        # present it as an explanation rather than as evidence.
        "ai_explanation_text": recommendation.ai_explanation_text,
        "ai_explanation_is_advisory": True,
        "candidates": [
            _candidate_document(item, currency=currency) for item in candidates
        ],
        "candidate_count": len(candidates),
    }

    if reason in NULL_SELECTION_REASONS:
        document["refusal"] = _refusal_block(
            reason=reason,
            baseline_probability=baseline_probability,
            selected=selected_candidate,
            config=config,
        )
    return document


COST_SPLIT_NOT_MEASURED: Final[str] = EstimationMethod.COST_SPLIT_NOT_MEASURED.value
"""The marking R31.C10 requires beside a migrated cost split.

Named here rather than compared inline because two fields carry it and the dashboard reads a
single boolean off the pair — see :func:`_candidate_document`."""


def _candidate_document(
    candidate: RecommendationCandidate, *, currency: str
) -> dict[str, object]:
    """One candidate with all six figures R14.C4 names, and the cost split R31.C7 names.

    Excluded candidates are here too, with their reason. An action that could not be used is
    different evidence from an action that was not worth using, and both are different from an
    action that never appears.

    **Four cost figures and the total, never the total instead of them** (R31.C7). The blended
    ``action_cost`` this replaced made an exclusion under ``MAX_COST_TO_VALUE_RATIO``
    unattributable: a payment link excluded because links are expensive and one excluded because
    messages are expensive presented identically. Emitting only ``total_action_cost`` would put
    that ambiguity straight back, so the four terms and their sum all go out, each formatted.

    **The sum is computed here and not in the browser** (R14.C12). Every money figure crosses the
    boundary pre-formatted precisely so a client never has to add two of them, and ``web``'s
    ``no-restricted-syntax`` rules reject arithmetic on ``.minor`` to keep it that way. A total
    derived in JSX would be free to disagree with the ``net_recovery_value`` sitting beside it.

    **A migrated row says so, beside the two figures the migration wrote** (R31.C10).
    ``cost_split_not_measured`` is derived server-side from the two per-figure method labels rather
    than left to the client to compare, so there is one definition of "this split was never
    measured" and the dashboard renders a marking rather than deciding what one is.
    """
    action = str(candidate.action)
    financial_cost = int(candidate.financial_cost)
    communication_cost = int(candidate.communication_cost)
    risk_cost = int(candidate.risk_cost)
    customer_cost = int(candidate.customer_cost)
    financial_method = candidate.financial_cost_method
    communication_method = candidate.communication_cost_method
    return {
        "action": action,
        "is_executable": CandidateAction(action) in _EXECUTABLE,
        "incremental_probability": str(candidate.incremental_probability),
        "expected_incremental_revenue": money(
            int(candidate.expected_incremental_revenue), currency=currency
        ),
        "financial_cost": money(financial_cost, currency=currency),
        "communication_cost": money(communication_cost, currency=currency),
        "risk_cost": money(risk_cost, currency=currency),
        "customer_cost": money(customer_cost, currency=currency),
        "total_action_cost": money(
            financial_cost + communication_cost + risk_cost + customer_cost,
            currency=currency,
        ),
        # Per-figure provenance for the split, because the row-level ``method`` records the
        # weakest of five figures and so cannot say that *these two* were never measured.
        "financial_cost_method": financial_method,
        "communication_cost_method": communication_method,
        "cost_split_not_measured": (
            financial_method == COST_SPLIT_NOT_MEASURED
            or communication_method == COST_SPLIT_NOT_MEASURED
        ),
        "net_recovery_value": money(int(candidate.net_recovery_value), currency=currency),
        "excluded": bool(candidate.excluded),
        "exclusion_reason": candidate.exclusion_reason,
        "rank": candidate.rank,
    }


def _refusal_block(
    *,
    reason: str,
    baseline_probability: str | None,
    selected: RecommendationCandidate | None,
    config: Configuration,
) -> dict[str, object]:
    """Why doing nothing was the answer, with every number that decided it (R11.C5).

    The three thresholds are included even though only one of them usually decided, because a
    merchant asking "why not?" is asking about the whole comparison. Showing the single failing
    bound invites the reply "so lower it", and the answer to that is the other two.
    """
    return {
        "reason": reason,
        "explanation": _REFUSAL_EXPLANATIONS[reason],
        "baseline_probability": baseline_probability
        or not_yet_recorded("UNKNOWN", "baseline probability"),
        "incremental_probability": (
            str(selected.incremental_probability) if selected is not None else None
        ),
        "net_recovery_value": (
            money(int(selected.net_recovery_value), currency="INR")
            if selected is not None
            else None
        ),
        "compared_thresholds": {
            "min_net_value_threshold": int(config.MIN_NET_VALUE_THRESHOLD),
            "min_incremental_probability": str(config.MIN_INCREMENTAL_PROBABILITY),
            "max_cost_to_value_ratio": str(config.MAX_COST_TO_VALUE_RATIO),
            "high_baseline_threshold": str(config.HIGH_BASELINE_THRESHOLD),
        },
    }


_REFUSAL_EXPLANATIONS: dict[str, str] = {
    SelectionReason.NO_POSITIVE_VALUE.value: (
        "No action cleared both the minimum net value and the minimum incremental probability, "
        "so acting would have cost more than it was expected to recover."
    ),
    SelectionReason.HIGH_BASELINE_NO_INTERVENTION.value: (
        "This customer was already likely to pay without being contacted. Acting would have "
        "spent money and a customer's patience on a recovery that was going to happen anyway."
    ),
}
"""Plain sentences, server-side. A client that composed these would eventually word one of them
as a failure, and "we chose not to act" being mistaken for "we could not act" is the single
misreading this product can least afford."""

_EXECUTABLE = frozenset(
    {
        CandidateAction.DO_NOTHING,
        CandidateAction.WAIT,
        CandidateAction.PAYMENT_LINK,
        CandidateAction.CUSTOMER_MESSAGE,
        CandidateAction.HUMAN_ESCALATION,
    }
)
"""Mirrors ``domain.actions.EXECUTABLE_ACTIONS``. Referenced rather than imported as a name so a
reader of this file can see what "is_executable" means without following an import — and asserted
equal to the domain set by a test, so the two cannot drift."""


def _policy_document(
    session: Session, merchant_id: uuid.UUID, decision: PolicyDecision
) -> dict[str, object]:
    """One policy decision and all twelve checks, in evaluation order (R14.C5).

    Twelve, not "the ones that ran". The evaluation order is fixed precisely so the stated reason
    cannot be an expensive or case-specific check, and a record showing fewer than twelve rows
    would be indistinguishable from an evaluation that stopped early and approved.
    """
    checks = PolicyDecisionRepository(session).check_results_for(merchant_id, decision.id)
    return {
        "policy_decision_id": str(decision.id),
        "verdict": str(decision.verdict),
        "primary_reason": str(decision.primary_reason),
        "selected_action": str(decision.selected_action),
        "evaluated_at": decision.evaluated_at.isoformat(),
        "expires_at": decision.expires_at.isoformat(),
        "earliest_permitted_at": (
            None
            if decision.earliest_permitted_at is None
            else decision.earliest_permitted_at.isoformat()
        ),
        "rule_set_version": str(decision.rule_set_version),
        "config_version": decision.config_version,
        "case_state_at_evaluation": str(decision.case_state_at_evaluation),
        "decision_cycle": int(decision.decision_cycle),
        "consumed_by_intent_id": (
            None
            if decision.consumed_by_intent_id is None
            else str(decision.consumed_by_intent_id)
        ),
        "checks": _check_documents(checks),
        "expected_check_count": len(POLICY_CHECK_ORDER),
        "recorded_check_count": len(checks),
    }


def _check_documents(checks: Sequence[PolicyCheckResult]) -> list[dict[str, object]]:
    """The twelve check rows, and a placeholder for any that is missing.

    A missing row is rendered as ``NOT_RECORDED`` rather than omitted. Omitting it would shorten
    the list, and a reader used to twelve rows seeing eleven will not notice which one went — the
    same failure this whole surface exists to prevent, one level down.
    """
    by_id = {str(check.check_id): check for check in checks}
    documents: list[dict[str, object]] = []
    for order, check in enumerate(POLICY_CHECK_ORDER, start=1):
        row = by_id.get(check.value)
        if row is None:
            documents.append(
                {
                    "check_order": order,
                    "check_id": check.value,
                    "outcome": "NOT_RECORDED",
                    "detail": "this check has no recorded result for this decision",
                }
            )
            continue
        documents.append(
            {
                "check_order": int(row.check_order),
                "check_id": str(row.check_id),
                "outcome": str(row.outcome),
                "detail": row.detail,
            }
        )
    return documents


# ---------------------------------------------------------------------------
# Metrics, experiments, audit
# ---------------------------------------------------------------------------


def metrics_document(
    metrics: CohortMetrics,
    *,
    currency: str,
    incremental_available: bool = True,
) -> dict[str, object]:
    """A cohort report with every money figure formatted and every label attached.

    ``incremental_available=False`` replaces the incremental figure — and only that figure — with a
    data-unavailable marker (R14.C16). The rest of the report returns with its own timestamps,
    which is the whole point: a slow experiment analysis must not blank a page that also holds
    revenue at risk and a recovery rate.

    Note what does *not* change when the incremental figure is unavailable: the
    ``CAUSALITY_NOT_ESTABLISHED`` label stays on. A figure we could not compute is certainly not a
    causal claim we established, so degrading fails toward the safe statement rather than dropping
    the caveat along with the number.
    """
    return {
        "reporting_period": metrics.period.as_document(),
        "computed_at": metrics.computed_at.isoformat(),
        "segment": metrics.segment.as_document(),
        "case_count": metrics.case_count,
        "recovered_case_count": metrics.recovered_case_count,
        "revenue_at_risk": money(metrics.revenue_at_risk, currency=currency),
        "observed_recovered_revenue": money(
            metrics.observed_recovered_revenue, currency=currency
        ),
        "natural_recovered_revenue": money(
            metrics.natural_recovered_revenue, currency=currency
        ),
        "total_recovery_cost": money(metrics.total_recovery_cost, currency=currency),
        "net_recovered_revenue": money(metrics.net_recovered_revenue, currency=currency),
        "unresolved_revenue": money(metrics.unresolved_revenue, currency=currency),
        # R31.C12. The four terms of every confirmed executed action, and their sum beside them
        # rather than instead of them. Summed on the server for the same reason the per-candidate
        # total is (R14.C12): a browser that added two formatted figures would be free to disagree.
        "financial_cost": money(metrics.financial_cost, currency=currency),
        "communication_cost": money(metrics.communication_cost, currency=currency),
        "risk_cost": money(metrics.risk_cost, currency=currency),
        "customer_cost": money(metrics.customer_cost, currency=currency),
        "total_action_cost": money(metrics.total_action_cost, currency=currency),
        "incremental_recovered_revenue": (
            _incremental_document(metrics, currency=currency)
            if incremental_available
            else data_unavailable(
                "incremental_recovered_revenue",
                "the experiment analysis did not complete within "
                "DASHBOARD_METRICS_TIMEOUT; every other figure in this response is current",
            )
        ),
        "recovery_rate": rate(metrics.recovery_rate),
        "intervention_rate": rate(metrics.intervention_rate),
        "action_success_rate": rate(metrics.action_success_rate),
        "escalation_rate": rate(metrics.escalation_rate),
        "average_hours_to_recovery": rate(metrics.average_hours_to_recovery),
        "blocked_case_count": metrics.blocked_case_count,
        "escalated_case_count": metrics.escalated_case_count,
        "intervened_case_count": metrics.intervened_case_count,
        "confirmed_action_count": metrics.confirmed_action_count,
        "successful_action_count": metrics.successful_action_count,
        "unnecessary_action_count": metrics.unnecessary_action_count,
        "cycles_without_action_count": metrics.cycles_without_action_count,
        "labels": list(metrics.labels),
        "causality_established": metrics.causality_established,
        "is_synthetic": metrics.is_synthetic,
    }


def _incremental_document(metrics: CohortMetrics, *, currency: str) -> dict[str, object]:
    """The incremental figure, or the sentinel with its reasons — never a zero.

    ``NOT_ESTABLISHED`` is emitted as the sentinel string rather than as ``null`` or ``0``. Both
    alternatives render as something: an empty cell reads as "nothing recovered" and a zero reads
    as "measured, and it was nothing". The claim being made is neither.
    """
    finding = metrics.incremental
    if not finding.established:
        return {
            "status": "NOT_ESTABLISHED",
            "value": str(finding.value),
            "refusal_codes": list(finding.refusal_codes),
            "detail": (
                "No completed, adequately powered experiment with a lift interval entirely "
                "above zero supports a causal claim for this period. Observed recovery is not "
                "presented as incremental."
            ),
        }
    return {
        "status": "ESTABLISHED",
        "amount": money(int(finding.value), currency=currency),
        "experiment_id": None if finding.experiment_id is None else str(finding.experiment_id),
        "control_case_count": finding.control_case_count,
        "treatment_case_count": finding.treatment_case_count,
        "lift": None if finding.lift is None else str(finding.lift),
        "lift_interval": (
            None
            if finding.lift_ci_low is None
            else f"[{finding.lift_ci_low}, {finding.lift_ci_high}]"
        ),
    }


def experiment_document(
    session: Session,
    merchant_id: uuid.UUID,
    experiment: Experiment,
    *,
    currency: str,
) -> dict[str, object]:
    """An experiment and its newest analysis, with per-arm figures and interval bounds (R14.C8).

    Returns the definition even when there is no analysis yet, and says so. An experiment with no
    result is a normal state — it is running — and an endpoint that 404'd would make a running
    experiment indistinguishable from one that does not exist.
    """
    result = ExperimentResultRepository(session).latest_for_experiment(
        merchant_id, experiment.id
    )
    document: dict[str, object] = {
        "experiment_id": str(experiment.id),
        "name": str(experiment.name),
        "state": str(experiment.state),
        "primary_metric": str(experiment.primary_metric),
        "allocation_ratio": str(experiment.allocation_ratio),
        "assumed_baseline_rate": str(experiment.assumed_baseline_rate),
        "minimum_detectable_effect": str(experiment.minimum_detectable_effect),
        "significance_level": str(experiment.significance_level),
        "power": str(experiment.power),
        "required_sample_size_per_group": int(experiment.required_sample_size_per_group),
        "analysis_method": str(experiment.analysis_method),
        "labels": list(experiment.labels or ()),
        "activated_at": (
            None if experiment.activated_at is None else experiment.activated_at.isoformat()
        ),
        "completed_at": (
            None if experiment.completed_at is None else experiment.completed_at.isoformat()
        ),
    }
    document["result"] = (
        _experiment_result_document(result, currency=currency)
        if result is not None
        else not_yet_recorded(str(experiment.state), "experiment analysis")
    )
    return document


def _experiment_result_document(
    result: ExperimentResult, *, currency: str
) -> dict[str, object]:
    """One analysis. Per-arm counts and the interval, never a lift without its interval."""
    comparison = result.comparison if isinstance(result.comparison, dict) else {}
    return {
        "result_id": str(result.id),
        "computed_at": result.computed_at.isoformat(),
        "analysis_method": str(result.analysis_method),
        "primary_metric": str(result.primary_metric),
        "control": {
            "case_count": int(result.control_case_count),
            "recoveries": int(result.control_recoveries),
            "rate": rate(result.control_rate),
        },
        "treatment": {
            "case_count": int(result.treatment_case_count),
            "recoveries": int(result.treatment_recoveries),
            "rate": rate(result.treatment_rate),
        },
        "lift": None if result.lift is None else str(result.lift),
        # The interval travels with the lift and is emitted as a pair of bounds rather than as a
        # width. A width can be rendered without its centre; bounds cannot be shown without
        # revealing whether zero is inside them, which is the only thing that licenses a claim.
        "lift_ci_low": None if result.lift_ci_low is None else str(result.lift_ci_low),
        "lift_ci_high": None if result.lift_ci_high is None else str(result.lift_ci_high),
        "interval_contains_zero": _contains_zero(result),
        "contaminated_count": int(result.contaminated_count),
        "excluded_count": int(result.excluded_count),
        "labels": list(result.labels or ()),
        "four_way_comparison": comparison,
        "currency": currency,
    }


def _contains_zero(result: ExperimentResult) -> bool | None:
    """Whether the reported interval straddles zero. ``None`` when there is no interval.

    Computed server-side so a client never has to compare two decimal strings — the comparison
    that decides whether a causal claim is permitted is not one to leave to JavaScript number
    coercion.
    """
    if result.lift_ci_low is None or result.lift_ci_high is None:
        return None
    return result.lift_ci_low <= 0 <= result.lift_ci_high


def unresolved_view(
    session: Session,
    merchant_id: uuid.UUID,
    *,
    start: datetime,
    end: datetime,
    currency: str,
) -> dict[str, object]:
    """The unresolved grouping with every amount formatted (R14.C10, R14.C12).

    This wrapper exists because of where formatting is allowed to live.
    :func:`revora.metrics.unresolved.unresolved_groups` returns integer minor units, which is
    correct for a computation layer — and it cannot format them, because ``grouping_for`` and the
    currency symbols live in :mod:`revora.api.rendering` and the ``feature modules depend downward
    only`` contract forbids ``revora.metrics`` from importing ``revora.api``. Rather than duplicate
    the symbol table one layer down, the amounts are formatted here.

    The alternative shipped briefly and was wrong: the endpoint returned ``amount_minor`` as a bare
    integer alongside a single ``currency`` field, which left the browser to divide by a hundred and
    pick a symbol. That is precisely the client-side currency arithmetic R14.C12 exists to prevent,
    and it would have been the one screen in the dashboard doing its own formatting — so the first
    rounding disagreement would have appeared between this page and the summary beside it.

    All five groups are always present, zero rows included, because that guarantee belongs to
    ``unresolved_groups`` and is preserved verbatim here.

    **R30.C13's absence is structural, not incidental.** ``unresolved_groups`` selects on
    ``state IN (UNRESOLVED_STATES)`` and every member of that tuple is a Terminal_State, so a case
    resting at ``POLICY_CHECK`` waiting for a review is not filtered out of this grouping — it was
    never in the scanned set. Nothing here needs a guard for it, and adding one would suggest the
    guard was what kept it out.
    """
    groups = unresolved_groups(session, merchant_id, start=start, end=end)
    return {
        "reporting_period": {"start": start.isoformat(), "end": end.isoformat()},
        "computed_at": now().isoformat(),
        "currency": currency,
        "groups": [
            {
                "state": group.state.value,
                # R26.C14. Three distinct labels for the three Terminal_States, chosen here rather
                # than derived in the browser from the member name — which is what this screen used
                # to do, and which left the one place a case's *ending* is named as the one place
                # nobody had chosen the words.
                "label": case_state_label(group.state.value),
                "case_count": group.case_count,
                "amount": money(group.amount, currency=currency),
            }
            for group in groups
        ],
        # R21.C11. Every case holding a live Contact_Suppression, listed under the ``ESCALATED``
        # group it belongs to, with its Hard_Stop_Reason, the suppression instant and the
        # unresolved amount. A breakdown rather than a sixth group, because a suppressed case *is*
        # escalated — R21.C4 and R21.C5 both terminate it that way — and a sixth row would split
        # one grouping's money across two places and make the total stop being the sum of it.
        #
        # Named ``suppressed`` and not ``escalated_detail``: the list is defined by the
        # suppression record, so a case escalated for another reason is correctly absent.
        "suppressed": _suppressed_cases(session, merchant_id, currency=currency),
        "total_case_count": sum(group.case_count for group in groups),
        # Summed from the same integers the groups carry, so the total is exactly the sum of the
        # rows above it and not a separately-rounded figure that can disagree with them.
        "total_amount": money(sum(group.amount for group in groups), currency=currency),
    }


SUPPRESSED_CASE_LIMIT: int = 100
"""How many suppressed cases the unresolved grouping lists.

Bounded because every list read in this codebase is, and because this one is rendered as a table on
a page a merchant loads interactively. A hundred is well past the point at which the page stops
being readable, so the bound bites long after the presentation already has — which is the right
order for a limit to bite in. The aggregate row above the list is *not* bounded: it comes from
``unresolved_groups``, which is a ``GROUP BY`` over every row, so a merchant with more than a
hundred suppressed cases still sees the correct count and the correct total and simply does not see
every case listed."""


def _suppressed_cases(
    session: Session, merchant_id: uuid.UUID, *, currency: str
) -> list[dict[str, object]]:
    """R21.C11: each suppressed case with its reason, instant and unresolved amount.

    Driven from the suppressions rather than from the cases, which is what makes the list correct
    rather than approximately correct: the suppression is the record R21.C11 names, so a case that
    ended ``ESCALATED`` for a different reason is absent by construction instead of by a filter.

    Only suppressions still in force (``list_in_force``), because a released one is not a case a
    merchant needs in this queue — a person has already decided contact may resume. The case-detail
    view shows released ones, and the two answer different questions.

    ``payment_amount`` is read off the originating case and formatted here, in minor units all the
    way to :func:`~revora.api.rendering.money`, for the reason ``unresolved_view`` gives at length:
    the browser must never divide by a hundred or pick a symbol.
    """
    cases = RecoveryCaseRepository(session)
    listed: list[dict[str, object]] = []
    for suppression in ContactSuppressionRepository(session).list_in_force(
        merchant_id, limit=SUPPRESSED_CASE_LIMIT
    ):
        case = cases.get(merchant_id, suppression.origin_case_id)
        if case is None:  # pragma: no cover - RESTRICT makes the case undeletable
            continue
        listed.append(
            {
                "case_id": str(case.id),
                "state": str(case.state),
                "terminal_reason": case.terminal_reason,
                "hard_stop_reason": str(suppression.hard_stop_reason),
                "hard_stop_label": HARD_STOP_LABELS.get(
                    str(suppression.hard_stop_reason), str(suppression.hard_stop_reason)
                ),
                "suppressed_at": suppression.suppressed_at.isoformat(),
                "unresolved_amount": money(
                    int(case.payment_amount), currency=str(case.currency) or currency
                ),
            }
        )
    return listed


def audit_document(records: Sequence[AuditRecord]) -> list[dict[str, object]]:
    """The ordered audit trail for a case (R11.C5).

    Sequence order, not timestamp order. Two records can share a millisecond and the per-case
    sequence is what actually says which came first — it is allocated inside the transaction that
    wrote the record, so it is gap-free and cannot be reordered by a clock.

    Every field is already masked at write time. Nothing here re-masks, and nothing here decides
    what to hide: a read model that filtered audit fields would be a second, weaker masking policy
    sitting in front of the real one.
    """
    return [
        {
            "seq": None if record.seq is None else int(record.seq),
            "occurred_at": record.occurred_at.isoformat(),
            "event_type": str(record.event_type),
            "actor": str(record.actor),
            "correlation_id": str(record.correlation_id),
            "previous_state": record.previous_state,
            "new_state": record.new_state,
            "action": record.action,
            "action_result": record.action_result,
            "idempotency_key": record.idempotency_key,
            "confidence": None if record.confidence is None else str(record.confidence),
            "diagnosis": record.diagnosis,
            "evidence": record.evidence,
            "decision": record.decision,
            "policy_result": record.policy_result,
        }
        for record in records
    ]


# NOTE. The candidate figures shown here come from ``recommendation_candidate`` — the *ranked*
# record — and not from ``candidate_estimate``, which is the *priced* one. They are not the same
# set: pricing happens for every eligible action, ranking happens after exclusion, and the ranked
# rows carry the exclusion reason and the rank that R7.C8 asks the dashboard to show. Reading the
# priced table here would display figures that were never part of a comparison.
# ---------------------------------------------------------------------------
# The Case_Timeline's input views (R26.C1 through C8)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TimelineInputs:
    """Everything :func:`revora.timeline.stages.project` needs, read once and frozen.

    This class exists so the router can hold the *whole* input to a projection as one value that
    outlived its transaction. That is what makes 50.3's guarantee checkable: every read happens
    inside one ``tenant_transaction``, the views are built there, and the projection then runs with
    no session in scope at all — so a concurrent write cannot change the input half way through, and
    the projection cannot re-read to find out that it did.

    Frozen, and every field is frozen, so nothing between the read and the response can add to it.
    """

    records: tuple[AuditRecordView, ...]
    case: CaseView
    signals: tuple[SignalView, ...]
    intents: tuple[IntentView, ...]
    figures: FigureView


def timeline_inputs(
    session: Session,
    merchant_id: uuid.UUID,
    case: RecoveryCase,
    *,
    config: Configuration,
) -> TimelineInputs:
    """Read one case's timeline inputs and reduce them to frozen views.

    Nine reads, all scoped to ``merchant_id`` and this case, and all of them inside whatever
    transaction the caller opened. Nothing here writes.

    **Why the reduction happens here rather than in ``revora.timeline``.** That package sits below
    ``revora.api`` in the layering contract and imports nothing from ``revora.persistence``, which
    is the property that makes P56 a ``pure``-tier property: the projection has no repository to
    call and no session to call one with. Somebody has to turn rows into views, and the only place
    that can is above both — here.

    **Every currency figure is formatted before it crosses into the projection** (R26.C8). The four
    money values below leave as ``MoneyField.formatted`` strings, so the timeline package receives
    ``₹4,120.00`` and never ``412000``. It therefore holds no currency vocabulary and cannot produce
    a figure that disagrees with the one the case detail shows beside it.

    **The two derived figures are integer sums of adjacent columns**, on exactly the terms
    :func:`_candidate_document` already justifies: ``total_action_cost`` is the four cost terms
    added up, and the cheapest available option is a ``min`` over those sums. Both are computed
    here, in a module ``scripts/check_no_float.py`` scans, rather than in the browser or in the
    timeline package.
    """
    currency = str(case.currency)
    records = AuditRecordRepository(session).list_for_case(merchant_id, case.id)
    diagnosis = DiagnosisRepository(session).active_for_case(merchant_id, case.id)
    baseline = BaselineEstimateRepository(session).latest_for_case(merchant_id, case.id)
    recommendation = RecommendationRepository(session).latest_for_case(merchant_id, case.id)
    # The same cycle derivation as `case_detail`, and for the same reason: the case counter and the
    # cycle a decision was recorded under are not the same number, and a timeline that disagreed
    # with the detail page directly below it would be worse than either being wrong alone.
    cycle = (
        int(recommendation.decision_cycle)
        if recommendation is not None
        else int(case.decision_cycle_count)
    )
    decisions = PolicyDecisionRepository(session).for_cycle(merchant_id, case.id, cycle)
    candidates = (
        ()
        if recommendation is None
        else RecommendationRepository(session).candidates_for(merchant_id, recommendation.id)
    )
    intents = ExecutionIntentRepository(session).list_for_case(merchant_id, case.id)
    outcome = RecoveryOutcomeRepository(session).for_case(merchant_id, case.id)
    signals = CustomerSignalRepository(session).list_for_case(
        merchant_id, case.id, limit=int(config.MAX_CUSTOMER_SIGNALS_PER_CASE)
    )

    runner_up = _runner_up(candidates, recommendation)
    decision = decisions[-1] if decisions else None

    return TimelineInputs(
        records=tuple(_audit_record_view(record) for record in records),
        case=CaseView(
            case_id=str(case.id),
            state=str(case.state),
            detected_at=case.detected_at,
            decision_cycle_count=int(case.decision_cycle_count),
            max_recovery_attempts=int(config.MAX_RECOVERY_ATTEMPTS),
            next_review_at=case.next_review_at,
            terminal_reason=case.terminal_reason,
            provider_payment_id=str(case.provider_payment_id),
        ),
        signals=tuple(
            SignalView(
                kind=str(signal.kind),
                submitted_at=signal.submitted_at,
                delay_reason=signal.delay_reason,
            )
            for signal in signals
        ),
        intents=tuple(
            IntentView(
                action=str(intent.action),
                state=str(intent.state),
                attempt_started_at=intent.attempt_started_at,
            )
            for intent in intents
        ),
        figures=FigureView(
            payment_amount_formatted=_formatted(int(case.payment_amount), currency),
            cause=None if diagnosis is None else str(diagnosis.cause),
            confidence=None if diagnosis is None else str(diagnosis.confidence),
            diagnosis_method=None if diagnosis is None else str(diagnosis.method),
            evidence_source=_evidence_source(diagnosis),
            baseline_probability=None if baseline is None else str(baseline.probability),
            baseline_interval=_interval(baseline),
            priced_count=None if recommendation is None else len(candidates),
            unavailable_count=(
                None
                if recommendation is None
                else sum(1 for candidate in candidates if bool(candidate.excluded))
            ),
            cheapest_total_action_cost_formatted=_cheapest_available(candidates, currency),
            selected_action=(
                None if recommendation is None else str(recommendation.selected_action)
            ),
            net_recovery_value_formatted=_selected_net_value(
                candidates, recommendation, currency
            ),
            selection_reason=(
                None if recommendation is None else str(recommendation.selection_reason)
            ),
            runner_up_action=None if runner_up is None else str(runner_up.action),
            runner_up_value_formatted=(
                None
                if runner_up is None
                else _formatted(int(runner_up.net_recovery_value), currency)
            ),
            policy_verdict=None if decision is None else str(decision.verdict),
            policy_primary_reason=None if decision is None else str(decision.primary_reason),
            recovered_amount_formatted=(
                None
                if outcome is None
                else _formatted(int(outcome.recovered_amount), currency)
            ),
            outcome_classification=None if outcome is None else str(outcome.classification),
            outcome_verified_at=None if outcome is None else outcome.recovery_timestamp,
            # R26.C9's `WHERE` clause, and as of task 50 it is always `None` in a running system:
            # `revora.reasoning` is declared and unwired, no module in `revora` imports it, and
            # nothing writes `ai_invocation`. The column exists and is read rather than skipped, so
            # the day a paragraph is recorded it appears here without a second change — and every
            # deterministic sentence stays what it was, because no template reads this field.
            ai_explanation_text=(
                None if recommendation is None else recommendation.ai_explanation_text
            ),
        ),
    )


def _audit_record_view(record: AuditRecord) -> AuditRecordView:
    """One ``audit_record`` row reduced to the fields a stage rule or a sentence reads.

    ``seq`` is coerced with ``int(...)`` and the column is nullable, but a null cannot arrive here:
    ``list_for_case`` filters on ``case_id``, and ``case_and_seq_together`` makes ``seq`` non-null
    wherever ``case_id`` is. The coercion is on the value the query guarantees exists rather than a
    guard against one it cannot return.

    **The three review fields are lifted by name from the record's ``decision`` document**, using
    :data:`~revora.timeline.stages.REVIEW_DECISION_KEYS`. Lifting three named keys rather than
    passing the document is what keeps the projection unable to reach a value no completion rule
    declares — the same document also carries ``selection_changed``, ``unresolved_amount`` and a
    config version, and R30.C14 asks the timeline for none of them.
    """
    decision = record.decision if isinstance(record.decision, dict) else {}
    trigger, previous_action, new_action = (
        decision.get(key) for key in REVIEW_DECISION_KEYS
    )
    return AuditRecordView(
        seq=int(record.seq or 0),
        event_type=str(record.event_type),
        occurred_at=record.occurred_at,
        previous_state=record.previous_state,
        new_state=record.new_state,
        review_trigger=trigger if isinstance(trigger, str) else None,
        previous_selected_action=(
            previous_action if isinstance(previous_action, str) else None
        ),
        new_selected_action=new_action if isinstance(new_action, str) else None,
    )


def _formatted(minor: int, currency: str) -> str:
    """One money figure as the string a browser renders, and nothing else.

    ``MoneyField.formatted`` rather than :func:`money`, because the projection substitutes a value
    into a sentence and has no envelope to unpack. The integer stays here; only the string crosses
    into ``revora.timeline``, which is what leaves that package with nothing to do arithmetic on.
    """
    return MoneyField(minor=minor, currency=currency).formatted


def _evidence_source(diagnosis: Diagnosis | None) -> str | None:
    """Which input the recorded cause was read off (R20.C4, R26.C4).

    Read out of the diagnosis row's ``evidence`` document under the key
    ``revora.domain.failure_taxonomy.EVIDENCE_SOURCE``, which is the constant the writer used. A
    literal ``"evidence_source"`` here would be a second spelling of a key one module owns, and the
    two would part company the first time either was renamed — with this side failing silently,
    because a missing key is indistinguishable from an older row that never carried one.

    ``None`` where the document has no source, which is a real state: rows written before R20.C4
    existed carry evidence without it, and the label table renders that as a named absence rather
    than assuming the provider was the source.
    """
    if diagnosis is None:
        return None
    evidence = diagnosis.evidence if isinstance(diagnosis.evidence, dict) else {}
    source = evidence.get(EVIDENCE_SOURCE)
    return source if isinstance(source, str) else None


def _interval(baseline: BaselineEstimate | None) -> str | None:
    """The baseline's uncertainty interval, or ``None`` for R26.C4's ``UNCERTAINTY_UNAVAILABLE``.

    Both conditions are checked — ``uncertainty_available`` and the two bounds being present —
    because they answer different questions and the second is the one that can be false while the
    first is true. The flag is what the estimator concluded; the columns are what it stored. An
    interval rendered from a half-present pair would be the worst of the three possible outputs,
    since it would look like a measurement.

    Formatted as ``[low, high]``, the same form :func:`case_detail` uses, so the timeline and the
    baseline panel below it show one string rather than two conventions.
    """
    if baseline is None:
        return None
    if not bool(baseline.uncertainty_available):
        return None
    if baseline.ci_low is None or baseline.ci_high is None:
        return None
    return f"[{baseline.ci_low}, {baseline.ci_high}]"


def _total_action_cost(candidate: RecommendationCandidate) -> int:
    """The four cost terms of one candidate, added up (R31.C7).

    An integer sum of four columns sitting next to each other, in a module the no-float guard scans.
    The same sum :func:`_candidate_document` emits for the case detail, so the cheapest figure the
    timeline names is the same figure the table below it shows.
    """
    return (
        int(candidate.financial_cost)
        + int(candidate.communication_cost)
        + int(candidate.risk_cost)
        + int(candidate.customer_cost)
    )


def _cheapest_available(
    candidates: Sequence[RecommendationCandidate], currency: str
) -> str | None:
    """The lowest total action cost among the candidates that were *not* excluded.

    Available only, because the sentence says "cheapest available option" and an excluded action's
    cost is not an option — a retry that the account cannot perform is not cheap, it is unavailable,
    and folding it into this figure would make the ``ALTERNATIVES_PRICED`` stage report a price
    nothing could have been bought at.

    ``None`` where every candidate was excluded, which is a reachable state and the one where a zero
    would be most misleading: "the cheapest available option cost nothing" is exactly the wrong
    reading of "there was no available option".
    """
    available = [
        _total_action_cost(candidate) for candidate in candidates if not bool(candidate.excluded)
    ]
    if not available:
        return None
    return _formatted(min(available), currency)


def _selected_net_value(
    candidates: Sequence[RecommendationCandidate],
    recommendation: Recommendation | None,
    currency: str,
) -> str | None:
    """The selected candidate's ``net_recovery_value``, formatted.

    Read off the candidate row rather than recomputed, so the figure in the sentence is the figure
    the optimizer recorded. ``None`` where the recommendation names an action with no candidate row
    — which the schema does not forbid and which would otherwise be presented as a zero, on the
    stage whose whole content is what the chosen action was worth.
    """
    if recommendation is None:
        return None
    selected = str(recommendation.selected_action)
    for candidate in candidates:
        if str(candidate.action) == selected:
            return _formatted(int(candidate.net_recovery_value), currency)
    return None


def _runner_up(
    candidates: Sequence[RecommendationCandidate], recommendation: Recommendation | None
) -> RecommendationCandidate | None:
    """The best-ranked candidate that was not selected (R26.C4).

    Ranked, not merely second in the list. ``rank`` is assigned by the optimizer after exclusion, so
    a ``None`` rank means the candidate never entered the comparison and cannot be its runner-up —
    presenting one would name an alternative that was never an alternative.

    ``None`` where nothing else was ranked, which is ordinary rather than exceptional: a cause with
    one eligible action, or a set where everything but the winner was excluded. The sentence then
    reads *"Runner-up not recorded"*, which is true, and the alternative — naming an excluded action
    with its figures — would suggest Revora had a second option it declined.
    """
    if recommendation is None:
        return None
    selected = str(recommendation.selected_action)
    ranked = sorted(
        (
            candidate
            for candidate in candidates
            if candidate.rank is not None and str(candidate.action) != selected
        ),
        key=lambda candidate: int(candidate.rank or 0),
    )
    return ranked[0] if ranked else None
