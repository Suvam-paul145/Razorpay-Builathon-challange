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
from revora.persistence.models.customer import (
    ContactSuppression,
    CustomerAccessToken,
    CustomerSignal,
    PromiseToPay,
)
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
    ExperimentResult,
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
    MerchantSession,
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
    "ContactSuppression",
    "CreatedAtMixin",
    "CustomerAccessToken",
    "CustomerConsent",
    "CustomerSignal",
    "DetectionVerdictRecord",
    "Diagnosis",
    "EnumBackedColumn",
    "EventQuarantine",
    "ExecutionIntent",
    "Experiment",
    "ExperimentAssignment",
    "ExperimentResult",
    "ExperimentVersionFreeze",
    "IdMixin",
    "Job",
    "JobAttempt",
    "MemoryObservation",
    "Merchant",
    "MerchantScopedMixin",
    "MerchantSession",
    "MerchantUser",
    "ModelPromotion",
    "ModelVersion",
    "PaymentStateRead",
    "PolicyCheckResult",
    "PolicyDecision",
    "PolicyRuleSet",
    "PromiseToPay",
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
"""Every table name, sorted. Thirty-seven of them.

``customer_access_token``, ``customer_signal``, ``contact_suppression`` and
``promise_to_pay`` arrived with migration ``0008`` and the customer response loop. They
are the four tables a customer with no session can cause rows in, and each declares its
own row-level security in that migration rather than inheriting it: ``0003`` derived its
table list from the metadata as it stood then, so a table added later gets no policy
from it.

``experiment_result`` was added in migration 0005 alongside
``memory_observation.diagnosis_method``. Both exist because the evidence phase needs to
persist a claim's *conditions* — the counts and interval an analysis was concluded from,
and how a diagnosis was reached — and neither is reconstructable after the fact from live
tables that keep moving.

``merchant_session`` arrived with the API layer in migration 0006. It is a row rather than a
signed token because R17 needs a session that can be revoked, and because a tenant read from a
row this system wrote is the literal form of "the merchant comes from the session and from
nothing in the request"."""

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
