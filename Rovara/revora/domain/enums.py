"""Every enumeration in Revora, in one place.

These are the authoritative definitions. The database stores them as ``TEXT`` with
a ``CHECK`` constraint rather than as a Postgres enum type, because a ``CHECK`` is
cheaper to evolve than an enum that needs a migration to extend — see the Type
Discipline discussion in design.md.

All members are string-valued so a stored row is readable without a lookup.
"""

from __future__ import annotations

from enum import StrEnum, unique

__all__ = [
    "ActionAvailability",
    "CaseState",
    "CheckOutcome",
    "CustomerSignalKind",
    "DecisionSource",
    "DelayReason",
    "DetectionVerdict",
    "DiagnosisEvidenceSource",
    "DiagnosisMethod",
    "EstimationMethod",
    "ExclusionReason",
    "ExecutionEffectKind",
    "ExperimentGroup",
    "ExperimentLabel",
    "ExperimentState",
    "FieldKind",
    "HardStopReason",
    "IntentState",
    "InterventionStatus",
    "OutcomeClass",
    "PolicyCheck",
    "PolicyVerdict",
    "PromiseStatus",
    "Provenance",
    "ReasoningCallKind",
    "ReviewTrigger",
    "RiskCause",
    "SelectionReason",
    "TerminalReason",
    "TimelineStage",
    "TimelineStageStatus",
    "TokenRevocationReason",
    "ValidationStatus",
]


@unique
class RiskCause(StrEnum):
    """Why revenue is at risk. Derived from the provider's own error fields."""

    INSUFFICIENT_FUNDS = "INSUFFICIENT_FUNDS"
    EXPIRED_PAYMENT_METHOD = "EXPIRED_PAYMENT_METHOD"
    BANK_OR_NETWORK_FAILURE = "BANK_OR_NETWORK_FAILURE"
    TECHNICAL_ISSUE = "TECHNICAL_ISSUE"
    ABANDONMENT = "ABANDONMENT"
    CUSTOMER_ACTION_REQUIRED = "CUSTOMER_ACTION_REQUIRED"
    FRAUD_OR_RISK_SIGNAL = "FRAUD_OR_RISK_SIGNAL"
    UNKNOWN = "UNKNOWN"


@unique
class CaseState(StrEnum):
    """The fourteen states a recovery case can hold."""

    NEW = "NEW"
    DETECTED = "DETECTED"
    DIAGNOSED = "DIAGNOSED"
    DECISION_PENDING = "DECISION_PENDING"
    POLICY_CHECK = "POLICY_CHECK"
    ACTION_SCHEDULED = "ACTION_SCHEDULED"
    EXECUTING = "EXECUTING"
    WAITING_FOR_OUTCOME = "WAITING_FOR_OUTCOME"
    RECOVERED = "RECOVERED"
    STOPPED = "STOPPED"
    BLOCKED = "BLOCKED"
    EXPIRED = "EXPIRED"
    ESCALATED = "ESCALATED"
    FAILED = "FAILED"


@unique
class PolicyVerdict(StrEnum):
    """The policy engine's answer. Only APPROVED permits an external effect."""

    APPROVED = "APPROVED"
    BLOCKED = "BLOCKED"
    DEFERRED = "DEFERRED"
    ESCALATE = "ESCALATE"


@unique
class PolicyCheck(StrEnum):
    """The twelve policy checks, in their fixed evaluation order.

    The order is not arbitrary. Absolute prohibitions come first so an expensive or
    case-specific check can never be the reason a paid or opted-out customer was
    contacted. ``ORDER`` below is the authoritative sequence.
    """

    ALREADY_PAID = "ALREADY_PAID"
    ALREADY_TERMINAL = "ALREADY_TERMINAL"
    DUPLICATE_ACTION = "DUPLICATE_ACTION"
    FRAUD_OR_RISK = "FRAUD_OR_RISK"
    CUSTOMER_OPTED_OUT = "CUSTOMER_OPTED_OUT"
    CONSENT_MISSING = "CONSENT_MISSING"
    HUMAN_OWNERSHIP = "HUMAN_OWNERSHIP"
    WINDOW_EXPIRED = "WINDOW_EXPIRED"
    MAX_ATTEMPTS_REACHED = "MAX_ATTEMPTS_REACHED"
    MAX_MESSAGES_REACHED = "MAX_MESSAGES_REACHED"
    COOLDOWN_ACTIVE = "COOLDOWN_ACTIVE"
    ACTION_NOT_ELIGIBLE = "ACTION_NOT_ELIGIBLE"


