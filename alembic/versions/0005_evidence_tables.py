"""Persist the conditions a causal claim was made under.

Two additions, both for the same reason: a claim about incremental revenue has to be
defensible months later, and the facts that make it defensible are not reconstructable from
tables that keep moving.

**``experiment_result``.** design.md lists it as an Experiment_Engine output and the initial
schema did not create it. Without it, an analysis has nowhere to live except the experiment
row — which would mean either overwriting the previous conclusion or deriving the lift afresh
on every read from a live assignment table. Both are wrong in the same way: a result that was
underpowered when it was concluded would silently become adequately powered later, and nobody
reading the dashboard would know the number had changed. Rows accumulate; each carries the
counts and the interval it was computed from.

**``memory_observation.diagnosis_method``.** R15.C1 names cause, confidence *and method* as
the diagnosis fields an observation must hold. The first two had columns and the third did
not. It matters because it decides whether a row is usable as a training label at all: a cause
reached by ``FALLBACK_UNKNOWN`` is a cause nobody established, and a model trained on those as
though they were diagnoses would learn the shape of our own ignorance. Nullable, because every
row written before this migration genuinely does not know.

Both changes are additive. No column is dropped, no constraint tightened on existing data, and
the new column is nullable — so this migration cannot fail on a populated database, which for
a table feeding a training set is the difference between a migration and an outage.

Revision ID: 0005
Revises: 0004
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from revora.domain.enums import DiagnosisMethod
from revora.platform.config import DEFAULTS_MERCHANT_ID

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "revora_app"

_TENANT_EXPR = (
    "merchant_id = NULLIF(current_setting('revora.merchant_id', true), '')::uuid"
)
"""Identical to migration 0003's expression, deliberately repeated rather than imported.

A migration is a historical record of what was applied. Importing a shared constant would let
a later edit to that constant silently change what this migration is understood to have done,
which is the one property a migration must not have.
"""

_NEW_TABLE = "experiment_result"

_DIAGNOSIS_METHODS = ", ".join(f"'{member.value}'" for member in DiagnosisMethod)


def upgrade() -> None:
    op.create_table(
        _NEW_TABLE,
        sa.Column(
            "id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "merchant_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("merchant.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "experiment_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("experiment.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("primary_metric", sa.Text(), nullable=False),
        sa.Column("analysis_method", sa.Text(), nullable=False),
        sa.Column("control_case_count", sa.Integer(), nullable=False),
        sa.Column("treatment_case_count", sa.Integer(), nullable=False),
        sa.Column("control_recoveries", sa.Integer(), nullable=False),
        sa.Column("treatment_recoveries", sa.Integer(), nullable=False),
        sa.Column("control_rate", sa.Numeric(6, 4)),
        sa.Column("treatment_rate", sa.Numeric(6, 4)),
        sa.Column("lift", sa.Numeric(7, 4)),
        sa.Column("lift_ci_low", sa.Numeric(7, 4)),
        sa.Column("lift_ci_high", sa.Numeric(7, 4)),
        sa.Column(
            "contaminated_count", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column("excluded_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("labels", sa.dialects.postgresql.ARRAY(sa.Text())),
        sa.Column("comparison", sa.dialects.postgresql.JSONB(), nullable=False),
        sa.Column("computed_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "control_case_count >= 0 AND treatment_case_count >= 0 "
            "AND control_recoveries >= 0 AND treatment_recoveries >= 0 "
            "AND contaminated_count >= 0 AND excluded_count >= 0",
            name=op.f("ck_experiment_result_result_counts_nonnegative"),
        ),
        sa.CheckConstraint(
            "control_recoveries <= control_case_count "
            "AND treatment_recoveries <= treatment_case_count",
            name=op.f("ck_experiment_result_recoveries_within_case_counts"),
        ),
        sa.CheckConstraint(
            "(lift_ci_low IS NULL) = (lift_ci_high IS NULL)",
            name=op.f("ck_experiment_result_interval_bounds_present_together"),
        ),
        sa.CheckConstraint(
            "lift_ci_low IS NULL OR lift_ci_low <= lift_ci_high",
            name=op.f("ck_experiment_result_interval_bounds_ordered"),
        ),
    )
    op.create_index(
        "ix_experiment_result_experiment_id_computed_at",
        _NEW_TABLE,
        ["experiment_id", "computed_at"],
    )

    # Row-level security. Migration 0003 derives its table list from the model metadata at
    # the time it ran, so a table added later gets no policy from it — this has to be done
    # here or the new table would be the one table in the schema without tenant isolation.
    op.execute(f"ALTER TABLE {_NEW_TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_isolation ON {_NEW_TABLE} "
        f"USING ({_TENANT_EXPR}) WITH CHECK ({_TENANT_EXPR})"
    )
    op.execute(
        f"""
DO $do$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{APP_ROLE}') THEN
    GRANT SELECT, INSERT, UPDATE, DELETE ON {_NEW_TABLE} TO {APP_ROLE};
  END IF;
END
$do$;
"""
    )

    op.add_column(
        "memory_observation", sa.Column("diagnosis_method", sa.Text(), nullable=True)
    )
    op.create_check_constraint(
        op.f("ck_memory_observation_diagnosis_method_enum"),
        "memory_observation",
        f'"diagnosis_method" IN ({_DIAGNOSIS_METHODS})',
    )

    op.execute(
        f"COMMENT ON TABLE {_NEW_TABLE} IS "
        "'One analysis of one experiment. Rows accumulate rather than being overwritten, so "
        "what was concluded when is reconstructable. Only a result whose interval lies "
        "entirely above zero, on a COMPLETED and adequately powered experiment, may support "
        "an attributed recovery claim.'"
    )
    # Referenced so a reader of this migration can see that the sentinel tenant is
    # deliberately not given a read exemption here, unlike app_config in 0003: a default
    # experiment result would be a claim nobody made.
    _ = DEFAULTS_MERCHANT_ID


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_memory_observation_diagnosis_method_enum"),
        "memory_observation",
        type_="check",
    )
    op.drop_column("memory_observation", "diagnosis_method")
    op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {_NEW_TABLE}")
    op.drop_index("ix_experiment_result_experiment_id_computed_at", table_name=_NEW_TABLE)
    op.drop_table(_NEW_TABLE)
