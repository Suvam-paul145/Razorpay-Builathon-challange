"""Widen ``ck_recommendation_candidate_exclusion_reason_enum`` with the two promise grounds.

``NO_PROMISE_RECORDED`` (R24.C2) and ``PROMISE_DATE_NOT_REACHED`` (R23.C9).

**Why this migration exists.** ``recommendation_candidate.exclusion_reason`` carries a ``CHECK``
generated from :class:`revora.domain.enums.ExclusionReason`, created by ``0008`` with seven
members and never widened. R24 makes ``PROMISE_TO_PAY_FOLLOW_UP`` a selectable candidate, and the
two grounds on which it is *not* selected are grounds no existing member expresses — so without
this revision the optimizer would rank the follow-up, exclude it correctly, and then fail the
``INSERT`` that records why. The failure would land on the recommendation write, which is inside
the decision cycle's transaction, so a case with a promise would stall its whole cycle rather
than produce a slightly less informative row. That is the shape of every enum-behind-a-``CHECK``
mistake in this codebase and it is why :class:`ExclusionReason` now says so at its own
declaration, the way :class:`revora.domain.enums.TerminalReason` has since ``0015``.

**Why two members rather than one.** Collapsing them would have needed no migration at all —
``PROVIDER_CAPABILITY_UNVERIFIED`` is already admitted and would have stored. It was rejected
because the three conditions answer a merchant's one question differently: no promise exists, a
promise exists and its date has not arrived, or the resend endpoint is gone. A single value for
all three makes "why did nobody chase this promise" unanswerable from the row, and the row is
where the case detail view reads it from.

**No column is added, no data is rewritten, no index moves.** The ``CHECK`` is dropped and
recreated from ``enum_check`` — the same generator the model declares it with, so the constraint
this migration installs and the constraint the metadata declares cannot disagree by
construction. Widening is safe on a populated table without a ``NOT VALID`` dance: PostgreSQL
verifies the new constraint against existing rows and every existing row already satisfies it,
because the new set is a superset of the old one.

**The downgrade refuses rather than corrupting**, on exactly ``0015``'s terms. A build at
``0016`` does not know these two values and the narrower ``CHECK`` cannot be restored while a row
holds one. The two ways to make it fit are both wrong: deleting the candidate rows destroys the
record of what the optimizer considered, which R7.C8 exists to keep, and rewriting
``exclusion_reason`` to a value the old set admits — ``PROVIDER_CAPABILITY_UNVERIFIED`` is the
tempting one — files "the customer has not reached their promised date" as "the provider cannot
do this", which is a false statement about the provider and is not reversible by re-upgrading,
because the original value would be gone. So the downgrade counts the affected rows and refuses,
naming them.

Revision ID: 0017
Revises: 0016
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from revora.domain.enums import ExclusionReason

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_CONSTRAINT = op.f("ck_recommendation_candidate_exclusion_reason_enum")
"""Wrapped in ``op.f`` because the metadata's naming convention would otherwise prefix
``ck_recommendation_candidate_`` onto whatever is passed, and the name already carries it.
``0008`` and ``0015`` do the same for every constraint they name."""

_ADDED: tuple[ExclusionReason, ...] = (
    ExclusionReason.NO_PROMISE_RECORDED,
    ExclusionReason.PROMISE_DATE_NOT_REACHED,
)
"""The two members this revision admits.

Listed rather than computed as a set difference against a hardcoded copy of ``0008``'s seven, for
the reason ``0015`` gives: a second copy of that list is a second thing to keep in step with a
migration that has already run everywhere. The only consumer is the downgrade's refusal query,
which needs exactly "the values a build at 0016 cannot store"."""


def _exclusion_reason_check_sql() -> str:
    """The ``IN`` list, rendered from the enumeration rather than spelt out here.

    Generated the same way :func:`revora.persistence.models.base.enum_check` generates it, so the
    constraint installed by this migration and the one declared on the model are the same string.
    Spelling the nine values out in this file would create the drift the generator exists to
    prevent, and would do it in the file that is hardest to notice is stale.
    """
    rendered = ", ".join(f"'{member.value}'" for member in ExclusionReason)
    return f'"exclusion_reason" IN ({rendered})'


def upgrade() -> None:
    op.drop_constraint(_CONSTRAINT, "recommendation_candidate", type_="check")
    op.create_check_constraint(
        _CONSTRAINT, "recommendation_candidate", _exclusion_reason_check_sql()
    )


def downgrade() -> None:
    connection = op.get_bind()

    added = [member.value for member in _ADDED]
    stranded = connection.execute(
        sa.text(
            "SELECT exclusion_reason, count(*) AS n FROM recommendation_candidate "
            "WHERE exclusion_reason = ANY(:added) "
            "GROUP BY exclusion_reason ORDER BY exclusion_reason"
        ),
        {"added": added},
    ).all()
    if stranded:
        breakdown = ", ".join(f"{row.exclusion_reason}={row.n}" for row in stranded)
        raise RuntimeError(
            f"refusing to downgrade 0017: recommendation_candidate holds rows excluded for "
            f"reasons a build at 0016 cannot store ({breakdown}). Restoring the narrower CHECK "
            "requires either deleting those candidate rows, which destroys the record of what "
            "the optimizer considered and rejected (R7.C8), or rewriting exclusion_reason to a "
            "value the old set admits — and the tempting rewrite, "
            "PROVIDER_CAPABILITY_UNVERIFIED, files 'the promised date has not arrived' as 'the "
            "provider cannot do this', which is false about the provider and which re-upgrading "
            "cannot undo because the original value would be gone. Decide per row what those "
            "exclusions become, or keep 0017 applied."
        )

    # The pre-0017 list: every member except the two this revision added. Rendered from the
    # enumeration minus that tuple for the same reason the upgrade renders from it — so the
    # restored constraint is the one 0008 installed and not a hand-copied approximation of it.
    permitted = ", ".join(
        f"'{member.value}'" for member in ExclusionReason if member not in _ADDED
    )
    op.drop_constraint(_CONSTRAINT, "recommendation_candidate", type_="check")
    op.create_check_constraint(
        _CONSTRAINT, "recommendation_candidate", f'"exclusion_reason" IN ({permitted})'
    )
