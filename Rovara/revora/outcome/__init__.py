"""Outcome: deciding whether money actually arrived.

One rule governs the whole package: **recovery is declared only from an authoritative
provider read reporting the payment captured.** Not from a webhook, not from an
authorization, not from a partial payment, and never from an inference.

The rule is not caution for its own sake. Razorpay's delivery is verified at-least-once and
out-of-order, so a ``payment.captured`` webhook establishes that something was reported once
— not that money is in the merchant's account now. A recovery figure built on webhooks is a
figure that cannot be defended when a merchant's finance team disagrees with it, and an
undefendable number is worth less than no number.

Two schema constraints make the accounting true rather than merely intended, and together
they are Property 20:

* ``recovery_outcome.verified_by_read_id`` is ``NOT NULL`` — no read, no recorded recovery.
* ``UNIQUE (case_id)`` on ``recovery_outcome`` — money is counted at most once per case, no
  matter how many duplicate success signals arrive.

The modules:

* :mod:`~revora.outcome.reads` — performs and persists the authoritative read, and holds
  :func:`~revora.outcome.reads.is_recovered`, which is the only place the recovery test is
  written down.
* :mod:`~revora.outcome.monitor` — the decision: duplicate discard, conflict hold, partial
  hold, escalation at the attempt bound, the race with execution, and the classification.

The detection-gap backfill is *not* here. It lives in :mod:`revora.ingestion.backfill`,
because what it does is ingest events that never arrived — it feeds the same
canonicalization and detection path a webhook does, and putting it here would have it
bypassing detection to write outcomes directly.

Classifications are ``NATURAL`` (no confirmed Revora action) or ``OBSERVED`` (at least one).
Never ``ATTRIBUTED`` from here — that is a causal claim, it requires a controlled comparison,
and only the experiment engine may make it.
"""

from __future__ import annotations

from revora.outcome.monitor import (
    OutcomeAssessment,
    OutcomeVerdict,
    observe_payment_outcome,
)
from revora.outcome.reads import is_partial, is_recovered

__all__ = [
    "OutcomeAssessment",
    "OutcomeVerdict",
    "is_partial",
    "is_recovered",
    "observe_payment_outcome",
]
