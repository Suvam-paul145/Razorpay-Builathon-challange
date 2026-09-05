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
    "ACTION_CANCELLED_CONTACT_SUPPRESSED",
    "ACTION_CANCELLED_PAYMENT_RECEIVED",
    "ALL_EVENT_TYPES",
    "AUDIT_MUTATION_REJECTED",
    "AUDIT_WRITE_FAILED",
    "AUTHENTICATION_FAILED",
    "AUTHORIZATION_DENIED",
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
    "CASE_REVIEWED",
    "CONCURRENT_EXECUTION_PREVENTED",
    "CONSENT_RECORDED",
    "CONTROL_ACTION_SUPPRESSED",
    "CONTROL_CONTAMINATED",
    "CREDENTIAL_UNAVAILABLE",
    "CUSTOMER_DATA_REDACTED",
    "CUSTOMER_SIGNAL_LIMIT_REACHED",
    "CUSTOMER_SIGNAL_RECORDED",
    "CUSTOMER_SIGNAL_REJECTED",
    "CUSTOMER_SUBMISSION_LIMIT_REACHED",
    "CUSTOMER_TOKEN_EXPIRED",
    "CUSTOMER_TOKEN_ISSUED",
    "CUSTOMER_TOKEN_ISSUE_FAILED",
    "CUSTOMER_TOKEN_KEY_RETIRED",
    "CUSTOMER_TOKEN_REJECTED",
    "DECISION_CYCLE_LIMIT_REACHED",
    "DELAYED_RECOVERY_RECONCILED",
    "DETECTION_VERDICT_RECORDED",
    "DIAGNOSIS_ALREADY_RECORDED",
    "DIAGNOSIS_RECORDED",
    "DIAGNOSIS_SUBSTITUTED_TO_UNKNOWN",
    "DIAGNOSIS_UNMAPPED_REASON",
    "DUPLICATE_EVENT_DISCARDED",
    "DUPLICATE_RECOVERY_EVENT_DISCARDED",
    "EVENT_ATTACHED_TO_CASE",
    "EVENT_INGESTED",
    "EVENT_QUARANTINED",
    "EXECUTION_ABANDONED_POLICY",
    "EXECUTION_INTENT_PROMOTED",
    "EXECUTION_REFUSED",
    "EXECUTION_RESULT_UNKNOWN",
    "EXECUTION_RESULT_UNVERIFIABLE",
    "EXECUTION_STARTED",
    "EXPERIMENT_ANALYSED",
    "EXPERIMENT_ASSIGNMENT_RECORDED",
    "EXPERIMENT_ASSIGNMENT_SKIPPED",
    "EXPERIMENT_INVALIDATED",
    "HUMAN_OWNER_ASSIGNED",
    "HUMAN_OWNER_RELEASED",
    "ILLEGAL_TRANSITION",
    "INVALID_ESTIMATE",
    "JOB_DEAD_LETTERED",
    "MALFORMED_EVENT",
    "MERCHANT_INTEGRATION_FAULT",
    "MODEL_PROMOTED",
    "MODEL_VERSION_RECORDED",
    "OUT_OF_ORDER_EVENT",
    "PARTIAL_PAYMENT_OBSERVED",
    "PAYMENT_STATE_CONFLICT",
    "PAYMENT_STATE_READ_RECORDED",
    "PAYMENT_STATE_READ_UNAVAILABLE",
    "PAYMENT_STATE_UNVERIFIABLE",
    "POLICY_DECISION_RECORDED",
    "POST_PAYMENT_ACTION",
    "POST_SUPPRESSION_ACTION",
    "PROMISE_ALREADY_RECORDED",
    "PROMISE_RECORDED",
    "PROMISE_REJECTED",
    "RATE_LIMIT_APPLIED",
    "RECOMMENDATION_RECORDED",
    "RECONCILED_TO_RECOVERED",
    "RECOVERY_RECORDED",
    "SCHEDULE_REJECTED",
    "SESSION_ESTABLISHED",
    "SESSION_REVOKED",
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
"""A dashboard authentication attempt failed. Unattached to any case.

Covers a wrong operator key, an unknown token, a revoked session and an expired one. They are
one event type because they get one answer — 401 with no body — and distinguishing them to the
caller would make the endpoint an oracle. The *record* names which it was, because an operator
debugging a locked-out colleague needs the difference and an attacker never sees it."""

AUTHORIZATION_DENIED: Final = "AUTHORIZATION_DENIED"
"""An authenticated session asked for a record belonging to another merchant (R17.C3).

Answered **404, not 403**, so the response does not confirm the record exists. The record
carries the requester, the requested id and the timestamp, which is the trail a cross-tenant
probe leaves — and the reason the wrong answer to the caller is the right one here: the
information the caller is denied is exactly the information the record must keep."""

