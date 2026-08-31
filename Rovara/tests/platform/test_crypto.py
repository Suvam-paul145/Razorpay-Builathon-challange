"""Envelope encryption and the customer key."""

from __future__ import annotations

import pytest

from revora.platform.crypto import (
    KEY_LENGTH,
    NONCE_LENGTH,
    CustomerKeyHasher,
    PayloadCipher,
    PayloadDecryptionError,
    UnknownKeyVersionError,
    normalize_contact,
)

KEY_V1 = bytes(range(32))
KEY_V2 = bytes(range(32, 64))
KEY_V3 = bytes(range(64, 96))
HMAC_SECRET = b"k" * 32

RAW_PAYLOAD = (
    b'{"event":"payment.failed","payload":{"payment":{"entity":'
    b'{"id":"pay_ABC","contact":"+919876543210","email":"buyer@example.com"}}}}'
)


@pytest.fixture
def cipher() -> PayloadCipher:
    return PayloadCipher({1: KEY_V1, 2: KEY_V2})


@pytest.mark.pure
def test_round_trip(cipher: PayloadCipher) -> None:
    ciphertext, nonce, key_version = cipher.encrypt(RAW_PAYLOAD)
    assert cipher.decrypt(ciphertext, nonce, key_version) == RAW_PAYLOAD


@pytest.mark.pure
def test_ciphertext_does_not_contain_the_plaintext(cipher: PayloadCipher) -> None:
    ciphertext, _, _ = cipher.encrypt(RAW_PAYLOAD)
    assert b"+919876543210" not in ciphertext
    assert b"buyer@example.com" not in ciphertext


@pytest.mark.pure
def test_two_encryptions_of_the_same_plaintext_differ(cipher: PayloadCipher) -> None:
    first = cipher.encrypt(RAW_PAYLOAD)
    second = cipher.encrypt(RAW_PAYLOAD)
    assert first.nonce != second.nonce
    assert first.ciphertext != second.ciphertext
    assert len(first.nonce) == NONCE_LENGTH


@pytest.mark.pure
def test_nonces_are_unique_across_many_calls(cipher: PayloadCipher) -> None:
    nonces = {cipher.encrypt(RAW_PAYLOAD).nonce for _ in range(200)}
    assert len(nonces) == 200


@pytest.mark.pure
def test_writes_use_the_highest_key_version(cipher: PayloadCipher) -> None:
    assert cipher.current_version == 2
    assert cipher.encrypt(b"x").key_version == 2


@pytest.mark.pure
def test_a_rotated_in_key_still_reads_old_rows() -> None:
    before = PayloadCipher({1: KEY_V1})
    stored = before.encrypt(RAW_PAYLOAD)
    assert stored.key_version == 1

    after_rotation = PayloadCipher({1: KEY_V1, 2: KEY_V2, 3: KEY_V3})
    assert after_rotation.current_version == 3
    assert after_rotation.decrypt(*stored) == RAW_PAYLOAD


@pytest.mark.pure
def test_dropping_a_key_version_is_distinguishable_from_tampering() -> None:
    stored = PayloadCipher({1: KEY_V1}).encrypt(RAW_PAYLOAD)
    only_v2 = PayloadCipher({2: KEY_V2})
    with pytest.raises(UnknownKeyVersionError):
        only_v2.decrypt(stored.ciphertext, stored.nonce, stored.key_version)


@pytest.mark.pure
def test_tampered_ciphertext_fails_authentication(cipher: PayloadCipher) -> None:
    ciphertext, nonce, version = cipher.encrypt(RAW_PAYLOAD)
    tampered = bytes([ciphertext[0] ^ 0x01]) + ciphertext[1:]
    with pytest.raises(PayloadDecryptionError):
        cipher.decrypt(tampered, nonce, version)


@pytest.mark.pure
def test_wrong_key_for_a_declared_version_fails_authentication() -> None:
    stored = PayloadCipher({1: KEY_V1}).encrypt(RAW_PAYLOAD)
    impostor = PayloadCipher({1: KEY_V2})
    with pytest.raises(PayloadDecryptionError):
        impostor.decrypt(stored.ciphertext, stored.nonce, stored.key_version)


@pytest.mark.pure
def test_short_key_is_refused() -> None:
    with pytest.raises(ValueError, match="AES-256"):
        PayloadCipher({1: b"too-short"})


@pytest.mark.pure
def test_empty_key_set_is_refused() -> None:
    with pytest.raises(ValueError, match="at least one"):
        PayloadCipher({})


@pytest.mark.pure
def test_cipher_repr_hides_key_material(cipher: PayloadCipher) -> None:
    text = repr(cipher)
    assert "versions=(1, 2)" in text
    for key in (KEY_V1, KEY_V2):
        assert repr(key) not in text
        assert key.hex() not in text


# ---------------------------------------------------------------------------
# customer_key
# ---------------------------------------------------------------------------


@pytest.fixture
def hasher() -> CustomerKeyHasher:
    return CustomerKeyHasher(HMAC_SECRET)


@pytest.mark.pure
@pytest.mark.parametrize(
    "variant",
    [
        "+919876543210",
        "919876543210",
        "09876543210",
        "9876543210",
        "98765 43210",
        "+91 98765-43210",
        "  +91 (98765) 43210  ",
        "0091 9876543210",
    ],
)
def test_customer_key_is_stable_across_phone_formats(
    hasher: CustomerKeyHasher, variant: str
) -> None:
    assert hasher.key(variant) == hasher.key("+919876543210")


@pytest.mark.pure
def test_customer_key_differs_for_different_contacts(hasher: CustomerKeyHasher) -> None:
    assert hasher.key("+919876543210") != hasher.key("+919876543211")


@pytest.mark.pure
def test_customer_key_is_stable_across_email_case(hasher: CustomerKeyHasher) -> None:
    assert hasher.key("Buyer@Example.COM") == hasher.key("buyer@example.com")
    assert hasher.key(" buyer@example.com ") == hasher.key("buyer@example.com")


@pytest.mark.pure
def test_customer_key_does_not_contain_the_contact(hasher: CustomerKeyHasher) -> None:
    key = hasher.key("+919876543210")
    assert "9876543210" not in key
    assert len(key) == 64


@pytest.mark.pure
def test_customer_key_depends_on_the_secret() -> None:
    other = CustomerKeyHasher(b"j" * 32)
    assert other.key("+919876543210") != CustomerKeyHasher(HMAC_SECRET).key("+919876543210")


@pytest.mark.pure
def test_hasher_refuses_a_weak_secret() -> None:
    with pytest.raises(ValueError, match=str(KEY_LENGTH)):
        CustomerKeyHasher(b"short")


@pytest.mark.pure
def test_hasher_repr_hides_the_secret(hasher: CustomerKeyHasher) -> None:
    assert repr(hasher) == "CustomerKeyHasher(<secret redacted>)"
    assert "kkk" not in repr(hasher)


@pytest.mark.pure
def test_normalize_contact_keeps_the_subscriber_digits() -> None:
    assert normalize_contact("+91 98765-43210") == "9876543210"
    assert normalize_contact("12345") == "12345"


@pytest.mark.pure
def test_normalize_contact_falls_back_for_a_digitless_identifier() -> None:
    assert normalize_contact("  Some.Handle  ") == "some.handle"


@pytest.mark.pure
def test_normalize_contact_refuses_empty() -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        normalize_contact("   ")
