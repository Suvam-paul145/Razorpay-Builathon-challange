"""Out-of-order delivery handling. Delivery is at-least-once and unordered (R1.C7).

The provider documents that events may arrive out of order — it gives
``payment.authorized`` before ``payment.captured`` as an order that is not
guaranteed, and ``payment.captured`` can precede ``payment.failed``. The rule is that
an event is acted on only when it is not stale relative to what has already been
processed for its payment, and a stale one leaves case state and every counter
untouched, recorded as ``OUT_OF_ORDER_EVENT`` with both timestamps (R1.C11).

This module is the comparison, kept pure: given the event's provider timestamp and
the newest already-processed timestamp for the same payment, it says whether the
event is in order. The ingestion service supplies the newest-prior timestamp from a
persisted read and records the audit; the decision itself has no I/O so it is
trivially testable.

The most consequential out-of-order case in the MVP — a capture arriving before the
failure — is also caught downstream: detection's "no verified captured state" rule
opens no case for a payment already seen captured. This guard is the earlier, more
general statement of the same principle, and it audits the ordering fact that the
detection rule would otherwise leave implicit.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from revora.platform.clock import ensure_utc

__all__ = ["OrderingDecision", "assess_ordering"]


@dataclass(frozen=True, slots=True)
class OrderingDecision:
    """Whether an event is in order, and the two timestamps that decided it."""

    in_order: bool
    event_at: datetime | None
    newest_prior_at: datetime | None


def assess_ordering(
    event_at: datetime | None,
    newest_prior_at: datetime | None,
) -> OrderingDecision:
    """Decide whether an event is in order for its payment.

    In order when there is no prior event, or when the event is at least as recent as
    the newest already processed. An event with no provider timestamp is treated as
    in order — the absence is a payload shape we cannot rank, and refusing to process
    it would drop a real event on a missing optional field.

    Ties (equal timestamps) are in order: two events sharing a provider second are
    not evidence of reordering, and treating a tie as stale would drop the second of
    two legitimate same-second events.
    """
    if event_at is None or newest_prior_at is None:
        return OrderingDecision(True, event_at, newest_prior_at)
    event_utc = ensure_utc(event_at)
    prior_utc = ensure_utc(newest_prior_at)
    return OrderingDecision(event_utc >= prior_utc, event_utc, prior_utc)
