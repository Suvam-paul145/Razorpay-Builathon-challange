"""The customer read model: eight fields, chosen one at a time, and no ninth.

This is the only projection in Revora a person outside the Merchant sees, so every field in it
is a disclosure decision and every field *not* in it is the same decision made the other way.
R19.C2 and R29.C3 are written as a list of exclusions, and a list is a thing an implementation
drifts away from — so the mechanism here is not a filter over the dashboard model. It is a
purpose-built frozen dataclass with exactly the declared fields, which means **adding a field
to the dashboard cannot leak it here**: there is no field to filter out, because there is no
inherited shape to filter.

The exclusion list, stated once so a reader can check it against the dataclass below rather
than against a requirements document. Absent by construction: any customer contact identifier
in any form, masked or not; any payment instrument reference;
``baseline_recovery_probability``; ``intervention_recovery_probability``;
``incremental_probability``; ``expected_incremental_revenue``; ``financial_cost``;
``communication_cost``; ``risk_cost``; ``customer_cost``; ``total_action_cost``;
``net_recovery_value``; any rejected Candidate_Action; any Policy_Decision; any configuration
value; any Merchant_User identifier; any Audit_Record; any field of a second Recovery_Case.

**On the declared count.** The design's prose says "nine fields" and then enumerates eight, in
its own JSON sample and again in its bullet list, and the requirements enumerate the same
eight (R19.C1's seven presented items, with the amount's currency counted separately). The
enumeration is the requirement and the count is a miscount, so this module declares the eight
that are named. The alternative — inventing a ninth to satisfy the arithmetic — would be a
disclosure decision taken by counting, which is precisely the way this list must not grow.

**Three things about how the fields are produced.**

``amount`` is an integer count of minor units, formatted on the server (R19.C3). It is
formatted by an *injected* renderer rather than here, and that is a layering fact rather than
a preference: the currency symbol table, the minor-unit digits and the lakh-versus-thousands
grouping live in ``revora.api.rendering``, which sits above this package in the layering
contract and is therefore unreachable from it. Duplicating those tables here would give the
customer page its own idea of what a rupee looks like, and the one screen where the server's
figure and the customer's figure must agree is this one. So the router hands the renderer down
and :func:`as_document` assembles the key set — one currency vocabulary, one key set, no
upward import.

``reason`` is a plain-language rendering of the **recorded** ``Risk_Cause``, from
:data:`PLAIN_LANGUAGE_CAUSE`, which is total over the enumeration and checked to be total at
import. It is never the provider's error string. That string is internal vocabulary, it
sometimes names our own integration, and it is written for an operator debugging a payment
rail rather than for a person deciding whether they can afford to pay today.

``pay_url`` is the payment link short URL, which is a bearer capability — whoever holds it can
pay. Disclosing it here adds no risk the system did not already take: it is the same URL the
customer already received in the message that carried this page's link, so the token grants no
payment access the SMS did not. It is the one field in this projection that is masked
everywhere else, and that asymmetry is deliberate rather than an oversight.

Nothing in this module transitions a case, evaluates policy, schedules an action or calls a
provider. It reads four tables and returns a value.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, fields
from datetime import datetime
from types import MappingProxyType
from typing import TYPE_CHECKING, Final

from revora.domain.enums import ExecutionEffectKind, IntentState, PromiseStatus, RiskCause
from revora.domain.money import Minor
from revora.persistence.repositories.cases import RecoveryCaseRepository
from revora.persistence.repositories.customer import PromiseToPayRepository
from revora.persistence.repositories.diagnosis import DiagnosisRepository
from revora.persistence.repositories.execution import ExecutionIntentRepository
from revora.platform.clock import ensure_utc

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sqlalchemy.orm import Session

    from revora.customer.tokens import VerifiedToken

__all__ = [
    "NO_CAUSE_RECORDED",
    "PLAIN_LANGUAGE_CAUSE",
    "PROJECTION_FIELDS",
    "AmountRenderer",
    "CustomerCaseProjection",
    "PromiseView",
    "as_document",
    "build_projection",
]


# ---------------------------------------------------------------------------
# The plain-language cause table (R19.C1)
# ---------------------------------------------------------------------------

PLAIN_LANGUAGE_CAUSE: Final[Mapping[RiskCause, str]] = MappingProxyType(
    {
        RiskCause.INSUFFICIENT_FUNDS: (
            "The payment did not go through because there were not enough funds "
            "available at the time."
        ),
        RiskCause.EXPIRED_PAYMENT_METHOD: (
            "The payment did not go through because the card or account details on "
            "file have expired."
        ),
        RiskCause.BANK_OR_NETWORK_FAILURE: (
            "The payment did not go through because the bank could not be reached "
            "when it was attempted."
        ),
        RiskCause.TECHNICAL_ISSUE: (
            "The payment did not go through because of a technical problem during "
            "the attempt."
        ),
        RiskCause.ABANDONMENT: (
            "The payment was started but not finished, so nothing has been charged."
        ),
        RiskCause.CUSTOMER_ACTION_REQUIRED: (
            "The payment needs one more step from you before it can complete."
        ),
        RiskCause.FRAUD_OR_RISK_SIGNAL: (
            "The payment could not be completed. Please use the link below to pay, "
            "or contact the seller."
        ),
        RiskCause.UNKNOWN: (
            "The payment did not go through. The reason is not clear from what the "
            "bank told us."
        ),
    }
)
"""One sentence per ``RiskCause``, written for the person who owes the money.