POLICY_CHECK_ORDER: tuple[PolicyCheck, ...] = (
    PolicyCheck.ALREADY_PAID,
    PolicyCheck.ALREADY_TERMINAL,
    PolicyCheck.DUPLICATE_ACTION,
    PolicyCheck.FRAUD_OR_RISK,
    PolicyCheck.CUSTOMER_OPTED_OUT,
    PolicyCheck.CONSENT_MISSING,
    PolicyCheck.HUMAN_OWNERSHIP,
    PolicyCheck.WINDOW_EXPIRED,
    PolicyCheck.MAX_ATTEMPTS_REACHED,
    PolicyCheck.MAX_MESSAGES_REACHED,
    PolicyCheck.COOLDOWN_ACTIVE,
    PolicyCheck.ACTION_NOT_ELIGIBLE,
)
"""The fixed order. Every decision records an outcome for all twelve."""

POLICY_INPUT_UNAVAILABLE = "POLICY_INPUT_UNAVAILABLE"
"""Reason recorded when a required policy input could not be read. The engine
blocks rather than assuming the check would have passed."""


@unique
class CheckOutcome(StrEnum):
    """One policy check's result. UNAVAILABLE forces a block, never a pass."""

    PASS = "PASS"
    FAIL = "FAIL"
    UNAVAILABLE = "UNAVAILABLE"


@unique
class DiagnosisMethod(StrEnum):
    """How a risk cause was arrived at."""

    DETERMINISTIC = "DETERMINISTIC"
    AI_ASSISTED = "AI_ASSISTED"
    REJECTED_AI_OUTPUT = "REJECTED_AI_OUTPUT"
    FALLBACK_UNKNOWN = "FALLBACK_UNKNOWN"


@unique
class DiagnosisEvidenceSource(StrEnum):
    """Which input the recorded cause was derived from.

    ``DiagnosisMethod`` already says *how* a cause was arrived at — a table, a model, a
    rejection, a fallback. It deliberately does not say *what was read*, and once two
    different deterministic tables can each produce a ``DETERMINISTIC`` cause at a
    configured confidence, "the provider told us" and "the customer told us" become
    indistinguishable in the record unless the source is written down beside the method
    (R20.C4).

    The distinction is not cosmetic. A provider error code is an authoritative
    observation of a failed charge; a stated reason is a stranger's account of their own
    finances typed into a public page. Both may inform an estimate, neither authorizes
    anything, and only one of them is evidence a reviewer should weigh at face value —
    so a reviewer reading a recommendation has to be able to tell which one they are
    looking at without joining back to the signal table.
    """

    PROVIDER_ERROR_CODE = "PROVIDER_ERROR_CODE"
    """The canonical event's ``error_reason``, ``error_source``, ``error_step`` and
    ``error_code``, through the failure taxonomy (R3.C1)."""

    CUSTOMER_STATED_REASON = "CUSTOMER_STATED_REASON"
    """A persisted ``Delay_Reason``, through the mapping table R20.C5 declares."""


@unique
class EstimationMethod(StrEnum):
    """How a probability or cost figure was produced.

    ``DEFINITIONAL`` is reserved for DO_NOTHING, whose figures are fixed by
    definition rather than estimated. ``UNCALIBRATED`` means no observation of that
    action exists for the segment yet, and it propagates to every surface.

    ``COST_SPLIT_NOT_MEASURED`` is narrower than the other four and is the weakest
    claim the enumeration can make. It marks the ``financial_cost`` and
    ``communication_cost`` of a row that migration ``0008`` split out of a blended
    ``action_cost``: the whole pre-split total went into ``financial_cost`` and the
    communication term is zero because **nothing measured it and nothing guessed
    it** (R31.C9). It is not produced by any estimator — a figure carrying it is a
    historical row, and R31.C10 requires the marking be shown beside the two
    figures so a migrated zero does not read as a measured zero.
    """

    DETERMINISTIC = "DETERMINISTIC"
    PRIOR_FALLBACK = "PRIOR_FALLBACK"
    UNCALIBRATED = "UNCALIBRATED"
    DEFINITIONAL = "DEFINITIONAL"
    COST_SPLIT_NOT_MEASURED = "COST_SPLIT_NOT_MEASURED"


