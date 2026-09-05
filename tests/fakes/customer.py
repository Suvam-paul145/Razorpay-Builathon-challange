"""In-memory stand-ins for the Customer_Access_Token's store, audit sink and secrets.

The design's Testing Strategy puts P31, P33 and the read half of P32 in the ``model`` tier —
"Customer_Response_Service against in-memory fake repositories" — and this module is that.
The reason those three belong here rather than in ``pg`` is worth stating: they are claims
about **scoping and disclosure**, and a real database contributes nothing to either. Whether a
token reaches a second case, and whether a secret reaches an audit field, is decided entirely
by the code above the repository. Running them against PostgreSQL would make them slower and
no stronger, and would hide the one claim that genuinely needs a server — P32's row-lock
bound, which is about two concurrent transactions and is tested against the real repository.

**The fake is not a permissive one.** :class:`FakeTokenStore` enforces
``one_live_token_per_case`` and raises the same ``IntegrityError`` the real index produces, so
a test that mints twice without revoking fails here exactly as it would in ``pg``. A fake that
accepted the second insert would let the revoke-then-mint ordering rot undetected, which is
the single mistake the partial index exists to catch.

Nothing here writes a database row, opens a session, or reads a real credential.
"""

from __future__ import annotations

import base64
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.exc import IntegrityError

from revora.audit.writer import AuditEntry
from revora.domain.enums import TokenRevocationReason
from revora.persistence.models.customer import CustomerAccessToken
from revora.platform.secrets import (
    ENV_CUSTOMER_TOKEN_SIGNING_SECRETS,
    EnvironmentSecretResolver,
    SecretStore,
    reset_secret_store,
    set_secret_store,
)

__all__ = [
    "FakeTokenStore",
    "RecordedAudit",
    "RecordingAuditSink",
    "installed_signing_secrets",
    "signing_key",
]


def signing_key(version: int) -> bytes:
    """A deterministic 32-byte key for a version. Distinct per version, never a real secret.

    Deterministic so a failing example reproduces, and derived from the version so two
    versions cannot accidentally be the same key — which would make "verification tried every
    secret" pass for a rotation that had not actually rotated anything.
    """
    return bytes((version * 7 + index) % 256 for index in range(32))


@contextmanager
def installed_signing_secrets(*versions: int) -> Iterator[Mapping[int, bytes]]:
    """Install a ``SecretStore`` holding exactly ``versions``, restoring the real one after.

    Empty ``versions`` installs a store with the variable absent, which is how R29.C13's
    refusal path is reached: no mint, no verify, ``CREDENTIAL_UNAVAILABLE``, 503.

    Retiring a version is expressed by *not* listing it — there is no "retired" flag, because
    the configured set is the active set and that identity is what makes rotation a
    configuration change rather than a schema one.
    """
    keys = {version: signing_key(version) for version in versions}
    environ: dict[str, str] = {}
    if keys:
        environ[ENV_CUSTOMER_TOKEN_SIGNING_SECRETS] = ",".join(
            f"{version}:{base64.b64encode(key).decode('ascii')}"
            for version, key in sorted(keys.items())
        )
    set_secret_store(SecretStore(EnvironmentSecretResolver(environ)))
    try:
        yield keys
    finally:
        reset_secret_store()


@dataclass(frozen=True, slots=True)
class RecordedAudit:
    """One audit record the sink was asked to write, before masking."""

    attached: bool
    merchant_id: uuid.UUID
    case_id: uuid.UUID | None
    entry: AuditEntry


class RecordingAuditSink:
    """Collects audit entries instead of writing them. Satisfies ``AuditSink``."""

    __slots__ = ("records",)

    def __init__(self) -> None:
        self.records: list[RecordedAudit] = []

    def write_unattached(
        self,
        merchant_id: uuid.UUID,
        entry: AuditEntry,
        *,
        correlation_id: uuid.UUID | None = None,
        occurred_at: datetime | None = None,
    ) -> RecordedAudit:
        record = RecordedAudit(False, merchant_id, None, entry)
        self.records.append(record)
        return record

    def write_for_case(
        self,
        merchant_id: uuid.UUID,
        case_id: uuid.UUID,
        entry: AuditEntry,
        *,
        correlation_id: uuid.UUID | None = None,
        occurred_at: datetime | None = None,
    ) -> RecordedAudit:
        record = RecordedAudit(True, merchant_id, case_id, entry)
        self.records.append(record)
        return record

    def event_types(self) -> list[str]:
        return [record.entry.event_type for record in self.records]


class FakeTokenStore:
    """An in-memory ``customer_access_token`` table with its two indexes enforced.

    ``uq_customer_access_token_merchant_id_token_id`` is the dict key.
    ``one_live_token_per_case`` is checked on insert and raises ``IntegrityError``, because a
    fake that let a second live token through would make the revoke-then-mint ordering
    untested — and that ordering is the one thing about minting that a reader would plausibly
    simplify away.
    """

    __slots__ = ("rows",)

    def __init__(self) -> None:
        self.rows: dict[tuple[uuid.UUID, str], CustomerAccessToken] = {}

    # -- reads -----------------------------------------------------------------

    def by_token_id(
        self, merchant_id: uuid.UUID, token_id: str
    ) -> CustomerAccessToken | None:
        return self.rows.get((merchant_id, token_id))

    def live_for_case(
        self, merchant_id: uuid.UUID, case_id: uuid.UUID
    ) -> CustomerAccessToken | None:
        live = [
            row
            for (owner, _token_id), row in self.rows.items()
            if owner == merchant_id and row.case_id == case_id and row.revoked_at is None
        ]
        if len(live) > 1:  # pragma: no cover - the insert below makes this unreachable
            raise AssertionError(
                "two live tokens for one case; one_live_token_per_case would have refused"
            )
        return live[0] if live else None

    # -- writes ----------------------------------------------------------------

    def insert(
        self, merchant_id: uuid.UUID, *, values: Mapping[str, object]
    ) -> CustomerAccessToken:
        row = CustomerAccessToken(**dict(values))
        row.merchant_id = merchant_id
        if self.live_for_case(merchant_id, row.case_id) is not None:
            raise IntegrityError(
                "one_live_token_per_case", None, Exception("duplicate live token")
            )
        self.rows[(merchant_id, row.token_id)] = row
        return row

    def revoke_for_case(
        self,
        merchant_id: uuid.UUID,
        case_id: uuid.UUID,
        *,
        moment: datetime,
        reason: TokenRevocationReason,
    ) -> int:
        revoked = 0
        for (owner, _token_id), row in self.rows.items():
            if owner != merchant_id or row.case_id != case_id or row.revoked_at is not None:
                continue
            row.revoked_at = moment
            row.revocation_reason = reason.value
            revoked += 1
        return revoked

    def increment_accepted_submissions(
        self, merchant_id: uuid.UUID, token_id: str, *, max_submissions: int
    ) -> int | None:
        if max_submissions < 0:
            raise ValueError("max_submissions must not be negative")
        row = self.rows.get((merchant_id, token_id))
        if row is None or int(row.accepted_submission_count) >= max_submissions:
            # ``None`` for "bound reached" is the real repository's contract: a normal answer
            # the caller turns into a 429, not an error.
            return None
        row.accepted_submission_count = int(row.accepted_submission_count) + 1
        return int(row.accepted_submission_count)