**Total over the enumeration, and asserted total at import.** A ``.get`` with a fallback would
let a cause added tomorrow ship under a generic sentence nobody chose, on the one surface where
the wording is the entire product — a customer who cannot tell why a payment failed cannot fix
it, and a sentence that guesses is worse than one that says the reason is unclear.

Three of the eight are worth their own note.

``FRAUD_OR_RISK_SIGNAL`` deliberately does **not** say what it is. Telling a customer their
payment was flagged as a risk is either an accusation or a hint, and both are wrong: if the
signal is correct it tells a fraudster which of their attempts tripped a control, and if it is
incorrect it accuses somebody of something a template cannot substantiate. So the sentence
declines to give a cause and offers the two things that are actually useful.

``UNKNOWN`` says the reason is unclear rather than inventing one. It is the substituted cause
under R3.C8 whenever recorded confidence falls below the floor, so it is not a rare
end-of-enum case — it is the honest answer for a real and common state.

``ABANDONMENT`` states that nothing has been charged, because the single most likely worry of
somebody reading this page is that they have been charged twice."""

_MISSING_CAUSES = sorted(
    cause.value for cause in RiskCause if cause not in PLAIN_LANGUAGE_CAUSE
)
if _MISSING_CAUSES:  # pragma: no cover - import-time invariant
    raise RuntimeError(
        "PLAIN_LANGUAGE_CAUSE has no sentence for "
        f"{_MISSING_CAUSES}; the table must be total over RiskCause so a new cause "
        "cannot ship to a customer under a sentence nobody chose"
    )

NO_CAUSE_RECORDED: Final[str] = (
    "The payment did not go through. We are still checking why with the bank."
)
"""What ``reason`` says before a Diagnosis exists for the case.

A distinct sentence rather than reusing ``UNKNOWN``'s, and the difference is a real one: the
``UNKNOWN`` cause means we looked and could not tell, while this means we have not finished
looking. A customer reading the first should stop waiting for a better answer; a customer
reading the second should not.

Reachable in practice, not defensive: a token is minted at execution and a customer can open
the page during a decision cycle whose diagnosis was superseded, so "no active diagnosis right
now" is a state the read genuinely lands in."""


# ---------------------------------------------------------------------------
# The projection
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PromiseView:
    """The recorded Promise_To_Pay, as the customer's own two facts about it.

    The date they gave and where it stands. Deliberately **not** the Follow_Up_Instant: when
    Revora intends to contact them next is scheduling information, and a customer told the
    follow-up date would reasonably read it as a second deadline. Deliberately not the
    ``window_end_at_snapshot`` either — that is already the projection's ``window_end_at``, and
    a promise carrying its own copy is a second number that can disagree with the first.
    """

    promise_date: datetime
    status: PromiseStatus