@unique
class ActionAvailability(StrEnum):
    """Whether an action can actually be executed against the provider."""

    AVAILABLE = "AVAILABLE"
    UNAVAILABLE = "UNAVAILABLE"


@unique
class IntentState(StrEnum):
    """The four states of an execution intent.

    ``UNCERTAIN`` means the provider was called and the outcome is unknown. While
    an intent sits there, no further external call is issued for that case until
    reconciliation resolves it. That fails safe, but it fails silently, so the
    count of records in this state needs an alarm.
    """

    ATTEMPTED = "ATTEMPTED"
    CONFIRMED = "CONFIRMED"
    FAILED = "FAILED"
    UNCERTAIN = "UNCERTAIN"


@unique
class OutcomeClass(StrEnum):
    """The three kinds of recovery, which license three different claims.

    NATURAL — the money arrived without us.
    OBSERVED — we acted and the money arrived. Not "we caused it".
    ATTRIBUTED — a controlled comparison supports the causal claim.
    """

    NATURAL = "NATURAL"
    OBSERVED = "OBSERVED"
    ATTRIBUTED = "ATTRIBUTED"


@unique
class Provenance(StrEnum):
    """Whether a figure came from real traffic or generated data.

    SYNTHETIC propagates to every presentation surface and every export. A figure
    is only REAL when every observation behind it is real.
    """

    REAL = "REAL"
    SYNTHETIC = "SYNTHETIC"


@unique
class InterventionStatus(StrEnum):
    """Whether an observation is usable as a baseline training label.

    Only ``NO_INTERVENTION_CONFIRMED`` is usable, and even that means "no Revora
    action and no *recorded* merchant action" — Revora cannot see a merchant
    phoning a customer. The weakness is labelled rather than solved.
    """

    NO_INTERVENTION_CONFIRMED = "NO_INTERVENTION_CONFIRMED"
    REVORA_INTERVENED = "REVORA_INTERVENED"
    MERCHANT_INTERVENTION_UNKNOWN = "MERCHANT_INTERVENTION_UNKNOWN"


@unique
class DecisionSource(StrEnum):
    """Who or what chose the action on a finished case.

    Retained so that a model trained on this data cannot silently reproduce past
    human choices without the skew being visible.
    """

    AUTOMATED = "AUTOMATED"
    HUMAN_OVERRIDE = "HUMAN_OVERRIDE"
    BASELINE_WORKFLOW = "BASELINE_WORKFLOW"


@unique
class ValidationStatus(StrEnum):
    """How much an estimate has been checked against observed outcomes."""

    VALIDATED = "VALIDATED"
    UNVALIDATED_BASELINE = "UNVALIDATED_BASELINE"
    CALIBRATION_SUSPECT = "CALIBRATION_SUSPECT"
    CALIBRATION_UNVERIFIED = "CALIBRATION_UNVERIFIED"


@unique
class FieldKind(StrEnum):
    """What sort of sensitive value a field holds, for the masking serializer.

    ``PROVIDER_SHORT_URL`` is sensitive because a payment link is a bearer
    capability — anyone holding the URL can pay. It is shown in the dashboard but
    must never reach a log line or an audit record unmasked.

    ``CUSTOMER_ACCESS_TOKEN`` is sensitive **on exactly the same terms**, and that is
    why it is a member of this enumeration rather than a check somewhere in the
    customer package: the wire token ``rvc_<token_id>.<secret>`` is the second bearer
    capability in the system, so whoever holds it can read one case's projection and
    write signals against it. R18.C11 and R29.C4 permit a token to appear in an audit
    field or a log record **only** as its ``token_id``, which is separately random and
    discloses nothing about the secret. Adding the kind here means the masking
    serializer already covers it, rather than every future call site remembering to.
    """

    CONTACT = "CONTACT"
    INSTRUMENT = "INSTRUMENT"
    PROVIDER_SHORT_URL = "PROVIDER_SHORT_URL"
    CUSTOMER_ACCESS_TOKEN = "CUSTOMER_ACCESS_TOKEN"
    NON_SENSITIVE = "NON_SENSITIVE"


