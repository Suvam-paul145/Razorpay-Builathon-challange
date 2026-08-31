"""The append-only audit log: the record that makes an explanation possible.

Public surface:

* :mod:`revora.audit.events` — the one place every ``event_type`` string is declared.
* :class:`revora.audit.writer.AuditWriter` — masks at write time, truncates the
  oversized, allocates the gap-free per-case sequence, and blocks a case's external
  action if a record cannot be persisted.
* :mod:`revora.audit.queries` — the single-query explainability read.
"""

from __future__ import annotations

from revora.audit.writer import (
    AuditEntry,
    AuditWriter,
    block_case,
    clear_case_block,
    is_case_blocked,
)

__all__ = [
    "AuditEntry",
    "AuditWriter",
    "block_case",
    "clear_case_block",
    "is_case_blocked",
]