SESSION_ESTABLISHED: Final = "SESSION_ESTABLISHED"
"""A dashboard session was minted, naming the merchant user it acts as.

Recorded because the credential that mints it is a shared per-merchant operator key rather
than a user password. The key cannot say who used it; this record says which user the session
will be attributed to, so every later action has a named actor."""

SESSION_REVOKED: Final = "SESSION_REVOKED"
"""A session was explicitly ended. The reason a session is a row and not a signed token."""

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
"""The case was routed to a person rather than stopped, and the reason it was.

Two producers, and they share a type because they are one occurrence from the reader's side —
*this case now needs a human* — carrying the same field, ``unresolved_amount``, which is what a
merchant triages the ``ESCALATED`` grouping by.

- Attempt exhaustion at or above ``ESCALATION_AMOUNT_THRESHOLD``: enough money that giving up
  quietly is the wrong answer.
- A Hard_Stop_Reason (R21.C4, R21.C5): the customer disputed the charge or cancelled the order.
  Carries the ``hard_stop_reason``, the Suppression_Scope key and the ``terminal_reason``, so the
  record answers "why is this case in front of me" without a join.

The ``STATE_TRANSITION`` record ``apply_transition`` writes is the authority on the edge itself.
This one carries what that record has no field for, which is why it is a second record and not a
duplicate: a transition record answers *what moved*, and the amount still owed is not part of the
edge."""

DECISION_CYCLE_LIMIT_REACHED: Final = "DECISION_CYCLE_LIMIT_REACHED"
"""The decision-cycle counter reached its bound; the case terminates rather than
looping."""

CASE_REVIEWED: Final = "CASE_REVIEWED"
"""A case that had chosen restraint was looked at again (R30.C11).

Carries the ``ReviewTrigger``, the previously selected action, the newly selected action,
the decision-cycle counter *after* the review, and the newly persisted ``next_review_at``
where one exists.

**Written whether or not the selection changed**, and the unchanged case is the one this
record exists for. A review that re-selected ``WAIT`` produces no state visible anywhere
else — same state, same selected action, one more decision cycle spent — so without this
record "we re-examined and still think waiting is right" and "we forgot about it" are the
same row. One of those is the product working and the other is the defect R30 exists to
fix, and they must not be indistinguishable.

Also written on the terminating path, when a review finds the counter already at
``MAX_RECOVERY_ATTEMPTS`` and stops the case instead: the review happened, it reached a
conclusion, and the conclusion was that there are no cycles left to spend."""

WITHHELD_ACTION_DISCARDED: Final = "WITHHELD_ACTION_DISCARDED"
"""On restart, a withheld action whose bounds no longer permit execution was
discarded rather than run late."""

RECONCILED_TO_RECOVERED: Final = "RECONCILED_TO_RECOVERED"
"""A case already terminal for another reason was moved to ``RECOVERED`` on a
verified captured read. At most once per case."""

# ---------------------------------------------------------------------------
# Execution — R9
# ---------------------------------------------------------------------------

CONCURRENT_EXECUTION_PREVENTED: Final = "CONCURRENT_EXECUTION_PREVENTED"
"""``pg_try_advisory_xact_lock`` refused: another worker is already executing this
case. Abandoned with zero external calls (R9.C13).

Written **unattached**, unlike every other execution record. Allocating a case-attached
sequence number requires the case row under ``FOR UPDATE``, and the worker that holds
the advisory lock is holding that row — so recording "someone else is executing" against
the case would block on the very worker being reported. The case id travels in the
record's fields instead."""

EXECUTION_ABANDONED_POLICY: Final = "EXECUTION_ABANDONED_POLICY"
"""Policy was re-evaluated against freshly reloaded state and did not return
``APPROVED``. Zero external calls.

The re-evaluation is not a formality. The approval that scheduled this action was
computed against state that is now older than the job's time in the queue, and anything
could have happened in between — the customer may have paid, the window may have closed,
the merchant may have withdrawn consent. This record is the evidence that authority was
re-checked at the moment of acting rather than inherited from the past."""

EXECUTION_REFUSED: Final = "EXECUTION_REFUSED"
"""A structural precondition on the approval failed: absent, mismatched, expired or
already consumed. Names the failed check. State and both counters unchanged (R9.C12).

Distinct from ``EXECUTION_ABANDONED_POLICY`` on purpose. That one means the policy engine
said no, which is the system working. This one means the request did not carry a valid
authorization at all, which is either a bug or an attempt to replay one — and those
should not be searchable under the same name."""

