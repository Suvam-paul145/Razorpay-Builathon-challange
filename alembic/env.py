"""Alembic environment.

Two decisions worth stating.

**The URL comes from the environment, not from ``alembic.ini``.** A connection string
in a committed file is one somebody eventually runs a migration against by mistake.
``REVORA_DATABASE_URL`` is the same variable the application reads, so a migration
cannot be applied to a database the application is not configured for.

**Autogenerate compares types and server defaults.** Both are off by default in
Alembic, and both matter here: the type discipline (money as ``BIGINT``, probability
as ``NUMERIC(6,4)``, no floats anywhere) is only enforced if a drifting column type
shows up as a diff, and several invariants are carried by server-side defaults.

Importing ``revora.persistence.models`` is what populates the metadata. It is the
only application import here, and it pulls in ``revora.domain`` — which is
deliberate, because the terminal-state list in the partial unique index and every
enum ``CHECK`` are generated from the domain's own declarations rather than restated
in SQL.
"""

from __future__ import annotations

import os
from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

from alembic import context
from revora.persistence.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

_ENV_URL = "REVORA_DATABASE_URL"


def _url() -> str:
    """The database URL, from the environment or from ``-x url=...``.

    The ``-x`` form exists for the test harness, which points at a container whose
    port is only known at runtime.
    """
    override = context.get_x_argument(as_dictionary=True).get("url")
    if override:
        return str(override)
    from_env = (os.environ.get(_ENV_URL) or "").strip()
    if not from_env:
        raise RuntimeError(
            f"{_ENV_URL} is not set. Migrations need a target database; "
            "there is deliberately no URL in alembic.ini."
        )
    return from_env


def _include_object(
    obj: object,
    name: str | None,
    type_: str,
    reflected: bool,
    compare_to: object,
) -> bool:
    """Keep autogenerate away from objects no model describes.

    Row-level security policies, the audit trigger and its functions are created by
    hand-written migrations because SQLAlchemy's metadata has no concept of them.
    Without this filter, a later autogenerate would not see them and would not
    propose dropping them — but the ``alembic_version`` table and any future
    extension-owned object would still show up as spurious diffs.
    """
    return not (type_ == "table" and name == "alembic_version")


def run_migrations_offline() -> None:
    """Emit SQL to stdout instead of running it.

    Used to review a migration before it touches a real database, and to validate
    that a migration's Python executes without needing a server at all.
    """
    context.configure(
        url=_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
        include_object=_include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Apply migrations against the configured database.

    One transaction for the whole run: Postgres has transactional DDL, so a failed
    migration leaves the schema exactly as it was rather than half-applied. That is
    the property that makes a failed release recoverable by fixing the migration
    instead of by repairing the database.
    """
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = _url()
    connectable = engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
            include_object=_include_object,
            transaction_per_migration=False,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