SENSITIVE_FIELD_KINDS: frozenset[FieldKind] = frozenset(
    {
        FieldKind.CONTACT,
        FieldKind.INSTRUMENT,
        FieldKind.PROVIDER_SHORT_URL,
        FieldKind.CUSTOMER_ACCESS_TOKEN,
    }
)
"""Kinds the masking serializer must never write in clear."""


@unique
class DetectionVerdict(StrEnum):
    """Exactly one of these is recorded per persisted event.

    ``DEFERRED_TRIGGER`` covers checkout abandonment, missed promise-to-pay and
    payment-window expiry: modelled in the event schema, out of scope as triggers,
    so the event is retained and visible but no case opens.
    """

    AT_RISK = "AT_RISK"
    NOT_AT_RISK = "NOT_AT_RISK"
    DEFERRED_TRIGGER = "DEFERRED_TRIGGER"


@unique
class SelectionReason(StrEnum):
    """Why the optimizer chose what it chose.

    The two null-action reasons matter as much as the positive one: a merchant who
    cannot see why Revora did nothing will assume it is broken.
    """

    HIGHEST_NET_VALUE = "HIGHEST_NET_VALUE"
    NO_POSITIVE_VALUE = "NO_POSITIVE_VALUE"
    HIGH_BASELINE_NO_INTERVENTION = "HIGH_BASELINE_NO_INTERVENTION"


@unique
class ExclusionReason(StrEnum):
    """Why a candidate action was excluded from selection."""

    CAUSE_NOT_ELIGIBLE = "CAUSE_NOT_ELIGIBLE"
    PROVIDER_CAPABILITY_UNVERIFIED = "PROVIDER_CAPABILITY_UNVERIFIED"
    NON_POSITIVE_INCREMENTAL_VALUE = "NON_POSITIVE_INCREMENTAL_VALUE"
    COST_RATIO_EXCEEDED = "COST_RATIO_EXCEEDED"
    INVALID_ESTIMATE_INPUT = "INVALID_ESTIMATE_INPUT"
    BELOW_NET_VALUE_THRESHOLD = "BELOW_NET_VALUE_THRESHOLD"
    BELOW_INCREMENTAL_PROBABILITY = "BELOW_INCREMENTAL_PROBABILITY"


DIVERGENCE_HIGHER_PROBABILITY_LOWER_NET_VALUE = "HIGHER_PROBABILITY_LOWER_NET_VALUE"
"""Recorded when the highest-probability candidate is not the selected one. The
dashboard shows both, because that divergence is the product's whole argument."""


