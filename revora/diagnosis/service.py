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
``ai_invocation_id`` on the row it writes is ``NULL`` on every path this module can reach
by itself, which is the queryable form of the same claim.

**The optional reasoning path arrives as an argument, not as a dependency** (R27.C4,
R27.C16). ``run_diagnosis`` takes ``ai_proposal: AiCauseProposal | None``, and
:class:`AiCauseProposal` names a cause, a confidence and a method in this package's own
vocabulary — there is no ``ReasoningResult`` in this signature, because the ``layering``
contract makes ``revora.reasoning`` a sibling of this package and therefore unreachable
from it. The job pipeline is the one layer that can see both, so it is the layer that
translates. Two consequences worth stating: "identical with every model response removed"
is a call with ``None`` rather than a mocked provider, and
:func:`plan_cause_hypothesis` — which decides whether a call is warranted at all — is a
read that cannot itself issue one.

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
from revora.domain.probability import AI_CONFIDENCE_CEILING
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
    "EVIDENCE_AI_PROPOSAL_UNUSED",
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
    "AiCauseProposal",
    "CauseHypothesisInputs",
    "DiagnosisOutcome",
    "RecordedDiagnosis",
    "capped_ai_confidence",
    "plan_cause_hypothesis",
    "resolve_ai_diagnosis",
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

EVIDENCE_AI_PROPOSAL_UNUSED: Final = "ai_proposal_unused"
"""Present and ``True`` where a reasoning proposal arrived and a deterministic cause won.

R27.C16 says a deterministic diagnosis issues no ``CAUSE_HYPOTHESIS`` call at all, and
:func:`plan_cause_hypothesis` is what makes that true in the ordinary case. This key records
the residual: a proposal built from a read that has since been overtaken — a Delay_Reason
submitted between the plan and the write — arrives for a cycle the deterministic table can
now answer. The deterministic answer wins, because 1.0 confidence is reserved for "the
provider told us" (R3.C10), and this key is how the discarded proposal stays visible instead
of looking like a call nobody made."""

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


@dataclass(frozen=True, slots=True)
class AiCauseProposal:
    """A reasoning invocation's outcome, in this package's vocabulary rather than that layer's.

    Not a ``ReasoningResult``. The ``layering`` import contract makes ``revora.reasoning`` a
    sibling of this package, so a signature naming one of its types would not import-check —
    and that is the desired shape rather than an obstacle. The job pipeline is the one layer
    that can see both, so it maps the five result variants onto the three fields below and
    this package stays unable to tell a model from a table.

    ``method`` carries the whole mapping, and R27.C4, R27.C5 and R27.C9 each pin one member:
    ``AI_ASSISTED`` for an accepted response, ``REJECTED_AI_OUTPUT`` for one that failed the
    output schema, ``FALLBACK_UNKNOWN`` for a timeout or an unreachable provider.
    ``DETERMINISTIC`` is not a legal value here and :meth:`__post_init__` refuses it — a
    proposal claiming the deterministic method would put a model's answer at the confidence
    R3.C10 reserves for the provider's own error field.
    """

    cause: RiskCause
    confidence: Decimal
    method: DiagnosisMethod
    invoked: bool
    """Whether a request actually left the process (R3.C7).

    A fact, not a derivation. ``FALLBACK_UNKNOWN`` means "the model timed out" here and "the
    table did not resolve it" on the deterministic path, so the method cannot distinguish one
    invocation from none; and a payload refused by the Prompt_Contract's allow-list produces
    the same method with nothing sent at all. Only the component that issued the request
    knows, so it says so."""

    invocation_id: uuid.UUID | None = None
    """The ``ai_invocation`` row this proposal came from, written before this call (R27.C12).

    ``None`` where no row exists because no request was issued. Recorded on the Diagnosis so
    "which invocation produced this cause" is a join rather than a reconstruction."""

    def __post_init__(self) -> None:
        if self.method is DiagnosisMethod.DETERMINISTIC:
            raise ValueError(
                "an AiCauseProposal may not claim DiagnosisMethod.DETERMINISTIC; that "
                "method and its 1.0 confidence are reserved for the provider's own error "
                "field and the Delay_Reason mapping table (R3.C10)"
            )


