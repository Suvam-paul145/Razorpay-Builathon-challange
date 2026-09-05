"""The payment-link request, and the derived key that makes exactly-once possible.

Two things live here. One is a builder for the verified creation field set. The other
is :func:`reference_id_for`, which is the **single source of truth for the execution
key** — the same string is the ``Idempotency_Key`` on the intent row, the
``reference_id`` on the provider object, and the query parameter reconciliation uses
to ask "does this effect already exist?". If those three ever disagree, exactly-once
stops holding and nothing else in the system notices.

A third thing joined them with the promise follow-up: :func:`resend_response_id`, the
composed token that stands in for a provider identifier a resend does not return. It lives
beside the reference-id derivation because both answer the same question — *what string
identifies this attempt?* — and because the two must never be confusable. One is a real
provider ``reference_id``; the other is deliberately not a provider identifier at all.

**The key format**, from the design's Outbound Contract:

    ``rv_`` + the first 16 hex characters of ``SHA-256(case_id ‖ action ‖ ordinal)``

19 characters, comfortably inside the verified 40-character ``reference_id`` limit,
deterministic across processes and machines because SHA-256 is, and derived rather
than random because a random key recomputed after a crash would be a different key
and the reconciliation read would answer about the wrong object. The separator is
``\\x1f`` (ASCII unit separator), a byte that cannot occur in a UUID, in a
``CandidateAction`` member name, or in a decimal integer, so the encoding is
injective: no two distinct triples can hash the same input. Bare concatenation would
not have that property — case ``…1`` at ordinal 12 and case ``…11`` at ordinal 2 would
produce the same bytes.

**Four settings on the request carry correctness weight**, and three of them are
switches somebody could flip later without realizing what they cost:

* ``reminder_enable: false`` — **this is the one to read twice.** Provider-sent
  reminders are customer-visible messages, and Revora's ``MAX_CUSTOMER_MESSAGES``
  bound does not count them because Revora never sent them. Turning reminders on
  therefore breaks **Property 9** (the message cap) silently: the counters would still
  be inside their bounds while the customer received messages the system never
  authorized. Nothing in the codebase would fail. The only defence is that this stays
  false and that this paragraph explains why.

  **The promise follow-up made this more load-bearing, not less.** Before it, every
  provider-delivered message was one Revora never asked for. Now Revora deliberately
  triggers provider-delivered messages of its own, through ``notify_by``, and counts each
  one against ``MAX_CUSTOMER_MESSAGES``. Both kinds travel the same channel and look
  identical to the customer; only one is counted. Enabling reminders would put uncounted
  provider messages alongside counted Revora ones **on the same link** — Property 9 would
  still pass, the counters would still be inside their bounds, and a customer would be
  receiving messages nothing authorized. No test in the codebase would fail, which is why
  this stays a paragraph rather than an assertion: there is nothing to assert against.
* ``accept_partial: false`` — a partial payment must not be mistakable for recovery.
  ``payment_link.partially_paid`` exists precisely because partial payment is
  possible, and Revora has no notion of partial recovery, so it does not accept one.
* ``expire_by`` clamped to ``min(window_end, now + six months)`` — the six months is
  the provider's documented ceiling; the window end is Revora's. A link outliving the
  recovery window would let a customer pay through it after policy stopped permitting
  the case to act, which would produce a payment nothing in the system authorized.
* ``notify`` — the provider sends the notification itself, which is why there is no
  separate communication vendor at all. ``description`` is therefore the *entire*
  customer-visible message, which is why it is validated rather than truncated.

**Customer contact never touches this module's storage or logs.** The caller decrypts
it just in time and passes it in; the request object holds it transiently, declares it
sensitive so the platform masker recognizes it if it ever reaches a log field, and
redacts it in ``repr`` so a traceback cannot print it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum, unique
from typing import Final

from revora.domain.actions import CandidateAction
from revora.domain.enums import FieldKind
from revora.domain.keys import (
    KEY_HEX_LENGTH,
    KEY_SEPARATOR,
    MAX_REFERENCE_ID_LENGTH,
    REFERENCE_ID_PREFIX,
    ExecutionKeyError,
    execution_key,
)
from revora.domain.money import Minor
from revora.platform.clock import ensure_utc
from revora.platform.masking import sensitive
from revora.providers.classification import PAYMENT_LINK_ID_PREFIX

__all__ = [
    "KEY_HEX_LENGTH",
    "KEY_SEPARATOR",
    "MAX_REFERENCE_ID_LENGTH",
    "NOTES_CASE_ID_FIELD",
    "NOTES_KEY_FIELD",
    "PROVIDER_EXPIRY_CEILING",
    "REFERENCE_ID_PREFIX",
    "RESEND_RESPONSE_ID_MARKER",
    "RESEND_RESPONSE_ID_SEPARATOR",
    "CustomerContact",
    "NotifyMedium",
    "PaymentLinkRequest",
    "PaymentLinkRequestError",
    "build_payment_link_request",
    "clamp_expire_by",
    "is_resend_response_id",
    "reference_id_for",
    "resend_response_id",
    "validate_description",
]

PROVIDER_EXPIRY_CEILING: Final[timedelta] = timedelta(days=180)
"""The six-month creation-to-expiry ceiling, expressed conservatively.