@unique
class TerminalReason(StrEnum):
    """Why a case ended. Drives the unresolved-revenue grouping on the dashboard.

    ``recovery_case.terminal_reason`` carries ``enum_check`` generated from this enumeration, so
    **a member added here is a migration**. Migration ``0001`` created that ``CHECK`` with the
    twelve members above the divider; ``0015`` widened it with the four below, which are the
    customer-stated endings the response loop introduced. The lesson is written down in
    :class:`ReviewTrigger`'s docstring and it applies here in the other direction: that
    enumeration declared its complete set up front *because* a persisted ``CHECK`` reads it, and
    this one could not, because nobody knew in ``0001`` that a customer would one day be able to
    say "I dispute this charge".

    The four customer-stated reasons are grouped together deliberately. Every reason above them
    is something *Revora* concluded — a bound was reached, a window closed, a read could not be
    verified. Every one below is something a *person* said, arriving through the public customer
    surface and recorded verbatim in the sense that matters: the case ends for the reason the
    customer gave, not for a reason Revora inferred from it. That distinction is the whole point
    of not collapsing them onto ``CUSTOMER_OPTED_OUT``, which would have needed no migration and
    would have made "how many customers disputed a charge" unanswerable."""

    RECOVERED_VERIFIED = "RECOVERED_VERIFIED"
    RECOVERY_WINDOW_ELAPSED = "RECOVERY_WINDOW_ELAPSED"
    MAX_ATTEMPTS_REACHED = "MAX_ATTEMPTS_REACHED"
    DECISION_CYCLE_LIMIT_REACHED = "DECISION_CYCLE_LIMIT_REACHED"
    CUSTOMER_OPTED_OUT = "CUSTOMER_OPTED_OUT"
    ALREADY_PAID = "ALREADY_PAID"
    FRAUD_OR_RISK_FLAG = "FRAUD_OR_RISK_FLAG"
    PAYMENT_STATE_UNVERIFIABLE = "PAYMENT_STATE_UNVERIFIABLE"
    EXECUTION_RESULT_UNVERIFIABLE = "EXECUTION_RESULT_UNVERIFIABLE"
    POLICY_BLOCKED = "POLICY_BLOCKED"
    HUMAN_OWNERSHIP = "HUMAN_OWNERSHIP"
    COMMUNICATION_FAILED = "COMMUNICATION_FAILED"

    # -- what the customer said (migration 0015) ------------------------------
    CUSTOMER_DISPUTED_CHARGE = "CUSTOMER_DISPUTED_CHARGE"
    """R21.C4. A Hard_Stop_Reason of ``DISPUTES_THE_CHARGE``, escalated to a person.

    Distinct from ``CUSTOMER_OPTED_OUT`` and R21.C9 is why: an opt-out is a withdrawal of
    consent to be contacted at all, a dispute is an objection to one debt. They also have
    different consequences outside Revora — a dispute implies a possible chargeback — so a
    merchant reading the ``ESCALATED`` grouping needs to see which of the two happened."""

    CUSTOMER_CANCELLED_ORDER = "CUSTOMER_CANCELLED_ORDER"
    """R21.C5. A Hard_Stop_Reason of ``NO_LONGER_WANTS_THE_ORDER``, escalated to a person.

    Separate from a dispute even though both suppress contact identically, because what has to
    happen next differs: a cancellation implies fulfilment and refund questions, a dispute
    implies a chargeback. Both are a person's problem and they are not the same person's."""

    CUSTOMER_REQUESTED_PARTIAL_ARRANGEMENT = "CUSTOMER_REQUESTED_PARTIAL_ARRANGEMENT"
    """R22.C2. The customer asked to settle for less, or in instalments.

    Declared in ``0015`` alongside the two hard stops rather than in a migration of its own,
    because all four widen one ``CHECK`` on one column and rebuilding it four times is four
    chances to write the member list wrong. Its writer arrives with task 43."""

    PROMISE_BEYOND_RECOVERY_WINDOW = "PROMISE_BEYOND_RECOVERY_WINDOW"
    """R23.C4. A Promise_Date at or past ``window_end_at``, which is never extended.

    Declared in ``0015`` for the same reason as the member above; its writer arrives with task
    44. The window's immutability is what makes this an escalation rather than a reschedule —
    R2.C5 means the promise cannot be accommodated, so a person has to decide."""


@unique
class ReviewTrigger(StrEnum):
    """What caused a case resting at ``POLICY_CHECK`` to be looked at again (R30.C11).

    Recorded on the ``CASE_REVIEWED`` audit record and nowhere else. It is deliberately
    *not* an input to anything the review then does: R30.C15 requires the policy
    evaluation a review produces to take no input from the trigger and none from the
    count of prior reviews, so a case reviewed because a second payment failed is
    evaluated on exactly the twelve checks a case reviewed on a schedule is. The
    distinction is worth recording and must not be worth deciding on — if the trigger
    could change the outcome, "restraint was re-examined" would mean something different
    depending on who asked, and the twelve ordered checks would no longer be the whole
    story.

    ``CUSTOMER_SIGNAL`` is declared here before anything produces it (its producer is the
    customer response surface). Declaring it now rather than later is not speculative
    scaffolding: all three members are values of one audit field, and adding a member to
    a closed enumeration that a persisted ``CHECK`` constraint and a dashboard filter
    already read is a migration, whereas declaring the complete set once is not.
    """

    SCHEDULED_REVIEW = "SCHEDULED_REVIEW"
    EVENT_ATTACHED = "EVENT_ATTACHED"
    CUSTOMER_SIGNAL = "CUSTOMER_SIGNAL"


