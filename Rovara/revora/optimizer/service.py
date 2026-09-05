"""Persist the recommendation and every rejected alternative.

R7.C8 and R11.C6 together say something stronger than "record the decision": record every
candidate that was considered, with every one of its figures, its exclusion reason and its
rank, so that the single audit query that explains a case carries the whole comparison.
Since R31.C1 that means four separate cost terms rather than three, because "which cost
made this not worth doing" is a question a blended figure cannot answer.
A merchant explaining to a customer why they were sent a payment link should be able to
see what else was on the table and what each alternative was worth — and should not need
a second query to find out.

So this module writes one ``recommendation`` row and one ``recommendation_candidate`` row
per candidate, including the excluded ones. Nothing is filtered on the way to storage.

**The AI boundary is structural here, not procedural.** ``recommendation.ai_explanation_text``
is the only place a model's prose may land, and this module cannot import
``revora.reasoning`` — the import contract forbids it, so there is no code path by which
an AI-produced value could reach a figure. When explanation text is supplied by a caller
that *can* reach the reasoning layer, it is written to that column alone and an audit
note records that it held no influence on the selection. That is Property 2's persistence
half: the ranking read ``net_recovery_value``, the column holding prose is not an input to
anything, and both facts are checkable from this file.

**The substituted diagnosis travels with the recommendation.** R7.C9: where the diagnosis
was replaced by ``UNKNOWN`` because its confidence fell below the floor or its method was
untrusted, the recommendation records the original cause and the substitution reason. A
narrowed candidate set — ``UNKNOWN`` permits nothing customer-visible — otherwise looks
like a failure rather than like the deliberate conservatism it is.

**So does the Customer_Signal that produced the cause.** R25.C5: where a Delay_Reason refined
the Risk_Cause of a case's next decision cycle under R20.C4, the recommendation records the
Customer_Signal identifier behind it. The candidate set itself needs no change to satisfy the
first half of R25.C5 — the set is already built from the cause on the *active diagnosis for this
cycle*, and R20.C4's refinement is recorded as exactly that, so a stated reason reaches the next
cycle's candidates through the mechanism every other cause reaches them through. What was missing
is the attribution: the recorded comparison said "these were the candidates for
``INSUFFICIENT_FUNDS``" without saying that ``INSUFFICIENT_FUNDS`` came from a customer typing
"my salary is late" rather than from the provider's error code. Those two produce identical
candidate sets and license very different amounts of confidence, and a merchant explaining the
decision afterwards needs to be able to tell them apart.

It is recorded in the ``RECOMMENDATION_RECORDED`` Audit_Record rather than in a new column on
``recommendation``, and that is a deliberate choice with a cost worth naming. The audit record is
already what R11.C6 makes the recommendation's complete recorded form — the whole candidate set
with every figure lives there and nowhere else — so ``decision->>'cause_signal_id'`` is queryable
and joins to ``customer_signal`` without a migration, and the two facts a reader needs (which
cause, from which evidence) end up in one document instead of split across a column and a JSON
blob. The cost is that it is not a foreign key, so nothing at the database level stops the id
naming a signal that does not exist. That is acceptable here because the id is copied from
``diagnosis.evidence``, which the diagnosis service wrote in the same transaction as a row it had
just read — this module invents no identifier — and because the audit trail is append-only, so
the value cannot drift after the fact.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Final

from sqlalchemy.orm import Session

from revora.audit.events import RECOMMENDATION_RECORDED
from revora.audit.writer import AuditEntry, AuditWriter
from revora.diagnosis.service import (
    EVIDENCE_CAUSE_REFINED,
    EVIDENCE_CUSTOMER_SIGNAL_ID,
    EVIDENCE_STATED_REASON,
)
from revora.domain.actions import CandidateAction
from revora.domain.enums import (
    ActionAvailability,
    DiagnosisEvidenceSource,
    RiskCause,
    SelectionReason,
)
from revora.domain.failure_taxonomy import EVIDENCE_SOURCE
from revora.domain.money import Minor
from revora.domain.probability import Probability
from revora.optimizer.arithmetic import CandidateInput
from revora.optimizer.selection import SelectionResult, Thresholds, select
from revora.persistence.models.estimates import CandidateEstimate
from revora.persistence.repositories.cases import RecoveryCaseRepository
from revora.persistence.repositories.diagnosis import DiagnosisRepository
from revora.persistence.repositories.estimates import (
    BaselineEstimateRepository,
    CandidateEstimateRepository,
)
from revora.persistence.repositories.recommendations import RecommendationRepository
from revora.platform.clock import now
from revora.platform.config import Configuration
from revora.platform.logging import get_logger

__all__ = [
    "FAILURE_NO_BASELINE",
    "FAILURE_NO_CANDIDATES",
    "FAILURE_NO_CASE",
    "CauseProvenance",
    "RecommendationOutcome",
    "run_optimizer",
    "thresholds_from_config",
]

_logger = get_logger(__name__)

_OPTIMIZER_ACTOR: Final = "value_optimizer"

FAILURE_NO_CASE: Final = "CASE_NOT_FOUND"
FAILURE_NO_BASELINE: Final = "NO_BASELINE_FOR_CYCLE"
FAILURE_NO_CANDIDATES: Final = "NO_CANDIDATES_FOR_BASELINE"
"""The three reasons no recommendation is produced.