@dataclass(frozen=True, slots=True)
class CustomerCaseProjection:
    """Everything the customer response page discloses. Eight fields, and no ninth.

    ``frozen`` so a caller between the read and the response cannot add to it, and ``slots`` so
    it cannot acquire an attribute that was never declared — which is the second half of the
    same guarantee. Together they mean the disclosure surface is fixed at class definition
    rather than at each call site.
    """

    merchant_display_name: str
    """The Merchant's configured display name. Not their slug, which is an internal handle, and
    not any Merchant_User's name, which R19.C2 excludes."""

    amount: Minor
    """The case's ``payment_amount``, as the stored integer count of minor units.

    An integer here and a formatted string on the wire. There is no arithmetic anywhere between
    the two — see :func:`as_document` — because the one place a rounding error in this system
    would be read by the person paying is this field."""

    currency: str
    reason: str
    """A sentence from :data:`PLAIN_LANGUAGE_CAUSE`, or :data:`NO_CAUSE_RECORDED`."""

    pay_url: str | None
    """The live payment link's short URL, or ``None`` when the case has no confirmed link.

    ``None`` is a real state and not a failure: a case whose selected action was a message or a
    human escalation has a token and no link, and the page then says what is owed without
    offering a way to pay it — which is correct, because inventing a link would be inventing an
    external effect no policy decision approved."""

    window_end_at: datetime
    promise: PromiseView | None
    signals_remaining: int
    """How many further signals this token may write, from ``CUSTOMER_TOKEN_MAX_SUBMISSIONS``
    minus the accepted count. Never negative.

    The one field here derived from a configured bound, and it is not a disclosure of that bound
    — R19.C2 excludes configuration values and this is a remaining count, which is the same
    difference as between a balance and a credit limit. It exists because a form that accepts a
    submission and then refuses it is worse than one that says beforehand how many are left."""


PROJECTION_FIELDS: Final[frozenset[str]] = frozenset(
    field.name for field in fields(CustomerCaseProjection)
)
"""The authoritative key set, derived from the dataclass rather than restated.

Derived so that P34 — "the projection's key set is exactly the declared fields" — is a claim
about one declaration rather than a comparison of two lists that could both be edited. A
hand-written copy would drift towards whatever the code started doing, which is the opposite of
what the property is for."""

type AmountRenderer = Callable[[Minor, str], Mapping[str, object]]
"""Turns minor units and a currency code into the wire envelope for an amount.

Injected rather than imported: the currency presentation tables live in ``revora.api.rendering``
and this package sits below it. See the module docstring."""


def as_document(
    projection: CustomerCaseProjection, *, render_amount: AmountRenderer
) -> dict[str, object]:
    """The wire form. Its keys are exactly :data:`PROJECTION_FIELDS`.

    Assembled here rather than in the router so there is one place the customer-facing key set
    exists. A router that built its own dict would be a second declaration of the disclosure
    surface, and the two would diverge on the day somebody added a field to one of them.

    Every value is either a string, an integer, ``None`` or the amount envelope. No object with
    a ``__dict__`` reaches the wire, so a field added to :class:`PromiseView` cannot arrive at a
    customer by being serialized wholesale.
    """
    document: dict[str, object] = {
        "merchant_display_name": projection.merchant_display_name,
        "amount": dict(render_amount(projection.amount, projection.currency)),
        "currency": projection.currency,
        "reason": projection.reason,
        "pay_url": projection.pay_url,
        "window_end_at": projection.window_end_at.isoformat(),
        "promise": (
            None
            if projection.promise is None
            else {
                "promise_date": projection.promise.promise_date.isoformat(),
                "status": projection.promise.status.value,
            }
        ),
        "signals_remaining": projection.signals_remaining,
    }
    if frozenset(document) != PROJECTION_FIELDS:  # pragma: no cover - import-time invariant
        raise RuntimeError(
            "the customer document's keys are not the declared projection fields: "
            f"extra {sorted(frozenset(document) - PROJECTION_FIELDS)}, "
            f"missing {sorted(PROJECTION_FIELDS - frozenset(document))}"
        )
    return document


