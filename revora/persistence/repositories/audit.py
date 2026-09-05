"""Audit writes, and the one function that records a rejected mutation.

Appends only. There is no update method and no delete method on this repository, and
not because of discipline: the application role has no ``UPDATE`` or ``DELETE`` grant
on ``audit_record`` and a trigger raises on top of that, so a method that tried would
fail at the database. The absence here is documentation of a fact rather than the
mechanism enforcing it.

:meth:`AuditRecordRepository.append_for_case` allocates the sequence number and
inserts in one transaction. The caller must already hold the case row under ``FOR
UPDATE`` — which every audited occurrence does anyway, because an audited occurrence
is a state change or a decision recorded alongside the case read that produced it.

:func:`record_mutation_rejected` calls the insert-only SQL function installed by the
append-only migration. It exists because a rejected mutation attempt is itself an
audited event (R11.C9), and it cannot be written by ordinary application code — the
trigger has already aborted the transaction that tried the mutation, so the record
has to be written by something with its own privileges and its own transaction.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.orm import Session

from revora.persistence.models import AuditRecord
from revora.persistence.repositories.base import MerchantScopedRepository
from revora.persistence.repositories.cases import RecoveryCaseRepository

__all__ = ["AUDIT_MUTATION_REJECTED", "AuditRecordRepository", "record_mutation_rejected"]

AUDIT_MUTATION_REJECTED = "AUDIT_MUTATION_REJECTED"
"""The event type recorded when something tried to change an audit record."""


class AuditRecordRepository(MerchantScopedRepository[AuditRecord]):
    """Append-only audit writes and the reads that explain a case."""

    model = AuditRecord

    def append_for_case(
        self,
        merchant_id: uuid.UUID,
        case_id: uuid.UUID,
        *,
        event_type: str,
        actor: str,
        correlation_id: uuid.UUID,
        occurred_at: datetime,
        fields: dict[str, object] | None = None,
    ) -> AuditRecord:
        """Append a record for a case, allocating its sequence number.

        The allocation is an ``UPDATE ... RETURNING`` on the case row in this same
        transaction, so if this transaction rolls back the number is not consumed and
        the sequence stays gap-free.
        """
        seq = RecoveryCaseRepository(self.session).allocate_audit_seq(merchant_id, case_id)
        record = AuditRecord(
            merchant_id=merchant_id,
            case_id=case_id,
            seq=seq,
            event_type=event_type,
            actor=actor,
            correlation_id=correlation_id,
            occurred_at=occurred_at,
            **(fields or {}),
        )
        self.session.add(record)
        return record

    def append_unattached(
        self,
        merchant_id: uuid.UUID,
        *,
        event_type: str,
        actor: str,
        correlation_id: uuid.UUID,
        occurred_at: datetime,
        fields: dict[str, object] | None = None,
    ) -> AuditRecord:
        """Append a record that belongs to no case.

        A rejected signature, a rate limit, a failed login. ``case_id`` and ``seq``
        are both null — they move together, which the ``case_and_seq_together``
        check enforces, so an unattached record cannot look like a hole in some
        case's sequence.
        """
        record = AuditRecord(
            merchant_id=merchant_id,
            case_id=None,
            seq=None,
            event_type=event_type,
            actor=actor,
            correlation_id=correlation_id,
            occurred_at=occurred_at,
            **(fields or {}),
        )
        self.session.add(record)
        return record

    def list_for_case(
        self, merchant_id: uuid.UUID, case_id: uuid.UUID
    ) -> Sequence[AuditRecord]:
        """Every record for a case, in sequence order.

        Sequence order, not timestamp order. Two records can share a millisecond and
        the sequence is what actually says which came first.
        """
        statement = (
            self.scoped(merchant_id)
            .where(AuditRecord.case_id == case_id)
            .order_by(AuditRecord.seq)
        )
        return list(self.session.execute(statement).scalars())

    def list_for_correlation(
        self, merchant_id: uuid.UUID, correlation_id: uuid.UUID
    ) -> Sequence[AuditRecord]:
        """Every record sharing a correlation id, oldest first.

        This is the end-to-end trace of one inbound delivery, across every component
        and every job it spawned. Served by ``ix_audit_record_correlation_id``.
        """
        statement = (
            self.scoped(merchant_id)
            .where(AuditRecord.correlation_id == correlation_id)
            .order_by(AuditRecord.occurred_at)
        )
        return list(self.session.execute(statement).scalars())

    def export_window(
        self,
        merchant_id: uuid.UUID,
        *,
        start: datetime,
        end: datetime,
        limit: int,
    ) -> Sequence[AuditRecord]:
        """A page of records in a time window, for the audit export.

        ``merchant_id`` required, like every other read here. An export is the single
        most damaging place for a missing tenant filter, because its output leaves
        the system.
        """
        statement = (
            self.scoped(merchant_id)
            .where(AuditRecord.occurred_at >= start, AuditRecord.occurred_at < end)
            .order_by(AuditRecord.occurred_at)
            .limit(limit)
        )
        return list(self.session.execute(statement).scalars())


def record_mutation_rejected(
    session: Session,
    merchant_id: uuid.UUID,
    *,
    actor: str,
    operation: str,
    correlation_id: uuid.UUID,
) -> None:
    """Record that an audit mutation was attempted and refused, naming the actor.

    Calls the insert-only SQL function from the append-only migration. Must be
    called on a *fresh* transaction: the trigger that rejected the mutation has
    already aborted the one that attempted it, so writing this record on that
    session would fail with the original error still pending.
    """
    session.execute(
        text(
            "SELECT record_audit_mutation_rejected("
            ":merchant_id, :actor, :operation, :correlation_id)"
        ),
        {
            "merchant_id": str(merchant_id),
            "actor": actor,
            "operation": operation,
            "correlation_id": str(correlation_id),
        },
    )