def capped_ai_confidence(confidence: Decimal) -> Decimal:
    """R27.C4's ceiling: ``min(returned, 0.99)``. Pure.

    Applied to what the model returned rather than enforced in the output schema, and the
    difference matters. ``schemas.CauseHypothesisOutput`` accepts ``1.0`` because R27.C5's
    permitted range is 0 to 1 inclusive, so a model claiming certainty is *recorded* and then
    capped — rather than hidden behind a validation error that would report a confident answer
    as a malformed one.

    The cap is the only thing standing between an ``AI_ASSISTED`` row and a confidence of
    exactly 1.000, which is the value R3.C10 reserves for "the provider told us". A reader of
    the ``diagnosis`` table can therefore read the method off the confidence, and P54 checks
    both halves.
    """
    ceiling = AI_CONFIDENCE_CEILING.value
    return ceiling if confidence > ceiling else confidence


def resolve_ai_diagnosis(
    proposal: AiCauseProposal, *, confidence_floor: Decimal
) -> RecordedDiagnosis:
    """Apply R27.C4's ceiling and then R3.C8's substitution gate. Pure, no I/O.

    Two callers, deliberately: :func:`run_diagnosis` uses it to decide what to write, and the
    job pipeline uses it to decide whether the invocation row may claim
    ``influenced_recommendation``. One implementation means the row and the diagnosis cannot
    disagree about whether the model's answer was used — which is the whole point of that
    column.

    The ceiling is applied only to ``AI_ASSISTED``. The other two permitted methods carry
    zero confidence and are substituted to ``UNKNOWN`` by the gate anyway, so capping them
    would be arithmetic with no effect dressed up as a rule.
    """
    confidence = (
        capped_ai_confidence(proposal.confidence)
        if proposal.method is DiagnosisMethod.AI_ASSISTED
        else proposal.confidence
    )
    return resolve_recorded_diagnosis(
        cause=proposal.cause,
        confidence=confidence,
        method=proposal.method,
        confidence_floor=confidence_floor,
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


@dataclass(frozen=True, slots=True)
class CauseHypothesisInputs:
    """The six field values a ``CAUSE_HYPOTHESIS`` call may see, and nothing else.

    Exactly the field names ``reasoning.contracts.CAUSE_HYPOTHESIS_CONTRACT`` declares — but
    the contract is the authority and this is a struct that happens to match it, because this
    package cannot import that one to check. The Prompt_Contract's own allow-list is what
    stops an undeclared field reaching the wire; a mismatch here fails as a
    ``PROMPT_CONTRACT_VIOLATION`` with nothing transmitted rather than as a leak.

    Every field is optional, and each of them describes the failure rather than the person who
    suffered it. There is no case id, no contact, no instrument and no token — not filtered
    out, never gathered.
    """

    provider_error_code: str | None = None
    provider_error_reason: str | None = None
    provider_error_source: str | None = None
    provider_error_step: str | None = None
    delay_reason: str | None = None
    delay_reason_note: str | None = None
    """Free text a customer typed, untruncated here on purpose.

    ``DELAY_NOTE_MAX_LENGTH`` truncation happens in the adapter (R20.C11), which is the
    component holding both the value and the bound. Truncating here as well would give two
    places for the limit to be wrong and would make the recorded request disagree with the
    stored note."""


def plan_cause_hypothesis(
    session: Session,
    merchant_id: uuid.UUID,
    case_id: uuid.UUID,
    config: Configuration,
) -> CauseHypothesisInputs | None:
    """The inputs for a ``CAUSE_HYPOTHESIS`` call, or ``None`` where none is warranted.

    **R27.C16, as a read that cannot issue a request.** ``None`` means "do not ask a model",
    and it is returned whenever a deterministic cause already exists — from the provider
    failure taxonomy *or* from the Delay_Reason mapping table, which are R27.C16's "either
    mapping table". A well-mapped provider error therefore costs nothing at all: no
    credential resolution, no payload, no wait, no ``ai_invocation`` row.

    Read-only and lock-free. It takes no row lock, writes nothing and allocates no audit
    sequence number, which is why the caller can run it in its own short transaction, close
    it, and make the network call holding no database resource. Holding the case row under
    ``FOR UPDATE`` across a provider request is the mistake ``execution.engine``'s whole
    docstring is arranged against, and it would be no better here.

    Its answer can be overtaken — a Delay_Reason can be submitted between this read and the
    write — so :func:`run_diagnosis` re-derives the deterministic answer under the lock and
    lets it win. That is a duplicated table lookup, which is cheap, rather than a duplicated
    decision, which would be two answers.

    Returns:
        The inputs where a model may be asked, or ``None`` where one may not be. ``None``
        also covers the cases where asking would be pointless: no case row, or a diagnosis
        already active for this cycle, which a retried job returns idempotently.
    """
    case = RecoveryCaseRepository(session).get(merchant_id, case_id)
    if case is None:
        return None

    diagnoses = DiagnosisRepository(session)
    if diagnoses.active_for_cycle(merchant_id, case_id, case.decision_cycle_count) is not None:
        return None

    canonical = _canonical_for_case(session, merchant_id, case.source_event_id)
    match = classify_failure(
        error_reason=canonical.error_reason,
        error_source=canonical.error_source,
        error_step=canonical.error_step,
        error_code=canonical.error_code,
        risk_reason_codes=config.RISK_REASON_CODES,
    )
    if match.cause is not None:
        # The provider's own error field resolved it. R27.C16.
        return None

    signal = CustomerSignalRepository(session).latest_delay_reason(merchant_id, case_id)
    stated: str | None = None
    note: str | None = None
    if signal is not None:
        stated = str(signal.delay_reason)
        note = signal.delay_reason_note
        if cause_for_delay_reason(DelayReason(stated)) is not None:
            # The Delay_Reason mapping table resolved it. R27.C16's second table.
            return None

    return CauseHypothesisInputs(
        provider_error_code=canonical.error_code,
        provider_error_reason=canonical.error_reason,
        provider_error_source=canonical.error_source,
        provider_error_step=canonical.error_step,
        delay_reason=stated,
        delay_reason_note=note,
    )


def run_diagnosis(
    session: Session,
    merchant_id: uuid.UUID,
    case_id: uuid.UUID,
    config: Configuration,
    *,
    correlation_id: uuid.UUID | None = None,
    ai_proposal: AiCauseProposal | None = None,
) -> DiagnosisOutcome:
    """Determine and record the risk cause for one case's current decision cycle.

    ``ai_proposal`` is the optional reasoning path (R27.C4). With ``None`` — the default, and
    the only value reachable without a configured credential — this function behaves exactly
    as it did before the reasoning layer existed: the deterministic tables decide, the
    recorded ``ai_invocation_id`` stays ``NULL`` and ``reasoning_layer_invoked`` stays
    ``False``. That is what makes "identical with every model response removed" a call with
    ``None`` rather than a mocked provider.

    A proposal is consulted **only** where the deterministic tables produce no cause. Where
    they do, the deterministic answer wins and the proposal is recorded as unused under
    :data:`EVIDENCE_AI_PROPOSAL_UNUSED` — see :func:`plan_cause_hypothesis` on why that
    residual exists at all.

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
    # The deterministic answer is computed first and unconditionally, whatever the reasoning
    # path produced. It is the value a proposal has to beat, and on the two paths where a
    # proposal is discarded it is also the value that gets recorded.
    deterministic = _resolve_from_match(match, config.DIAGNOSIS_CONFIDENCE_FLOOR)
    proposal_used = ai_proposal is not None and match.cause is None
    # Named for what it is — the answer before a Delay_Reason refinement, whether the
    # provider table or a model produced it — rather than ``provider_recorded``, which would
    # be a lie on the one path where a model's answer reaches it.
    pre_refinement = (
        resolve_ai_diagnosis(ai_proposal, confidence_floor=config.DIAGNOSIS_CONFIDENCE_FLOOR)
        if proposal_used and ai_proposal is not None
        else deterministic
    )

    evidence = match_evidence(
        match,
        error_reason=canonical.error_reason,
        error_source=canonical.error_source,
        error_step=canonical.error_step,
        error_code=canonical.error_code,
        payment_method=canonical.method,
    )
    # R3.C7. Read off the proposal rather than inferred from the method, for the reason
    # ``AiCauseProposal.invoked`` gives: a request that was issued and whose answer was thrown
    # away is an invocation, and a payload the allow-list refused is not.
    evidence[EVIDENCE_REASONING_INVOKED] = ai_proposal is not None and ai_proposal.invoked
    if ai_proposal is not None and not proposal_used:
        evidence[EVIDENCE_AI_PROPOSAL_UNUSED] = True

    # R20.C4. Read after the taxonomy rather than instead of it, so the provider's answer is
    # computed and recorded even when the customer's words supersede it.
    signal = CustomerSignalRepository(session).latest_delay_reason(merchant_id, case_id)
    recorded = pre_refinement
    signal_id: uuid.UUID | None = None
    if signal is not None:
        signal_id = signal.id
        recorded = resolve_stated_reason_diagnosis(
            reason=DelayReason(str(signal.delay_reason)),
            provider=pre_refinement,
            stated_confidence=config.CUSTOMER_STATED_CAUSE_CONFIDENCE,
            confidence_floor=config.DIAGNOSIS_CONFIDENCE_FLOOR,
        )
        refined = recorded is not pre_refinement
        evidence[EVIDENCE_CUSTOMER_SIGNAL_ID] = str(signal.id)
        evidence[EVIDENCE_STATED_REASON] = str(signal.delay_reason)
        evidence[EVIDENCE_CAUSE_REFINED] = refined
        if refined:
            evidence[EVIDENCE_SOURCE] = DiagnosisEvidenceSource.CUSTOMER_STATED_REASON.value
            evidence[EVIDENCE_SUPERSEDED_CAUSE] = pre_refinement.cause.value

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
            # ``NULL`` unless a model was actually consulted for this cycle, which is how
            # "zero LLM invocations" stays a queryable claim rather than a promise. Set from
            # the row the pipeline committed *before* calling here, so the foreign key
            # always resolves.
            "ai_invocation_id": None if ai_proposal is None else ai_proposal.invocation_id,
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
        # ``deterministic.cause`` rather than ``pre_refinement.cause``, and the difference is
        # only visible on the reasoning path: R3.C6's fraud routing reads the *provider's*
        # risk decline, and a model's answer must be able to add that routing but never to
        # talk it away. Passing the provider table's own cause keeps the gate reading a
        # trusted input while ``recorded`` and ``recorded.original_cause`` still let an
        # AI-proposed fraud signal route conservatively.
        requires_policy_evaluation=_requires_policy_evaluation(
            recorded, superseded=deterministic.cause
        ),
        case_version=case.version,
        reasoning_layer_invoked=ai_proposal is not None and ai_proposal.invoked,
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
        # Read back out of ``evidence`` rather than restated, so the record's headline field
        # and its evidence document cannot disagree about whether a model was consulted.
        EVIDENCE_REASONING_INVOKED: bool(evidence.get(EVIDENCE_REASONING_INVOKED, False)),
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
