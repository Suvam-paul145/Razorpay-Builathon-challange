"""Task 31.4. The checks that answer "is this deployment actually wired up?"

These are not unit tests. Every one of them asserts something about the *deployed process* rather
than about a function, and each one fails in a way that a behavioural test cannot see because the
behaviour would look perfectly correct while being wrong about the world:

* **The schema revision matches.** A process serving against a schema it was not built for produces
  *wrong numbers*, not errors, and the investigation starts in the wrong place (R16.C9's neighbour).
* **TLS verification is on, on every outbound client.** A disabled certificate check does not fail —
  it succeeds, silently, against whatever answered (R17.C5).
* **Every credential resolves, and a missing one raises rather than defaulting.** A secret that
  quietly falls back to a default is worse than one that is absent, because the system runs.
* **The webhook signature canary passes.** A broken HMAC construction rejects every real event
  uniformly, which looks exactly like a provider that stopped sending (R1.C1).

The latency assertions are tagged ``smoke`` so they can be excluded from gating **without being
deleted**. That distinction is the point of the tag: a timing assertion on shared CI hardware is a
measurement of the runner's mood as much as of the code, so it must not be able to turn a build red
on its own — and deleting it instead would remove the only place the acknowledgement budget is
written down as a number anyone checks. The margins are deliberately generous for the same reason:
these catch an order-of-magnitude regression, not a percentage.
"""

from __future__ import annotations

import base64
import re
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine

from revora.ingestion.signature import signature_canary
from revora.persistence.repositories.schema import (
    EXPECTED_REVISION,
    SchemaRevisionMismatchError,
    current_revision,
    verify_schema_revision,
)
from revora.platform.config import default_configuration
from revora.platform.secrets import (
    CredentialUnavailableError,
    SecretStore,
    set_secret_store,
)

_REPO = Path(__file__).resolve().parents[1]
_CONFIG = default_configuration()


# ---------------------------------------------------------------------------
# The webhook signature canary (R1.C1)
# ---------------------------------------------------------------------------


@pytest.mark.pure
def test_the_webhook_signature_canary_passes() -> None:
    """A known body, signed and verified through the real path.

    This is the check that would have caught a changed digest or a broken constant-time comparison
    before the first real delivery did. The failure it guards against is uniquely nasty: a broken
    HMAC rejects *every* event identically, so the endpoint keeps answering 401 and the symptom is
    "the provider stopped sending us anything" — a conclusion that sends the investigation to the
    provider's dashboard rather than to this process.
    """
    assert signature_canary() is True, (
        "the webhook verification path is broken; every real delivery would be rejected and the "
        "symptom would look like the provider having stopped sending"
    )


# ---------------------------------------------------------------------------
# TLS verification (R17.C5)
# ---------------------------------------------------------------------------


