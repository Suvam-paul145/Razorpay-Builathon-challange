"""Diagnosis: one active cause per decision cycle, from the provider's own fields.

Runs inside the transaction the worker opened for the diagnosis job, so the diagnosis
row, the audit records and any follow-on enqueue commit together or not at all. A case
with a diagnosis row but no audit record explaining it is not a state this can be left
in.

**Zero LLM invocations and zero provider calls on this path** (R3.C1, R3.C2). That is
structural, not a promise: this module imports ``domain.failure_taxonomy``,
``persistence`` and ``audit``, and nothing from ``revora.reasoning`` or
``revora.providers``. It reads ``error_code``, ``error_reason``, ``error_source``,
``error_step`` and ``method`` off the canonical event that ingestion already persisted.
``ai_invocation_id`` on the row it writes is always ``NULL``, which is the queryable
form of the same claim. The reasoning path (task 14) is a separate, optional caller of
the same substitution gate below — it does not go through this function.

**Exactly one active diagnosis per ``(case_id, decision_cycle)``** is the database's
guarantee, via the ``one_active_diagnosis_per_cycle`` partial unique index, not this
code's. The service checks for an existing active diagnosis first so a retried job
returns cleanly, and the ``ON CONFLICT DO NOTHING`` insert underneath is what makes a
concurrent second job lose rather than commit a second cause. Two active causes for one
cycle would mean two different actions could each claim to be the justified one, with
nothing in the record saying which the recommendation used.

**The decision cycle is read off the case, not passed in.** ``decision_cycle_count``
increments on the edge into ``DECISION_PENDING``, so at diagnosis time — leaving
``DETECTED``, or re-entering after an outcome — the counter already holds the cycle this
diagnosis belongs to. A caller supplying its own number could disagree with the case,
and the partial index would then happily hold two active rows for what is really one
cycle.

**Substitution is recorded, never silent.** R3.C8 has the optimizer treat a low
confidence, a ``REJECTED_AI_OUTPUT`` or a ``FALLBACK_UNKNOWN`` as ``UNKNOWN``. Rather
than leaving each consumer to reimplement that rule and one of them to forget, the
substitution happens here, once: the row stores ``UNKNOWN`` with
``substituted_to_unknown = true`` and keeps the original cause in ``evidence``. The
optimizer reads the recorded cause and gets the right answer without knowing the rule;
a reviewer reads the evidence and can see what was thrown away.

**A second deterministic source, and it is not the provider** (R20.C4). A persisted
``Delay_Reason`` refines the cause for the *next* decision cycle through the mapping table
``revora.customer.signals`` declares. It is deterministic in the same sense the taxonomy is
— a closed table, no model, no call — and different in the one sense that matters: the
input is a stranger's account of their own finances typed into a public page, not the
provider's own error field. So it is recorded at ``CUSTOMER_STATED_CAUSE_CONFIDENCE``
rather than at 1.0, which stays reserved for "the provider told us" (R3.C10), and
``evidence_source`` names which of the two produced the cause. The resolution is R4's: the
input may inform an estimate and may not authorize anything — and structurally it cannot,
because this module records a cause and schedules nothing.

Three things survive a refinement rather than being overwritten, and each is a mistake that
would be invisible if it were not deliberate. The taxonomy evidence keys stay exactly as the
provider path wrote them, so the **coverage metric keeps measuring the provider table** and
a stated reason cannot flatter it. The superseded cause is retained in ``evidence``, so a
reviewer can see what the customer's words replaced. And R3.C6's fraud routing still reads
the provider's cause, so "the provider declined this for risk" cannot be talked out of by a
customer who says their salary is late.

**Coverage is measured, not asserted.** design.md's claim that the deterministic table
handles the large majority of real failures is marked ``[INFERENCE]``, and an inference
in a document is not a fact about production. Every diagnosis persists the matched key
and the taxonomy outcome in ``evidence``, and
``DiagnosisRepository.coverage`` / ``unmapped_reasons`` read them back as counts. An
unmapped reason also gets its own audit record, so the gap is visible in the case's
trail and not only in an aggregate somebody has to think to run.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Final

from sqlalchemy.orm import Session

from revora.audit.events import (
    DIAGNOSIS_ALREADY_RECORDED,
    DIAGNOSIS_RECORDED,
    DIAGNOSIS_SUBSTITUTED_TO_UNKNOWN,
    DIAGNOSIS_UNMAPPED_REASON,
    MERCHANT_INTEGRATION_FAULT,
)
from revora.audit.writer import AuditEntry, AuditWriter
from revora.customer.signals import cause_for_delay_reason
from revora.domain.enums import (
    DelayReason,
    DiagnosisEvidenceSource,
    DiagnosisMethod,
    RiskCause,
)
from revora.domain.failure_taxonomy import (
    EVIDENCE_SOURCE,
    MatchKey,
    TaxonomyMatch,
    classify_failure,
    match_evidence,
)
from revora.domain.payment_event import CanonicalPaymentEvent
from revora.persistence.models import Diagnosis
from revora.persistence.repositories.cases import (
    RecoveryCaseRepository,
    WebhookEventRepository,
)
from revora.persistence.repositories.customer import CustomerSignalRepository
from revora.persistence.repositories.diagnosis import DiagnosisRepository
from revora.platform.clock import now
from revora.platform.config import Configuration
from revora.platform.logging import get_logger

__all__ = [
    "DETERMINISTIC_CONFIDENCE",
    "EVIDENCE_CAUSE_REFINED",
    "EVIDENCE_CUSTOMER_SIGNAL_ID",
    "EVIDENCE_ORIGINAL_CAUSE",
    "EVIDENCE_REASONING_INVOKED",
    "EVIDENCE_STATED_REASON",
    "EVIDENCE_SUBSTITUTED",
    "EVIDENCE_SUBSTITUTION_REASON",
    "EVIDENCE_SUPERSEDED_CAUSE",
    "SUBSTITUTION_BELOW_FLOOR",
    "SUBSTITUTION_METHOD_UNTRUSTED",
    "UNKNOWN_CONFIDENCE",
    "UNTRUSTED_METHODS",
    "DiagnosisOutcome",
    "RecordedDiagnosis",
    "resolve_recorded_diagnosis",
    "resolve_stated_reason_diagnosis",
    "run_diagnosis",
]

_logger = get_logger(__name__)

_DIAGNOSIS_ACTOR: Final = "diagnosis_engine"

DETERMINISTIC_CONFIDENCE: Final[Decimal] = Decimal("1.000")
"""Reserved for ``DETERMINISTIC`` (R3.C10). The AI path is capped at 0.99 precisely so
that a confidence of exactly 1.0 in the record means "the provider told us", and never
"a model was sure"."""

UNKNOWN_CONFIDENCE: Final[Decimal] = Decimal("0.000")
"""Recorded with ``UNKNOWN`` on the fallback path (R3.C9). Zero rather than the
confidence floor: the floor is where a claim stops being usable, and this is the absence
of a claim."""

SUBSTITUTION_BELOW_FLOOR: Final = "CONFIDENCE_BELOW_FLOOR"
SUBSTITUTION_METHOD_UNTRUSTED: Final = "METHOD_NOT_TRUSTED"
"""The two substitution reasons R3.C8 names, as recorded tokens.

