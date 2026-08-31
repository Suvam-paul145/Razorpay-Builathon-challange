"""The one place every audit event type string is declared.

An audit record's ``event_type`` is plain ``TEXT`` in the schema — the vocabulary
grows with every requirement that names a new audited occurrence, and a ``CHECK``
would force a migration each time. The freedom that buys has one cost: nothing stops
a component inventing ``"signature_rejected"`` where another wrote
``"SIGNATURE_REJECTED"``, and the audit trail would then answer a query for one and
miss the other. So the strings live here, as constants, and a component that writes
a literal is the bug review looks for.

The constants are grouped by the requirement area that names them. The set is not
final — later phases (diagnosis, reasoning, policy, execution, outcome, metrics) add
their own — but every one of them is added *here*, not at the call site.

``AUDIT_MUTATION_REJECTED`` and ``CREDENTIAL_UNAVAILABLE`` are re-exported from the
modules that already own them (the persistence repository and the secret store
respectively), so there is still exactly one spelling of each even though the
constant is defined below this layer.
"""

from __future__ import annotations

from typing import Final

from revora.persistence.repositories.audit import AUDIT_MUTATION_REJECTED
from revora.platform.secrets import CREDENTIAL_UNAVAILABLE

__all__ = [
    "ALL_EVENT_TYPES",
    "AUDIT_MUTATION_REJECTED",
    "AUDIT_WRITE_FAILED",
    "AUTHENTICATION_FAILED",
    "BASELINE_ALREADY_RECORDED",
    "BASELINE_ESTIMATE_RECORDED",
    "BASELINE_ESTIMATION_FAILED",
    "CANDIDATE_ACTION_UNAVAILABLE",
    "CANDIDATE_ESTIMATES_ALREADY_RECORDED",
    "CANDIDATE_ESTIMATES_RECORDED",
    "CANDIDATE_MEMORY_UNAVAILABLE",
    "CASE_DETECTED",
    "CASE_ESCALATED",
    "CASE_EXPIRED",
    "CREDENTIAL_UNAVAILABLE",
    "DECISION_CYCLE_LIMIT_REACHED",
    "DETECTION_VERDICT_RECORDED",
    "DIAGNOSIS_ALREADY_RECORDED",
    "DIAGNOSIS_RECORDED",
    "DIAGNOSIS_SUBSTITUTED_TO_UNKNOWN",
    "DIAGNOSIS_UNMAPPED_REASON",
    "DUPLICATE_EVENT_DISCARDED",
    "EVENT_ATTACHED_TO_CASE",
    "EVENT_INGESTED",
    "EVENT_QUARANTINED",
    "ILLEGAL_TRANSITION",
    "INVALID_ESTIMATE",
    "JOB_DEAD_LETTERED",
    "MALFORMED_EVENT",
    "MERCHANT_INTEGRATION_FAULT",
    "OUT_OF_ORDER_EVENT",
    "POLICY_DECISION_RECORDED",
    "RATE_LIMIT_APPLIED",
    "RECOMMENDATION_RECORDED",
    "RECONCILED_TO_RECOVERED",
    "SCHEDULE_REJECTED",
    "SIGNATURE_REJECTED",
    "STATE_TRANSITION",
    "VERSION_CONFLICT",
    "WITHHELD_ACTION_DISCARDED",
]

# ---------------------------------------------------------------------------
# Ingestion — R1, R16, R17
# ---------------------------------------------------------------------------

SIGNATURE_REJECTED: Final = "SIGNATURE_REJECTED"
"""HMAC verification failed against every active secret. No case, answered 401."""

DUPLICATE_EVENT_DISCARDED: Final = "DUPLICATE_EVENT_DISCARDED"
"""A ``provider_event_id`` already persisted for this merchant. The insert returned
zero rows; the provider is redelivering, which is documented behaviour, so the
answer is 200 with this record rather than an error."""

MALFORMED_EVENT: Final = "MALFORMED_EVENT"
"""A payload that failed the canonicalization round-trip or schema. Quarantined,
answered 202 so an unparseable payload is not invited back."""