@unique
class ExperimentGroup(StrEnum):
    """Which arm a case was assigned to, before any diagnosis ran."""

    CONTROL = "CONTROL"
    TREATMENT = "TREATMENT"


@unique
class ExperimentState(StrEnum):
    """An experiment's lifecycle."""

    DRAFT = "DRAFT"
    ACTIVE = "ACTIVE"
    COMPLETED = "COMPLETED"
    ABANDONED = "ABANDONED"


@unique
class ExperimentLabel(StrEnum):
    """Labels that disqualify a result from supporting a causal claim.

    Any of these present means no attributed recovery from that experiment.
    """

    UNDERPOWERED = "UNDERPOWERED"
    INVALIDATED = "INVALIDATED"
    SYNTHETIC = "SYNTHETIC"
    CONTAMINATED = "CONTAMINATED"
    EXPLORATORY = "EXPLORATORY"
    CAUSALITY_NOT_ESTABLISHED = "CAUSALITY_NOT_ESTABLISHED"


@unique
class CustomerSignalKind(StrEnum):
    """What a customer said on the response page.

    ``PAGE_VIEWED`` is a signal rather than a log line because "the customer opened
    the link and said nothing" is evidence about the next decision, and it is the
    only evidence available when nothing else is submitted.
    """

    PAGE_VIEWED = "PAGE_VIEWED"
    DELAY_REASON = "DELAY_REASON"
    PROMISE_TO_PAY = "PROMISE_TO_PAY"
    PARTIAL_ARRANGEMENT_REQUEST = "PARTIAL_ARRANGEMENT_REQUEST"


@unique
class DelayReason(StrEnum):
    """The reason a customer gives for a late payment (R20.C1).

    A closed enumeration, not free text: the mapping to a ``RiskCause`` in R20.C5 is
    declared per member, and a value outside the set has no mapping and no meaning.
    ``OTHER`` exists so a customer with a reason we did not anticipate is not forced
    into one that is wrong, and it deliberately maps to no cause.
    """

    SALARY_OR_CASHFLOW_TIMING = "SALARY_OR_CASHFLOW_TIMING"
    BANK_OR_CARD_PROBLEM = "BANK_OR_CARD_PROBLEM"
    AMOUNT_TOO_HIGH_RIGHT_NOW = "AMOUNT_TOO_HIGH_RIGHT_NOW"
    DISPUTES_THE_CHARGE = "DISPUTES_THE_CHARGE"
    NO_LONGER_WANTS_THE_ORDER = "NO_LONGER_WANTS_THE_ORDER"
    OTHER = "OTHER"


@unique
class HardStopReason(StrEnum):
    """The two ``DelayReason`` members that end contact permanently (R21.C1).

    Neither is a payment problem. Both are objections to the debt itself, so both
    write a ``Contact_Suppression`` and escalate to a person rather than scheduling
    another message.

    The values are taken from :class:`DelayReason` rather than restated, so the
    subset relationship cannot drift: a rename there is a failure here at import
    time instead of a suppression that silently stops matching.
    """

    DISPUTES_THE_CHARGE = DelayReason.DISPUTES_THE_CHARGE.value
    NO_LONGER_WANTS_THE_ORDER = DelayReason.NO_LONGER_WANTS_THE_ORDER.value


@unique
class PromiseStatus(StrEnum):
    """The state of a recorded Promise_To_Pay.

    ``BEYOND_WINDOW_ESCALATED`` is terminal for the promise and schedules nothing: a
    Promise_Date at or past the Recovery_Window end is a case for a person, because
    the window is never extended (R2.C5) and stretching it would remove the
    termination guarantee that immutability is what proves.
    """

    RECORDED = "RECORDED"
    FOLLOW_UP_SCHEDULED = "FOLLOW_UP_SCHEDULED"
    KEPT = "KEPT"
    MISSED = "MISSED"
    BEYOND_WINDOW_ESCALATED = "BEYOND_WINDOW_ESCALATED"
    VOIDED = "VOIDED"