**[ASSUMPTION]** in the conversion only: the ceiling itself is verified, "six months"
is not a fixed number of days. 180 days is inside every possible six-calendar-month
span — the shortest is 181 days (September to March in a non-leap year) — so this can
never ask for an expiry the provider would reject. Erring the other way would mean a
creation call failing on a date-dependent boundary, which is the worst kind of bug to
find in production."""

NOTES_CASE_ID_FIELD: Final[str] = "revora_case_id"
NOTES_KEY_FIELD: Final[str] = "revora_key"
"""Verified-by-design ``notes`` keys. They give a second, provider-side path from a
link back to a case during manual investigation, which is the only correlation route
available to somebody looking at the provider dashboard rather than at Revora."""

_CONTROL_CHARACTERS: Final[frozenset[str]] = frozenset(chr(code) for code in range(0x20)) | {
    "\x7f"
}


class PaymentLinkRequestError(ValueError):
    """The request could not be built from the values supplied.

    Raised, not classified — and that is a deliberate difference from the client, where
    nothing raises. A malformed request is a Revora-side defect (a description that
    failed validation, a window that already closed, an ordinal of zero), discovered
    *before* any call and therefore before any uncertainty exists. The execution engine
    builds the request while it still holds the lock and before it commits the intent,
    so a raise here costs a rolled-back transaction and nothing else. Turning it into a
    classification would make a programming error look like a provider outcome.

    ``rule`` names which validation failed, so the audit record can say so without the
    caller re-deriving it from a message.
    """

    def __init__(self, rule: str, detail: str = "") -> None:
        self.rule = rule
        super().__init__(f"{rule}: {detail}" if detail else rule)


def reference_id_for(case_id: object, action: CandidateAction, attempt_ordinal: int) -> str:
    """The provider ``reference_id`` for one ``(case, action, attempt)`` triple.

    A typed wrapper over :func:`revora.domain.keys.execution_key`, which holds the
    actual construction. The construction lives in ``domain`` because two packages need
    it — ``revora.policy`` mints the key when it approves an action, ``revora.providers``
    puts it on the wire — and the ``policy-isolation`` import contract forbids ``policy``
    from importing ``providers``. Defining it in either package would therefore force a
    second copy in the other, which is exactly the duplication that would let the
    ``Idempotency_Key`` and the ``reference_id`` drift apart and turn exactly-once into a
    coincidence.

    What this wrapper adds is the ``CandidateAction`` type: ``execution_key`` takes the
    action as a plain string so that ``domain.keys`` needs no import even from
    ``domain.actions``, and this is the one place that conversion happens.

    Raises:
        PaymentLinkRequestError: on any invalid input, translated from
            ``ExecutionKeyError`` so that every failure a caller can hit while building
            a request is one exception type carrying one ``rule`` attribute.
    """
    try:
        return execution_key(case_id, action.value, attempt_ordinal)
    except ExecutionKeyError as exc:
        raise PaymentLinkRequestError(exc.rule, str(exc)) from exc


RESEND_RESPONSE_ID_SEPARATOR: Final[str] = "#"
"""Separates the link id from the notification marker in a composed resend token.