Tokens rather than sentences because the optimizer copies the reason onto the
recommendation and the dashboard groups by it. A prose reason is a string nobody can
aggregate."""

UNTRUSTED_METHODS: Final[frozenset[DiagnosisMethod]] = frozenset(
    {DiagnosisMethod.REJECTED_AI_OUTPUT, DiagnosisMethod.FALLBACK_UNKNOWN}
)
"""Methods whose cause is never used, whatever confidence accompanies it.

``REJECTED_AI_OUTPUT`` carries a cause only because the rejected payload is retained as
evidence; using it would defeat the rejection. ``FALLBACK_UNKNOWN`` has no cause to
begin with. Both are listed here rather than checked inline so that a fifth
``DiagnosisMethod`` added later has to be classified deliberately."""

EVIDENCE_ORIGINAL_CAUSE: Final = "original_cause"
EVIDENCE_SUBSTITUTION_REASON: Final = "substitution_reason"
EVIDENCE_SUBSTITUTED: Final = "substituted_to_unknown"
EVIDENCE_REASONING_INVOKED: Final = "reasoning_layer_invoked"

EVIDENCE_CUSTOMER_SIGNAL_ID: Final = "customer_signal_id"
EVIDENCE_STATED_REASON: Final = "delay_reason"
EVIDENCE_CAUSE_REFINED: Final = "delay_reason_refined_cause"
EVIDENCE_SUPERSEDED_CAUSE: Final = "superseded_cause"
"""The four keys R20.C4 and R20.C6 add to the evidence document.