EXECUTION_STARTED: Final = "EXECUTION_STARTED"
"""The intent is durable as ``ATTEMPTED``, the case is ``EXECUTING``, the decision is
consumed and the counters have moved — all committed **before** the provider call.

The ordering is the whole guarantee. After this record exists, a crash cannot lose the
knowledge that a call may have gone out; before it exists, no call can have gone out.
There is no window where the two disagree."""

EXECUTION_RESULT_UNKNOWN: Final = "EXECUTION_RESULT_UNKNOWN"
"""The call returned a timeout, a 5xx or an unparseable body, so whether the effect
exists is not known. The intent is ``UNCERTAIN`` and **every external call for that case
stops** until reconciliation resolves it (R9.C9).

Also written when a stale ``ATTEMPTED`` intent is promoted, because the two situations are
the same fact arriving by different routes: nobody knows whether the provider acted."""

EXECUTION_INTENT_PROMOTED: Final = "EXECUTION_INTENT_PROMOTED"
"""An ``ATTEMPTED`` intent older than ``PROVIDER_CALL_TIMEOUT`` was moved to
``UNCERTAIN``, by the sweeper or by the startup sequence.

``ATTEMPTED`` past the call timeout means the worker that owned it died mid-call. The
promotion routes it to reconciliation, which reads, and never to a retry, which would
call again. Counters are left exactly as they were until the resolution persists."""

EXECUTION_RESULT_UNVERIFIABLE: Final = "EXECUTION_RESULT_UNVERIFIABLE"
"""Reconciliation used its whole attempt bound without establishing whether the effect
exists. The case escalates to a human and **no further external call is ever issued for
it** (R9.C17).

The deliberately unsatisfying ending. The alternative — guess, and act on the guess — is
either a duplicate payment request or a case abandoned while a live link is outstanding.
An escalation a human can pick up is worse for the metrics and better for the customer."""

# ---------------------------------------------------------------------------
# Recovery memory and experiments — R13, R15
# ---------------------------------------------------------------------------

RECOVERY_OBSERVATION_RECORDED: Final = "RECOVERY_OBSERVATION_RECORDED"
"""One resolved case was flattened into a training observation, in the same transaction as its
terminal transition (R15.C1).

Carries ``intervention_status`` and whether the row is usable as a baseline training label,
because that is the field that decides whether this case can teach the estimator anything.
Most cannot: only a control-arm case with zero confirmed actions qualifies, and the count of
those is the real measure of how much Revora knows about what happens when it does nothing."""

EXPERIMENT_ASSIGNMENT_RECORDED: Final = "EXPERIMENT_ASSIGNMENT_RECORDED"
"""A case was assigned to an experiment arm, before any diagnosis ran (R13.C1, C2).

Before, not after, and the record proves the ordering. An arm chosen once the cause is known
is an arm chosen on the strength of the case, which destroys the comparison — so the audit
trail has to show that assignment preceded knowledge."""

EXPERIMENT_ASSIGNMENT_SKIPPED: Final = "EXPERIMENT_ASSIGNMENT_SKIPPED"
"""No arm was assigned, and the case runs the baseline workflow (R13.C14).

Either no experiment was active or the assignment could not be persisted. The important part
is what does *not* happen: an unassigned case is never quietly treated as treatment, because
that would put cases into the treatment arm that the randomization never selected."""

CONTROL_ACTION_SUPPRESSED: Final = "CONTROL_ACTION_SUPPRESSED"
"""A control-arm case produced a recommendation and it was withheld (R13.C3).

The recommendation is recorded, not discarded — that is what makes the control arm a
counterfactual record rather than an absence. For every control case we know what Revora would
have done, which is the comparison the whole experiment rests on."""

CONTROL_CONTAMINATED: Final = "CONTROL_CONTAMINATED"
"""A confirmed action reached a control case. The case is excluded from every reported result.

Contamination invalidates the comparison; hiding it would invalidate the claim instead. Note
the limit of what this can detect: a merchant phoning a customer is invisible to Revora, so an
uncontaminated control arm means "no contamination we could see"."""

EXPERIMENT_INVALIDATED: Final = "EXPERIMENT_INVALIDATED"
"""A frozen component changed while the experiment was ``ACTIVE`` (R13.C16).

Assignment stops and the experiment is labelled. A mid-experiment model promotion silently
changes what the treatment arm *is*, and the measured difference stops meaning anything — so
this is detected and recorded rather than absorbed."""