Chosen because **no Razorpay identifier contains it**. That is the whole requirement: the
composed token has to be unmistakable for a provider id, so that nothing can later hand it
to a fetch endpoint believing it is one. A character the provider's own id alphabet uses
would have made the token merely unlikely to be misread."""

RESEND_RESPONSE_ID_MARKER: Final[str] = "notify_by:"
"""Names the operation inside the composed token, matching the endpoint path segment.

Present so a person reading ``provider_response_id`` on an intent row can tell what the
row records without consulting ``effect_kind`` — the two agree, and a reader who has only
one of them is not stuck."""


@unique
class NotifyMedium(StrEnum):
    """The channel a resend goes out on. The verified ``:medium`` path segment.

    ``POST /v1/payment_links/:id/notify_by/:medium`` documents exactly ``sms`` and
    ``email``, and the values here are the wire values rather than a Revora vocabulary
    mapped onto them, because there is nothing to map: this enumeration exists to make the
    path segment un-typo-able, not to abstract it.
    """

    SMS = "sms"
    EMAIL = "email"


def resend_response_id(payment_link_id: str, medium: NotifyMedium) -> str:
    """The identifier persisted for a resend, composed by Revora because the provider sends none.

    ``"<plink_id>#notify_by:<medium>"``. A resend response carries a success boolean and
    nothing else — no notification id, and no endpoint that reports whether a notification
    was sent — so ``execution_intent.provider_response_id`` cannot hold a provider value for
    a resend. Leaving it null was the alternative and it is worse: the column is what a
    reader looks at to find out what an intent did, and a null there is indistinguishable
    from an intent that never got a result.

    **The composed form is deliberately not a valid Razorpay identifier**, and that is the
    property doing the work rather than a naming preference. A token that merely *looked*
    unusual could still be passed to a fetch endpoint by a future reader who assumed the
    column always holds a provider id; this one cannot be, because
    :data:`RESEND_RESPONSE_ID_SEPARATOR` never occurs in a provider identifier. The same
    property is checked in the other direction by :func:`is_resend_response_id`, which the
    client uses to refuse a composed token handed back to it as a link id.

    The link id is validated on the way in for the same reason a description is: the failure
    belongs where the defect is. A blank id, an id that is not a payment link id, or a token
    that has already been composed once are all Revora-side mistakes discoverable before any
    call, so they raise here rather than becoming a provider outcome later.

    Raises:
        PaymentLinkRequestError: if ``payment_link_id`` is blank, already composed, or not a
            payment link id.
    """
    identifier = payment_link_id.strip()
    if not identifier:
        raise PaymentLinkRequestError("payment_link_id_blank", "payment link id is empty")
    if RESEND_RESPONSE_ID_SEPARATOR in identifier:
        raise PaymentLinkRequestError(
            "payment_link_id_composed",
            f"already carries {RESEND_RESPONSE_ID_SEPARATOR!r}; a composed token is not a link id",
        )
    if not identifier.startswith(PAYMENT_LINK_ID_PREFIX):
        raise PaymentLinkRequestError(
            "payment_link_id_not_a_link_id",
            f"expected an id beginning {PAYMENT_LINK_ID_PREFIX!r}",
        )
    return f"{identifier}{RESEND_RESPONSE_ID_SEPARATOR}{RESEND_RESPONSE_ID_MARKER}{medium.value}"


def is_resend_response_id(value: str) -> bool:
    """True if ``value`` is a Revora-composed resend token rather than a provider identifier.

    The guard that makes the composed form's un-fetchability enforced rather than merely
    intended. Read by the client before it builds a resend URL, so a stored
    ``provider_response_id`` fed back in is refused locally — nothing sent, nothing
    uncertain — instead of being pasted into a path the provider would 404.
    """
    return f"{RESEND_RESPONSE_ID_SEPARATOR}{RESEND_RESPONSE_ID_MARKER}" in value


def validate_description(description: str, *, max_length: int) -> str:
    """The customer-visible description, validated. Rejects rather than truncates.

    **Rejection is the decision, and it is not the convenient one.** The description is
    the whole customer-visible message — the provider sends the notification, so there
    is no other copy of the text — and it is LLM-drafted, which R4.C11 requires be
    validated before sending. Truncating an over-long draft would send a customer a
    sentence that stops mid-word, and worse, it would turn the length bound from a
    validation gate into a formatting step: nothing would ever fail, so nothing would
    ever be fixed upstream. Rejecting puts the failure where the defect is, in the
    drafting layer, and the deterministic fallback description is always available to
    the caller.

    Control characters are refused for the same reason: this string is rendered to a
    customer by a third party, and a newline or an escape sequence in it is not
    something Revora intended to send.

    Raises:
        PaymentLinkRequestError: if the description is blank, longer than
            ``max_length``, or contains control characters.
    """
    if max_length < 1:
        raise PaymentLinkRequestError(
            "max_length_invalid", f"expected at least 1, got {max_length}"
        )
    text = description.strip()
    if not text:
        raise PaymentLinkRequestError("description_blank", "description is empty")
    if len(text) > max_length:
        raise PaymentLinkRequestError(
            "description_too_long", f"{len(text)} characters, limit is {max_length}"
        )
    offending = sorted(_CONTROL_CHARACTERS & set(text))
    if offending:
        raise PaymentLinkRequestError(
            "description_control_characters",
            f"{len(offending)} disallowed control character(s)",
        )
    return text


def clamp_expire_by(
    *,
    window_end: datetime,
    now: datetime,
    ceiling: timedelta = PROVIDER_EXPIRY_CEILING,
) -> int:
    """``min(window_end, now + ceiling)`` as a Unix timestamp.

    Two bounds, both real, and the nearer one wins. The ceiling is the provider's: a
    link cannot be created with an expiry more than six months out. The window end is
    Revora's: past it, policy no longer permits the case to act, and a link that
    outlived the window would let a customer pay through a case that had already
    expired — a payment nobody authorized and no case is waiting for.

    Both instants go through ``ensure_utc``, so a naive datetime is refused rather than
    assumed to be UTC. An expiry is a comparison against provider-side time; being an
    hour wrong about it is not a rounding error, it is a link that dies early or lives
    too long.

    Raises:
        PaymentLinkRequestError: if the clamped expiry is not in the future. A link that
            expires at or before the moment it is created is not a recovery action, and
            the provider would reject it — better to fail before the call than to spend
            an execution attempt discovering that.
    """
    window = ensure_utc(window_end)
    moment = ensure_utc(now)
    expiry = min(window, moment + ceiling)
    if expiry <= moment:
        raise PaymentLinkRequestError(
            "expiry_not_in_future", "recovery window ends at or before the current instant"
        )
    return int(expiry.timestamp())


@dataclass(frozen=True, slots=True)
class CustomerContact:
    """Where the notification goes. Decrypted by the caller, just in time.

    Both fields are declared sensitive, so ``platform.masking`` masks them if this
    object is ever handed to the audit writer or a log field. ``repr`` is overridden
    anyway, because the leak that actually happens is a traceback frame or a debugger,
    neither of which consults a masking registry.

    ``contact`` is required and ``email`` optional: a phone number is the channel the
    provider always has, and a link nobody can be told about is not an action.
    """

    contact: str = field(metadata=sensitive(FieldKind.CONTACT))
    email: str | None = field(default=None, metadata=sensitive(FieldKind.CONTACT))

    def __post_init__(self) -> None:
        if not self.contact.strip():
            raise PaymentLinkRequestError("contact_blank", "customer contact is empty")

    def __repr__(self) -> str:
        return f"CustomerContact(contact=<redacted>, email={'<redacted>' if self.email else None})"

    __str__ = __repr__


@dataclass(frozen=True, slots=True)
class PaymentLinkRequest:
    """A validated creation request. Every field is verified provider surface.

    Built by :func:`build_payment_link_request`, never by hand — the constructor does no
    clamping and no validation beyond what the types enforce, so constructing one
    directly is how an unclamped ``expire_by`` or an unvalidated description would get
    past the gates above.
    """

    amount: Minor
    currency: str
    description: str
    reference_id: str
    customer: CustomerContact
    expire_by: int
    case_id: str
    notify_sms: bool = True
    notify_email: bool = True
    accept_partial: bool = False
    """False, always. A partial payment must not be mistakable for recovery."""
    reminder_enable: bool = False
    """False, always. Provider reminders are customer-visible messages that
    ``MAX_CUSTOMER_MESSAGES`` does not count — see this module's docstring, Property 9."""

    def to_payload(self) -> dict[str, object]:
        """The JSON body, using the verified field names and nothing else.

        Amounts stay integers all the way to the wire: the provider takes "currency
        subunits", which is the same thing ``Minor`` counts, so there is no conversion
        to get wrong and no place for a float to appear.
        """
        return {
            "amount": int(self.amount),
            "currency": self.currency,
            "description": self.description,
            "reference_id": self.reference_id,
            "customer": self._customer_payload(),
            "notify": {"sms": self.notify_sms, "email": self.notify_email},
            "reminder_enable": self.reminder_enable,
            "expire_by": self.expire_by,
            "accept_partial": self.accept_partial,
            "notes": {
                NOTES_CASE_ID_FIELD: self.case_id,
                NOTES_KEY_FIELD: self.reference_id,
            },
        }

    def _customer_payload(self) -> dict[str, str]:
        payload = {"contact": self.customer.contact}
        if self.customer.email:
            payload["email"] = self.customer.email
        return payload

    def __repr__(self) -> str:
        # No customer fields, and no description either: the description is
        # customer-visible text that may name an order or an amount, and a request repr
        # turning up in a log line should carry identifiers only.
        return (
            "PaymentLinkRequest("
            f"reference_id={self.reference_id!r}, case_id={self.case_id!r}, "
            f"amount={int(self.amount)}, currency={self.currency!r}, "
            f"expire_by={self.expire_by}, accept_partial={self.accept_partial}, "
            f"reminder_enable={self.reminder_enable})"
        )

    __str__ = __repr__


