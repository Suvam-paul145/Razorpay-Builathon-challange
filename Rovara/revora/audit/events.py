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
    "CASE_DETECTED",
    "CASE_ESCALATED",
    "CASE_EXPIRED",
    "CREDENTIAL_UNAVAILABLE",
    "DECISION_CYCLE_LIMIT_REACHED",
    "DETECTION_VERDICT_RECORDED",
    "DUPLICATE_EVENT_DISCARDED",
    "EVENT_ATTACHED_TO_CASE",
    "EVENT_INGESTED",
    "EVENT_QUARANTINED",
    "ILLEGAL_TRANSITION",
    "JOB_DEAD_LETTERED",
    "MALFORMED_EVENT",
    "OUT_OF_ORDER_EVENT",
    "RATE_LIMIT_APPLIED",
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