EVENT_QUARANTINED: Final = "EVENT_QUARANTINED"
"""Companion to ``MALFORMED_EVENT`` naming the quarantine row and the failed rule."""

OUT_OF_ORDER_EVENT: Final = "OUT_OF_ORDER_EVENT"
"""An event older than the newest already processed for its payment. State and every
counter left untouched; both timestamps recorded."""

RATE_LIMIT_APPLIED: Final = "RATE_LIMIT_APPLIED"
"""A source exceeded ``INGEST_RATE_LIMIT``. Rejected without discarding anything
already persisted."""

AUTHENTICATION_FAILED: Final = "AUTHENTICATION_FAILED"
"""A dashboard authentication attempt failed. Unattached to any case."""

EVENT_INGESTED: Final = "EVENT_INGESTED"
"""A signature-verified, canonical, deduplicated event was persisted and a detection
job enqueued in the same transaction."""

# ---------------------------------------------------------------------------
# Detection — R1
# ---------------------------------------------------------------------------

DETECTION_VERDICT_RECORDED: Final = "DETECTION_VERDICT_RECORDED"
"""Exactly one per persisted event, negatives included, naming the applied rule ids
and the verdict."""

CASE_DETECTED: Final = "CASE_DETECTED"
"""An ``AT_RISK`` verdict opened a new recovery case. Recorded against the case."""

EVENT_ATTACHED_TO_CASE: Final = "EVENT_ATTACHED_TO_CASE"
"""An ``AT_RISK`` verdict for a payment that already had an open case. The event is
attached; ``payment_amount`` and the detection timestamp are left unchanged."""

# ---------------------------------------------------------------------------
# Diagnosis — R3
# ---------------------------------------------------------------------------

DIAGNOSIS_RECORDED: Final = "DIAGNOSIS_RECORDED"
"""One per decision cycle, carrying cause, confidence, evidence, method, and whether
the reasoning layer was invoked (R3.C7). On the deterministic path the answer to that
last one is always no, and recording it explicitly is what makes the claim auditable
rather than architectural."""

DIAGNOSIS_ALREADY_RECORDED: Final = "DIAGNOSIS_ALREADY_RECORDED"
"""A diagnosis job ran again for a cycle that already has an active diagnosis. Nothing
changed. Recorded rather than silently returned because a retry that keeps happening is
a job-queue problem worth seeing, and the alternative — no record — makes the second run
indistinguishable from one that never happened."""

DIAGNOSIS_UNMAPPED_REASON: Final = "DIAGNOSIS_UNMAPPED_REASON"
"""The taxonomy table did not resolve the failure. Names the unresolved provider error
fields so the table can be extended from the audit log alone, and so the count of these
is queryable — which is what turns the design's ``[INFERENCE]`` about deterministic
coverage into a measurement."""

DIAGNOSIS_SUBSTITUTED_TO_UNKNOWN: Final = "DIAGNOSIS_SUBSTITUTED_TO_UNKNOWN"
"""A recorded cause was replaced with ``UNKNOWN`` because its confidence fell below
``DIAGNOSIS_CONFIDENCE_FLOOR`` or its method was ``REJECTED_AI_OUTPUT`` or
``FALLBACK_UNKNOWN`` (R3.C8). Carries the original cause and the substitution reason, so
the substitution is reviewable rather than a cause that quietly vanished."""

MERCHANT_INTEGRATION_FAULT: Final = "MERCHANT_INTEGRATION_FAULT"
"""The failure reason names a fault in our own integration — a bad order id, a
mismatched amount, a payment method that was never enabled. The operational alert, not
the diagnosis: the customer did nothing wrong and contacting them would waste the
contact. Deliberately an audit record rather than a log line, because this is the class
of failure nobody knows to go looking for."""

# ---------------------------------------------------------------------------
# Estimation — R5, R6
# ---------------------------------------------------------------------------

BASELINE_ESTIMATE_RECORDED: Final = "BASELINE_ESTIMATE_RECORDED"
"""One baseline probability was produced for a decision cycle. Carries the probability,
the interval or ``UNCERTAINTY_UNAVAILABLE``, the segment id with the backoff level that
produced it, the feature values, the observation counts behind the posterior, the
method, the provenance and the training snapshot id (R5.C2).

The counts are in the record and not only in the row because they are what makes the
number checkable. "0.312" is unarguable; "0.312 from four confirmed observations at the
cause-and-amount level" is something a reader can disagree with."""

