"""The primary tenant control: no repository function can read across merchants.

No database needed, so this runs in the cheap tier. It walks every public callable on
every repository class in ``revora.persistence.repositories`` and asserts
``merchant_id`` is a required parameter. That is the mechanism the design calls
primary — row-level security is the backstop — and it is the one a new repository
written six months from now could quietly break.

One exception is allowed and it is named explicitly below. Adding to that list should
take an argument.
"""

from __future__ import annotations

import inspect
import uuid
from typing import Any

import pytest

from revora.persistence import repositories
from revora.persistence.repositories.base import MerchantScopedRepository

pytestmark = pytest.mark.pure
"""Marked here rather than relying on the directory.

The docstring above has always said this runs in the cheap tier, and it did not: CI's fast job
selects ``pure or model``, this module carried no marker, and the ``pytestmark`` in the sibling
conftest does not apply to other modules — so the primary tenant control was checked only when
somebody ran the whole suite locally. Four tests, in the cheapest tier there is, guarding the
mechanism the design calls primary."""

DELIBERATE_EXCEPTIONS: frozenset[str] = frozenset(
    {
        # Returns merchant ids and nothing else. A worker has to choose a tenant before
        # it can bind a transaction to one, and that choice cannot itself be scoped
        # without becoming "poll every merchant in turn".
        "claimable_merchant_ids",
    }
)

_INFRASTRUCTURE: frozenset[str] = frozenset(
    {
        # Session, engine, lock and schema helpers. They take no tenant because they
        # operate on a connection rather than on rows — set_tenant is how a tenant gets
        # attached, so requiring one here would be circular.
        "advisory_xact_lock",
        "build_engine",
        "build_session_factory",
        "case_advisory_key",
        "current_revision",
        "database_url",
        "dispose_engine",
        "for_update",
        "for_update_skip_locked",
        "get_engine",
        "get_session_factory",
        "set_engine",
        "set_tenant",
        "tenant_transaction",
        "transaction",
        "try_advisory_xact_lock",
        "verify_schema_revision",
    }
)


def _repository_classes() -> list[type[Any]]:
    return [
        obj
        for name in repositories.__all__
        if inspect.isclass(obj := getattr(repositories, name))
        and issubclass(obj, MerchantScopedRepository)
        and obj is not MerchantScopedRepository
    ]


def test_there_is_at_least_one_repository_to_check() -> None:
    """Guard against the walk silently finding nothing."""
    assert len(_repository_classes()) >= 8


def test_every_repository_method_requires_merchant_id() -> None:
    """No public repository method can be called without naming a merchant."""
    offenders: list[str] = []

    for repository in _repository_classes():
        for name, member in inspect.getmembers(repository, inspect.isfunction):
            if name.startswith("_"):
                continue
            parameters = inspect.signature(member).parameters
            if "merchant_id" not in parameters:
                offenders.append(f"{repository.__name__}.{name} takes no merchant_id")
                continue
            parameter = parameters["merchant_id"]
            if parameter.default is not inspect.Parameter.empty:
                offenders.append(
                    f"{repository.__name__}.{name} defaults merchant_id; "
                    "a defaulted tenant is an unscoped read waiting to happen"
                )

    assert not offenders, "\n".join(offenders)


def test_every_module_level_function_requires_merchant_id_or_is_listed() -> None:
    """Module-level helpers too, with the two documented categories exempted."""
    offenders: list[str] = []

    for name in repositories.__all__:
        member = getattr(repositories, name)
        if not inspect.isfunction(member):
            continue
        if name in DELIBERATE_EXCEPTIONS or name in _INFRASTRUCTURE:
            continue
        parameters = inspect.signature(member).parameters
        if "merchant_id" not in parameters:
            offenders.append(
                f"{name} takes no merchant_id and is not a listed exception"
            )

    assert not offenders, "\n".join(offenders)


def test_scoped_statement_filters_on_the_given_merchant() -> None:
    """The one helper every read is built from actually filters.

    Compiled rather than executed, so this stays in the cheap tier. It checks the
    thing that would be catastrophic and silent: a ``scoped`` that returned an
    unfiltered select would make every method above pass its signature check while
    reading every tenant's rows.
    """
    from revora.persistence.repositories.cases import RecoveryCaseRepository

    merchant_id = uuid.uuid4()
    statement = RecoveryCaseRepository(session=None).scoped(merchant_id)  # type: ignore[arg-type]
    compiled = str(statement.compile(compile_kwargs={"literal_binds": True}))

    assert "WHERE recovery_case.merchant_id" in compiled
    # Postgres renders a UUID literal without dashes, so compare on the hex form.
    assert merchant_id.hex in compiled