EXPERIMENT_ANALYSED: Final = "EXPERIMENT_ANALYSED"
"""An experiment result was computed: per-arm counts, the lift, its interval, and the labels.

Written for every analysis, including the ones that establish nothing. An interval containing
zero is a real finding and the most likely one, and a system that only recorded the flattering
analyses would be a system whose history could not be checked."""

MODEL_VERSION_RECORDED: Final = "MODEL_VERSION_RECORDED"
"""A model version row was created, ``INACTIVE`` (R15.C5, C11).

Training completing does not activate anything. The split between recording a version and
promoting it is what makes activation a decision a person takes rather than a side effect of a
job finishing."""

MODEL_PROMOTED: Final = "MODEL_PROMOTED"
"""A named person activated a model version (R15.C6).

``approving_user_id`` is ``NOT NULL`` in the schema, which is the same reasoning that puts the
tunable bounds in database rows rather than environment variables: a redeploy cannot supply an
approving user, and "why did the numbers move" should always have a name attached to it."""

# ---------------------------------------------------------------------------
# Outcome — R10
# ---------------------------------------------------------------------------

PAYMENT_STATE_READ_RECORDED: Final = "PAYMENT_STATE_READ_RECORDED"
"""One authoritative ``fetch_payment`` was performed and persisted. Carries the status,
whether it was captured, and the amounts.

Written for every read, including the ones that change nothing. A recovery figure is only
defensible if the reads behind it are enumerable, and a read that happened without a record
is a number nobody can check."""

PAYMENT_STATE_READ_UNAVAILABLE: Final = "PAYMENT_STATE_READ_UNAVAILABLE"
"""The authoritative read could not be completed — a 5xx, a timeout, or a body that would
not parse. No recovery is declared and no state moves.

The *count of consecutive* records of this type for a case is the attempt counter behind
R10.C7. Derived from the audit log rather than from a column, because the log is
append-only and gap-free per case, so the count cannot be lost or quietly reset — and
because a failed read produces no ``payment_state_read`` row to count instead."""

RECOVERY_RECORDED: Final = "RECOVERY_RECORDED"
"""A recovery was declared and counted, naming the read that verified it.

The only event type that may accompany money being reported as recovered. Its presence
without a ``verified_by_read_id`` is impossible by schema, which is the point."""

PAYMENT_STATE_CONFLICT: Final = "PAYMENT_STATE_CONFLICT"
"""Two or more signals disagree about a payment's state. The case holds in
``WAITING_FOR_OUTCOME``, no recovery is declared, and the read is retried.

Names both signals. Most of these resolve themselves on the next read — the design marks
read-lag after a webhook ``[EVIDENCE INSUFFICIENT]``, so a webhook arriving before the read
catches up looks exactly like a conflict."""

PAYMENT_STATE_UNVERIFIABLE: Final = "PAYMENT_STATE_UNVERIFIABLE"
"""The read attempt bound was exhausted without establishing the payment's state. The case
escalates, the last known state and the unresolved amount are preserved, and **no recovery
is declared** (R10.C7).

Declaring recovery here would be the single most damaging thing this system could do: it
would report money as recovered on the strength of a webhook nobody could corroborate."""

PARTIAL_PAYMENT_OBSERVED: Final = "PARTIAL_PAYMENT_OBSERVED"
"""A partial payment was observed. **Not recovery** (R10.C11); the case holds.

``accept_partial`` is false on every link Revora creates precisely so this is rare, but a
customer can still part-pay through another channel. Counting a partial as recovery would
inflate every figure by the difference."""

DUPLICATE_RECOVERY_EVENT_DISCARDED: Final = "DUPLICATE_RECOVERY_EVENT_DISCARDED"
"""A success signal arrived for a case already ``RECOVERED``. Discarded: no read is issued,
nothing changes, and the amount is not counted again (R10.C13).

Verified at-least-once delivery makes duplicate success signals ordinary, not exceptional.
The record exists so the discard is visible rather than silent."""

