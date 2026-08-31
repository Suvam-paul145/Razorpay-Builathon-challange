"""Secret resolution, the rotation window, and the no-leak guarantees."""

from __future__ import annotations

import base64
import hmac
import traceback
from datetime import UTC, datetime, timedelta
from hashlib import sha256

import pytest

from revora.platform.secrets import (
    CREDENTIAL_UNAVAILABLE,
    ENV_CUSTOMER_KEY_SECRET,
    ENV_LLM_CREDENTIAL,
    ENV_PAYLOAD_ENCRYPTION_KEYS,
    ENV_RAZORPAY_KEY_ID,
    ENV_RAZORPAY_KEY_SECRET,
    ENV_WEBHOOK_SECRETS_PREFIX,
    WEBHOOK_REDELIVERY_WINDOW,
    CredentialUnavailableError,
    EnvironmentSecretResolver,
    SecretStore,
    SecretValue,
    WebhookSecretEntry,
    active_webhook_secrets,
    verify_webhook_signature,
)

BODY = b'{"event":"payment.failed","payload":{"payment":{"entity":{"id":"pay_ABC"}}}}'
OLD_SECRET = "rotation_previous_secret"
NEW_SECRET = "rotation_current_secret"
KEY_32 = base64.b64encode(bytes(range(32))).decode("ascii")
OTHER_KEY_32 = base64.b64encode(bytes(range(32, 64))).decode("ascii")


def signature_for(secret: str, body: bytes = BODY) -> str:
    return hmac.new(secret.encode("utf-8"), body, sha256).hexdigest()


def store_with(**environ: str) -> SecretStore:
    return SecretStore(EnvironmentSecretResolver(environ))


# ---------------------------------------------------------------------------
# Multi-secret verification
# ---------------------------------------------------------------------------


@pytest.mark.pure
def test_verification_accepts_a_match_on_a_non_first_secret() -> None:
    active = (SecretValue(NEW_SECRET), SecretValue(OLD_SECRET))
    # An event still being retried was signed before the rotation.
    assert verify_webhook_signature(BODY, signature_for(OLD_SECRET), active) is True


@pytest.mark.pure
def test_verification_accepts_the_current_secret() -> None:
    active = (SecretValue(NEW_SECRET), SecretValue(OLD_SECRET))
    assert verify_webhook_signature(BODY, signature_for(NEW_SECRET), active) is True


@pytest.mark.pure
def test_verification_rejects_a_signature_from_no_active_secret() -> None:
    active = (SecretValue(NEW_SECRET), SecretValue(OLD_SECRET))
    assert verify_webhook_signature(BODY, signature_for("someone_elses_secret"), active) is False


@pytest.mark.pure
def test_verification_rejects_a_signature_over_different_bytes() -> None:
    active = (SecretValue(NEW_SECRET),)
    assert verify_webhook_signature(BODY, signature_for(NEW_SECRET, BODY + b" "), active) is False


@pytest.mark.pure
def test_no_active_secret_is_classifiable_not_a_silent_false() -> None:
    with pytest.raises(CredentialUnavailableError) as caught:
        verify_webhook_signature(BODY, signature_for(NEW_SECRET), ())
    assert caught.value.credential == "webhook_signing_secret"
    assert caught.value.audit_event_type == CREDENTIAL_UNAVAILABLE


# ---------------------------------------------------------------------------
# Rotation window
# ---------------------------------------------------------------------------

NOW = datetime(2025, 6, 1, 12, 0, tzinfo=UTC)


@pytest.mark.pure
def test_retired_secret_stays_active_inside_the_redelivery_window() -> None:
    rotated_at = NOW - timedelta(hours=2)
    entries = [
        WebhookSecretEntry(SecretValue(OLD_SECRET), NOW - timedelta(days=30), rotated_at),
        WebhookSecretEntry(SecretValue(NEW_SECRET), rotated_at),
    ]
    active = active_webhook_secrets(entries, now=NOW)
    assert [secret.reveal() for secret in active] == [NEW_SECRET, OLD_SECRET]


@pytest.mark.pure
def test_retired_secret_drops_out_once_the_window_closes() -> None:
    retired_at = NOW - WEBHOOK_REDELIVERY_WINDOW - timedelta(minutes=1)
    entries = [
        WebhookSecretEntry(SecretValue(OLD_SECRET), NOW - timedelta(days=30), retired_at),
        WebhookSecretEntry(SecretValue(NEW_SECRET), retired_at),
    ]
    active = active_webhook_secrets(entries, now=NOW)
    assert [secret.reveal() for secret in active] == [NEW_SECRET]


@pytest.mark.pure
def test_a_secret_not_yet_activated_is_not_active() -> None:
    entries = [WebhookSecretEntry(SecretValue(NEW_SECRET), NOW + timedelta(hours=1))]
    assert active_webhook_secrets(entries, now=NOW) == ()


# ---------------------------------------------------------------------------
# Store resolution
# ---------------------------------------------------------------------------


