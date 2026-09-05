"""The derived key, and the four request settings that carry correctness weight.

``reference_id_for`` is the single source of truth for the execution key: the same
string is the provider ``reference_id``, the persisted ``Idempotency_Key``, and the
reconciliation query parameter. If it ever changed between two executions of the same
attempt, the second execution would ask the provider about an object that could not
exist, conclude nothing was created, and create a second payment link. So the format is
pinned here against a literal value rather than only against itself — a golden value is
the only assertion that fails when somebody "harmlessly" changes the separator or the
digest length.

The other half of this file is the request settings. ``accept_partial`` and
``reminder_enable`` are asserted explicitly and by name, because both are single
booleans that look like defaults and are not: one keeps a partial payment from being
mistaken for recovery, and the other is the difference between Property 9 holding and
Property 9 being silently false.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from hypothesis import given
from hypothesis import strategies as st

from revora.domain.actions import CandidateAction
from revora.domain.money import Minor
from revora.platform.clock import NaiveDatetimeError
from revora.providers.payment_link import (
    KEY_HEX_LENGTH,
    MAX_REFERENCE_ID_LENGTH,
    NOTES_CASE_ID_FIELD,
    NOTES_KEY_FIELD,
    PROVIDER_EXPIRY_CEILING,
    REFERENCE_ID_PREFIX,
    CustomerContact,
    PaymentLinkRequestError,
    build_payment_link_request,
    clamp_expire_by,
    reference_id_for,
    validate_description,
)

pytestmark = pytest.mark.pure

_MAX_MESSAGE_LENGTH = 300
_CASE_ID = "8f14e45f-ea0d-4c2b-9f7e-000000000001"
_NOW = datetime(2025, 1, 1, tzinfo=UTC)
_CONTACT = CustomerContact(contact="+919876500000", email="buyer@example.com")


def _build(**overrides: object):
    """A valid request, with any field overridden. Keeps each test to its own subject."""
    kwargs: dict[str, object] = {
        "case_id": _CASE_ID,
        "action": CandidateAction.PAYMENT_LINK,
        "attempt_ordinal": 1,
        "amount": Minor(200_000),
        "currency": "INR",
        "description": "Complete your payment for order 1042",
        "customer": _CONTACT,
        "window_end": _NOW + timedelta(hours=168),
        "now": _NOW,
        "max_message_length": _MAX_MESSAGE_LENGTH,
    }
    kwargs.update(overrides)
    return build_payment_link_request(**kwargs)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# reference_id_for
# ---------------------------------------------------------------------------


def test_reference_id_format_is_pinned_to_a_golden_value() -> None:
    """Stability across processes, machines and releases, asserted the only way that
    actually catches a format change: against a value computed once and written down.

    A test that recomputed the hash would pass after somebody changed the separator, and
    every intent written before that change would then be unreconcilable."""
    key = reference_id_for(_CASE_ID, CandidateAction.PAYMENT_LINK, 1)

    # "rv_" + first 16 hex of SHA-256("<case_id>\x1fPAYMENT_LINK\x1f1"), verified
    # independently of the implementation.
    assert key == "rv_096573c0465be516"


def test_reference_id_is_deterministic() -> None:
    first = reference_id_for(_CASE_ID, CandidateAction.PAYMENT_LINK, 3)
    second = reference_id_for(_CASE_ID, CandidateAction.PAYMENT_LINK, 3)

    assert first == second


def test_reference_id_fits_the_verified_forty_character_limit() -> None:
    key = reference_id_for(_CASE_ID, CandidateAction.PAYMENT_LINK, 1)

    assert len(key) == len(REFERENCE_ID_PREFIX) + KEY_HEX_LENGTH == 19
    assert len(key) <= MAX_REFERENCE_ID_LENGTH
    assert key.startswith(REFERENCE_ID_PREFIX)


def test_reference_id_accepts_a_uuid_and_its_string_form_identically() -> None:
    """The engine holds a ``UUID``; the notes field holds a string. Both must key the
    same provider object, or reconciliation would query the wrong one."""
    import uuid

    case_uuid = uuid.UUID(_CASE_ID)

    assert reference_id_for(case_uuid, CandidateAction.PAYMENT_LINK, 1) == reference_id_for(
        _CASE_ID, CandidateAction.PAYMENT_LINK, 1
    )


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ((_CASE_ID, CandidateAction.PAYMENT_LINK, 1), (_CASE_ID, CandidateAction.PAYMENT_LINK, 2)),
        (
            (_CASE_ID, CandidateAction.PAYMENT_LINK, 1),
            (_CASE_ID, CandidateAction.CUSTOMER_MESSAGE, 1),
        ),
        (
            (_CASE_ID, CandidateAction.PAYMENT_LINK, 1),
            ("8f14e45f-ea0d-4c2b-9f7e-000000000002", CandidateAction.PAYMENT_LINK, 1),
        ),
    ],
)
def test_reference_id_differs_when_any_input_differs(
    left: tuple[str, CandidateAction, int], right: tuple[str, CandidateAction, int]
) -> None:
    assert reference_id_for(*left) != reference_id_for(*right)


@given(
    case_ids=st.lists(st.uuids().map(str), min_size=2, max_size=6, unique=True),
    actions=st.lists(st.sampled_from(CandidateAction), min_size=1, max_size=3, unique=True),
    ordinals=st.lists(st.integers(min_value=1, max_value=9), min_size=1, max_size=3, unique=True),
)
def test_distinct_triples_produce_distinct_keys(
    case_ids: list[str], actions: list[CandidateAction], ordinals: list[int]
) -> None:
    """Injectivity over the cross product.

    The separator exists for this: bare concatenation would collide case ``…1`` at
    ordinal 12 with case ``…11`` at ordinal 2, and a collision means two attempts
    sharing one provider object."""
    triples = [
        (case_id, action, ordinal)
        for case_id in case_ids
        for action in actions
        for ordinal in ordinals
    ]
    keys = {reference_id_for(*triple) for triple in triples}

    assert len(keys) == len(triples)


@pytest.mark.parametrize("ordinal", [0, -1, -100])
def test_reference_id_refuses_a_non_positive_ordinal(ordinal: int) -> None:
    with pytest.raises(PaymentLinkRequestError) as caught:
        reference_id_for(_CASE_ID, CandidateAction.PAYMENT_LINK, ordinal)

    assert caught.value.rule == "attempt_ordinal_out_of_range"


def test_reference_id_refuses_an_empty_case_id() -> None:
    with pytest.raises(PaymentLinkRequestError) as caught:
        reference_id_for("   ", CandidateAction.PAYMENT_LINK, 1)

    assert caught.value.rule == "case_id_empty"


# ---------------------------------------------------------------------------
# description
# ---------------------------------------------------------------------------


def test_description_at_the_limit_is_accepted() -> None:
    text = "a" * _MAX_MESSAGE_LENGTH

    assert validate_description(text, max_length=_MAX_MESSAGE_LENGTH) == text


def test_description_over_the_limit_is_rejected_not_truncated() -> None:
    """Rejection, deliberately, and not the convenient choice.

    The description is the entire customer-visible message — the provider sends the
    notification, so there is no other copy of the text. Truncating an over-long draft
    would send a sentence that stops mid-word, and it would turn the length bound from a
    validation gate into a formatting step, so nothing upstream would ever be fixed."""
    with pytest.raises(PaymentLinkRequestError) as caught:
        validate_description("a" * (_MAX_MESSAGE_LENGTH + 1), max_length=_MAX_MESSAGE_LENGTH)

    assert caught.value.rule == "description_too_long"


def test_build_rejects_an_over_long_description() -> None:
    with pytest.raises(PaymentLinkRequestError) as caught:
        _build(description="a" * 301)

    assert caught.value.rule == "description_too_long"


@pytest.mark.parametrize("text", ["", "   ", "\t\n"])
def test_blank_description_is_rejected(text: str) -> None:
    with pytest.raises(PaymentLinkRequestError):
        validate_description(text, max_length=_MAX_MESSAGE_LENGTH)


def test_control_characters_in_a_description_are_rejected() -> None:
    """This string is rendered to a customer by a third party. A newline or an escape
    sequence in it is not something Revora intended to send."""
    with pytest.raises(PaymentLinkRequestError) as caught:
        validate_description("Pay now\x07please", max_length=_MAX_MESSAGE_LENGTH)

    assert caught.value.rule == "description_control_characters"


def test_description_is_stripped() -> None:
    assert validate_description("  pay now  ", max_length=_MAX_MESSAGE_LENGTH) == "pay now"


# ---------------------------------------------------------------------------
# expire_by
# ---------------------------------------------------------------------------


def test_expire_by_clamps_to_the_window_end_when_the_window_is_nearer() -> None:
    """The recovery window default is 168 hours, far inside the six-month ceiling, so
    this is the ordinary case. A link outliving the window would let a customer pay
    through a case that had already expired."""
    window_end = _NOW + timedelta(hours=168)

    expiry = clamp_expire_by(window_end=window_end, now=_NOW)

    assert expiry == int(window_end.timestamp())


def test_expire_by_clamps_to_six_months_when_the_window_is_further() -> None:
    """The provider's ceiling wins when it is the nearer bound. A merchant configuring a
    year-long recovery window must not produce a creation call the provider rejects."""
    window_end = _NOW + timedelta(days=365)

    expiry = clamp_expire_by(window_end=window_end, now=_NOW)

    assert expiry == int((_NOW + PROVIDER_EXPIRY_CEILING).timestamp())
    assert expiry < int(window_end.timestamp())


def test_the_expiry_ceiling_is_inside_every_six_calendar_month_span() -> None:
    """180 days, not 183: the shortest six-calendar-month span is 181 days (September to
    March in a non-leap year), so this can never ask for an expiry the provider rejects."""
    shortest_six_months = datetime(2025, 3, 1, tzinfo=UTC) - datetime(2024, 9, 1, tzinfo=UTC)

    assert shortest_six_months > PROVIDER_EXPIRY_CEILING


def test_expire_by_refuses_a_window_that_has_already_closed() -> None:
    with pytest.raises(PaymentLinkRequestError) as caught:
        clamp_expire_by(window_end=_NOW - timedelta(seconds=1), now=_NOW)

    assert caught.value.rule == "expiry_not_in_future"


def test_expire_by_refuses_a_naive_datetime() -> None:
    """An expiry is a comparison against provider-side time; being an hour wrong about it
    is not a rounding error."""
    with pytest.raises(NaiveDatetimeError):
        clamp_expire_by(window_end=datetime(2025, 1, 8), now=_NOW)


# ---------------------------------------------------------------------------
# the assembled request
# ---------------------------------------------------------------------------


def test_accept_partial_is_false() -> None:
    """A partial payment must not be mistakable for recovery. Revora has no notion of
    partial recovery, so it does not accept one."""
    assert _build().accept_partial is False
    assert _build().to_payload()["accept_partial"] is False


def test_reminder_enable_is_false() -> None:
    """**Property 9.** Provider-sent reminders are customer-visible messages, and
    ``MAX_CUSTOMER_MESSAGES`` does not count them because Revora never sent them.
    Enabling reminders would therefore break the message cap silently: the counters
    would stay inside their bounds while the customer received messages the system never
    authorized, and no test anywhere else would fail. This assertion is the defence."""
    assert _build().reminder_enable is False
    assert _build().to_payload()["reminder_enable"] is False


def test_payload_carries_exactly_the_verified_field_set() -> None:
    payload = _build().to_payload()

    assert set(payload) == {
        "amount",
        "currency",
        "description",
        "reference_id",
        "customer",
        "notify",
        "reminder_enable",
        "expire_by",
        "accept_partial",
        "notes",
    }


def test_notes_carry_the_case_id_and_the_key() -> None:
    """A second, provider-side path from a link back to a case — the only correlation
    route available to somebody looking at the provider dashboard."""
    payload = _build().to_payload()
    notes = payload["notes"]

    assert notes == {
        NOTES_CASE_ID_FIELD: _CASE_ID,
        NOTES_KEY_FIELD: reference_id_for(_CASE_ID, CandidateAction.PAYMENT_LINK, 1),
    }


def test_amount_stays_an_integer_count_of_minor_units() -> None:
    payload = _build(amount=Minor(2_000_000)).to_payload()

    assert payload["amount"] == 2_000_000
    assert isinstance(payload["amount"], int)
    assert not isinstance(payload["amount"], bool)


def test_notify_is_enabled_on_both_channels_when_both_are_available() -> None:
    """The provider sends the notification itself, which is why there is no separate
    communication vendor at all."""
    assert _build().to_payload()["notify"] == {"sms": True, "email": True}


def test_notify_email_is_false_when_no_email_was_supplied() -> None:
    """The provider cannot notify an address it was not given, so the flag follows the
    data rather than being asserted true against an absent field."""
    request = _build(customer=CustomerContact(contact="+919876500000"))
    payload = request.to_payload()

    assert payload["notify"] == {"sms": True, "email": False}
    assert payload["customer"] == {"contact": "+919876500000"}


def test_currency_is_normalized() -> None:
    assert _build(currency=" inr ").currency == "INR"


@pytest.mark.parametrize("amount", [0, -1])
def test_non_positive_amount_is_refused(amount: int) -> None:
    with pytest.raises(PaymentLinkRequestError) as caught:
        _build(amount=Minor(amount))

    assert caught.value.rule == "amount_not_positive"


def test_blank_contact_is_refused() -> None:
    with pytest.raises(PaymentLinkRequestError) as caught:
        CustomerContact(contact="  ")

    assert caught.value.rule == "contact_blank"


def test_repr_does_not_disclose_customer_contact_or_message_text() -> None:
    """The leak that actually happens is a traceback frame or a debugger, neither of
    which consults a masking registry."""
    request = _build()

    assert "+919876500000" not in repr(request)
    assert "buyer@example.com" not in repr(request)
    assert "order 1042" not in repr(request)
    assert "+919876500000" not in repr(request.customer)
    assert "buyer@example.com" not in repr(request.customer)
    assert request.reference_id in repr(request)


def test_request_reference_id_matches_the_shared_construction() -> None:
    """The builder must not mint its own key. One construction, or exactly-once is a
    coincidence rather than a property."""
    request = _build(attempt_ordinal=2)

    assert request.reference_id == reference_id_for(_CASE_ID, CandidateAction.PAYMENT_LINK, 2)