ACTION_CANCELLED_CONTACT_SUPPRESSED: Final = "ACTION_CANCELLED_CONTACT_SUPPRESSED"
"""A Contact_Suppression was persisted while an action was scheduled or queued and **no**
execution-intent record existed for it, so the action was cancelled before any external call
(R21.C6).

**One record per cancelled action**, keyed on the approved-but-unconsumed Policy_Decision that
authorized it — ``consumed_by_intent_id IS NULL`` is precisely R21.C6's "for which no
execution-intent record exists", and ``one_intent_per_decision`` makes the correspondence
one-to-one rather than approximate. So the count of these records for a case equals the count of
authorizations the hard stop invalidated, which is the number a merchant asking "what were you
about to send me" needs.

Both counters are unchanged, and that is structural rather than asserted here: the
executed-action and customer-message counters move only on the edge into ``EXECUTING``, and a
cancelled action never reaches it. No Payment_Provider request and no Communication_Provider
request is issued, for the same reason — the terminal transition this record accompanies makes
every queued execution job refuse on its own re-evaluation against reloaded state.

Its counterpart is ``ACTION_CANCELLED_PAYMENT_RECEIVED``, which is the same shape for a happier
cause. Two types rather than one with a reason field, because "the customer paid" and "the
customer objected" are the two endings a merchant most needs to be able to count separately."""

ACTION_CANCELLED_PAYMENT_RECEIVED: Final = "ACTION_CANCELLED_PAYMENT_RECEIVED"
"""A confirmed payment arrived while an action was scheduled and **no** intent existed, so
the action was cancelled before any external call. Counters unchanged (R10.C4).

The good outcome of the race: the customer paid on their own and we noticed in time to stay
quiet."""

POST_PAYMENT_ACTION: Final = "POST_PAYMENT_ACTION"
"""An intent already existed when the payment was confirmed, so the action went out after
the customer had already paid. Recorded ``is_post_payment`` and counted in
``unnecessary_action_count`` (R10.C5).

Deliberately visible. This is the cost of Revora being wrong — a customer contacted for
money they had already sent — and a system that hid it would be optimising its own numbers
rather than the merchant's outcome."""

DELAYED_RECOVERY_RECONCILED: Final = "DELAYED_RECOVERY_RECONCILED"
"""A case that had already ended for another reason turned out to have been paid. One
reconciliation transition to ``RECOVERED``, naming the superseded terminal state, with the
amount counted exactly once (R10.C14).

Permitted only against a verified capture, and at most once per case — the transition rule
carries both conditions, so this cannot become a second count of the same money."""

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

# ---------------------------------------------------------------------------
# Dashboard actions — R14, R17
# ---------------------------------------------------------------------------

HUMAN_OWNER_ASSIGNED: Final = "HUMAN_OWNER_ASSIGNED"
"""A merchant user took ownership of a case, suspending all automated action.

This is not a display preference. Policy check 7 fails while an owner is set, so this record
is the reason a case stopped producing actions — and without it, an operator looking at a
silent case would be reading the absence of automation as a bug."""

HUMAN_OWNER_RELEASED: Final = "HUMAN_OWNER_RELEASED"
"""Ownership was released and automation may resume from the next decision cycle."""

CONSENT_RECORDED: Final = "CONSENT_RECORDED"
"""A consent or opt-out was recorded against a ``customer_key``, with its source.

Keyed on the customer rather than the case, so it is authoritative for every policy
evaluation beginning after its ``effective_at`` — across the cases that already exist and the
ones that do not yet (R17.C10). Recorded unattached, because it is a statement about a person
and not about any one payment."""

# ---------------------------------------------------------------------------
# Customer_Access_Token — R18, R29
# ---------------------------------------------------------------------------

CUSTOMER_TOKEN_ISSUED: Final = "CUSTOMER_TOKEN_ISSUED"
"""A Customer_Access_Token was minted (R18.C12).

Carries the case id, the ``token_id``, the issuance and expiry instants, and the approved
candidate action whose execution the token accompanies. **No part of the secret**, on any
field: the secret has no reversible representation anywhere in the system, and this record is
the one most likely to be written by somebody who has the wire token in a local variable.

Written inside ``execute_approved_action``'s first transaction, alongside the intent insert and
before the provider call, so a token that exists is a token some message could have carried."""

CUSTOMER_TOKEN_ISSUE_FAILED: Final = "CUSTOMER_TOKEN_ISSUE_FAILED"
"""A token could not be minted for an approved customer-visible action (R18.C13).

Names the failure reason. The important part is what the record accompanies: the execution
attempt was abandoned, no provider request was issued, no counter moved and the case state is
unchanged — because the mint shares the intent insert's transaction, so a failed mint rolls
back the intent rather than requiring a compensating action.

Written in its own transaction *after* that rollback, for the same reason
``ILLEGAL_TRANSITION`` is: the record has to survive the rollback of the work it describes."""

