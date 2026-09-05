"""Transaction boundaries, the tenant session variable, and the three lock modes.

Everything in this module exists because the execution and audit designs need
control of transaction boundaries rather than a framework's idea of them.

**The tenant variable.** ``SET LOCAL revora.merchant_id`` is issued at the start of
every transaction, and ``LOCAL`` is the important word: the setting reverts at
commit or rollback, so a pooled connection cannot carry one merchant's identity
into the next transaction that borrows it. This is what the row-level-security
policies read. It is defence in depth — the repository layer's mandatory
``merchant_id`` argument is the primary control, and RLS is the belt that catches
the mistake the application layer is most likely to make.

**The three lock modes**, each with a specific caller:

* ``FOR UPDATE`` — the case row, held for the whole of a state transition. Audit
  sequence allocation piggybacks on this lock, which is what makes per-case audit
  numbering gap-free without a sequence.
* ``FOR UPDATE SKIP LOCKED`` — the queue claim. Two workers must not wait on each
  other for a job either of them could take; skipping is the whole point.
* ``pg_advisory_xact_lock`` — the per-case execution lock, taken when there is no
  single row to lock or when the lock must be held across reads of several tables.
  Transaction-scoped, so it is released by commit or rollback and a crashed worker
  cannot strand it. A session-scoped advisory lock would need explicit release and
  would leak on exactly the failure it exists to guard.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, TypeVar

from sqlalchemy import Select, text
from sqlalchemy.orm import Session

from revora.persistence.repositories.engine import get_session_factory

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sqlalchemy.orm import sessionmaker

__all__ = [
    "TENANT_SETTING",
    "advisory_xact_lock",
    "case_advisory_key",
    "for_update",
    "for_update_skip_locked",
    "rows_visible_to",
    "set_tenant",
    "tenant_transaction",
    "transaction",
    "try_advisory_xact_lock",
]

TENANT_SETTING = "revora.merchant_id"
"""The session variable the RLS policies read. A custom GUC rather than a role per
merchant: roles do not scale to a tenant count that changes at runtime."""

_T = TypeVar("_T")


def set_tenant(session: Session, merchant_id: uuid.UUID) -> None:
    """Bind this transaction to one merchant.

    ``SET LOCAL`` scopes the value to the transaction, so a pooled connection
    cannot leak it into the next one. Parameterized rather than interpolated: the
    value arrives from an authenticated session or a job row, and a ``SET`` built
    by string concatenation is an injection point in the one place that is meant
    to be the safety net.
    """
    session.execute(
        text(f"SELECT set_config('{TENANT_SETTING}', :merchant_id, true)"),
        {"merchant_id": str(merchant_id)},
    )


@contextmanager
def transaction(factory: sessionmaker[Session] | None = None) -> Iterator[Session]:
    """One explicit transaction. Commits on success, rolls back on any exception.

    Untenanted deliberately: migrations, the schema check and the scheduler's own
    bookkeeping have no merchant. Anything touching tenant data should use
    :func:`tenant_transaction` instead, and the RLS policies make that not merely
    advice — an untenanted read of a tenant table returns nothing.
    """
    session_factory = factory or get_session_factory()
    session = session_factory()
    try:
        with session.begin():
            yield session
    finally:
        session.close()


@contextmanager
def tenant_transaction(
    merchant_id: uuid.UUID,
    factory: sessionmaker[Session] | None = None,
) -> Iterator[Session]:
    """One explicit transaction bound to one merchant.

    The API sets the merchant from the authenticated session; the worker sets it
    from the claimed job's ``merchant_id``. Both go through here so there is one
    place where the binding can be verified.
    """
    session_factory = factory or get_session_factory()
    session = session_factory()
    try:
        with session.begin():
            set_tenant(session, merchant_id)
            yield session
    finally:
        session.close()


def for_update(statement: Select[Any], *, nowait: bool = False) -> Select[Any]:
    """Add ``FOR UPDATE`` to a select.

    ``nowait`` for the caller who would rather fail immediately than queue behind
    another writer — the API's human-override path, where a merchant is waiting on
    a response and a lock wait would read as a hang.
    """
    return statement.with_for_update(nowait=nowait)


def for_update_skip_locked(statement: Select[Any]) -> Select[Any]:
    """Add ``FOR UPDATE SKIP LOCKED``, for queue claims.

    Rows another worker already holds are passed over rather than waited for. Two
    workers claiming concurrently take different jobs and neither blocks.
    """
    return statement.with_for_update(skip_locked=True)


def case_advisory_key(case_id: uuid.UUID) -> int:
    """A stable 64-bit advisory-lock key for a case.

    Derived from the UUID's own bits rather than from a hash of its string form, so
    it is reproducible across processes and Python versions — a lock key that
    changes between two workers is not a lock. Signed, because Postgres advisory
    keys are ``bigint``.
    """
    unsigned = case_id.int & 0xFFFFFFFFFFFFFFFF
    return unsigned - (1 << 64) if unsigned >= (1 << 63) else unsigned


def advisory_xact_lock(session: Session, key: int) -> None:
    """Take a transaction-scoped advisory lock, waiting if another holder has it.

    Released by commit or rollback, with no explicit unlock to forget and nothing
    stranded if the process dies mid-transaction.
    """
    session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})


def try_advisory_xact_lock(session: Session, key: int) -> bool:
    """Take the lock if it is free, otherwise return ``False`` immediately.

    For the execution path: if another worker is already acting on this case, the
    correct response is to leave it alone, not to queue up behind it and then issue
    a second provider call once it finishes.
    """
    result = session.execute(text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": key})
    return bool(result.scalar_one())


def rows_visible_to(session: Session) -> Sequence[str]:  # pragma: no cover - diagnostic
    """The merchant id this transaction is bound to, as the database sees it.

    A diagnostic, used by the RLS tests and by an operator asking "what does this
    connection think it is". Returns a one-element sequence, or an empty one when
    the transaction is untenanted.
    """
    result = session.execute(
        text(f"SELECT current_setting('{TENANT_SETTING}', true)")
    ).scalar_one_or_none()
    return () if result in (None, "") else (str(result),)
