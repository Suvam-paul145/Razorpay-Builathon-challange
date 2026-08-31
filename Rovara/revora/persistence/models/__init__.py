"""Every model in one namespace, plus the two lists the migrations are built from.

Importing this package is what populates ``Base.metadata``, so the Alembic
environment imports it and nothing else. It also gives two derived facts that
would otherwise be hand-maintained lists in a migration:

* :data:`TENANT_SCOPED_TABLES` — every table carrying ``merchant_id``, which is
  exactly the set that gets row-level security. Derived by asking the metadata
  which tables have the column, so a new table cannot be added without RLS unless
  it is also added without ``merchant_id``, which the mixin makes hard.
* :func:`enum_backed_columns` — every ``TEXT`` column with an enum-derived
  ``CHECK``. The constraint test iterates it, so a new enum column is covered the
  moment it is declared.
"""

from __future__ import annotations

from revora.persistence.models.audit import AiInvocation, AuditRecord
from revora.persistence.models.base import (
    AUDIT_TIMESTAMP,
    CONFIDENCE,
    MONEY,
    PROBABILITY,
    SIGNED_INCREMENT,
    TIMESTAMPTZ,
    Base,
    CreatedAtMixin,
    EnumBackedColumn,
    IdMixin,
    MerchantScopedMixin,
    RowBase,
    enum_backed_columns,
)
from revora.persistence.models.cases import Diagnosis, RecoveryCase
from revora.persistence.models.estimates import (
    BaselineEstimate,
    CandidateEstimate,
    Recommendation,
    RecommendationCandidate,
)
from revora.persistence.models.execution import (
    ExecutionIntent,
    PaymentStateRead,
    RecoveryOutcome,
)
from revora.persistence.models.ingestion import (
    DetectionVerdictRecord,
    EventQuarantine,
    WebhookEvent,
)
from revora.persistence.models.jobs import Job, JobAttempt
from revora.persistence.models.learning import (
    Experiment,
    ExperimentAssignment,
    ExperimentVersionFreeze,
    MemoryObservation,
    ModelPromotion,
    ModelVersion,
)
from revora.persistence.models.ops import AppConfig, SyntheticRun
from revora.persistence.models.policy import (
    PolicyCheckResult,
    PolicyDecision,
    PolicyRuleSet,
)
from revora.persistence.models.tenancy import (
    CustomerConsent,
    Merchant,
    MerchantUser,
    WebhookSecret,
)

__all__ = [
    "ALL_TABLES",
    "AUDIT_TIMESTAMP",
    "CONFIDENCE",
    "MONEY",
    "PROBABILITY",
    "SIGNED_INCREMENT",
    "TENANT_SCOPED_TABLES",
    "TIMESTAMPTZ",
    "AiInvocation",
    "AppConfig",
    "AuditRecord",
    "Base",
    "BaselineEstimate",
    "CandidateEstimate",
    "CreatedAtMixin",
    "CustomerConsent",
    "DetectionVerdictRecord",
    "Diagnosis",
    "EnumBackedColumn",
    "EventQuarantine",
    "ExecutionIntent",
    "Experiment",
    "ExperimentAssignment",
    "ExperimentVersionFreeze",
    "IdMixin",
    "Job",
    "JobAttempt",
    "MemoryObservation",
    "Merchant",
    "MerchantScopedMixin",
    "MerchantUser",
    "ModelPromotion",
    "ModelVersion",
    "PaymentStateRead",
    "PolicyCheckResult",
    "PolicyDecision",
    "PolicyRuleSet",
    "Recommendation",
    "RecommendationCandidate",
    "RecoveryCase",
    "RecoveryOutcome",
    "RowBase",
    "SyntheticRun",
    "WebhookEvent",
    "WebhookSecret",
    "enum_backed_columns",
]

ALL_TABLES: tuple[str, ...] = tuple(sorted(Base.metadata.tables))
"""Every table name, sorted. Thirty-one of them."""

TENANT_SCOPED_TABLES: tuple[str, ...] = tuple(
    sorted(
        name
        for name, table in Base.metadata.tables.items()
        if "merchant_id" in table.columns
    )
)
"""Every table row-level security applies to: all of them except ``merchant``.

Derived rather than listed, because a hand-maintained list is how a table ends up
without a policy — and a table without a policy is a table where a repository bug
becomes a cross-tenant read.
"""