CUSTOMER_TOKEN_REJECTED: Final = "CUSTOMER_TOKEN_REJECTED"
"""A presented token failed verification, or named no persisted record (R18.C6).

One event type for both, because the caller gets one answer — 404 with an empty body — and
R29.C6 requires the two to be indistinguishable in status and in body. The *record* names the
rejection category, because an operator debugging a customer who cannot open their link needs
the difference and an attacker never sees it.

``CUSTOMER_TOKEN_KEY_RETIRED`` is the third category recorded under this type. Carries **no
part of the presented token** — not a prefix, not a length, not the parsed handle when the
handle was not a real one."""

CUSTOMER_TOKEN_KEY_RETIRED: Final = "CUSTOMER_TOKEN_KEY_RETIRED"
"""The rejection category recorded when a token verifies against no *active* signing secret.

Its own constant rather than a bare string inside a ``CUSTOMER_TOKEN_REJECTED`` field, because
"how many customers were locked out by that rotation" is a question the audit log has to be
able to answer, and it cannot if the category is spelt differently by whoever wrote it.

**The caller sees 404, not the 410 R29.C14 asks for.** That is deliberate and it is stronger
than the requirement: distinguishing "signed by a retired key" from "not a real token" tells an
attacker that their guess had the right shape. The rotation is recorded here; the response
discloses nothing."""

CUSTOMER_TOKEN_EXPIRED: Final = "CUSTOMER_TOKEN_EXPIRED"
"""A token verified but the request arrived at or after its expiry instant (R18.C7).

Answered 410 with ``{"expired": true}`` and no other case field. The 410 discloses that a
token once existed; accepted, because ``CUSTOMER_TOKEN_ENTROPY_BITS`` makes enumeration
infeasible and a customer holding a dead link needs to be told it is dead rather than shown a
404 that reads as "wrong URL"."""

CUSTOMER_SIGNAL_RECORDED: Final = "CUSTOMER_SIGNAL_RECORDED"
"""A Customer_Signal was accepted and persisted (R19.C6, R29.C9).

**The actor is the ``token_id``**, which is R29.C9 extending R17.C9 by admitting a credential as an
actor where no Merchant_User initiated the operation. Nothing else in the audit log has a non-human
actor that is also a bearer credential's handle, and the handle is the whole of what may appear —
the secret has no reversible representation anywhere.

Carries the Customer_Signal_Kind, the submitted values, the signal id, and the correlation id
generated for the submission. Written inside the accepting transaction, alongside the signal insert,
the submission-count increment and the enqueued review: all four or none. So this record existing is
proof the other three did, and its absence is proof none of them did — which is what makes R29.C12's
"nothing persisted" a transaction boundary rather than a compensating action.

Exactly one per accepted write. The timeline's ``CUSTOMER_RESPONDED`` stage keys on the presence of
any record of this type, so a second one for a single submission would not be a duplicate line in a
log — it would be a case history claiming the customer said something twice."""

CUSTOMER_SIGNAL_REJECTED: Final = "CUSTOMER_SIGNAL_REJECTED"
"""A Customer_Signal write was refused for a reason that is not a bound being reached.

Two conditions today, and they are answered differently because they are different statements. A
field outside the declared request schema, or an enumeration member that does not exist, is 422 and
the record names **the field only** — never the submitted value, which is attacker-supplied text on
an endpoint reachable without a session (R19.C4, R20.C1). A case already holding a Terminal_State is
409 and the record names the Terminal_State, because that is the one thing the customer is entitled
to be told: the case ended, and nothing they write now will be read (R19.C8).

No signal is persisted, no submission count moves, and the Recovery_Case state and every counter are
left unchanged. The record is written in the same transaction as nothing else, which is why it
commits: there is no work for it to be atomic with."""

CUSTOMER_SIGNAL_LIMIT_REACHED: Final = "CUSTOMER_SIGNAL_LIMIT_REACHED"
"""A Recovery_Case already holds ``MAX_CUSTOMER_SIGNALS_PER_CASE`` signals (R19.C7).

Per **case**, and distinct from ``CUSTOMER_SUBMISSION_LIMIT_REACHED``, which is per
**token**. The two are not redundant even though the configured numbers happen to agree
today: a case can outlive a
token — a terminal-state revocation followed by a further approved action mints a second one — so a
customer could reach the per-case cap with submissions to spare on their current token, and reach
the per-token cap with the case nowhere near its own. An operator reading "this customer's
submission was refused" needs to know which of the two it was, because one is answered by raising a
bound and the other by looking at the case.

Answered 429 with every persisted signal unchanged."""

