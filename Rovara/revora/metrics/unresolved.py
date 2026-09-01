"""Unresolved cases, grouped by the reason they did not recover, with explicit zero rows.

R12.C10 and R14.C10 ask for unresolved revenue broken out by terminal reason rather than as one
total, and the reason is operational rather than presentational: ``BLOCKED`` and ``EXPIRED`` are
the same money and completely different problems. Blocked means policy stopped Revora — often
correctly, and a large blocked total is worth investigating because it may mean a threshold is
wrong. Expired means the window closed with nothing having worked. Escalated means a human was
asked and has not answered. One "unresolved: ₹4,20,000" figure hides all three, and the merchant's
next action is different in each case.

**Every group is present even when it is empty**, and this is the load-bearing part. A grouping
built from a ``GROUP BY`` returns rows only for the states that occurred, so a period with no
blocked cases has no blocked row — and an absent row renders as nothing at all, which reads as
"we did not look" rather than "there were none". Worse, a reader who is used to seeing five rows
and sees four will not notice which one is missing. So the five states are enumerated here and the
query's answers are merged onto them.

``STOPPED`` is in the list even though it is not a state a case reaches on its own. It is the
terminal state for a case whose attempts were exhausted below ``ESCALATION_AMOUNT_THRESHOLD`` —
small amounts stop, large ones escalate — so a deployment with a high threshold shows a large
``STOPPED`` group and that is a finding about the threshold, not about the cases.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from revora.domain.enums import CaseState
from revora.persistence.models import RecoveryCase
from revora.platform.clock import now

__all__ = ["UNRESOLVED_STATES", "UnresolvedGroup", "unresolved_groups"]

UNRESOLVED_STATES: Final[tuple[CaseState, ...]] = (
    CaseState.STOPPED,
    CaseState.BLOCKED,
    CaseState.EXPIRED,
    CaseState.ESCALATED,
    CaseState.FAILED,
)
"""The five terminal states that are not ``RECOVERED``, in the order R14.C10 lists them.

Ordered rather than a set, because the dashboard renders them in this order and an order derived
from a dict's iteration would change the layout when a state was added. ``RECOVERED`` is absent by
definition — this is the money that did not come back."""


@dataclass(frozen=True, slots=True)
class UnresolvedGroup:
    """One terminal reason's count and summed amount. Both integers.

    ``amount`` is a sum of integer minor units, so the total of the groups is exactly the total of
    the cases. No intermediate non-integer representation exists to lose a paisa in.
    """

    state: CaseState
    case_count: int
    amount: int

    def as_document(self) -> dict[str, object]:
        return {
            "state": self.state.value,
            "case_count": self.case_count,
            "amount_minor": self.amount,
        }


def unresolved_groups(
    session: Session,
    merchant_id: uuid.UUID,
    *,
    start: datetime,
    end: datetime,
) -> tuple[UnresolvedGroup, ...]:
    """Every unresolved group for a detection-time cohort. Always five entries.

    The cohort is ``[start, end)`` over ``detected_at``, matching
    :class:`revora.metrics.engine.ReportingPeriod` exactly — a grouping computed over a different
    window from the totals it sits beside would not sum to them, and a reader comparing the two
    would be right to distrust both.

    Reads only; commits nothing.
    """
    if end <= start:
        raise ValueError(f"period must have positive duration, got {start} to {end}")

    rows = session.execute(
        select(
            RecoveryCase.state,
            func.count().label("case_count"),
            func.coalesce(func.sum(RecoveryCase.payment_amount), 0).label("amount"),
        )
        .where(
            RecoveryCase.merchant_id == merchant_id,
            RecoveryCase.detected_at >= start,
            RecoveryCase.detected_at < end,
            RecoveryCase.state.in_([state.value for state in UNRESOLVED_STATES]),
        )
        .group_by(RecoveryCase.state)
    ).all()

    found = {str(row.state): (int(row.case_count), int(row.amount)) for row in rows}
    return tuple(
        UnresolvedGroup(
            state=state,
            case_count=found.get(state.value, (0, 0))[0],
            amount=found.get(state.value, (0, 0))[1],
        )
        for state in UNRESOLVED_STATES
    )


def unresolved_document(
    session: Session,
    merchant_id: uuid.UUID,
    *,
    start: datetime,
    end: datetime,
    moment: datetime | None = None,
) -> dict[str, object]:
    """The grouping plus its period and computation instant.

    The timestamps travel with the figures for the same reason they do on a cohort report: an
    unresolved total without its window cannot be reconciled against a later recomputation, and
    these figures *do* move — a delayed capture reconciles an ``EXPIRED`` case to ``RECOVERED``
    weeks after the period closed, which removes it from this grouping retroactively.
    """
    groups = unresolved_groups(session, merchant_id, start=start, end=end)
    return {
        "reporting_period": {"start": start.isoformat(), "end": end.isoformat()},
        "computed_at": (moment or now()).isoformat(),
        "groups": [group.as_document() for group in groups],
        "total_case_count": sum(group.case_count for group in groups),
        "total_amount_minor": sum(group.amount for group in groups),
    }