@pytest.mark.pure
def test_ordered_webhook_secrets_come_back_in_declared_order() -> None:
    store = store_with(**{f"{ENV_WEBHOOK_SECRETS_PREFIX}ACME_LTD": f"{NEW_SECRET}, {OLD_SECRET}"})
    secrets = store.webhook_signing_secrets("acme-ltd")
    assert [secret.reveal() for secret in secrets] == [NEW_SECRET, OLD_SECRET]


@pytest.mark.pure
def test_missing_webhook_secret_names_the_credential() -> None:
    with pytest.raises(CredentialUnavailableError) as caught:
        store_with().webhook_signing_secrets("acme-ltd")
    assert caught.value.credential == "webhook_signing_secret"


@pytest.mark.pure
@pytest.mark.parametrize(
    ("accessor", "credential"),
    [
        ("razorpay_key_id", "razorpay_key_id"),
        ("razorpay_key_secret", "razorpay_key_secret"),
        ("llm_credential", "llm_credential"),
        ("customer_key_secret", "customer_key_secret"),
        ("payload_encryption_keys", "payload_encryption_keys"),
    ],
)
def test_every_missing_credential_is_classifiable(accessor: str, credential: str) -> None:
    store = store_with()
    with pytest.raises(CredentialUnavailableError) as caught:
        getattr(store, accessor)()
    assert caught.value.credential == credential
    assert caught.value.audit_event_type == CREDENTIAL_UNAVAILABLE


@pytest.mark.pure
def test_a_blank_credential_counts_as_missing() -> None:
    store = store_with(**{ENV_RAZORPAY_KEY_ID: "   "})
    with pytest.raises(CredentialUnavailableError):
        store.razorpay_key_id()


@pytest.mark.pure
def test_resolved_credentials_are_secret_values() -> None:
    store = store_with(
        **{
            ENV_RAZORPAY_KEY_ID: "rzp_test_abc",
            ENV_RAZORPAY_KEY_SECRET: "super_secret_value",
            ENV_LLM_CREDENTIAL: "sk-llm-token",
        }
    )
    assert store.razorpay_key_id().reveal() == "rzp_test_abc"
    assert store.razorpay_key_secret().reveal() == "super_secret_value"
    assert store.llm_credential().reveal() == "sk-llm-token"


@pytest.mark.pure
def test_payload_keys_parse_multiple_versions() -> None:
    store = store_with(**{ENV_PAYLOAD_ENCRYPTION_KEYS: f"1:{KEY_32}, 2:{OTHER_KEY_32}"})
    keys = store.payload_encryption_keys()
    assert sorted(keys) == [1, 2]
    assert keys[1] == bytes(range(32))
    assert keys[2] == bytes(range(32, 64))


@pytest.mark.pure
@pytest.mark.parametrize(
    "value",
    [
        "not-a-versioned-entry",
        "x:" + KEY_32,
        "1:not-base64!!",
        "1:" + base64.b64encode(b"short").decode("ascii"),
    ],
)
def test_malformed_payload_keys_are_credential_unavailable(value: str) -> None:
    store = store_with(**{ENV_PAYLOAD_ENCRYPTION_KEYS: value})
    with pytest.raises(CredentialUnavailableError) as caught:
        store.payload_encryption_keys()
    assert caught.value.credential == "payload_encryption_keys"


@pytest.mark.pure
def test_customer_key_secret_decodes_to_bytes() -> None:
    store = store_with(**{ENV_CUSTOMER_KEY_SECRET: KEY_32})
    assert store.customer_key_secret() == bytes(range(32))


# ---------------------------------------------------------------------------
# No leaks
# ---------------------------------------------------------------------------

LEAKY = "super_secret_value"


@pytest.mark.pure
def test_secret_value_never_prints_itself() -> None:
    secret = SecretValue(LEAKY)
    assert LEAKY not in repr(secret)
    assert LEAKY not in str(secret)
    assert LEAKY not in f"{secret}"
    assert LEAKY not in f"{secret!r}"
    assert LEAKY not in f"{secret:>40}"
    # Held in variables so the literal-format lint rules do not rewrite the very
    # paths being tested: %-style is what stdlib logging uses.
    printf_template = "%s"
    brace_template = "{}"
    assert LEAKY not in printf_template % (secret,)
    assert LEAKY not in brace_template.format(secret)


@pytest.mark.pure
def test_store_repr_does_not_reach_any_secret() -> None:
    store = store_with(**{ENV_RAZORPAY_KEY_SECRET: LEAKY})
    assert LEAKY not in repr(store)


@pytest.mark.pure
def test_exception_text_never_carries_a_value() -> None:
    bad = base64.b64encode(b"short").decode("ascii")
    store = store_with(**{ENV_CUSTOMER_KEY_SECRET: bad})
    with pytest.raises(CredentialUnavailableError) as caught:
        store.customer_key_secret()
    rendered = "".join(
        traceback.format_exception(type(caught.value), caught.value, caught.value.__traceback__)
    )
    assert bad not in str(caught.value)
    assert bad not in rendered


@pytest.mark.pure
def test_secret_equality_is_by_value_not_identity() -> None:
    assert SecretValue(LEAKY) == SecretValue(LEAKY)
    assert SecretValue(LEAKY) != SecretValue("other_secret_value")
    assert SecretValue(LEAKY) != LEAKY