CUSTOMER_SUBMISSION_LIMIT_REACHED: Final = "CUSTOMER_SUBMISSION_LIMIT_REACHED"
"""A Customer_Access_Token reached ``CUSTOMER_TOKEN_MAX_SUBMISSIONS`` (R18.C9).

The durable bound of the whole write path, and the reason this record is worth reading: it is
written when the conditional ``UPDATE`` that increments ``accepted_submission_count``
matched no row, which is the *only* way the cap can be observed. There is no read-then-compare
above it that a
concurrent request could interleave with, so this record cannot be written while a submission that
exceeded the cap succeeded elsewhere.

Answered 429, and **the read projection keeps being served until expiry**. R18.C9 is explicit about
that asymmetry: a customer who has explained themselves five times must not lose the page telling
them what they owe as a consequence."""

PROMISE_RECORDED: Final = "PROMISE_RECORDED"
"""A Promise_To_Pay was persisted, with the clamp computed (R23.C8).

**The record is written so that a reader can check the clamp from the audit trail alone**, which
is R23.C8's stated purpose and the reason this is not folded into ``CUSTOMER_SIGNAL_RECORDED``.
It carries the Promise_Date, the computed Follow_Up_Instant, the Recovery_Window end timestamp
and the ``token_id`` in the actor field, so ``follow_up_at ≤ window_end -
PROMISE_WINDOW_SAFETY_MARGIN`` — half of Property 42 — is verifiable from three fields of one
record without joining to ``promise_to_pay`` or to ``recovery_case``. A reader who had to join
could not check a clamp against a window end that a later migration had moved; a reader of this
record can, because the record holds the window end as it stood.

**Written for the ``BEYOND_WINDOW_ESCALATED`` path too, not only for ``RECORDED``.** R23.C8 names
``RECORDED``, and writing one for the escalated status as well is strictly more information than
the clause asks for rather than a different record: the alternative is a promise that was
persisted, escalated a case and left no trace in the transaction that accepted it, so the only
audit evidence would be the worker's ``CASE_ESCALATED`` some seconds later. The ``status`` field
distinguishes the two, and ``follow_up_at`` is null on the escalated one — which is
``escalated_schedules_nothing`` as an audit fact beside the database one.

Written inside the accepting transaction, immediately before ``CUSTOMER_SIGNAL_RECORDED``, which
stays last. So this record existing is proof the ``promise_to_pay`` row and the
``customer_signal`` row it names both committed, and its absence is proof neither did."""

PROMISE_REJECTED: Final = "PROMISE_REJECTED"
"""A Promise_To_Pay submission was refused for a reason about the date itself (R23.C2).

Two conditions, both 422 and both persisting nothing — no promise, no signal, no submission-count
increment. A Promise_Date at or before the submission instant is the degenerate one and consults
no bound: ``CHECK (promise_date > recorded_at)`` means such a date is not a promise the system can
*hold*, so refusing it is a statement about storability. A Promise_Date inside
``PROMISE_MIN_LEAD_TIME`` is the configured one, and the record names **the bound key** —
``PROMISE_MIN_LEAD_TIME`` — rather than the interval or the submitted date, because R23.C2 asks
the rejection to name the lead-time rule and the submitted date is attacker-supplied text on an
endpoint reachable without a session.

Distinct from ``CUSTOMER_SIGNAL_REJECTED`` because the two answer different operational
questions. That one covers a malformed request or a case that has ended; this one covers a
well-formed request about a date, and "how many customers named a date too soon to be useful" is
a question about the *bound*, answerable only if these refusals are separable from schema
rejections in the log."""

PROMISE_ALREADY_RECORDED: Final = "PROMISE_ALREADY_RECORDED"
"""A Recovery_Case already holds ``MAX_PROMISES_PER_CASE`` promises (R23.C7). 409.

**The signal is still persisted, and that asymmetry is the whole of R23.C7.** Unlike every other
refusal on this surface, this one keeps the write: the Customer_Signal is recorded for
Recovery_Memory, the submission count increments, and only the ``promise_to_pay`` row is refused.
A customer saying "actually, Friday" is evidence about this case whether or not the system will
hold a second promise, and discarding it would lose the one fact a future model most wants —
that the first promise was already being revised.

So a case can hold both this record and a ``CUSTOMER_SIGNAL_RECORDED`` for one submission, and
that is not a contradiction: the first says the promise was refused, the second says the
submission was kept. The persisted Promise_To_Pay, its Promise_Status and its Follow_Up_Instant
are all left exactly as they stand, which is the rest of the clause.

Recorded when the application check against the configured bound refuses, and also when
``uq_promise_to_pay_merchant_id_case_id`` refuses behind it — the backstop path, reached only by
two concurrent submissions. Both produce this record, because from the customer's side they are
the same answer."""