BASELINE_ESTIMATION_FAILED: Final = "BASELINE_ESTIMATION_FAILED"
"""Estimation reached ``BASELINE_ESTIMATION_TIMEOUT`` or the memory store was
unreachable. **No estimate row exists for this cycle** and the case stays in
``DIAGNOSED`` (R5.C11).

This is the record that stops a missing baseline reading as a baseline of zero. A zero
baseline makes every intervention look maximally valuable, so the failure has to be
loud and the case has to stop, rather than the pipeline continuing on an absent
denominator."""

BASELINE_ALREADY_RECORDED: Final = "BASELINE_ALREADY_RECORDED"
"""An estimation job ran again for a cycle that already has a baseline. Nothing
changed. Recorded rather than silently returned, for the same reason
``DIAGNOSIS_ALREADY_RECORDED`` is: a retry that keeps happening is a queue problem
worth seeing, and with no record the second run is indistinguishable from one that
never happened."""

POLICY_DECISION_RECORDED: Final = "POLICY_DECISION_RECORDED"
"""The policy engine reached a verdict. Carries all twelve ordered check outcomes, the
rule set version, the evaluation instant and the observed case state — written **before**
any authorization is released (R8.C12), so no external effect can exist without a record
of the decision that permitted it."""

RECOMMENDATION_RECORDED: Final = "RECOMMENDATION_RECORDED"
"""The value optimizer selected an action. Carries the whole comparison — every
candidate with its six figures, its exclusion reason and its rank — because R11.C6 wants
the single explanatory query to answer what else was considered and what it was worth."""

CANDIDATE_ESTIMATES_RECORDED: Final = "CANDIDATE_ESTIMATES_RECORDED"
"""The candidate set for a baseline was produced and persisted. Names every member,
its availability, and the method behind each of its four figures — so the record shows
that ``DO_NOTHING`` was priced on the same terms as everything else rather than
assumed."""

CANDIDATE_ESTIMATES_ALREADY_RECORDED: Final = "CANDIDATE_ESTIMATES_ALREADY_RECORDED"
"""A candidate-estimation job ran again for a baseline that already has a full set."""

CANDIDATE_ACTION_UNAVAILABLE: Final = "CANDIDATE_ACTION_UNAVAILABLE"
"""A candidate action was marked ``UNAVAILABLE`` with its reason, and **retained** in
the recorded set (R6.C9). One record per unavailable action.

Retention is the requirement and this record is its trail: the dashboard can show that
a retry was considered and why it could not be used, which is a different and much
more defensible statement than the action never appearing at all."""

CANDIDATE_MEMORY_UNAVAILABLE: Final = "CANDIDATE_MEMORY_UNAVAILABLE"
"""The segment query failed during candidate estimation. Every produced figure is a
configured prior marked ``UNCALIBRATED`` and no provider request was issued (R6.C11).
Unlike the baseline path this does not abandon the estimate: a candidate figure is
already a prior, so degrading it to a labelled prior loses nothing, whereas a missing
baseline would leave the whole comparison without a denominator."""

INVALID_ESTIMATE: Final = "INVALID_ESTIMATE"
"""An estimated figure fell outside its declared range or carried no recorded method.
The candidate is marked ``UNAVAILABLE`` and excluded from selection, and the record
names the action and the rejected figure (R6.C12). The figure is named rather than
merely counted, because the interesting question afterwards is which of the four went
wrong."""

# ---------------------------------------------------------------------------
# Case lifecycle — R2, R16
# ---------------------------------------------------------------------------

STATE_TRANSITION: Final = "STATE_TRANSITION"
"""A legal transition was applied: previous state, new state, counter effects."""

ILLEGAL_TRANSITION: Final = "ILLEGAL_TRANSITION"
"""A transition not in the table was requested. Nothing changed; recorded in its own
transaction because the requesting one is rejected."""

