"""The lifecycle sweeper: the safety net that makes termination independent of jobs.

Every timing rule in the case lifecycle is also enforced here, from persisted
timestamps, so a case terminates on time even if the job that would have advanced it
was never scheduled or was lost (R2.C13, R16.C6). The sweeper visits every
non-terminal case whose recovery window has closed and expires it — reading the
window end from the row, never recomputing it from the current
``RECOVERY_WINDOW_DURATION``, because the bound may have changed since the case was
created and a live case must not be expired or reprieved by a configuration edit.

It does not depend on any earlier job having run. That is the whole point: the
scheduler enqueues a sweep at least once per ``LIFECYCLE_EVALUATION_INTERVAL`` (task
7.3), and the sweep re-derives what is due from the database rather than from a
queue of pending transitions. If the queue is empty and the worker has been idle,
the next sweep still terminates a case whose window elapsed while nothing was
running.

Each expiry goes through ``apply_transition`` like every other state change, so it
is legal, versioned, audited and counted by the one writer. A version conflict
(another writer moved the case between the sweep's read and the transition) is not
an error — the case is simply revisited on the next sweep.
"""

from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from revora.cases.manager import apply_transition
from revora.domain.enums import CaseState, TerminalReason
from revora.persistence.repositories.cases import RecoveryCaseRepository
from revora.persistence.repositories.session import tenant_transaction
from revora.platform.clock import now
from revora.platform.logging import get_logger

try:  # pragma: no cover - typing convenience only
    from sqlalchemy.orm import sessionmaker
except ImportError:  # pragma: no cover
    sessionmaker = object  # type: ignore[assignment,misc]

__all__ = ["DEFAULT_SWEEP_LIMIT", "sweep_expired_cases"]

_logger = get_logger(__name__)

DEFAULT_SWEEP_LIMIT: int = 500
"""How many due cases one sweep pass handles. A bound rather than "all of them" so a
backlog after an outage is drained in bounded passes instead of one transaction that
holds locks on thousands of rows."""

_SWEEPER_ACTOR = "lifecycle_sweeper"


def sweep_expired_cases(
    merchant_id: uuid.UUID,
    *,
    limit: int = DEFAULT_SWEEP_LIMIT,
    factory: sessionmaker[Session] | None = None,
) -> int:
    """Expire every non-terminal case whose recovery window has closed.

    Returns the number expired. Reads the due set in one transaction, then expires
    each in its own transaction through ``apply_transition`` — the read is not held
    across the writes, so a long backlog does not hold a lock while the whole batch
    is processed.
    """
    moment = now()
    with tenant_transaction(merchant_id, factory) as session:
        due = [
            (case.id, case.version)
            for case in RecoveryCaseRepository(session).list_due_for_lifecycle(
                merchant_id, now=moment, limit=limit
            )
        ]

    expired = 0
    for case_id, version in due:
        result = apply_transition(
            merchant_id,
            case_id,
            expected_version=version,
            target_state=CaseState.EXPIRED,
            reason="recovery window elapsed",
            actor=_SWEEPER_ACTOR,
            terminal_reason=TerminalReason.RECOVERY_WINDOW_ELAPSED,
            factory=factory,
        )
        if result.applied:
            expired += 1
    if expired:
        _logger.info("lifecycle sweep expired cases", expired=expired, considered=len(due))
    return expired