CUSTOMER_DATA_REDACTED: Final = "CUSTOMER_DATA_REDACTED"
"""Contact data was deleted or irreversibly masked after ``CUSTOMER_DATA_RETENTION`` elapsed.

Names the applied retention configuration version (R17.C11), because "we deleted it on time"
is a claim about the bound that was in force, and the bound is configurable. The non-identifying
fields metrics depend on are retained — a retention sweep that also destroyed the amount would
make every historical figure irreproducible, which is a different failure from a privacy one and
just as real."""


POST_SUPPRESSION_ACTION: Final = "POST_SUPPRESSION_ACTION"
"""An execution intent was already ``ATTEMPTED``, ``CONFIRMED`` or ``UNCERTAIN`` when a
Contact_Suppression was persisted (R21.C7).

The unhappy half of the suppression's arrival, and the honest one: something had already gone out,
or may have. There is no undoing it, so the record exists to say so rather than to pretend the
suppression arrived in time. **No further external call is issued** for the case, and the intent
resolves through the existing reconciliation path of R9.C15 rather than through anything this
requirement adds — a suppressed case still has to find out whether its payment link exists, and
that question is answered by a read, not by a message.

Written by the Outcome_Monitor, beside ``POST_PAYMENT_ACTION``, which is the same shape for the
same reason: an action whose timing turned out to be wrong, counted where a person is looking.
Unlike that one it carries no ``is_post_payment``-style flag on the intent row, because there is
no column for it and none is needed — the suppression handler is deduped by
``contact_suppression:{case_id}`` and returns early on an already-terminal case, so a retried job
does not write a second record."""


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
        CASE_REVIEWED,
        DECISION_CYCLE_LIMIT_REACHED,
        WITHHELD_ACTION_DISCARDED,
        RECONCILED_TO_RECOVERED,
        CONCURRENT_EXECUTION_PREVENTED,
        EXECUTION_ABANDONED_POLICY,
        EXECUTION_REFUSED,
        EXECUTION_STARTED,
        EXECUTION_RESULT_UNKNOWN,
        EXECUTION_INTENT_PROMOTED,
        EXECUTION_RESULT_UNVERIFIABLE,
        PAYMENT_STATE_READ_RECORDED,
        PAYMENT_STATE_READ_UNAVAILABLE,
        RECOVERY_RECORDED,
        PAYMENT_STATE_CONFLICT,
        PAYMENT_STATE_UNVERIFIABLE,
        PARTIAL_PAYMENT_OBSERVED,
        DUPLICATE_RECOVERY_EVENT_DISCARDED,
        ACTION_CANCELLED_PAYMENT_RECEIVED,
        ACTION_CANCELLED_CONTACT_SUPPRESSED,
        POST_PAYMENT_ACTION,
        POST_SUPPRESSION_ACTION,
        DELAYED_RECOVERY_RECONCILED,
        RECOVERY_OBSERVATION_RECORDED,
        EXPERIMENT_ASSIGNMENT_RECORDED,
        EXPERIMENT_ASSIGNMENT_SKIPPED,
        CONTROL_ACTION_SUPPRESSED,
        CONTROL_CONTAMINATED,
        EXPERIMENT_INVALIDATED,
        EXPERIMENT_ANALYSED,
        MODEL_VERSION_RECORDED,
        MODEL_PROMOTED,
        AUDIT_WRITE_FAILED,
        JOB_DEAD_LETTERED,
        AUDIT_MUTATION_REJECTED,
        CREDENTIAL_UNAVAILABLE,
        AUTHORIZATION_DENIED,
        SESSION_ESTABLISHED,
        SESSION_REVOKED,
        HUMAN_OWNER_ASSIGNED,
        HUMAN_OWNER_RELEASED,
        CONSENT_RECORDED,
        CUSTOMER_DATA_REDACTED,
        CUSTOMER_SIGNAL_RECORDED,
        CUSTOMER_SIGNAL_REJECTED,
        CUSTOMER_SIGNAL_LIMIT_REACHED,
        CUSTOMER_SUBMISSION_LIMIT_REACHED,
        PROMISE_RECORDED,
        PROMISE_REJECTED,
        PROMISE_ALREADY_RECORDED,
        CUSTOMER_TOKEN_ISSUED,
        CUSTOMER_TOKEN_ISSUE_FAILED,
        CUSTOMER_TOKEN_REJECTED,
        CUSTOMER_TOKEN_KEY_RETIRED,
        CUSTOMER_TOKEN_EXPIRED,
    }
)
"""Every event type declared in this phase. A test asserts a writer's type is a
member, which is what turns "no component invents a type string" from a convention
into a checked fact. Later phases extend the set as they add their record types."""