VERSION_CONFLICT: Final = "VERSION_CONFLICT"
"""Optimistic-concurrency loss: the case version moved under a writer. Nothing
changed; the caller must re-read before any external call."""

SCHEDULE_REJECTED: Final = "SCHEDULE_REJECTED"
"""An action could not be scheduled, naming the specific bound that refused it."""

CASE_EXPIRED: Final = "CASE_EXPIRED"
"""The recovery window elapsed; the case moved to ``EXPIRED`` recording the
unresolved amount."""

CASE_ESCALATED: Final = "CASE_ESCALATED"
"""Attempt exhaustion at or above ``ESCALATION_AMOUNT_THRESHOLD`` moved the case to
``ESCALATED`` rather than stopping it."""

DECISION_CYCLE_LIMIT_REACHED: Final = "DECISION_CYCLE_LIMIT_REACHED"
"""The decision-cycle counter reached its bound; the case terminates rather than
looping."""

WITHHELD_ACTION_DISCARDED: Final = "WITHHELD_ACTION_DISCARDED"
"""On restart, a withheld action whose bounds no longer permit execution was
discarded rather than run late."""

RECONCILED_TO_RECOVERED: Final = "RECONCILED_TO_RECOVERED"
"""A case already terminal for another reason was moved to ``RECOVERED`` on a
verified captured read. At most once per case."""

# ---------------------------------------------------------------------------
# Audit integrity and infrastructure — R11, R16
# ---------------------------------------------------------------------------

AUDIT_WRITE_FAILED: Final = "AUDIT_WRITE_FAILED"
"""An audit write did not reach durable storage within ``AUDIT_WRITE_TIMEOUT``. No
partial record persisted; the per-case block is set until an audit record for the
occurrence persists."""

JOB_DEAD_LETTERED: Final = "JOB_DEAD_LETTERED"
"""A job exhausted its attempt cap and moved to ``DEAD_LETTER``. Recorded so a poison
job is visible rather than silently abandoned."""


ALL_EVENT_TYPES: frozenset[str] = frozenset(
    {
        SIGNATURE_REJECTED,
        DUPLICATE_EVENT_DISCARDED,
        MALFORMED_EVENT,
        EVENT_QUARANTINED,
        OUT_OF_ORDER_EVENT,
        RATE_LIMIT_APPLIED,
        AUTHENTICATION_FAILED,
        EVENT_INGESTED,
        DETECTION_VERDICT_RECORDED,
        CASE_DETECTED,
        EVENT_ATTACHED_TO_CASE,
        DIAGNOSIS_RECORDED,
        DIAGNOSIS_ALREADY_RECORDED,
        DIAGNOSIS_UNMAPPED_REASON,
        DIAGNOSIS_SUBSTITUTED_TO_UNKNOWN,
        MERCHANT_INTEGRATION_FAULT,
        BASELINE_ESTIMATE_RECORDED,
        BASELINE_ESTIMATION_FAILED,
        BASELINE_ALREADY_RECORDED,
        CANDIDATE_ESTIMATES_RECORDED,
        RECOMMENDATION_RECORDED,
        POLICY_DECISION_RECORDED,
        CANDIDATE_ESTIMATES_ALREADY_RECORDED,
        CANDIDATE_ACTION_UNAVAILABLE,
        CANDIDATE_MEMORY_UNAVAILABLE,
        INVALID_ESTIMATE,
        STATE_TRANSITION,
        ILLEGAL_TRANSITION,
        VERSION_CONFLICT,
        SCHEDULE_REJECTED,
        CASE_EXPIRED,
        CASE_ESCALATED,
        DECISION_CYCLE_LIMIT_REACHED,
        WITHHELD_ACTION_DISCARDED,
        RECONCILED_TO_RECOVERED,
        AUDIT_WRITE_FAILED,
        JOB_DEAD_LETTERED,
        AUDIT_MUTATION_REJECTED,
        CREDENTIAL_UNAVAILABLE,
    }
)
"""Every event type declared in this phase. A test asserts a writer's type is a
member, which is what turns "no component invents a type string" from a convention
into a checked fact. Later phases extend the set as they add their record types."""
