"""The explainability read: one ordered query, no joins, no derived state.

R11.C5 and R11.C6 ask that a merchant explaining a decision to a customer can get
the whole story of a case from the audit log without reconstructing it. That is a
constraint on this read: it is a single ordered ``SELECT`` over ``audit_record``,
and every record already carries the diagnosis, the full candidate set with net
values and exclusion reasons, and all twelve ordered policy check outcomes in its
own columns. There is nothing to join and nothing to infer — if answering "why did
this customer get this message" needed a second query, the audit design would have
failed its own test.

This module is the audit log's public read surface. Other components read case
history through here rather than reaching into the persistence repository, so the
"one query, no joins" guarantee has one enforcement point.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy.orm import Session

from revora.persistence.models import AuditRecord
from revora.persistence.repositories.audit import AuditRecordRepository

__all__ = ["case_history", "correlation_trace"]


def case_history(
    session: Session, merchant_id: uuid.UUID, case_id: uuid.UUID
) -> Sequence[AuditRecord]:
    """Every audit record for a case, in sequence order.

    Sequence order, not timestamp order: several records for one case can share a
    millisecond, and the gap-free sequence is what actually orders them. This is the
    single query behind the case explanation view.
    """
    return AuditRecordRepository(session).list_for_case(merchant_id, case_id)


def correlation_trace(
    session: Session, merchant_id: uuid.UUID, correlation_id: uuid.UUID
) -> Sequence[AuditRecord]:
    """Every record sharing a correlation id, oldest first.

    The end-to-end trace of one inbound delivery across every component and every
    asynchronous job it spawned — including the unattached records (a rejected
    signature, a rate limit) that have no case but share the id.
    """
    return AuditRecordRepository(session).list_for_correlation(merchant_id, correlation_id)
