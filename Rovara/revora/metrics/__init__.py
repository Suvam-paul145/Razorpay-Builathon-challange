"""Metrics: the numbers a merchant would take to their finance team.

Everything here is arranged so that a figure survives being questioned. Four refusals do most of
the work, and each prevents a specific way of overstating what Revora knows.

**A rate with a zero denominator is ``UNDEFINED``, never zero.** A period with no cases has no
recovery rate, and reporting ``0`` claims it recovered nothing — a measurement, and a false one.
The distinction bites exactly when it arises: a new merchant's first week reads as total failure
under zeroes and as "no data yet" under ``UNDEFINED``.

**Incremental revenue is ``NOT_ESTABLISHED`` unless an experiment earned it.** Observed recovery is
never presented as incremental. "We recovered ₹X" and "we caused ₹X to be recovered" are different
claims; only a completed comparison whose lift interval lies entirely above zero supports the
second, and everything else Revora observes is consistent with the money having arrived anyway.

**Observed recovery is labelled when causality is not established.** The number is real. The
implication a reader would draw from it is not, so the label travels with the figure.

**One synthetic case labels the whole report ``SYNTHETIC``.** Not a proportion — one is enough,
because a proportion would invite somebody to decide a little generated data is acceptable.

The attribution gate itself is not here. It lives in :mod:`revora.domain.attribution`, because the
experiment engine applies the same rule and the two packages are siblings in the layering contract
— two copies would be two chances for one of them to be more permissive.
"""

from __future__ import annotations

from revora.metrics.engine import (
    RATE_PLACES,
    CohortMetrics,
    IncrementalFinding,
    ReportingPeriod,
    SegmentKey,
    compute_metrics,
    incremental_finding,
    rate,
)
from revora.metrics.unresolved import (
    UNRESOLVED_STATES,
    UnresolvedGroup,
    unresolved_groups,
)

__all__ = [
    "RATE_PLACES",
    "UNRESOLVED_STATES",
    "CohortMetrics",
    "IncrementalFinding",
    "ReportingPeriod",
    "SegmentKey",
    "UnresolvedGroup",
    "compute_metrics",
    "incremental_finding",
    "rate",
    "unresolved_groups",
]