@pytest.mark.pure
def test_no_outbound_client_disables_certificate_verification() -> None:
    """``verify=False`` must appear nowhere, and the provider client must set it explicitly.

    Checked as source text because that is where the failure lives. A disabled certificate check is
    not a runtime error — the request succeeds against whatever presented a certificate — so there
    is no behaviour to assert on. The only observable form of this bug is the literal.

    ``verify=True`` is also asserted *present* rather than left to httpx's default. The default is
    correct today; stating it means a future change to that default cannot silently turn this off,
    and it documents at the call site that the choice was made rather than inherited.
    """
    offenders: list[str] = []
    for path in (_REPO / "revora").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for number, line in enumerate(text.splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if re.search(r"verify\s*=\s*False", stripped):
                offenders.append(f"{path.relative_to(_REPO)}:{number} {stripped}")
    assert offenders == [], f"certificate verification is disabled somewhere: {offenders}"

    razorpay = (_REPO / "revora" / "providers" / "razorpay.py").read_text(encoding="utf-8")
    assert "verify=True" in razorpay, (
        "the provider client does not state verify=True; relying on the library default means a "
        "change to that default silently disables certificate checking"
    )


# ---------------------------------------------------------------------------
# Credentials resolve, and absence is loud (R17.C5)
# ---------------------------------------------------------------------------


class _CompleteResolver:
    """Every credential the running system needs, and no LLM one."""

    def get(self, name: str) -> str | None:
        if name.startswith("REVORA_DASHBOARD_KEYS_"):
            return "smoke-operator-key"
        if name.startswith("REVORA_WEBHOOK_SECRETS_"):
            return "smoke-webhook-secret"
        return {
            "REVORA_PAYLOAD_ENCRYPTION_KEYS": "1:" + base64.b64encode(b"P" * 32).decode(),
            "REVORA_CUSTOMER_KEY_SECRET": base64.b64encode(b"K" * 32).decode(),
            "REVORA_CUSTOMER_TOKEN_SIGNING_SECRETS": "1:"
            + base64.b64encode(b"K" * 32).decode(),
            "REVORA_SESSION_TOKEN_SECRET": base64.b64encode(b"S" * 32).decode(),
            "REVORA_RAZORPAY_KEY_ID": "rzp_test_smoke",
            "REVORA_RAZORPAY_KEY_SECRET": "smoke-secret",
        }.get(name)


class _EmptyResolver:
    def get(self, name: str) -> str | None:
        return None


@pytest.fixture
def restore_secret_store() -> Iterator[None]:
    previous = set_secret_store(SecretStore(_EmptyResolver()))
    try:
        yield
    finally:
        set_secret_store(previous)


@pytest.mark.pure
def test_every_required_credential_resolves_when_configured(
    restore_secret_store: None,
) -> None:
    """The full set, resolved through the real store.

    A deployment missing one of these does not fail at startup — it fails at the moment the
    credential is first needed, which for the session-token secret is the first sign-in and for the
    payload key is the first webhook. Naming them all in one place is what makes a deployment
    checklist checkable.
    """
    store = SecretStore(_CompleteResolver())
    set_secret_store(store)

    assert store.razorpay_key_id().reveal() == "rzp_test_smoke"
    assert store.razorpay_key_secret().reveal() == "smoke-secret"
    assert len(store.customer_key_secret()) == 32
    assert len(store.session_token_secret()) == 32
    assert set(store.payload_encryption_keys()) == {1}
    assert store.webhook_signing_secrets("some-merchant")
    assert store.dashboard_keys("some-merchant")


@pytest.mark.pure
def test_a_missing_credential_raises_rather_than_defaulting(
    restore_secret_store: None,
) -> None:
    """Absence must be loud. A default would let the system run and be wrong.

    Each of these has a specific consequence if it silently defaulted: a default payload key means
    every stored payload is encrypted under a key an attacker also knows; a default customer-key
    secret makes the consent table an enumerable contact list; a default session secret makes every
    session forgeable. There is no safe fallback for any of them, so there is no fallback.
    """
    store = SecretStore(_EmptyResolver())

    for label, call in (
        ("razorpay key id", store.razorpay_key_id),
        ("razorpay key secret", store.razorpay_key_secret),
        ("customer key secret", store.customer_key_secret),
        ("session token secret", store.session_token_secret),
        ("payload encryption keys", store.payload_encryption_keys),
    ):
        with pytest.raises(CredentialUnavailableError):
            call()
            pytest.fail(f"{label} resolved to a default with nothing configured")


@pytest.mark.pure
def test_the_llm_credential_is_absent_and_nothing_needs_it(
    restore_secret_store: None,
) -> None:
    """This build has no reasoning layer, so this credential must be unnecessary.

    Asserted rather than assumed. A deployment checklist that listed an LLM key would imply the
    system degrades without one; it does not degrade, because there is nothing to degrade — and the
    accessor still exists, so the only way to state that is to show it raises and that no code
    path consults it.
    """
    store = SecretStore(_EmptyResolver())
    with pytest.raises(CredentialUnavailableError):
        store.llm_credential()

    callers = [
        str(path.relative_to(_REPO))
        for path in (_REPO / "revora").rglob("*.py")
        if "llm_credential()" in path.read_text(encoding="utf-8")
        and path.name != "secrets.py"
    ]
    assert callers == [], f"something consults the LLM credential: {callers}"


# ---------------------------------------------------------------------------
# Schema revision (R16.C9's neighbour)
# ---------------------------------------------------------------------------


@pytest.mark.pg
def test_the_migrated_database_is_at_the_revision_this_build_expects(
    owner_engine: Engine,
) -> None:
    """The check the API runs at startup, run here against the migrated database.

    ``EXPECTED_REVISION`` is hand-maintained on purpose: a value derived from the migration files
    present can never disagree with them, and so could never detect a database that has not been
    migrated. This test is the other half of that arrangement — it is what makes forgetting the bump
    a build failure rather than a production surprise.
    """
    assert current_revision(owner_engine) == EXPECTED_REVISION
    assert verify_schema_revision(owner_engine, expected=EXPECTED_REVISION) == EXPECTED_REVISION


@pytest.mark.pg
def test_a_wrong_expected_revision_refuses_to_serve(owner_engine: Engine) -> None:
    """Refusing to start is the point, so the refusal is asserted.

    A process that started anyway against the wrong schema would answer requests with subtly wrong
    figures. Loud, immediate and cheap beats quietly different.
    """
    with pytest.raises(SchemaRevisionMismatchError) as caught:
        verify_schema_revision(owner_engine, expected="0000")
    # Both revisions in the message, because the operator's next action differs: behind means run
    # migrations, ahead means this build is the old one and should not be serving.
    assert "0000" in str(caught.value)
    assert EXPECTED_REVISION in str(caught.value)


# ---------------------------------------------------------------------------
# Latency bounds — tagged so they can be excluded from gating, not deleted
# ---------------------------------------------------------------------------


# NOTE. The webhook acknowledgement-latency check lives in
# ``tests/integration/test_latency_smoke.py``, not here: it needs the app, the tenant and a signed
# delivery, and those fixtures are defined in the integration conftest — which is visible only to
# tests below it. Splitting on fixture visibility rather than on subject keeps both files able to
# use the real entry points instead of reconstructing them.


@pytest.mark.smoke
@pytest.mark.pure
def test_the_signature_canary_is_cheap_enough_to_run_at_startup() -> None:
    """It runs on every process start, so it must not be a startup cost worth skipping.

    A canary somebody disabled because it made deploys slow is a canary that is not running.
    """
    started = time.perf_counter()
    for _ in range(100):
        assert signature_canary()
    elapsed = time.perf_counter() - started
    assert elapsed < 1.0, f"100 canary runs took {elapsed:.3f}s; one startup check must be trivial"
