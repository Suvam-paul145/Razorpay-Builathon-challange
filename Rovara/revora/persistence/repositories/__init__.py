"""Repositories, sessions, locks and the startup schema check.

The organizing rule of this package: **every read, list, query and export function
takes ``merchant_id`` as a required argument.** There is one documented exception,
:func:`~revora.persistence.repositories.jobs.claimable_merchant_ids`, which returns
merchant ids and no tenant data, and exists because a worker must choose a tenant
before it can bind a transaction to one.

Row-level security backs this up, but it is the backstop rather than the control.
The mistake that actually happens is a forgotten ``WHERE`` in application code, and
that is what a required argument prevents.
"""

from __future__ import annotations

from revora.persistence.repositories.audit import (
    AUDIT_MUTATION_REJECTED,
    AuditRecordRepository,
    record_mutation_rejected,
)
from revora.persistence.repositories.base import MerchantScopedRepository
from revora.persistence.repositories.cases import (
    RecoveryCaseRepository,
    WebhookEventRepository,
)
from revora.persistence.repositories.config import (
    ConfigurationRepository,
    load_configuration,
)
from revora.persistence.repositories.engine import (
    DatabaseNotConfiguredError,
    build_engine,
    build_session_factory,
    database_url,
    dispose_engine,
    get_engine,
    get_session_factory,
    set_engine,
)
from revora.persistence.repositories.execution import (
    ExecutionIntentRepository,
    PaymentStateReadRepository,
    RecoveryOutcomeRepository,
)
from revora.persistence.repositories.jobs import (
    JobAttemptRepository,
    JobRepository,
    claimable_merchant_ids,
)
from revora.persistence.repositories.schema import (
    EXPECTED_REVISION,
    SchemaRevisionMismatchError,
    current_revision,
    verify_schema_revision,
)
from revora.persistence.repositories.session import (
    TENANT_SETTING,
    advisory_xact_lock,
    case_advisory_key,
    for_update,
    for_update_skip_locked,
    set_tenant,
    tenant_transaction,
    transaction,
    try_advisory_xact_lock,
)

__all__ = [
    "AUDIT_MUTATION_REJECTED",
    "EXPECTED_REVISION",
    "TENANT_SETTING",
    "AuditRecordRepository",
    "ConfigurationRepository",
    "DatabaseNotConfiguredError",
    "ExecutionIntentRepository",
    "JobAttemptRepository",
    "JobRepository",
    "MerchantScopedRepository",
    "PaymentStateReadRepository",
    "RecoveryCaseRepository",
    "RecoveryOutcomeRepository",
    "SchemaRevisionMismatchError",
    "WebhookEventRepository",
    "advisory_xact_lock",
    "build_engine",
    "build_session_factory",
    "case_advisory_key",
    "claimable_merchant_ids",
    "current_revision",
    "database_url",
    "dispose_engine",
    "for_update",
    "for_update_skip_locked",
    "get_engine",
    "get_session_factory",
    "load_configuration",
    "record_mutation_rejected",
    "set_engine",
    "set_tenant",
    "tenant_transaction",
    "transaction",
    "try_advisory_xact_lock",
    "verify_schema_revision",
]