@unique
class TokenRevocationReason(StrEnum):
    """Why a Customer_Access_Token was revoked.

    Every revocation names one of these, which is what makes the revocation
    auditable rather than an unexplained ``revoked_at``. ``EXPIRED_SUPERSEDED`` is
    the one that is not a policy event: it is recorded when a replacement token is
    minted for a case whose previous token has expired, because expiry cannot live
    in an index predicate — it needs ``now()`` — so the supersession is written down
    instead of inferred (R18.C14).
    """

    CASE_TERMINAL = "CASE_TERMINAL"
    CONTACT_SUPPRESSED = "CONTACT_SUPPRESSED"
    EXPIRED_SUPERSEDED = "EXPIRED_SUPERSEDED"
    KEY_RETIRED = "KEY_RETIRED"


@unique
class ExecutionEffectKind(StrEnum):
    """Which external effect an Execution_Intent attempted.

    The distinction exists because **the two effects are not equally observable**, and
    that asymmetry is a fact about the provider, not a preference.

    A link creation returns a ``plink_…`` identifier and the created object is re-readable
    by ``reference_id``, which is what reconciliation uses after a crash to establish
    whether the effect exists. A resend response carries **only a success boolean** — no
    notification identifier, and there is no endpoint that reports whether a notification
    was sent. **A resend is therefore re-readable by nothing**, and an ``UNCERTAIN`` resend
    intent is not slow to resolve or resolvable with more attempts: it is *permanently
    unresolvable by provider read*, because no observation answers the question. Such an
    intent escalates once with ``EXECUTION_RESULT_UNVERIFIABLE`` and is never retried.

    This column is what keeps a resend row **out of the reconciliation sweep's scanned
    set**. ``ix_execution_intent_unresolved`` carries
    ``effect_kind = 'PAYMENT_LINK_CREATE'`` in its predicate, so ``reconcile_intents`` and
    ``promote_stale_intents`` never see a resend — it is not skipped by a branch someone
    can delete, it is absent from the set being read. The same predicate keeps
    ``unresolved_intent_count`` counting only intents that *can* be resolved, instead of
    ringing forever on one that cannot.
    """

    PAYMENT_LINK_CREATE = "PAYMENT_LINK_CREATE"
    PAYMENT_LINK_RESEND = "PAYMENT_LINK_RESEND"


@unique
class ReasoningCallKind(StrEnum):
    """The complete permitted surface of the reasoning layer.

    Three bounded advisory calls, and **nothing else is permitted to exist**. Each is
    optional, each has a deterministic result when it is absent, rejected or slow, and
    none of them can be the reason an external effect happened — that authority belongs
    to ``policy_decision``. A fourth kind is not an extension of this enumeration; it is
    a change to the AI boundary and needs the argument that goes with one.

    Recorded as its own column rather than encoded into ``prompt_contract_id`` because
    both facts are queried independently: "how many ``CAUSE_HYPOTHESIS`` calls fell back
    this week" should be a ``WHERE``, not a ``LIKE`` over a version string.
    """

    CAUSE_HYPOTHESIS = "CAUSE_HYPOTHESIS"
    DECISION_EXPLANATION = "DECISION_EXPLANATION"
    LINK_DESCRIPTION = "LINK_DESCRIPTION"


