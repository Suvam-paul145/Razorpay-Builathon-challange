"""Widen ``ck_recommendation_candidate_exclusion_reason_enum`` with the duplicate-link ground.

``LIVE_PAYMENT_LINK_EXISTS`` (R24.C10, read at system scope).

**Why this migration exists.** ``recommendation_candidate.exclusion_reason`` carries a ``CHECK``
generated from :class:`revora.domain.enums.ExclusionReason`, created by ``0008`` with seven members
and widened by ``0017`` with two. R24.C10 forbids a second payment link for a Recovery_Case that
already holds one, and until now that clause was only read on the promise follow-up's own execution
path — which is not where the duplicate came from. It came from a *second decision cycle* choosing
``PAYMENT_LINK`` again, and the ground on which that choice must not be offered is a ground no
existing member expresses. Without this revision the optimizer would exclude the link correctly and
then fail the ``INSERT`` that records why, inside the decision cycle's transaction, so a case
holding a live link would stall its whole cycle rather than produce a slightly less informative
row. That is the shape of every enum-behind-a-``CHECK`` mistake in this codebase and it is why
:class:`ExclusionReason` says so at its own declaration.

**Why a new member rather than an existing one.** ``PROVIDER_CAPABILITY_UNVERIFIED`` is already
admitted and would have stored, so reusing it would have needed no migration at all. It was
rejected because it is *false*: the provider creates payment links perfectly well, and a merchant
reading "the provider cannot do this" beside a case that already has a live link is being told the
wrong thing about their own integration. The reason nobody sent a link is that the customer already
has one, and that is the only sentence which answers the question the row exists to answer.

**No column is added, no data is rewritten, no index moves.** The ``CHECK`` is dropped and
recreated from ``enum_check`` — the same generator the model declares it with, so the constraint
this migration installs and the constraint the metadata declares cannot disagree by construction.
Widening is safe on a populated table without a ``NOT VALID`` dance: PostgreSQL verifies the new
constraint against existing rows and every existing row already satisfies it, because the new set
is a superset of the old one.

**The downgrade refuses rather than corrupting**, on exactly ``0015``'s and ``0017``'s terms. A
build at ``0017`` does not know this value and the narrower ``CHECK`` cannot be restored while a row
holds it. The two ways to make it fit are both wrong: deleting the candidate rows destroys the
record of what the optimizer considered, which R7.C8 exists to keep, and rewriting
``exclusion_reason`` to a value the old set admits — ``PROVIDER_CAPABILITY_UNVERIFIED`` is the
tempting one again — files "this customer already holds a live link" as "the provider cannot create
links", which is false about the provider and is not reversible by re-upgrading, because the
original value would be gone. So the downgrade counts the affected rows and refuses, naming them.

Revision ID: 0018
Revises: 0017
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from revora.domain.enums import ExclusionReason

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_CONSTRAINT = op.f("ck_recommendation_candidate_exclusion_reason_enum")
"""Wrapped in ``op.f`` because the metadata's naming convention would otherwise prefix
``ck_recommendation_candidate_`` onto whatever is passed, and the name already carries it.
``0008``, ``0015`` and ``0017`` do the same for every constraint they name."""

_ADDED: tuple[ExclusionReason, ...] = (ExclusionReason.LIVE_PAYMENT_LINK_EXISTS,)
"""The one member this revision admits.

Listed rather than computed as a set difference against a hardcoded copy of the previous nine, for
the reason ``0015`` and ``0017`` give: a second copy of that list is a second thing to keep in step
with a migration that has already run everywhere. The only consumer is the downgrade's refusal
query, which needs exactly "the values a build at 0017 cannot store"."""


def _exclusion_reason_check_sql() -> str:
    """The ``IN`` list, rendered from the enumeration rather than spelt out here.

    Generated the same way :func:`revora.persistence.models.base.enum_check` generates it, so the
    constraint installed by this migration and the one declared on the model are the same string.
    Spelling the ten values out in this file would create the drift the generator exists to
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
            f"refusing to downgrade 0018: recommendation_candidate holds rows excluded for "
            f"reasons a build at 0017 cannot store ({breakdown}). Restoring the narrower CHECK "
            "requires either deleting those candidate rows, which destroys the record of what "
            "the optimizer considered and rejected (R7.C8), or rewriting exclusion_reason to a "
            "value the old set admits — and the tempting rewrite, "
            "PROVIDER_CAPABILITY_UNVERIFIED, files 'this case already holds a live payment link' "
            "as 'the provider cannot create links', which is false about the provider and which "
            "re-upgrading cannot undo because the original value would be gone. Decide per row "
            "what those exclusions become, or keep 0018 applied."
        )

    # The pre-0018 list: every member except the one this revision added. Rendered from the
    # enumeration minus that tuple for the same reason the upgrade renders from it — so the
    # restored constraint is the one 0017 installed and not a hand-copied approximation of it.
    permitted = ", ".join(
        f"'{member.value}'" for member in ExclusionReason if member not in _ADDED
    )
    op.drop_constraint(_CONSTRAINT, "recommendation_candidate", type_="check")
    op.create_check_constraint(
        _CONSTRAINT, "recommendation_candidate", f'"exclusion_reason" IN ({permitted})'
    )
