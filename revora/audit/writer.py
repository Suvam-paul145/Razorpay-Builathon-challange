"""The audit writer: masks at write time, truncates the oversized, blocks on failure.

Three responsibilities the repository underneath does not have, because they are
policy rather than storage:

**Masking happens here, at emission.** Every field routes through the task-4.2
masking serializer before it reaches the row, so there is no path on which an
unmasked contact, instrument reference or ``provider_short_url`` reaches durable
storage and is masked later on the way out (R11.C8, P32). A reader-side mask is a
display convention; this is the property.

**Oversized fields are truncated and named.** A value longer than
``MAX_AUDIT_FIELD_LENGTH`` is cut and its column recorded in ``truncated_fields``,
so a shortened value is never mistaken for the whole value (R11.C11). Provider
error descriptions and rejected AI responses are the fields that hit this.

**A failed audit write blocks further external action for the case.** R11.C10 and
P29: if an audit record cannot be persisted, no partial record is written and the
Execution_Engine must withhold every further external call for that case until an
audit record for the occurrence persists. The common case is covered for free —
the audit write shares the caller's transaction with the state change (R16.C1), so
a failure rolls the whole thing back and state and audit cannot diverge. The
per-case block below covers the residual case of a record written in its own
transaction.

The gap-free per-case sequence is not implemented here. It is allocated by
``AuditRecordRepository.append_for_case`` from a counter on the case row the caller
already holds under ``FOR UPDATE`` — which is what makes it gap-free under
concurrency — and this module calls that. Sequencing lives with the row lock, not
with the masking.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Final

from sqlalchemy.orm import Session

from revora.persistence.models import AuditRecord
from revora.persistence.repositories.audit import AuditRecordRepository
from revora.platform.clock import now
from revora.platform.logging import current_correlation_id, get_logger
from revora.platform.masking import MASK_DISCLOSURE_LENGTH, mask_record

__all__ = [
    "DEFAULT_MAX_AUDIT_FIELD_LENGTH",
    "AuditEntry",
    "AuditWriter",
    "block_case",
    "clear_case_block",
    "is_case_blocked",
]

_logger = get_logger(__name__)

DEFAULT_MAX_AUDIT_FIELD_LENGTH: Final[int] = 8000
"""Mirrors the ``MAX_AUDIT_FIELD_LENGTH`` configuration default. Used only when a
caller does not pass the configured value; the configured one wins where present."""

#: JSONB and string columns that can carry a value derived from a provider payload,
#: so they are the ones the truncation walk descends into. The state and action
#: columns are enum members and short by construction.
_JSON_FIELDS: Final[frozenset[str]] = frozenset(
    {"diagnosis", "evidence", "decision", "policy_result"}
)
_TEXT_FIELDS: Final[frozenset[str]] = frozenset(
    {"previous_state", "new_state", "action", "action_result", "idempotency_key"}
)


@dataclass(frozen=True, slots=True)
class AuditEntry:
    """One audit record's payload, before masking, truncation and sequencing.

    A frozen struct rather than keyword soup at the call site, so a writer records
    ``AuditEntry(event_type=..., actor=...)`` and the recognized fields are a fixed,
    reviewable set rather than an open ``**kwargs`` that silently swallows a typo.
    """

    event_type: str
    actor: str
    previous_state: str | None = None
    new_state: str | None = None
    action: str | None = None
    action_result: str | None = None
    idempotency_key: str | None = None
    confidence: Decimal | None = None
    diagnosis: Mapping[str, Any] | None = None
    evidence: Mapping[str, Any] | None = None
    decision: Mapping[str, Any] | None = None
    policy_result: Mapping[str, Any] | None = None


# ---------------------------------------------------------------------------
# Per-case block (R11.C10, P29)
# ---------------------------------------------------------------------------

_blocked_cases: set[tuple[uuid.UUID, uuid.UUID]] = set()
"""Process-local set of ``(merchant_id, case_id)`` whose external action is withheld
because an audit record could not be persisted. Process-local is sufficient: the
Execution_Engine that consults it runs in the same worker, and a restart re-derives
the block from the ``ATTEMPTED`` intents it reconciles (task 20.6)."""


def block_case(merchant_id: uuid.UUID, case_id: uuid.UUID) -> None:
    """Withhold further external action for a case after an audit-write failure."""
    _blocked_cases.add((merchant_id, case_id))


def is_case_blocked(merchant_id: uuid.UUID, case_id: uuid.UUID) -> bool:
    """True while a case's external action is withheld for a failed audit write.

    The Execution_Engine calls this before any provider request. Nothing else
    should need it.
    """
    return (merchant_id, case_id) in _blocked_cases


def clear_case_block(merchant_id: uuid.UUID, case_id: uuid.UUID) -> None:
    """Release the block once an audit record for the occurrence has persisted."""
    _blocked_cases.discard((merchant_id, case_id))


# ---------------------------------------------------------------------------
# The writer
# ---------------------------------------------------------------------------


class AuditWriter:
    """Appends masked, truncated, sequence-numbered audit records.

    Constructed per unit of work with the session it should write on and the two
    length bounds that govern masking and truncation. The bounds default to the
    module fallbacks; a caller that has loaded configuration passes the configured
    values so a merchant's ``MASK_DISCLOSURE_LENGTH`` is honoured.
    """

    __slots__ = ("_disclosure_length", "_max_field_length", "_repo", "_session")

    def __init__(
        self,
        session: Session,
        *,
        disclosure_length: int = MASK_DISCLOSURE_LENGTH,
        max_field_length: int = DEFAULT_MAX_AUDIT_FIELD_LENGTH,
    ) -> None:
        self._session = session
        self._repo = AuditRecordRepository(session)
        self._disclosure_length = disclosure_length
        self._max_field_length = max_field_length

    def write_for_case(
        self,
        merchant_id: uuid.UUID,
        case_id: uuid.UUID,
        entry: AuditEntry,
        *,
        correlation_id: uuid.UUID | None = None,
        occurred_at: datetime | None = None,
    ) -> AuditRecord:
        """Append a record for a case, allocating its gap-free sequence number.

        The caller must already hold the case row under ``FOR UPDATE`` — every
        audited case occurrence is a state change recorded alongside the read that
        produced it, so the lock is already held and the sequence allocation
        piggybacks on it.
        """
        fields, truncated = self._prepare(entry)
        return self._repo.append_for_case(
            merchant_id,
            case_id,
            event_type=entry.event_type,
            actor=entry.actor,
            correlation_id=correlation_id or _ambient_correlation_id(),
            occurred_at=occurred_at or now(),
            fields=self._with_truncated(fields, truncated),
        )

    def write_unattached(
        self,
        merchant_id: uuid.UUID,
        entry: AuditEntry,
        *,
        correlation_id: uuid.UUID | None = None,
        occurred_at: datetime | None = None,
    ) -> AuditRecord:
        """Append a record that belongs to no case.

        A rejected signature, a rate limit, a failed login. ``case_id`` and ``seq``
        are both null and the record relies on its correlation id instead.
        """
        fields, truncated = self._prepare(entry)
        return self._repo.append_unattached(
            merchant_id,
            event_type=entry.event_type,
            actor=entry.actor,
            correlation_id=correlation_id or _ambient_correlation_id(),
            occurred_at=occurred_at or now(),
            fields=self._with_truncated(fields, truncated),
        )

    # -- internals -------------------------------------------------------------

    def _prepare(self, entry: AuditEntry) -> tuple[dict[str, Any], list[str]]:
        """Mask every field and truncate the oversized, returning the column dict
        and the names that were shortened."""
        raw: dict[str, Any] = {}
        if entry.previous_state is not None:
            raw["previous_state"] = entry.previous_state
        if entry.new_state is not None:
            raw["new_state"] = entry.new_state
        if entry.action is not None:
            raw["action"] = entry.action
        if entry.action_result is not None:
            raw["action_result"] = entry.action_result
        if entry.idempotency_key is not None:
            raw["idempotency_key"] = entry.idempotency_key
        if entry.confidence is not None:
            raw["confidence"] = entry.confidence
        for name in _JSON_FIELDS:
            value = getattr(entry, name)
            if value is not None:
                raw[name] = dict(value)

        # Mask first, truncate second: masking can lengthen nothing (it only ever
        # shortens or preserves), so truncation applied after it still bounds the
        # stored value, and a masked-then-truncated field is never a partial secret.
        masked = mask_record(raw, disclosure_length=self._disclosure_length)
        truncated: list[str] = []
        prepared: dict[str, Any] = {}
        for name, value in masked.items():
            if name in _JSON_FIELDS:
                shortened, was_cut = self._truncate_json(value)
                prepared[name] = shortened
            elif name in _TEXT_FIELDS:
                shortened, was_cut = self._truncate_text(value)
                prepared[name] = shortened
            else:
                prepared[name] = value
                was_cut = False
            if was_cut:
                truncated.append(name)
        return prepared, truncated

    def _truncate_text(self, value: Any) -> tuple[Any, bool]:
        if isinstance(value, str) and len(value) > self._max_field_length:
            return value[: self._max_field_length], True
        return value, False

    def _truncate_json(self, value: Any) -> tuple[Any, bool]:
        """Truncate string leaves inside a JSONB value.

        Descends the structure and cuts any string longer than the bound. The
        column is reported truncated if any leaf was, which is what stops an
        oversized nested value from being read as complete.
        """
        cut = False

        def walk(node: Any) -> Any:
            nonlocal cut
            if isinstance(node, str) and len(node) > self._max_field_length:
                cut = True
                return node[: self._max_field_length]
            if isinstance(node, Mapping):
                return {key: walk(item) for key, item in node.items()}
            if isinstance(node, list):
                return [walk(item) for item in node]
            return node

        return walk(value), cut

    def _with_truncated(
        self, fields: dict[str, Any], truncated: list[str]
    ) -> dict[str, Any]:
        if truncated:
            fields = {**fields, "truncated_fields": sorted(truncated)}
        return fields


def _ambient_correlation_id() -> uuid.UUID:
    """The correlation id from the logging context, as a UUID.

    The context holds a string (a uuid, or ``"unset"`` outside any context). A
    record with no inherited id gets a fresh one rather than failing — an audit
    record with a generated correlation id is still traceable; a dropped audit
    record is not.
    """
    ambient = current_correlation_id()
    try:
        return uuid.UUID(ambient)
    except (ValueError, AttributeError):
        return uuid.uuid4()
