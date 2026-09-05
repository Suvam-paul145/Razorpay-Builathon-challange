"""Case re-evaluation on restart, from persisted rows only.

The design's restart sequence has several steps — verify migrations, run the
signature canary, promote stale ``ATTEMPTED`` intents, reload non-terminal cases and
re-evaluate their timing, discard withheld actions whose bounds no longer permit
execution. Only the *case* steps belong here: the schema check lives in
``persistence`` (below this layer), and the signature canary and intent promotion
belong to ``ingestion`` and ``execution`` (above this layer). Putting any of them in
this module would invert the dependency rule. So the full sequence is composed by the
process bootstrap in ``jobs``/``api``, which sits above every layer it orchestrates;
this module supplies the piece that re-evaluates cases.

What re-evaluation means for a case is deliberately narrow, because the design made
it narrow: every timing bound is a comparison of persisted timestamps, and every
counter is monotonic and already persisted. There is nothing to reconstruct. The one
thing that must happen on restart is applying the elapse that happened while nothing
was running — a case whose window closed during the outage has to be expired now,
not whenever a job next happens to touch it. That is exactly the sweep, run to
completion rather than one bounded pass, because at startup draining the backlog is
the job rather than a side effect of a periodic tick.

The counters are not re-derived. Re-deriving a monotonic counter from history is how
a replay reintroduces the double-count the counter exists to prevent; the persisted
value is authoritative by construction (R2.C7, R16.C6).
"""

from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from revora.cases.sweeper import DEFAULT_SWEEP_LIMIT, sweep_expired_cases
from revora.platform.logging import get_logger

try:  # pragma: no cover - typing convenience only
    from sqlalchemy.orm import sessionmaker
except ImportError:  # pragma: no cover
    sessionmaker = object  # type: ignore[assignment,misc]

__all__ = ["MAX_STARTUP_SWEEP_PASSES", "reevaluate_cases_on_startup"]

MAX_STARTUP_SWEEP_PASSES: int = 100
"""A ceiling on startup drain passes, so a bug that keeps producing due cases cannot
spin here forever. At the default sweep limit this drains 50,000 overdue cases, well
beyond any realistic downtime backlog at MVP scale."""


def reevaluate_cases_on_startup(
    merchant_id: uuid.UUID,
    *,
    factory: sessionmaker[Session] | None = None,
) -> int:
    """Apply, at startup, every window expiry that came due during downtime.

    Runs the expiry sweep in bounded passes until a pass finds nothing due, so the
    whole backlog is cleared before the process starts taking new work. Returns the
    total number of cases expired.
    """
    logger = get_logger(__name__)
    total = 0
    for _ in range(MAX_STARTUP_SWEEP_PASSES):
        expired = sweep_expired_cases(merchant_id, limit=DEFAULT_SWEEP_LIMIT, factory=factory)
        total += expired
        if expired < DEFAULT_SWEEP_LIMIT:
            break
    if total:
        logger.info("startup re-evaluation expired overdue cases", merchant_id=str(merchant_id),
                    expired=total)
    return total