All three are ordering faults in the job pipeline rather than data conditions: the
optimizer runs after estimation, and estimation guarantees a baseline and a candidate set
or reports its own failure. Distinguished as tokens because "the baseline never got
written" and "the candidates never got written" point at different jobs."""

_AI_NOTE: Final = "ai_explanation_text held no influence on selection"


def thresholds_from_config(config: Configuration) -> Thresholds:
    """The four bounds the optimizer applies, read from configuration.

    Extracted so the pure selection code never touches a ``Configuration`` and the
    properties can vary a bound without constructing one.
    """
    return Thresholds(
        min_net_value=config.MIN_NET_VALUE_THRESHOLD,
        min_incremental_probability=config.MIN_INCREMENTAL_PROBABILITY,
        max_cost_to_value_ratio=config.MAX_COST_TO_VALUE_RATIO,
        high_baseline=config.HIGH_BASELINE_THRESHOLD,
    )


@dataclass(frozen=True, slots=True)
class RecommendationOutcome:
    """What one optimizer run decided, and what the caller must do next.

    The caller is the job handler, and it owns the transition to ``POLICY_CHECK`` — the
    optimizer recommends, and only the policy engine can authorize. That separation is
    the core principle of the whole system, and it shows up here as this service
    deliberately not moving the case forward itself.
    """

    recommendation_id: uuid.UUID | None
    selected_action: CandidateAction | None
    selection_reason: SelectionReason | None
    divergence_reason: str | None
    net_recovery_value: Minor | None
    expected_incremental_revenue: Minor | None
    candidate_count: int
    failure_reason: str | None
    case_version: int | None
    already_recorded: bool = False

    cause_provenance: CauseProvenance | None = None
    """Which Risk_Cause the candidate set was built from and what produced it (R25.C5).

    Defaulted to ``None`` so the failure and already-recorded constructions stay valid. ``None``
    means "this run recorded no recommendation", never "the cause had no provenance" — a run that
    produced a recommendation always carries one, even where every field inside it is absent."""