def build_projection(
    session: Session,
    token: VerifiedToken,
    *,
    merchant_display_name: str,
    signals_remaining: int,
) -> CustomerCaseProjection | None:
    """Read the one case a token names. ``None`` when there is no such case.

    Every read below is scoped to ``token.merchant_id`` and ``token.case_id``, and both come off
    the persisted token row rather than from the request (R18.C4, R29.C2). There is no argument
    to this function through which a caller could name a second case, which is a stronger
    statement than "the caller does not".

    ``None`` is unreachable in practice — ``customer_access_token.case_id`` is a foreign key with
    ``ON DELETE RESTRICT``, so the case cannot have gone away — and it is returned rather than
    asserted anyway, because the alternative on the one endpoint reachable without a session is
    an unhandled exception becoming a 500 with a body somebody has to be sure discloses nothing.

    ``merchant_display_name`` is passed in rather than read here. The ``merchant`` table is the
    one table with no ``merchant_id`` column and no row-level-security policy — it *is* the
    tenant — so reading it is untenanted by nature, and the router has already done exactly that
    read to resolve which tenant's tokens to look in. Doing it twice would be a second
    untenanted read on the public surface for a value the caller already holds.
    """
    case = RecoveryCaseRepository(session).get(token.merchant_id, token.case_id)
    if case is None:  # pragma: no cover - RESTRICT makes the case undeletable
        return None

    diagnosis = DiagnosisRepository(session).active_for_case(
        token.merchant_id, token.case_id
    )
    promise = PromiseToPayRepository(session).for_case(token.merchant_id, token.case_id)

    return CustomerCaseProjection(
        merchant_display_name=merchant_display_name,
        amount=Minor(int(case.payment_amount)),
        currency=str(case.currency),
        reason=(
            NO_CAUSE_RECORDED
            if diagnosis is None
            else PLAIN_LANGUAGE_CAUSE[RiskCause(str(diagnosis.cause))]
        ),
        pay_url=_live_pay_url(session, token.merchant_id, token.case_id),
        window_end_at=ensure_utc(case.window_end_at),
        promise=(
            None
            if promise is None
            else PromiseView(
                promise_date=ensure_utc(promise.promise_date),
                status=PromiseStatus(str(promise.status)),
            )
        ),
        signals_remaining=max(0, signals_remaining),
    )


def _live_pay_url(
    session: Session, merchant_id: uuid.UUID, case_id: uuid.UUID
) -> str | None:
    """The short URL of the newest confirmed payment link for a case, or ``None``.

    Three filters, and each excludes something that would be wrong to show a customer.

    ``effect_kind == PAYMENT_LINK_CREATE`` — a resend intent carries no URL of its own and never
    could: the resend response is a success boolean and nothing else. Filtering on the kind
    rather than on ``provider_short_url IS NOT NULL`` alone says why the column is empty.

    ``state == CONFIRMED`` — an ``ATTEMPTED`` or ``UNCERTAIN`` intent is one where we do not know
    whether the link exists, and offering an unconfirmed URL to a customer is inviting them to
    open a page that may 404 while they are trying to pay. A ``FAILED`` intent's link definitely
    does not exist.

    Newest first among those — ``list_for_case`` returns attempt order, so the last match wins.
    A case with two confirmed links has the second because the first was superseded, and the
    customer should be sent to the current one.
    """
    latest: str | None = None
    for intent in ExecutionIntentRepository(session).list_for_case(merchant_id, case_id):
        if (
            str(intent.effect_kind) == ExecutionEffectKind.PAYMENT_LINK_CREATE.value
            and str(intent.state) == IntentState.CONFIRMED.value
            and intent.provider_short_url
        ):
            latest = str(intent.provider_short_url)
    return latest
