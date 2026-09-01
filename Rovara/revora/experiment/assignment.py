"""Deterministic arm assignment, decided and persisted before anything knows about the case.

Two properties carry the whole comparison, and both are structural rather than procedural.

**Assignment is decided before diagnosis.** Not "as early as convenient" — before. An arm
chosen once the failure cause is known is an arm chosen on the strength of the case, and the
difference between arms then measures which cases we sorted rather than what we did to them.
So the write happens in the same transaction that creates the case, before the diagnosis job is
even enqueued, and the audit record proves the ordering.

**Assignment is deterministic and stateless.** ``HMAC-SHA256(experiment_id, case_id)`` reduced
to a group. No counter, no coordination between workers, no database read to decide. That means
two workers racing on the same case compute the same arm, a retried job computes the same arm,
and re-running an analysis a year later can recompute every assignment from ids alone. A
sequential allocator would need a lock and would produce a different arm on a retry — which
would be invisible in the result and would destroy the comparison.

**No float anywhere in the randomization.** The design sketches the reduction as
``int.from_bytes(digest[:8]) / 2**64`` compared against a share. That division is a float
operation and it is the wrong tool here twice over: money and probabilities in this system are
never floats, and a value derived by rounding is a value that can land on the wrong side of a
boundary. The comparison below is pure integer arithmetic and is exact by construction — see
:func:`assign_group`.

**The unassigned case is a real case, not an error.** No active experiment, or an assignment
that could not be persisted, means the case runs the baseline workflow and belongs to neither
arm (R13.C14). It must never be silently treated as treatment: that would put cases into the
treatment arm that the randomization never selected, which is the same contamination as moving
one there by hand.
"""

from __future__ import annotations

import hashlib
import hmac
import uuid
from dataclasses import dataclass
from typing import Final

from revora.domain.enums import ExperimentGroup

__all__ = [
    "DIGEST_BYTES",
    "DIGEST_SPACE",
    "AllocationRatio",
    "assign_group",
    "parse_allocation_ratio",
]

DIGEST_BYTES: Final[int] = 8
"""Bytes of the HMAC digest used for the reduction.

Eight, giving 2**64 buckets. More would not make the assignment more uniform in any way a
finite case count could detect, and fewer would make collisions between the boundary and the
bucket grid observable at a few million cases."""

DIGEST_SPACE: Final[int] = 1 << (DIGEST_BYTES * 8)
"""The size of the bucket space, ``2**64``. An exact integer, which is what lets the
share comparison avoid division entirely."""


@dataclass(frozen=True, slots=True)
class AllocationRatio:
    """A control-to-treatment ratio, as two integers.

    Integers rather than a share, and stored in the database as text like ``"1:1"`` for the same
    reason: a ratio is a pair, and rendering it as ``0.5`` invites arithmetic on a value that is
    really two counts. Keeping the pair means the comparison in :func:`assign_group` can be
    exact — a share would have to be a rounded ``Decimal`` and the boundary would sit at a
    rounded place.
    """

    control: int
    treatment: int

    def __post_init__(self) -> None:
        if self.control < 0 or self.treatment < 0:
            raise ValueError(f"allocation ratio parts cannot be negative: {self}")
        if self.control + self.treatment <= 0:
            raise ValueError("allocation ratio must have a positive total")

    @property
    def total(self) -> int:
        return self.control + self.treatment

    def __str__(self) -> str:
        return f"{self.control}:{self.treatment}"


def parse_allocation_ratio(raw: str) -> AllocationRatio:
    """Parse ``"control:treatment"`` into two integers.

    Strict: two integer parts separated by one colon, nothing else. A permissive parser here
    would be a way for a malformed configuration value to silently become an allocation nobody
    chose — and an allocation nobody chose is an experiment whose arms are the wrong size, which
    is not detectable from the result.

    Raises:
        ValueError: on anything that is not two non-negative integers with a positive total.
    """
    parts = raw.strip().split(":")
    if len(parts) != 2:
        raise ValueError(f"allocation ratio must be 'control:treatment', got {raw!r}")
    try:
        control, treatment = int(parts[0]), int(parts[1])
    except ValueError as exc:
        raise ValueError(f"allocation ratio parts must be integers, got {raw!r}") from exc
    return AllocationRatio(control=control, treatment=treatment)


def assign_group(
    experiment_id: uuid.UUID | str,
    case_id: uuid.UUID | str,
    ratio: AllocationRatio,
) -> ExperimentGroup:
    """Which arm this case belongs to. Deterministic, stateless, and exact.

    ``HMAC-SHA256`` keyed on the experiment id with the case id as the message. Keying on the
    experiment matters: the same case in two different experiments gets two independent
    assignments, so a case that was control in one comparison is not forced to be control in
    the next. A plain hash of the concatenation would correlate the two.

    The reduction is integer-only::

        bucket = int.from_bytes(digest[:8])
        TREATMENT  iff  bucket * ratio.total < ratio.treatment * 2**64

    That is the exact rearrangement of ``bucket / 2**64 < treatment / total`` with both
    divisions cleared. Every quantity is a Python ``int`` of unbounded width, so there is no
    rounding and no boundary ambiguity — the case either falls below the cut or it does not, and
    the answer is the same on every machine and every Python version. The float form in the
    design sketch is correct in intent and would give a different answer for a vanishingly small
    set of digests; this form has no such set.

    Args:
        experiment_id: accepted as a ``UUID`` or its string form, and rendered with ``str`` so
            both key identically. A caller holding one form should not have to know which.
        ratio: the allocation. ``0:1`` assigns everything to treatment and ``1:0`` everything to
            control, both of which are legitimate — a ramp-up starts near one and a paused
            experiment sits at the other.
    """
    digest = hmac.new(
        str(experiment_id).encode("utf-8"),
        str(case_id).encode("utf-8"),
        hashlib.sha256,
    ).digest()
    bucket = int.from_bytes(digest[:DIGEST_BYTES], "big")

    if bucket * ratio.total < ratio.treatment * DIGEST_SPACE:
        return ExperimentGroup.TREATMENT
    return ExperimentGroup.CONTROL