def run_optimizer(
    session: Session,
    merchant_id: uuid.UUID,
    case_id: uuid.UUID,
    config: Configuration,
    *,
    correlation_id: uuid.UUID | None = None,
    ai_explanation_text: str | None = None,
) -> RecommendationOutcome:
    """Rank the candidate set and persist the recommendation for the current cycle.

    Must be called inside a transaction; it commits nothing itself. Takes the case row
    under ``FOR UPDATE`` because the audit writer allocates its gap-free sequence from a
    counter on that row, and because the lock serializes a concurrent second optimizer
    job onto the existing-recommendation check rather than onto the unique index beneath
    it.

    Args:
        ai_explanation_text: optional prose from the reasoning layer, supplied by a
            caller that can reach it. Written to ``ai_explanation_text`` and nowhere
            else. It is a parameter rather than something this module fetches precisely
            so that the module cannot import the reasoning layer — which is what makes
            "AI cannot influence the ranking" a structural fact.
    """
    cases = RecoveryCaseRepository(session)
    case = cases.lock_for_update(merchant_id, case_id)
    if case is None:
        _logger.warning("optimizer for missing case", case_id=str(case_id))
        return _failed(FAILURE_NO_CASE, case_version=None)

    moment = now()
    decision_cycle = case.decision_cycle_count
    recommendations = RecommendationRepository(session)
    writer = AuditWriter(
        session,
        disclosure_length=config.MASK_DISCLOSURE_LENGTH,
        max_field_length=config.MAX_AUDIT_FIELD_LENGTH,
    )

    existing = recommendations.for_cycle(merchant_id, case_id, decision_cycle)
    if existing is not None:
        return RecommendationOutcome(
            recommendation_id=existing.id,
            selected_action=CandidateAction(existing.selected_action),
            selection_reason=SelectionReason(existing.selection_reason),
            divergence_reason=existing.divergence_reason,
            net_recovery_value=None,
            expected_incremental_revenue=None,
            candidate_count=recommendations.candidate_count(merchant_id, existing.id),
            failure_reason=None,
            case_version=None,
            already_recorded=True,
        )

    baseline = BaselineEstimateRepository(session).for_cycle(
        merchant_id, case_id, decision_cycle
    )
    if baseline is None:
        _logger.warning("optimizer with no baseline", case_id=str(case_id))
        return _failed(FAILURE_NO_BASELINE, case_version=case.version)

    rows = CandidateEstimateRepository(session).list_for_baseline(merchant_id, baseline.id)
    if not rows:
        _logger.warning("optimizer with no candidates", case_id=str(case_id))
        return _failed(FAILURE_NO_CANDIDATES, case_version=case.version)

    result = select(
        tuple(_to_input(row) for row in rows),
        baseline=Probability(baseline.probability),
        amount=Minor(int(case.payment_amount)),
        thresholds=thresholds_from_config(config),
    )

    # The whole estimate row rather than just its id, because the two per-figure method
    # labels have to be copied onto the ranked candidate. They deliberately do not travel
    # through ``_to_input``: the optimizer must not be able to read a provenance label, or
    # a ranking could come to depend on one.
    estimates = {CandidateAction(row.action): row for row in rows}
    substitution = _substitution(session, merchant_id, case_id, decision_cycle)
    # R25.C5. Read from the same active diagnosis the candidate set was priced against, so the
    # cause this records and the cause the estimates correspond to cannot be different causes.
    provenance = _cause_provenance(session, merchant_id, case_id, decision_cycle)

    recommendation = recommendations.insert(
        merchant_id,
        values={
            "case_id": case_id,
            "baseline_estimate_id": baseline.id,
            "decision_cycle": decision_cycle,
            "selected_action": result.selected.action.value,
            "selection_reason": result.selection_reason.value,
            "divergence_reason": result.divergence_reason,
            "substituted_risk_cause": substitution[0],
            "substitution_reason": substitution[1],
            # The only column AI prose may occupy, and it is write-only from the
            # ranking's point of view.
            "ai_explanation_text": ai_explanation_text,
        },
    )
    recommendations.insert_candidates(
        merchant_id,
        rows=[
            {
                "recommendation_id": recommendation.id,
                "candidate_estimate_id": estimates[item.action].id,
                "action": item.action.value,
                "incremental_probability": item.incremental_probability.value,
                "expected_incremental_revenue": int(item.expected_incremental_revenue),
                "financial_cost": int(item.financial_cost),
                "communication_cost": int(item.communication_cost),
                "risk_cost": int(item.risk_cost),
                "customer_cost": int(item.customer_cost),
                # Copied from the estimate, not re-derived (R31.C10). The dashboard renders
                # the comparison from these rows, so the marking that says a split was never
                # measured has to sit on the same row as the two figures it qualifies —
                # otherwise a migrated zero reads as a measured zero for want of a join.
                "financial_cost_method": estimates[item.action].financial_cost_method,
                "communication_cost_method": (
                    estimates[item.action].communication_cost_method
                ),
                "net_recovery_value": int(item.net_recovery_value),
                "excluded": item.excluded,
                "exclusion_reason": _reason_value(item.exclusion_reason),
                "rank": item.rank,
            }
            for item in result.candidates
        ],
    )

    _write_audit(
        writer,
        merchant_id,
        case_id,
        result=result,
        baseline_probability=baseline.probability,
        decision_cycle=decision_cycle,
        config_version=config.version,
        substitution=substitution,
        provenance=provenance,
        ai_explanation_present=ai_explanation_text is not None,
        correlation_id=correlation_id,
        moment=moment,
    )

    return RecommendationOutcome(
        recommendation_id=recommendation.id,
        selected_action=result.selected.action,
        selection_reason=result.selection_reason,
        divergence_reason=result.divergence_reason,
        net_recovery_value=result.selected.net_recovery_value,
        expected_incremental_revenue=result.selected.expected_incremental_revenue,
        candidate_count=len(result.candidates),
        failure_reason=None,
        case_version=case.version,
        cause_provenance=provenance,
    )


