"""Synthetic runs and database-backed configuration.

``synthetic_run.ground_truth`` holds the embedded true uplift the generator used.
No Revora component reads it — only the comparison reporter does, and the import
contracts are what keep that true. A generator whose ground truth leaked into the
decision path would make every synthetic result circular.

``app_config`` is where the ~50 tunable bounds live, and the reason they live in a
table rather than in environment variables is R15.C6: a policy change has to be
recorded with an approving user, and a redeploy cannot name one. Environment holds
only the connection string, secret references and the process role.

Two structural details:

* **Rows are versioned, not updated.** A change inserts a new row with a new
  ``config_version`` and flips ``is_active``. The decision that was made under the
  old bound stays explicable, because ``policy_decision.config_version`` points at
  the row that was live at the time.
* **Defaults belong to a sentinel merchant.** The seed migration has no real
  merchant to attach to, and making ``merchant_id`` nullable to accommodate that
  would put a hole in the column every isolation mechanism depends on. Instead the
  seeded defaults belong to a fixed sentinel tenant, and the loader reads the
  merchant's own rows with the sentinel's as a fallback. The row-level-security
  policy on this table admits the sentinel for the same reason.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Index,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from revora.persistence.models.base import TIMESTAMPTZ, RowBase

__all__ = ["AppConfig", "SyntheticRun"]


class SyntheticRun(RowBase):
    """One generated dataset, reproducible from its seed."""

    __tablename__ = "synthetic_run"

    seed: Mapped[int] = mapped_column(BigInteger, nullable=False)
    """Reported alongside every synthetic result. A result nobody can regenerate is
    not evidence of anything."""

    scenario: Mapped[str | None] = mapped_column(Text)
    assumptions: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    ground_truth: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    """The embedded true uplift. Read by the comparison reporter and nothing else."""

    generator_version: Mapped[str | None] = mapped_column(Text)
    case_count: Mapped[int | None] = mapped_column(BigInteger)

    __table_args__ = (
        UniqueConstraint(
            "merchant_id", "seed", "scenario", name="uq_synthetic_run_merchant_seed_scenario"
        ),
    )


class AppConfig(RowBase):
    """One configured bound, at one version, for one tenant.

    ``value`` is ``TEXT`` and parsed by ``platform.config`` against the declared
    kind. Text rather than a column per type because there are ~50 bounds of six
    different kinds, and a typed column per kind would mean a nullable column per
    kind on every row plus a ``CHECK`` asserting exactly one is populated. The
    parsing has to happen somewhere; doing it in one typed accessor is cheaper than
    encoding it in the schema.
    """

    __tablename__ = "app_config"

    key: Mapped[str] = mapped_column(Text, nullable=False)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    value_kind: Mapped[str] = mapped_column(Text, nullable=False)
    """How to parse ``value``: an integer, a duration, a decimal, a money amount, a
    plain string or a set of strings. Stored on the row so a value written by a
    migration is self-describing."""

    config_version: Mapped[str] = mapped_column(Text, nullable=False)
    """Recorded on every policy decision. Without it, a decision made under a
    bound of 3 is indistinguishable from one made under a bound of 5."""

    is_active: Mapped[bool] = mapped_column(nullable=False, server_default="true")
    effective_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, nullable=False)
    approved_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("merchant_user.id", ondelete="RESTRICT")
    )
    """Null only for the seeded defaults, which nobody approved because nobody
    chose them — they are the requirements document's placeholders. Any change
    made through the application must name a user."""

    is_assumption: Mapped[bool] = mapped_column(nullable=False, server_default="true")
    """True while the value is still the requirements document's placeholder rather
    than a figure calibrated against merchant data. Surfaced wherever a bound is
    displayed, so "3 attempts" is not mistaken for a measured optimum."""

    purpose: Mapped[str | None] = mapped_column(Text)
    note: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        UniqueConstraint(
            "merchant_id", "key", "config_version", name="uq_app_config_merchant_key_version"
        ),
        # Exactly one active row per key per tenant. Two active rows would make the
        # effective configuration depend on row order.
        Index(
            "one_active_config_per_key",
            "merchant_id",
            "key",
            unique=True,
            postgresql_where=text("is_active"),
        ),
        CheckConstraint(
            "value_kind IN ('INTEGER', 'DURATION_SECONDS', 'DECIMAL', 'MONEY_MINOR', "
            "'STRING', 'STRING_SET')",
            name="value_kind_known",
        ),
        # Reason: the loader reads every active row for a merchant in one query at
        # the start of a request or job.
        Index("ix_app_config_merchant_id_key", "merchant_id", "key"),
    )