def build_payment_link_request(
    *,
    case_id: object,
    action: CandidateAction,
    attempt_ordinal: int,
    amount: Minor,
    currency: str,
    description: str,
    customer: CustomerContact,
    window_end: datetime,
    now: datetime,
    max_message_length: int,
    expiry_ceiling: timedelta = PROVIDER_EXPIRY_CEILING,
) -> PaymentLinkRequest:
    """Validate, clamp and assemble a creation request.

    The one supported way to build a :class:`PaymentLinkRequest`. Called by the
    execution engine while it still holds the case lock and before it commits the
    intent, so that every rejection below happens in a window where nothing has been
    sent and nothing has been recorded.

    Args:
        case_id: recovery case identifier; also goes into ``notes``.
        action: the approved action, hashed into the key.
        attempt_ordinal: the attempt this key belongs to.
        amount: the payment amount in minor units, exactly as the provider wants it.
        currency: the case currency.
        description: the customer-visible message, validated against
            ``max_message_length``.
        customer: contact decrypted just in time by the caller.
        window_end: the recovery window end; ``expire_by`` cannot exceed it.
        now: the current instant, from ``platform.clock.now()``.
        max_message_length: ``Configuration.MAX_MESSAGE_LENGTH`` (300).
        expiry_ceiling: the provider's creation-to-expiry ceiling.

    Raises:
        PaymentLinkRequestError: on any validation failure, naming the rule.
    """
    if int(amount) <= 0:
        raise PaymentLinkRequestError(
            "amount_not_positive", f"amount must be positive minor units, got {int(amount)}"
        )
    normalized_currency = currency.strip().upper()
    if not normalized_currency:
        raise PaymentLinkRequestError("currency_blank", "currency is empty")

    return PaymentLinkRequest(
        amount=amount,
        currency=normalized_currency,
        description=validate_description(description, max_length=max_message_length),
        reference_id=reference_id_for(case_id, action, attempt_ordinal),
        customer=customer,
        expire_by=clamp_expire_by(window_end=window_end, now=now, ceiling=expiry_ceiling),
        case_id=str(case_id),
        # The provider cannot notify an address it was not given, so the email flag
        # follows the data rather than being asserted true against an absent field.
        notify_sms=True,
        notify_email=customer.email is not None,
    )
