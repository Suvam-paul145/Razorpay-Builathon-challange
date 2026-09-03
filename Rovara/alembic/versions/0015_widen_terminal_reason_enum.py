"""Widen ``ck_recovery_case_terminal_reason_enum`` with the four customer-stated endings.

``CUSTOMER_DISPUTED_CHARGE``, ``CUSTOMER_CANCELLED_ORDER``,
``CUSTOMER_REQUESTED_PARTIAL_ARRANGEMENT`` and ``PROMISE_BEYOND_RECOVERY_WINDOW``.

**Why this migration exists at all, when the spec's overview says "one migration (0008)".** It
is a gap in that plan rather than a change of design. R21.C4, R21.C5, R22.C2 and R23.C4 each
require a case to terminate carrying one of these four reasons, and in this codebase "terminate
with the transition reason X" is uniformly ``apply_transition(..., terminal_reason=X)`` — the
same call shape ``DECISION_CYCLE_LIMIT_REACHED`` and ``EXECUTION_RESULT_UNVERIFIABLE`` already
use. ``recovery_case.terminal_reason`` carries a ``CHECK`` generated from
:class:`revora.domain.enums.TerminalReason`, created in ``0001`` with twelve members and never
widened, so all four escalations were **unstorable** before this revision. ``0008`` created the
``contact_suppression`` table the hard stops write to and did not touch this column.

**Why all four at once, rather than one migration per task.** They widen one ``CHECK`` on one
column. Rebuilding it four times across tasks 42, 43 and 44 means writing the full member list
four times, and each rewrite is a chance to drop a member that an earlier revision added — a
mistake that would surface as a perfectly valid transition failing at ``INSERT`` time on one
deployment and not another. The enum is additive and the constraint is derived from it, so one
rebuild against the current enumeration is both the smallest and the safest change. Two of the
four therefore land ahead of their writers, which is the accepted cost: an admitted value that
nothing writes is inert, whereas a writer with no admitted value is a failed transaction.

**No column is added, no data is rewritten, no index moves.** The ``CHECK`` is dropped and
recreated from ``enum_check`` — the same generator the model uses, so the constraint this
migration installs and the constraint the metadata declares cannot disagree by construction.
Dropping a ``CHECK`` and adding a wider one is not a validating scan of the table in the way a
narrowing would be: PostgreSQL still verifies the new constraint against existing rows, but
every existing row already satisfies it, because the new set is a superset of the old one. So
this is safe on a populated table and needs no ``NOT VALID`` dance.

**The downgrade refuses rather than corrupting.** A build at ``0014`` does not know these four
values, and the narrower ``CHECK`` cannot be restored while a row holds one. There are two ways
to make it fit and both are wrong: deleting the cases destroys the record of what the customer
said, and rewriting ``terminal_reason`` to something the old set admits — ``CUSTOMER_OPTED_OUT``
is the tempting one — files a dispute as a withdrawal of consent, which is exactly the
conflation R21.C9 exists to prevent and is not reversible by re-upgrading, because the original
value is gone. So the downgrade counts the affected rows and refuses, naming them. A deployment
that genuinely has to go back has to decide, per case, what those endings become.

Revision ID: 0015
Revises: 0014
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from revora.domain.enums import TerminalReason

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_CONSTRAINT = op.f("ck_recovery_case_terminal_reason_enum")
"""Wrapped in ``op.f`` because the metadata's naming convention would otherwise prefix
``ck_recovery_case_`` onto whatever is passed, and the name already carries it. ``0008`` does
the same for every constraint it names."""

_ADDED: tuple[TerminalReason, ...] = (
    TerminalReason.CUSTOMER_DISPUTED_CHARGE,
    TerminalReason.CUSTOMER_CANCELLED_ORDER,
    TerminalReason.CUSTOMER_REQUESTED_PARTIAL_ARRANGEMENT,
    TerminalReason.PROMISE_BEYOND_RECOVERY_WINDOW,
)
"""The four members this revision admits.

Listed rather than computed as a set difference against a hardcoded copy of ``0001``'s twelve.
A second copy of that list is a second thing to keep in step with a migration that has already
run everywhere, and the only consumer of this tuple is the downgrade's refusal query — which
needs exactly "the values a build at 0014 cannot store"."""


def _terminal_reason_check_sql() -> str:
    """The ``IN`` list, rendered from the enumeration rather than spelt out here.

    Generated the same way :func:`revora.persistence.models.base.enum_check` generates it, so
    the constraint installed by this migration and the one declared on the model are the same
    string. Spelling the sixteen values out in this file would create the drift the generator
    exists to prevent, and would do it in the file that is hardest to notice is stale.
    """
    rendered = ", ".join(f"'{member.value}'" for member in TerminalReason)
    return f'"terminal_reason" IN ({rendered})'


def upgrade() -> None:
    op.drop_constraint(_CONSTRAINT, "recovery_case", type_="check")
    op.create_check_constraint(_CONSTRAINT, "recovery_case", _terminal_reason_check_sql())


def downgrade() -> None:
    connection = op.get_bind()

    added = [member.value for member in _ADDED]
    stranded = connection.execute(
        sa.text(
            "SELECT terminal_reason, count(*) AS n FROM recovery_case "
            "WHERE terminal_reason = ANY(:added) GROUP BY terminal_reason ORDER BY terminal_reason"
        ),
        {"added": added},
    ).all()
    if stranded:
        breakdown = ", ".join(f"{row.terminal_reason}={row.n}" for row in stranded)
        raise RuntimeError(
            f"refusing to downgrade 0015: recovery_case holds rows terminating on reasons a "
            f"build at 0014 cannot store ({breakdown}). Restoring the narrower CHECK requires "
            "either deleting those cases, which destroys the record of what the customer said, "
            "or rewriting terminal_reason to a value the old set admits — and the tempting "
            "rewrite, CUSTOMER_OPTED_OUT, files a dispute as a withdrawal of consent, which is "
            "the conflation R21.C9 exists to prevent and which re-upgrading cannot undo because "
            "the original value would be gone. Decide per case what those endings become, or "
            "keep 0015 applied."
        )

    # The pre-0015 list: every member except the four this revision added. Rendered from the
    # enumeration minus that tuple for the same reason the upgrade renders from it — so the
    # restored constraint is the one 0001 installed and not a hand-copied approximation of it.
    permitted = ", ".join(
        f"'{member.value}'" for member in TerminalReason if member not in _ADDED
    )
    op.drop_constraint(_CONSTRAINT, "recovery_case", type_="check")
    op.create_check_constraint(
        _CONSTRAINT, "recovery_case", f'"terminal_reason" IN ({permitted})'
    )