# ---------------------------------------------------------------------------
# internals
# ---------------------------------------------------------------------------


def _to_input(row: CandidateEstimate) -> CandidateInput:
    """Turn a persisted candidate estimate into the optimizer's input struct.

    The narrow point at which the ORM stops and the pure arithmetic starts. Only the
    named columns the value chain needs cross this boundary, which is what keeps
    ``arithmetic`` and ``selection`` free of ``persistence`` — and means a column added
    to the table later cannot silently become an input to the ranking.

    The two cost columns migration ``0008`` produced from the blended ``action_cost``
    cross as two arguments and are summed downstream, never here. A row migrated under
    R31.C9 carries its whole pre-split total in ``financial_cost`` and zero in
    ``communication_cost``, so it sums to the same figure the pre-split optimizer read and
    reaches exactly the same decision — the P67 claim, applied to history rather than to a
    generator.
    """
    return CandidateInput(
        action=CandidateAction(row.action),
        intervention_probability=Probability(row.intervention_probability),
        financial_cost=Minor(int(row.financial_cost)),
        communication_cost=Minor(int(row.communication_cost)),
        risk_cost=Minor(int(row.risk_cost)),
        customer_cost=Minor(int(row.customer_cost)),
        availability=ActionAvailability(row.availability),
        unavailable_reason=row.unavailable_reason,
    )


def _reason_value(reason: object | None) -> str | None:
    """The stored form of an exclusion reason.

    ``EvaluatedCandidate.exclusion_reason`` is typed loosely so that ``arithmetic`` need
    not import the exclusion vocabulary it does not use. This is the one place that
    loose type is narrowed, and it narrows to the enum's value or to ``None`` — the
    schema's ``exclusion_reason_iff_excluded`` check refuses anything else.
    """
    if reason is None:
        return None
    value = getattr(reason, "value", None)
    return value if isinstance(value, str) else str(reason)


def _substitution(
    session: Session,
    merchant_id: uuid.UUID,
    case_id: uuid.UUID,
    decision_cycle: int,
) -> tuple[str | None, str | None]:
    """The substituted cause and reason from the active diagnosis, if it was substituted.

    R7.C9. Read from the diagnosis row rather than recomputed: the substitution rule
    lives in ``revora.diagnosis`` and applying it a second time here would be a second
    implementation that could disagree with the recorded one.
    """
    diagnosis = DiagnosisRepository(session).active_for_cycle(
        merchant_id, case_id, decision_cycle
    )
    if diagnosis is None or not diagnosis.substituted_to_unknown:
        return None, None
    evidence: Mapping[str, object] = diagnosis.evidence or {}
    original = evidence.get("original_cause")
    reason = evidence.get("substitution_reason")
    cause = original if isinstance(original, str) and original in set(RiskCause) else None
    return cause, reason if isinstance(reason, str) else None