:data:`EVIDENCE_CAUSE_REFINED` is present whenever a stated reason exists and is ``False``
for the three members that name no cause — which is R20.C6's "record that the Delay_Reason
produced no cause refinement" verbatim. Its **absence** means something different again: no
reason was ever submitted. Three states, and collapsing "they said OTHER" into "they said
nothing" would lose the one fact a merchant asking why the second contact repeated the
first actually needs.

:data:`EVIDENCE_SUPERSEDED_CAUSE` appears only when the refinement changed the cause. It is
the provider table's answer, kept because a cause a customer's words replaced is a review
item, and because R3.C6's fraud routing is computed from it — see
:func:`_requires_policy_evaluation`."""


# ---------------------------------------------------------------------------
# The substitution gate — pure, and shared with the reasoning path
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RecordedDiagnosis:
    """What actually gets written, after the confidence and method gate.

    ``cause`` is what every consumer reads. ``original_cause`` is what the classifier
    or the model said, kept so a substitution can be reviewed and so the deterministic
    coverage metric still counts a hit that was later downgraded. They differ only when
    ``substituted_to_unknown`` is true.
    """

    cause: RiskCause
    original_cause: RiskCause
    confidence: Decimal
    method: DiagnosisMethod
    substituted_to_unknown: bool
    substitution_reason: str | None


def resolve_recorded_diagnosis(
    *,
    cause: RiskCause,
    confidence: Decimal,
    method: DiagnosisMethod,
    confidence_floor: Decimal,
) -> RecordedDiagnosis:
    """Apply R3.C8's substitution rule. Pure, no I/O.

    Public because the reasoning path (task 14) must apply the identical rule to an
    ``AI_ASSISTED`` or ``REJECTED_AI_OUTPUT`` result. One implementation, two callers:
    the alternative is two implementations that agree until one of them is edited.

    The method check comes before the confidence check so that the reason recorded for
    a ``FALLBACK_UNKNOWN`` at zero confidence is ``METHOD_NOT_TRUSTED`` rather than
    ``CONFIDENCE_BELOW_FLOOR``. Both are true; only one is the actual explanation, and
    the dashboard groups by this token.

    Args:
        cause: the cause the classifier or the model produced.
        confidence: the recorded confidence, ``Decimal`` and never a float — this
            value is compared against a configured bound and stored in a
            ``NUMERIC(4,3)`` column, and binary rounding either side of that
            comparison would make the boundary case non-deterministic.
        method: how the cause was arrived at.
        confidence_floor: ``Configuration.DIAGNOSIS_CONFIDENCE_FLOOR``.

    Returns:
        A :class:`RecordedDiagnosis`. Substitution replaces the cause with ``UNKNOWN``
        and keeps ``confidence`` untouched — the recorded confidence is a fact about
        the answer that was produced, and rewriting it to zero would erase the evidence
        that a near-threshold answer existed.
    """
    if method in UNTRUSTED_METHODS:
        return RecordedDiagnosis(
            cause=RiskCause.UNKNOWN,
            original_cause=cause,
            confidence=confidence,
            method=method,
            substituted_to_unknown=True,
            substitution_reason=SUBSTITUTION_METHOD_UNTRUSTED,
        )
    if confidence < confidence_floor:
        return RecordedDiagnosis(
            cause=RiskCause.UNKNOWN,
            original_cause=cause,
            confidence=confidence,
            method=method,
            substituted_to_unknown=True,
            substitution_reason=SUBSTITUTION_BELOW_FLOOR,
        )
    return RecordedDiagnosis(
        cause=cause,
        original_cause=cause,
        confidence=confidence,
        method=method,
        substituted_to_unknown=False,
        substitution_reason=None,
    )


def resolve_stated_reason_diagnosis(
    *,
    reason: DelayReason,
    provider: RecordedDiagnosis,
    stated_confidence: Decimal,
    confidence_floor: Decimal,
) -> RecordedDiagnosis:
    """Apply R20.C4's mapping to a provider-derived diagnosis. Pure, no I/O.

    Returns ``provider`` unchanged where the stated reason names no cause (R20.C6): ``OTHER``
    and the two Hard_Stop_Reasons leave the recorded cause exactly as the provider path left
    it. Returning the same object rather than a rebuilt equal one is deliberate — the caller
    tests identity to decide whether a refinement happened, and an equal-but-distinct value
    would make "unchanged" a comparison somebody could get subtly wrong.

    Where the reason does name a cause, the result goes through
    :func:`resolve_recorded_diagnosis` like every other path, so R3.C8's substitution rule
    keeps exactly one implementation. That matters more here than elsewhere: R20.C7 requires
    ``CUSTOMER_STATED_CAUSE_CONFIDENCE`` to sit at or above the floor, and if a deployment
    ever violates it the honest outcome is a recorded substitution to ``UNKNOWN`` naming
    ``CONFIDENCE_BELOW_FLOOR`` — not a cause that quietly outranks the gate every other
    source passes through.

    Args:
        reason: the persisted ``Delay_Reason``.
        provider: what the failure taxonomy produced for this cycle.
        stated_confidence: ``Configuration.CUSTOMER_STATED_CAUSE_CONFIDENCE``. ``Decimal``
            and never a float, for the reason :func:`resolve_recorded_diagnosis` gives.
        confidence_floor: ``Configuration.DIAGNOSIS_CONFIDENCE_FLOOR``.

    Returns:
        The diagnosis to record. ``method`` is ``DETERMINISTIC`` on a refinement, because a
        closed mapping table is what produced it and no model was invoked — the *source*, not
        the method, is what distinguishes this from a provider-derived cause.
    """
    cause = cause_for_delay_reason(reason)
    if cause is None:
        return provider
    return resolve_recorded_diagnosis(
        cause=cause,
        confidence=stated_confidence,
        method=DiagnosisMethod.DETERMINISTIC,
        confidence_floor=confidence_floor,
    )


# ---------------------------------------------------------------------------
# The service
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DiagnosisOutcome:
    """What one diagnosis run did, and what the caller must do next.

    The caller is the job handler, and it owns two things this service deliberately
    does not: the transition to ``DIAGNOSED`` (R3.C7 assigns that to the case manager,
    which opens its own transaction) and the operational alert delivery. Both are
    reported here as facts rather than performed here, so the service stays a
    participant in one transaction.
    """

    diagnosis_id: uuid.UUID | None
    cause: RiskCause
    original_cause: RiskCause
    confidence: Decimal
    method: DiagnosisMethod
    substituted_to_unknown: bool
    substitution_reason: str | None
    match_key: MatchKey | None
    rule_id: str
    deterministic_hit: bool
    coverage_gap: bool
    needs_operational_alert: bool
    requires_policy_evaluation: bool
    case_version: int | None
    already_recorded: bool = False
    reasoning_layer_invoked: bool = False
    """R3.C7's indicator. Always ``False`` from this function, and a recorded fact
    rather than something derived from ``method``.

    Deriving it would be wrong in both directions. ``FALLBACK_UNKNOWN`` means "the table
    did not resolve it" here and "the model timed out" on the reasoning path (R3.C9) —
    one invocation, one not — so the method cannot tell them apart. And
    ``REJECTED_AI_OUTPUT`` means a request *was* sent and its answer thrown away, which
    is an invocation that a naive "did AI contribute" reading would report as none. The
    only component that knows whether a request left the process is the one that sent
    it, so it says so explicitly."""

    evidence_source: DiagnosisEvidenceSource = DiagnosisEvidenceSource.PROVIDER_ERROR_CODE
    """Which input the recorded cause was read off (R20.C4).

    Defaulted to the provider because that is the source every caller predating the customer
    surface had, and a default of ``None`` would push an ``is None`` check into every reader
    for a case that cannot occur — a recorded cause always came from somewhere."""

    customer_signal_id: uuid.UUID | None = None
    """The Customer_Signal the stated reason came from, where one informed this diagnosis.

    Set whenever a stated reason was *read*, including when it named no cause. A merchant
    asking "did the customer's answer change anything" needs the signal identified either
    way; ``evidence_source`` is what says whether it changed the cause."""


def run_diagnosis(
    session: Session,
    merchant_id: uuid.UUID,
    case_id: uuid.UUID,
    config: Configuration,
    *,
    correlation_id: uuid.UUID | None = None,
) -> DiagnosisOutcome:
    """Determine and record the risk cause for one case's current decision cycle.

    Must be called inside a transaction; it commits nothing itself. The worker's job
    handler owns the transaction so the diagnosis row and its audit records are atomic
    with each other and with whatever the handler enqueues next.

    Takes the case row under ``FOR UPDATE``. Two reasons, and the second is the
    load-bearing one: the audit writer allocates its gap-free sequence number by
    updating a counter on that row and requires the lock to already be held, and the
    lock is what serializes a concurrent second diagnosis job onto the existing-row
    check rather than onto the unique index.
    """
    cases = RecoveryCaseRepository(session)
    case = cases.lock_for_update(merchant_id, case_id)
    if case is None:
        _logger.warning("diagnosis for missing case", case_id=str(case_id))
        return _missing_case_outcome(case_id)

    decision_cycle = case.decision_cycle_count
    diagnoses = DiagnosisRepository(session)
    writer = AuditWriter(
        session,
        disclosure_length=config.MASK_DISCLOSURE_LENGTH,
        max_field_length=config.MAX_AUDIT_FIELD_LENGTH,
    )
    moment = now()

    existing = diagnoses.active_for_cycle(merchant_id, case_id, decision_cycle)
    if existing is not None:
        return _already_recorded(
            writer, merchant_id, case_id, existing, decision_cycle, correlation_id, moment
        )

    canonical = _canonical_for_case(session, merchant_id, case.source_event_id)
    match = classify_failure(
        error_reason=canonical.error_reason,
        error_source=canonical.error_source,
        error_step=canonical.error_step,
        error_code=canonical.error_code,
        risk_reason_codes=config.RISK_REASON_CODES,
    )
    provider_recorded = _resolve_from_match(match, config.DIAGNOSIS_CONFIDENCE_FLOOR)

    evidence = match_evidence(
        match,
        error_reason=canonical.error_reason,
        error_source=canonical.error_source,
        error_step=canonical.error_step,
        error_code=canonical.error_code,
        payment_method=canonical.method,
    )
    evidence[EVIDENCE_REASONING_INVOKED] = False

    # R20.C4. Read after the taxonomy rather than instead of it, so the provider's answer is
    # computed and recorded even when the customer's words supersede it.
    signal = CustomerSignalRepository(session).latest_delay_reason(merchant_id, case_id)
    recorded = provider_recorded
    signal_id: uuid.UUID | None = None
    if signal is not None:
        signal_id = signal.id
        recorded = resolve_stated_reason_diagnosis(
            reason=DelayReason(str(signal.delay_reason)),
            provider=provider_recorded,
            stated_confidence=config.CUSTOMER_STATED_CAUSE_CONFIDENCE,
            confidence_floor=config.DIAGNOSIS_CONFIDENCE_FLOOR,
        )
        refined = recorded is not provider_recorded
        evidence[EVIDENCE_CUSTOMER_SIGNAL_ID] = str(signal.id)
        evidence[EVIDENCE_STATED_REASON] = str(signal.delay_reason)
        evidence[EVIDENCE_CAUSE_REFINED] = refined
        if refined:
            evidence[EVIDENCE_SOURCE] = DiagnosisEvidenceSource.CUSTOMER_STATED_REASON.value
            evidence[EVIDENCE_SUPERSEDED_CAUSE] = provider_recorded.cause.value

    if recorded.substituted_to_unknown:
        evidence[EVIDENCE_SUBSTITUTED] = True
        evidence[EVIDENCE_ORIGINAL_CAUSE] = recorded.original_cause.value
        if recorded.substitution_reason is not None:
            evidence[EVIDENCE_SUBSTITUTION_REASON] = recorded.substitution_reason

    diagnosis_id = diagnoses.insert_active(
        merchant_id,
        case_id=case_id,
        decision_cycle=decision_cycle,
        values={
            "cause": recorded.cause.value,
            "confidence": recorded.confidence,
            "method": recorded.method.value,
            "evidence": dict(evidence),
            "substituted_to_unknown": recorded.substituted_to_unknown,
            # Never set on this path, and the column is how that is audited rather
            # than merely claimed.
            "ai_invocation_id": None,
        },
    )
    if diagnosis_id is None:
        # Lost the insert to a concurrent job that committed between the read above
        # and here. The index did its job; re-read and report the existing row.
        existing = diagnoses.active_for_cycle(merchant_id, case_id, decision_cycle)
        if existing is None:  # pragma: no cover - only under a concurrent deactivation
            raise RuntimeError(
                f"diagnosis for case {case_id} cycle {decision_cycle} could neither "
                "be inserted nor found"
            )
        return _already_recorded(
            writer, merchant_id, case_id, existing, decision_cycle, correlation_id, moment
        )

    _write_audit(
        writer,
        merchant_id,
        case_id,
        recorded=recorded,
        match=match,
        evidence=evidence,
        decision_cycle=decision_cycle,
        correlation_id=correlation_id,
        moment=moment,
    )

    return DiagnosisOutcome(
        diagnosis_id=diagnosis_id,
        cause=recorded.cause,
        original_cause=recorded.original_cause,
        confidence=recorded.confidence,
        method=recorded.method,
        substituted_to_unknown=recorded.substituted_to_unknown,
        substitution_reason=recorded.substitution_reason,
        # The three taxonomy facts describe the *provider table*, refinement or not. A stated
        # reason resolving a cause the table missed leaves the gap a gap, which is what keeps
        # the coverage metric a measurement of the table rather than of the loop around it.
        match_key=match.match_key,
        rule_id=match.rule_id,
        deterministic_hit=match.is_deterministic_hit,
        coverage_gap=match.is_coverage_gap,
        needs_operational_alert=match.needs_operational_alert,
        requires_policy_evaluation=_requires_policy_evaluation(
            recorded, superseded=provider_recorded.cause
        ),
        case_version=case.version,
        evidence_source=_evidence_source(evidence),
        customer_signal_id=signal_id,
    )


# ---------------------------------------------------------------------------
# internals
# ---------------------------------------------------------------------------


def _resolve_from_match(match: TaxonomyMatch, confidence_floor: Decimal) -> RecordedDiagnosis:
    """Turn a taxonomy match into the values to record.

    A hit is ``DETERMINISTIC`` at confidence 1.0. Everything else — an unmapped reason,
    or ``order_already_paid``, which names no cause at all — is ``FALLBACK_UNKNOWN`` at
    zero, which R3.C9 already specifies for the case where the reasoning layer is
    unavailable. Using the same method value here is deliberate: with the optional
    reasoning path absent, "the table did not resolve it" and "no model was reachable"
    are the same situation from the consumer's point of view, and inventing a fifth
    method for it would put a value in the column that the frozen enum does not have.

    The result still passes through :func:`resolve_recorded_diagnosis` rather than
    being constructed directly, so the substitution rule has exactly one implementation
    and the deterministic path is covered by the same gate the AI path is.
    """
    if match.cause is not None:
        return resolve_recorded_diagnosis(
            cause=match.cause,
            confidence=DETERMINISTIC_CONFIDENCE,
            method=DiagnosisMethod.DETERMINISTIC,
            confidence_floor=confidence_floor,
        )
    return resolve_recorded_diagnosis(
        cause=RiskCause.UNKNOWN,
        confidence=UNKNOWN_CONFIDENCE,
        method=DiagnosisMethod.FALLBACK_UNKNOWN,
        confidence_floor=confidence_floor,
    )


def _requires_policy_evaluation(
    recorded: RecordedDiagnosis, *, superseded: RiskCause | None = None
) -> bool:
    """Whether R3.C6's fraud routing applies.

    Checked against the original cause as well as the recorded one. A risk signal that
    was substituted to ``UNKNOWN`` is still a risk signal for routing purposes — the
    substitution says "do not build an action set from this cause", not "the provider's
    risk decline did not happen". Reading only the recorded cause would let a
    low-confidence fraud answer skip the gate the requirement exists to force.

    ``superseded`` extends the same argument to R20.C4. A customer whose card was declined
    for risk and who then says their salary is late has told us something true and something
    that does not undo the decline, and a refinement that dropped the routing would let an
    untrusted input switch off a gate no trusted input can. So the provider's cause is read
    here whether or not it is the one that got recorded.
    """
    return RiskCause.FRAUD_OR_RISK_SIGNAL in (
        recorded.cause,
        recorded.original_cause,
        superseded,
    )


def _evidence_source(evidence: Mapping[str, str | bool]) -> DiagnosisEvidenceSource:
    """The recorded source, read back off the evidence document it was written into.

    Read back rather than tracked in a second variable, so the reported source and the stored
    one cannot disagree — the evidence column is what a reviewer sees, and a return value that
    described something else would be the more convincing of the two and the wrong one.
    """
    stored = evidence.get(EVIDENCE_SOURCE)
    if isinstance(stored, str):
        return DiagnosisEvidenceSource(stored)
    return DiagnosisEvidenceSource.PROVIDER_ERROR_CODE  # pragma: no cover - always set


def _canonical_for_case(
    session: Session, merchant_id: uuid.UUID, source_event_id: uuid.UUID | None
) -> CanonicalPaymentEvent:
    """The PII-free canonical event the case was opened from.

    A case with no reachable source event returns an empty canonical rather than
    raising. It is a degenerate input — a synthetic case, or a retention sweep that
    removed the event — and the honest diagnosis for "no error fields available" is
    ``UNKNOWN`` through the ordinary unmapped path, which is also what makes it show up
    in the coverage metric instead of crashing a job forever.
    """
    if source_event_id is None:
        return CanonicalPaymentEvent(event_name="")
    event = WebhookEventRepository(session).get(merchant_id, source_event_id)
    if event is None:
        _logger.warning("diagnosis source event missing", source_event_id=str(source_event_id))
        return CanonicalPaymentEvent(event_name="")
    return CanonicalPaymentEvent.from_dict(event.canonical)


def _write_audit(
    writer: AuditWriter,
    merchant_id: uuid.UUID,
    case_id: uuid.UUID,
    *,
    recorded: RecordedDiagnosis,
    match: TaxonomyMatch,
    evidence: dict[str, str | bool],
    decision_cycle: int,
    correlation_id: uuid.UUID | None,
    moment: datetime,
) -> None:
    """Write the diagnosis record, plus a record per notable condition.

    Separate records rather than flags on one, because each of the three extras is
    something a different person goes looking for: an unmapped reason is a backlog item
    for whoever owns the table, a substitution is a review item for whoever reads the
    recommendation, and an integration fault is an on-call item. A single record with
    three booleans is one nobody queries.
    """
    diagnosis_field = {
        "cause": recorded.cause.value,
        "method": recorded.method.value,
        "decision_cycle": decision_cycle,
        EVIDENCE_REASONING_INVOKED: False,
        # Beside the method rather than only inside ``evidence``, because this field is what
        # the dashboard reads to say why a cause was chosen, and "DETERMINISTIC" alone no
        # longer answers that question (R20.C4).
        EVIDENCE_SOURCE: _evidence_source(evidence).value,
    }
    writer.write_for_case(
        merchant_id,
        case_id,
        AuditEntry(
            event_type=DIAGNOSIS_RECORDED,
            actor=_DIAGNOSIS_ACTOR,
            confidence=recorded.confidence,
            diagnosis=diagnosis_field,
            evidence=dict(evidence),
        ),
        correlation_id=correlation_id,
        occurred_at=moment,
    )

    if match.is_coverage_gap:
        writer.write_for_case(
            merchant_id,
            case_id,
            AuditEntry(
                event_type=DIAGNOSIS_UNMAPPED_REASON,
                actor=_DIAGNOSIS_ACTOR,
                evidence=dict(evidence),
            ),
            correlation_id=correlation_id,
            occurred_at=moment,
        )

    if recorded.substituted_to_unknown:
        writer.write_for_case(
            merchant_id,
            case_id,
            AuditEntry(
                event_type=DIAGNOSIS_SUBSTITUTED_TO_UNKNOWN,
                actor=_DIAGNOSIS_ACTOR,
                confidence=recorded.confidence,
                evidence={
                    EVIDENCE_ORIGINAL_CAUSE: recorded.original_cause.value,
                    EVIDENCE_SUBSTITUTION_REASON: recorded.substitution_reason or "",
                    "recorded_cause": recorded.cause.value,
                },
            ),
            correlation_id=correlation_id,
            occurred_at=moment,
        )

    if match.needs_operational_alert:
        writer.write_for_case(
            merchant_id,
            case_id,
            AuditEntry(
                event_type=MERCHANT_INTEGRATION_FAULT,
                actor=_DIAGNOSIS_ACTOR,
                evidence=dict(evidence),
            ),
            correlation_id=correlation_id,
            occurred_at=moment,
        )


def _already_recorded(
    writer: AuditWriter,
    merchant_id: uuid.UUID,
    case_id: uuid.UUID,
    existing: Diagnosis,
    decision_cycle: int,
    correlation_id: uuid.UUID | None,
    moment: datetime,
) -> DiagnosisOutcome:
    """Report the diagnosis that is already active for this cycle, changing nothing.

    The idempotent answer for a retried job. It reports the *existing* row's values
    rather than the ones this run would have computed, because the lifecycle continues
    on the recorded diagnosis and a caller told otherwise would transition a case on a
    cause that was never persisted.
    """
    writer.write_for_case(
        merchant_id,
        case_id,
        AuditEntry(
            event_type=DIAGNOSIS_ALREADY_RECORDED,
            actor=_DIAGNOSIS_ACTOR,
            evidence={
                "decision_cycle": str(decision_cycle),
                "existing_diagnosis_id": str(existing.id),
            },
        ),
        correlation_id=correlation_id,
        occurred_at=moment,
    )
    cause = RiskCause(existing.cause)
    method = DiagnosisMethod(existing.method)
    stored: dict[str, object] = dict(existing.evidence or {})
    original = stored.get(EVIDENCE_ORIGINAL_CAUSE)
    original_cause = RiskCause(original) if isinstance(original, str) else cause
    reason = stored.get(EVIDENCE_SUBSTITUTION_REASON)
    recorded = RecordedDiagnosis(
        cause=cause,
        original_cause=original_cause,
        confidence=existing.confidence,
        method=method,
        substituted_to_unknown=bool(existing.substituted_to_unknown),
        substitution_reason=reason if isinstance(reason, str) else None,
    )
    # Read back rather than recomputed, so a retried job routes a fraud case the same way the
    # first run did even though the recorded cause is a customer's and the risk signal is the
    # provider's. Recomputing would need the canonical event this branch deliberately does not
    # load.
    superseded = stored.get(EVIDENCE_SUPERSEDED_CAUSE)
    signal_id = stored.get(EVIDENCE_CUSTOMER_SIGNAL_ID)
    source = stored.get(EVIDENCE_SOURCE)
    return DiagnosisOutcome(
        diagnosis_id=existing.id,
        cause=recorded.cause,
        original_cause=recorded.original_cause,
        confidence=recorded.confidence,
        method=recorded.method,
        substituted_to_unknown=recorded.substituted_to_unknown,
        substitution_reason=recorded.substitution_reason,
        match_key=None,
        rule_id=str(stored.get("rule_id", "")),
        deterministic_hit=method is DiagnosisMethod.DETERMINISTIC,
        coverage_gap=False,
        needs_operational_alert=False,
        requires_policy_evaluation=_requires_policy_evaluation(
            recorded,
            superseded=RiskCause(superseded) if isinstance(superseded, str) else None,
        ),
        case_version=None,
        already_recorded=True,
        evidence_source=(
            DiagnosisEvidenceSource(source)
            if isinstance(source, str)
            else DiagnosisEvidenceSource.PROVIDER_ERROR_CODE
        ),
        customer_signal_id=uuid.UUID(signal_id) if isinstance(signal_id, str) else None,
    )


def _missing_case_outcome(case_id: uuid.UUID) -> DiagnosisOutcome:
    """The answer for a diagnosis job whose case is gone.

    Reported as already-recorded so the handler completes the job rather than retrying
    it forever. A case cannot be deleted in normal operation, so this is either a
    stale job from a wiped environment or a merchant-scoping mistake; either way,
    retrying will not help and the warning above names it.
    """
    return DiagnosisOutcome(
        diagnosis_id=None,
        cause=RiskCause.UNKNOWN,
        original_cause=RiskCause.UNKNOWN,
        confidence=UNKNOWN_CONFIDENCE,
        method=DiagnosisMethod.FALLBACK_UNKNOWN,
        substituted_to_unknown=True,
        substitution_reason=SUBSTITUTION_METHOD_UNTRUSTED,
        match_key=None,
        rule_id="",
        deterministic_hit=False,
        coverage_gap=False,
        needs_operational_alert=False,
        requires_policy_evaluation=False,
        case_version=None,
        already_recorded=True,
    )
