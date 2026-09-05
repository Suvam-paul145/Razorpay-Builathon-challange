"""Masking serializer: sensitive kinds never survive, short values are not revealed."""

from __future__ import annotations

import dataclasses

import pytest

from revora.domain.enums import SENSITIVE_FIELD_KINDS, FieldKind
from revora.platform.masking import (
    MASK_CHARACTER,
    MASK_DISCLOSURE_LENGTH,
    field_kind_for,
    mask_record,
    mask_value,
    sensitive,
)

CONTACT = "+919876543210"
SHORT_URL = "https://rzp.io/i/aBcD1234"


@pytest.mark.pure
@pytest.mark.parametrize("kind", sorted(SENSITIVE_FIELD_KINDS))
def test_every_sensitive_kind_hides_the_original(kind: FieldKind) -> None:
    masked = mask_value(CONTACT, kind)
    assert isinstance(masked, str)
    assert CONTACT not in masked
    assert len(masked) == len(CONTACT)


@pytest.mark.pure
def test_contact_reveals_at_most_the_disclosure_length() -> None:
    masked = mask_value(CONTACT, FieldKind.CONTACT)
    assert masked == MASK_CHARACTER * (len(CONTACT) - MASK_DISCLOSURE_LENGTH) + "3210"
    revealed = sum(1 for char in str(masked) if char != MASK_CHARACTER)
    assert revealed <= MASK_DISCLOSURE_LENGTH


@pytest.mark.pure
def test_non_sensitive_kind_is_untouched() -> None:
    assert mask_value("case-123", FieldKind.NON_SENSITIVE) == "case-123"


@pytest.mark.pure
def test_provider_short_url_reveals_nothing() -> None:
    masked = mask_value(SHORT_URL, FieldKind.PROVIDER_SHORT_URL)
    assert masked == MASK_CHARACTER * len(SHORT_URL)
    assert "aBcD1234" not in str(masked)
    assert "rzp.io" not in str(masked)


@pytest.mark.pure
@pytest.mark.parametrize("value", ["1", "12", "123", "1234"])
def test_a_value_no_longer_than_the_window_is_fully_masked(value: str) -> None:
    masked = mask_value(value, FieldKind.CONTACT)
    assert masked == MASK_CHARACTER * len(value)
    assert value not in str(masked)


@pytest.mark.pure
def test_five_character_value_reveals_only_its_tail() -> None:
    masked = mask_value("12345", FieldKind.CONTACT)
    assert masked == MASK_CHARACTER + "2345"


@pytest.mark.pure
def test_none_and_empty_are_safe() -> None:
    assert mask_value(None, FieldKind.CONTACT) is None
    assert mask_value("", FieldKind.CONTACT) == ""


@pytest.mark.pure
def test_non_string_sensitive_value_is_still_masked() -> None:
    masked = mask_value(4111111111111111, FieldKind.INSTRUMENT)
    assert "4111111111111111" not in str(masked)
    assert str(masked).endswith("1111")


@pytest.mark.pure
def test_negative_disclosure_length_is_refused() -> None:
    with pytest.raises(ValueError, match="disclosure_length"):
        mask_value(CONTACT, FieldKind.CONTACT, disclosure_length=-1)


@pytest.mark.pure
def test_field_kind_lookup_is_case_insensitive_and_registry_backed() -> None:
    assert field_kind_for("Contact") is FieldKind.CONTACT
    assert field_kind_for("provider_short_url") is FieldKind.PROVIDER_SHORT_URL
    assert field_kind_for("case_id") is FieldKind.NON_SENSITIVE


@pytest.mark.pure
def test_already_masked_columns_are_left_alone() -> None:
    record = {"customer_contact_masked": "XXXXXXXXX3210"}
    assert mask_record(record) == record


@pytest.mark.pure
def test_explicit_field_kinds_override_the_registry() -> None:
    masked = mask_record({"weird_column": CONTACT}, field_kinds={"weird_column": FieldKind.CONTACT})
    assert CONTACT not in masked["weird_column"]


@pytest.mark.pure
def test_nested_structure_is_walked() -> None:
    record = {
        "event": "payment.failed",
        "payload": {
            "payment": {
                "entity": {
                    "id": "pay_ABC123",
                    "contact": CONTACT,
                    "email": "buyer@example.com",
                    "card": {"last4": "4321"},
                }
            }
        },
        "links": [{"short_url": SHORT_URL}],
        "contacts": ["+919876543210", "+918888777766"],
    }

    masked = mask_record(record)
    flattened = repr(masked)

    assert CONTACT not in flattened
    assert "buyer@example.com" not in flattened
    assert SHORT_URL not in flattened
    assert "+918888777766" not in flattened
    # Non-sensitive fields survive, or the record stops being useful.
    assert masked["event"] == "payment.failed"
    assert masked["payload"]["payment"]["entity"]["id"] == "pay_ABC123"


@dataclasses.dataclass(frozen=True)
class LinkRecord:
    case_id: str
    contact: str = dataclasses.field(metadata=sensitive(FieldKind.CONTACT))
    url: str = dataclasses.field(metadata=sensitive(FieldKind.PROVIDER_SHORT_URL))
    attempt: int = 1


@pytest.mark.pure
def test_dataclass_metadata_declares_the_kind() -> None:
    masked = mask_record(LinkRecord(case_id="case-1", contact=CONTACT, url=SHORT_URL))
    assert masked["case_id"] == "case-1"
    assert masked["attempt"] == 1
    assert CONTACT not in masked["contact"]
    assert masked["url"] == MASK_CHARACTER * len(SHORT_URL)


@pytest.mark.pure
def test_dataclass_nested_in_a_mapping_is_walked() -> None:
    masked = mask_record({"intent": LinkRecord(case_id="case-1", contact=CONTACT, url=SHORT_URL)})
    assert CONTACT not in repr(masked)