@dataclass(frozen=True, slots=True)
class CauseProvenance:
    """Which Risk_Cause this cycle's candidate set was built from, and what produced it.

    R25.C5. Every field is read off the active diagnosis for this cycle — none is derived here
    — because the candidate set was already priced against that diagnosis's cause and a second
    derivation could name a cause the estimates do not correspond to.
    """

    risk_cause: RiskCause | None
    """The cause the Candidate_Action set was constructed from. ``None`` only where no active
    diagnosis exists for the cycle, which the estimation stage makes impossible in the ordinary
    path — a recommendation cannot exist without a baseline and a baseline cannot exist without
    a diagnosis. Kept as an option rather than asserted, because a recommendation that failed to
    record its provenance is a worse outcome than one that records the absence."""

    evidence_source: str | None
    """``evidence_source`` from the diagnosis: ``CUSTOMER_STATED_REASON`` where a Delay_Reason
    refined the cause, ``PROVIDER_ERROR_CODE`` otherwise. The field that lets a reader tell a
    cause a customer supplied from one the provider's error code supplied — identical candidate
    sets, very different confidence."""

    signal_id: str | None
    """The Customer_Signal identifier R25.C5 requires, or ``None`` where no stated reason
    informed the cycle.

    Present whenever a stated reason was *read*, including where the reason named no cause —
    that is :attr:`~revora.diagnosis.service.DiagnosisOutcome.customer_signal_id`'s contract and
    the useful one here. "The customer told us something and it changed nothing" is a different
    fact from "the customer told us nothing", and only the first explains a second contact that
    looks like a repeat of the first."""

    delay_reason: str | None
    cause_refined: bool | None
    """Whether the stated reason changed the recorded cause (R20.C6). ``None`` where no reason
    was submitted, ``False`` where one was and named no cause, ``True`` where it refined. The
    three states are the diagnosis evidence's, carried through unflattened."""

    def as_document(self) -> dict[str, object]:
        """The provenance block of the ``RECOMMENDATION_RECORDED`` audit record."""
        return {
            "candidate_set_risk_cause": None if self.risk_cause is None else self.risk_cause.value,
            "cause_evidence_source": self.evidence_source,
            # R25.C5's identifier, at the top level of the block rather than nested, because
            # ``decision->>'cause_signal_id'`` is how a reviewer joins a recommendation to the
            # submission that shaped it.
            "cause_signal_id": self.signal_id,
            "cause_delay_reason": self.delay_reason,
            "cause_refined_by_customer": self.cause_refined,
            "customer_stated_cause": (
                self.evidence_source == DiagnosisEvidenceSource.CUSTOMER_STATED_REASON.value
            ),
        }


def _cause_provenance(
    session: Session,
    merchant_id: uuid.UUID,
    case_id: uuid.UUID,
    decision_cycle: int,
) -> CauseProvenance:
    """Read R25.C5's provenance off the active diagnosis for this cycle.

    A second read of the same row :func:`_substitution` reads, and left as two functions rather
    than merged. They answer unrelated questions — "was the cause replaced by ``UNKNOWN``" and
    "where did the cause come from" — and the merged version would return a five-tuple whose
    call site nobody can read. The row is in the session's identity map after the first read, so
    the second costs no round trip.
    """
    diagnosis = DiagnosisRepository(session).active_for_cycle(
        merchant_id, case_id, decision_cycle
    )
    if diagnosis is None:
        return CauseProvenance(None, None, None, None, None)

    evidence: Mapping[str, object] = diagnosis.evidence or {}
    signal_id = evidence.get(EVIDENCE_CUSTOMER_SIGNAL_ID)
    reason = evidence.get(EVIDENCE_STATED_REASON)
    refined = evidence.get(EVIDENCE_CAUSE_REFINED)
    source = evidence.get(EVIDENCE_SOURCE)
    cause = str(diagnosis.cause)
    return CauseProvenance(
        risk_cause=RiskCause(cause) if cause in set(RiskCause) else None,
        evidence_source=source if isinstance(source, str) else None,
        signal_id=signal_id if isinstance(signal_id, str) else None,
        delay_reason=reason if isinstance(reason, str) else None,
        cause_refined=refined if isinstance(refined, bool) else None,
    )


