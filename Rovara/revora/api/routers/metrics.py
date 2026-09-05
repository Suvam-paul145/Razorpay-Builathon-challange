"""Metrics summary and the unresolved grouping. Two endpoints, three honesty rules.

**Incremental revenue is time-bounded on its own.** It is the expensive figure — it reads experiment
results and both arms' outcomes — and R14.C16 requires a figure that could not be produced in time
to degrade *alone*. So it is computed in its own transaction under a Postgres ``statement_timeout``
derived from ``DASHBOARD_METRICS_TIMEOUT``; if it is cancelled, that field becomes a
data-unavailable marker and every other figure in the response still returns with its timestamps.

The timeout is enforced by the database rather than by a Python timer, and the difference matters: a
``signal.alarm`` or a thread with a deadline leaves the query running on the server, so a dashboard
that times out repeatedly would pile up work on the database it is already struggling to read. A
``statement_timeout`` cancels the query itself.

**Degrading the incremental figure does not drop its caveat.** When it is unavailable the report
still carries ``CAUSALITY_NOT_ESTABLISHED``, because a figure we could not compute is certainly not
a causal claim we established. The failure direction is the safe one.

**The unresolved grouping always returns all five groups.** A ``GROUP BY`` returns rows only for the
states that occurred, so a period with no blocked cases would have no blocked row — and an absent
row renders as nothing, which reads as "we did not look".
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Query
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, OperationalError

from revora.api.auth import AuthenticatedSession
from revora.api.deps import TenantSession
from revora.api.views import metrics_document, unresolved_view
from revora.domain.enums import RiskCause
from revora.domain.segments import AmountBand
from revora.metrics.engine import (
    IncrementalFinding,
    ReportingPeriod,
    SegmentKey,
    compute_metrics,
    incremental_finding,
)
from revora.persistence.repositories.session import tenant_transaction
from revora.platform.clock import now
from revora.platform.logging import get_logger

__all__ = ["router"]

_logger = get_logger(__name__)

router = APIRouter(tags=["metrics"])

_DEFAULT_PERIOD = timedelta(days=30)
"""The window used when a caller names neither end.

Thirty days rather than "all time". An all-time default would make the first dashboard load scan
every case a merchant has ever had, and it would answer a question nobody asked — a recovery rate
over an unbounded window mixes a merchant's first fumbling week with their current operation."""


class MetricsResponse(BaseModel):
    report: dict[str, object]
    incremental_available: bool


def _period(start: datetime | None, end: datetime | None) -> ReportingPeriod:
    """Resolve the reporting window, defaulting to the last thirty days.

    Half-open ``[start, end)``, enforced by ``ReportingPeriod`` itself, so adjacent periods
    partition cases exactly and the sum of two months cannot exceed the quarter.
    """
    finish = end or now()
    begin = start or (finish - _DEFAULT_PERIOD)
    return ReportingPeriod(start=begin, end=finish)


@router.get("/metrics/summary", response_model=MetricsResponse)
def metrics_summary(
    current: TenantSession,
    start: Annotated[datetime | None, Query()] = None,
    end: Annotated[datetime | None, Query()] = None,
    risk_cause: Annotated[RiskCause | None, Query()] = None,
    amount_band: Annotated[AmountBand | None, Query()] = None,
) -> MetricsResponse:
    """Every cohort figure for a period, optionally segmented (R14.C1, R14.C7 through C9).

    Segments and the aggregate come from the same function, so a segment can never be defined
    differently from the total it rolls into.
    """
    period = _period(start, end)
    segment = SegmentKey(risk_cause=risk_cause, amount_band=amount_band)
    timeout_ms = int(current.config.DASHBOARD_METRICS_TIMEOUT.total_seconds() * 1000)

    finding, available = _incremental_within_timeout(current, timeout_ms)

    with tenant_transaction(current.merchant_id) as session:
        metrics = compute_metrics(
            session,
            current.merchant_id,
            period,
            segment=segment,
            incremental=finding,
        )

    return MetricsResponse(
        report=metrics_document(
            metrics,
            currency=current.default_currency,
            incremental_available=available,
        ),
        incremental_available=available,
    )


def _incremental_within_timeout(
    current: AuthenticatedSession, timeout_ms: int
) -> tuple[IncrementalFinding, bool]:
    """The causality gate, or a refusal that says the timeout is why.

    On a cancellation the substitute finding carries ``NOT_ESTABLISHED`` and the refusal code
    ``METRICS_TIMEOUT``. That keeps two things true at once: the report's labels stay correct
    (nothing was established), and the rendered field says *data unavailable* rather than *not
    established* — which are different claims, and only the second one would be a lie.

    **This is a second transaction and it stays one.** Folding it into the cohort read's transaction
    to save a round trip was considered and measured, and it cannot be done without changing what
    ``DASHBOARD_METRICS_TIMEOUT`` bounds. Three facts, all confirmed against Postgres 18 rather than
    assumed:

    * A cancelled statement aborts the transaction it ran in. Every later statement fails with
      *current transaction is aborted*, so ``compute_metrics`` would not run at all and R14.C16's
      *every other figure still returns* would become *the page 500s*. Keeping the cancellation
      survivable needs a ``SAVEPOINT`` around this read.
    * ``SET LOCAL`` inside a savepoint that is **released** does not revert with it — the value
      persists to the end of the enclosing transaction. So on the success path ``compute_metrics``
      would inherit this bound, and a cohort read that is merely slow would be cancelled where today
      it completes. Undoing that needs an explicit ``RESET`` whose correct value is the connection's
      session default, which belongs to the engine module rather than to this endpoint.
    * The saving is negative anyway. One transaction costs one ``set_config`` for the tenant
      binding; the collapsed version would add ``SAVEPOINT``, ``SET LOCAL``, ``RELEASE`` and
      ``RESET`` — three more statements to remove one, for one fewer connection checkout.

    So the boundary is the mechanism rather than an accident of how this was written: the timeout
    applies to a transaction whose only contents are the reads it is allowed to cancel.
    """
    try:
        with tenant_transaction(current.merchant_id) as session:
            session.execute(text(f"SET LOCAL statement_timeout = {timeout_ms}"))
            return incremental_finding(session, current.merchant_id), True
    except (OperationalError, DBAPIError) as exc:
        # Broad on purpose. A cancelled statement surfaces as a driver error whose class depends on
        # the driver and the phase it was cancelled in, and treating an unrecognised one as a hard
        # failure would turn a slow query into a 500 on a page that could still have rendered.
        _logger.warning(
            "incremental figure unavailable within DASHBOARD_METRICS_TIMEOUT",
            merchant_id=str(current.merchant_id),
            timeout_ms=timeout_ms,
            error=type(exc).__name__,
        )
        return (
            IncrementalFinding(
                value="NOT_ESTABLISHED", refusal_codes=("METRICS_TIMEOUT",)
            ),
            False,
        )


@router.get("/metrics/unresolved")
def metrics_unresolved(
    current: TenantSession,
    start: Annotated[datetime | None, Query()] = None,
    end: Annotated[datetime | None, Query()] = None,
) -> dict[str, object]:
    """Unresolved revenue grouped by terminal reason, with explicit zero rows (R14.C10).

    Five groups, always: ``STOPPED``, ``BLOCKED``, ``EXPIRED``, ``ESCALATED``, ``FAILED``. They are
    the same money and completely different problems, and one combined total hides which one a
    merchant should act on.
    """
    period = _period(start, end)
    with tenant_transaction(current.merchant_id) as session:
        return unresolved_view(
            session,
            current.merchant_id,
            start=period.start,
            end=period.end,
            currency=current.default_currency,
        )