@unique
class TimelineStage(StrEnum):
    """The nine stages of a Case_Timeline, as a closed vocabulary (R26.C1).

    Here rather than in ``revora.timeline`` for the reason every other enumeration is here: two
    modules need the same spelling and neither may reach the other. ``revora.timeline.stages``
    declares which Audit_Record completes each stage and ``revora.timeline.templates`` declares how
    each one reads, and the second must not import the first — a template is a sentence, not a
    conclusion, and the dependency has to run one way so it can be read without knowing how the
    stage was decided. A vocabulary each side defined for itself would give two spellings of one
    stage and let a template silently exist for a stage no rule can reach.

    **The order is not declared here**, and the omission is deliberate. Declaration order in a
    ``StrEnum`` is easy to reorder by accident in a merge, and R26.C1 makes the order a presented
    fact rather than an incidental one — so ``revora.timeline.stages.STAGE_ORDER`` states it as a
    tuple and asserts at import that the tuple covers this enumeration exactly. Two things have to
    agree, and the disagreement is a startup failure rather than a reordered page.

    Nothing here is a Recovery_Case state. A stage is a *presentation* of what the audit trail
    shows happened; ``CaseState`` is what the case is. They overlap in wording and not in meaning:
    a case sitting in ``RECOVERED`` has all nine stages, most of them ``DONE``, and a case in
    ``BLOCKED`` has nine stages too — several of them ``SKIPPED`` with a recorded reason.
    """

    DETECTED = "DETECTED"
    DIAGNOSED = "DIAGNOSED"
    BASELINE_ESTIMATED = "BASELINE_ESTIMATED"
    ALTERNATIVES_PRICED = "ALTERNATIVES_PRICED"
    DECIDED = "DECIDED"
    POLICY_CHECKED = "POLICY_CHECKED"
    EXECUTED = "EXECUTED"
    CUSTOMER_RESPONDED = "CUSTOMER_RESPONDED"
    OUTCOME_VERIFIED = "OUTCOME_VERIFIED"


@unique
class TimelineStageStatus(StrEnum):
    """Exactly one of these is assigned to every Timeline_Stage (R26.C2).

    Four members, and the distinction between the last three is the entire honesty claim of the
    timeline. ``DONE`` may be assigned **only** where a persisted Audit_Record satisfies the
    declared completion rule for that stage — never on the strength of an absent record, and never
    inferred from a later stage having completed (R26.C2, R26.C11, P57).

    ``SKIPPED`` and ``UPCOMING`` are both "there is no completing record", and they are not
    interchangeable: ``SKIPPED`` means the lifecycle went past this stage without producing one and
    the timeline names the reason from the audit trail, while ``UPCOMING`` means the case has not
    reached it yet. A reader shown one where the other holds draws the opposite conclusion about
    whether anything further will happen — which is the same failure R14.C15 exists to prevent for
    figures, restated for stages.

    There is deliberately no ``FAILED`` and no ``UNKNOWN``. A stage whose evidence could not be
    read is not a stage with a status; it is a projection that did not complete, and R26.C10
    answers that with a data-unavailable marker naming the case rather than with a substituted
    status.
    """

    DONE = "DONE"
    IN_PROGRESS = "IN_PROGRESS"
    UPCOMING = "UPCOMING"
    SKIPPED = "SKIPPED"


NOT_ESTABLISHED = "NOT_ESTABLISHED"
"""Reported for incremental recovered revenue when no adequate experiment
supports a number. Deliberately not zero — "we have not measured this" and "we
measured this and it was nothing" are different statements."""

UNDEFINED = "UNDEFINED"
"""Reported for any rate whose denominator is zero (R12.C5). Never the number zero.

A reporting period with no cases has no recovery rate. Reporting ``0`` would say the
period recovered nothing, which is a measurement — and a false one, because nothing
was measured. The distinction matters most in exactly the situation it arises: a new
merchant's first week, where a dashboard full of zeroes reads as total failure and a
dashboard full of ``UNDEFINED`` reads as "no data yet".

A string rather than ``None`` deliberately. ``None`` renders as an empty cell in most
template engines and as ``0`` in a few, and both lose the distinction. A caller holding
``Decimal | str`` has to look at what it got."""

RECOVERY_GROSS_OF_REFUNDS = "RECOVERY_GROSS_OF_REFUNDS"
"""Label on MVP recovery figures. Refunds are captured on every authoritative
read but not yet netted out of reported recovery."""

SUPPRESSED_BY_CONTROL_ARM = "SUPPRESSED_BY_CONTROL_ARM"
"""Reason recorded when an action is withheld because the case is in a control arm.

Lives in the domain because two packages need the same spelling and neither is
permitted to reach the other: ``revora.experiment`` puts the case in the arm,
``revora.execution`` honours it, and they are siblings in the layering contract. A token
each side defined for itself would give two spellings of one reason and make the audit
trail unqueryable by exactly the question an operator asks — "which cases did the
experiment hold back".

A named token rather than a sentence for a second reason: an operator seeing a case
with a recommendation and no action needs to know instantly whether that was the
experiment working or policy blocking. From the outside those look identical and mean
completely different things."""