def _write_audit(
    writer: AuditWriter,
    merchant_id: uuid.UUID,
    case_id: uuid.UUID,
    *,
    result: SelectionResult,
    baseline_probability: Decimal,
    decision_cycle: int,
    config_version: str,
    substitution: tuple[str | None, str | None],
    provenance: CauseProvenance,
    ai_explanation_present: bool,
    correlation_id: uuid.UUID | None,
    moment: datetime,
) -> None:
    """One audit record carrying the whole comparison.

    The ``decision`` field holds every candidate with all of its figures — the four cost
    terms and their sum among them — its exclusion
    reason and its rank, which is what R11.C6 asks for and what makes the single-query
    explanation complete. It is one record rather than one per candidate because the
    comparison is a single decision — splitting it would make the explanation a join.
    """
    writer.write_for_case(
        merchant_id,
        case_id,
        AuditEntry(
            event_type=RECOMMENDATION_RECORDED,
            actor=_OPTIMIZER_ACTOR,
            action=result.selected.action.value,
            decision={
                "decision_cycle": decision_cycle,
                "config_version": config_version,
                "baseline_probability": str(baseline_probability),
                "selected_action": result.selected.action.value,
                "selection_reason": result.selection_reason.value,
                "divergence_reason": result.divergence_reason,
                "selected_net_recovery_value": int(result.selected.net_recovery_value),
                "substituted_risk_cause": substitution[0],
                "substitution_reason": substitution[1],
                # R25.C5. Merged into the top level of the ``decision`` document rather than
                # nested under a key of its own, so ``decision->>'cause_signal_id'`` reaches it
                # without a path expression — this is the join a reviewer makes from a
                # recommendation to the submission that shaped its candidate set.
                **provenance.as_document(),
                # Property 2, recorded rather than merely true: the ranking read net
                # recovery value, and the presence of model prose changed nothing.
                "ai_explanation_present": ai_explanation_present,
                "ai_influence": _AI_NOTE,
                "ranked_on": "net_recovery_value",
                "candidates": [
                    {
                        "action": item.action.value,
                        "intervention_probability": str(item.intervention_probability),
                        "incremental_probability": str(item.incremental_probability),
                        "expected_incremental_revenue": int(
                            item.expected_incremental_revenue
                        ),
                        # Four separate terms plus their sum, never the sum instead of
                        # them (R31.C7). The audit record is the one place besides
                        # presentation that reads a split term on its own, and it reads
                        # both so a cost-grounds exclusion can be attributed afterwards.
                        "financial_cost": int(item.financial_cost),
                        "communication_cost": int(item.communication_cost),
                        "risk_cost": int(item.risk_cost),
                        "customer_cost": int(item.customer_cost),
                        "total_action_cost": int(item.total_cost),
                        "net_recovery_value": int(item.net_recovery_value),
                        "cost_ratio": (
                            None if item.cost_ratio is None else str(item.cost_ratio)
                        ),
                        "availability": item.availability.value,
                        "excluded": item.excluded,
                        "exclusion_reason": _reason_value(item.exclusion_reason),
                        "rank": item.rank,
                    }
                    for item in result.candidates
                ],
            },
        ),
        correlation_id=correlation_id,
        occurred_at=moment,
    )


def _failed(reason: str, *, case_version: int | None) -> RecommendationOutcome:
    """The outcome for a run that produced no recommendation."""
    return RecommendationOutcome(
        recommendation_id=None,
        selected_action=None,
        selection_reason=None,
        divergence_reason=None,
        net_recovery_value=None,
        expected_incremental_revenue=None,
        candidate_count=0,
        failure_reason=reason,
        case_version=case_version,
    )
