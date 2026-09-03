"""Repositories, sessions, locks and the startup schema check.

The organizing rule of this package: **every read, list, query and export function
takes ``merchant_id`` as a required argument.** The documented exceptions are the
functions that run *before* a tenant is known and return merchant identity and
nothing else:
:func:`~revora.persistence.repositories.jobs.claimable_merchant_ids`, because a
worker must choose a tenant before it can bind a transaction to one;
:func:`~revora.persistence.repositories.tenancy.schedulable_merchants`, because the
ticker must enumerate tenants to create work for one — and a tenant with an empty
queue is precisely the one whose sweeps have not been enqueued yet, so the worker's
question cannot answer the ticker's; and
:func:`~revora.persistence.repositories.tenancy.merchant_by_slug`, because an inbound
webhook arrives carrying a slug and no session.

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
from revora.persistence.repositories.customer import (
    ContactSuppressionRepository,
    CustomerAccessTokenRepository,
    CustomerSignalRepository,
    PromiseToPayRepository,
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
from revora.persistence.repositories.users import (
    MerchantSessionRepository,
    MerchantUserRepository,
)

__all__ = [
    "AUDIT_MUTATION_REJECTED",
    "EXPECTED_REVISION",
    "TENANT_SETTING",
    "AuditRecordRepository",
    "ConfigurationRepository",
    "ContactSuppressionRepository",
    "CustomerAccessTokenRepository",
    "CustomerSignalRepository",
    "DatabaseNotConfiguredError",
    "ExecutionIntentRepository",
    "JobAttemptRepository",
    "JobRepository",
    "MerchantScopedRepository",
    "MerchantSessionRepository",
    "MerchantUserRepository",
    "PaymentStateReadRepository",
    "PromiseToPayRepository",
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
