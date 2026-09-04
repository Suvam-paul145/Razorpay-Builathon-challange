"""A scriptable Razorpay stand-in that can be told to fail in every way that matters.

This is as important as the real client, and for a reason worth stating plainly: the
guarantees Revora claims are all statements about what happens when the provider
misbehaves. A fake that only knows how to succeed tests the one path that was never in
doubt.

**The two rows this exists for** are ``TIMEOUT_EFFECT_CREATED`` and
``TIMEOUT_NO_EFFECT``. Both return the identical classification —
``Timeout(phase=AFTER_SEND)`` — and they differ only in whether the payment link now
exists on the provider side. That is the exact ambiguity Property 3 lives or dies on:
after a timeout the system cannot know which case it is in, so it must behave
correctly in both, and the only way to test that is a fake that can be either while
looking the same.

**Every call is recorded**, so a test asserts *zero calls were made* rather than
merely that nothing broke. "The policy check refused, therefore no provider call
happened" is a claim about a negative, and a negative is only provable against a log.
:meth:`FakeRazorpay.create_call_count_for` exists specifically for task 20's Property 3
assertion that at most one create is ever issued per idempotency key.

**Contact details are never recorded.** The call log stores identifiers, amounts and
the two correctness-critical flags — not the customer contact, even though the request
object carries it. A test fixture is still somewhere a PII habit can form, and the
recorded arguments are the part of a fake most likely to be printed on a failure.

**The resend has its own outcome set**, and one member of it exists for a reason worth
naming: ``RATE_LIMITED``. A 429 is the only outcome whose classification differs between the
create and the resend — for a resend it is definitive rather than unclassifiable — and it is
also the outcome that spends a customer-message increment for nothing. A fake that could not
produce one would leave both facts untestable. ``ACK_FALSE`` and ``UNPARSEABLE`` cover the
other half of the resend's table: a 200 that does not mean what a 200 usually means.

Scripting model, deliberately small: one behaviour object drives everything.
``create_outcomes`` is consumed in order and its last entry repeats, so
``(TIMEOUT_NO_EFFECT, SUCCESS)`` means "the first attempt times out having done
nothing, every attempt after that succeeds". The read paths count attempts per
identifier, so lag and unavailability are expressed as counts rather than as
callbacks — a callback would let a test script something the real provider could not
do.
"""

from __future__ import annotations

import hashlib
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum, unique
from typing import Final

from hypothesis import strategies as st

from revora.domain.payment_event import PaymentStatus
from revora.providers.classification import (
    RATE_LIMITED,
    RATE_LIMITED_REASON,
    RATE_LIMITED_STATUS,
    CallPhase,
    ClientError,
    PaymentEntity,
    PaymentLinkEntity,
    PaymentLinkList,
    PaymentLinkResendAck,
    PaymentList,
    ProviderResult,
    ResultSource,
    ServerError,
    Success,
    Timeout,
    Unclassifiable,
    refused_for_invalid_request,
)
from revora.providers.payment_link import NotifyMedium, PaymentLinkRequest
from revora.providers.razorpay import (
    MAX_PAYMENT_WINDOW_TS,
    MAX_PAYMENTS_PAGE_SIZE,
    MIN_PAYMENT_WINDOW_TS,
    OPERATION_CREATE_PAYMENT_LINK,
    OPERATION_FETCH_PAYMENT,
    OPERATION_FIND_PAYMENT_LINKS,
    OPERATION_LIST_PAYMENTS,
    OPERATION_NOTIFY_BY,
    PaymentProviderClient,
)

__all__ = [
    "DEFAULT_PAYMENT_AMOUNT",
    "FAKE_SHORT_URL_PREFIX",
    "CreateOutcome",
    "FakeRazorpay",
    "ProviderBehaviour",
    "RecordedCall",
    "ResendOutcome",
    "as_provider_client",
    "provider_behaviour",
]

FAKE_SHORT_URL_PREFIX: Final[str] = "https://fake.invalid/plink/"
"""Deliberately not a plausible provider URL. A payment link is a bearer capability,
and a fake that minted something resembling a real one invites a test artifact being
pasted somewhere it would be taken seriously. ``.invalid`` is reserved and cannot
resolve."""

DEFAULT_PAYMENT_AMOUNT: Final[int] = 100_000
"""Minor units. An integer, like every amount in Revora."""

_UNPARSEABLE_BODY: Final[str] = '{"entity": "payment_link", "id": '
"""A truncated JSON document, which is what a 200 with an unparseable body really
looks like when a proxy cuts a response short."""


@unique
class CreateOutcome(StrEnum):
    """What one ``create_payment_link`` attempt does.

    The name of each member states the *effect*, not the classification, because the
    classification is the part the caller can see and the effect is the part it cannot.
    ``TIMEOUT_EFFECT_CREATED`` and ``TIMEOUT_NO_EFFECT`` return the same result to the
    caller and differ only in what exists afterwards.
    """

    SUCCESS = "SUCCESS"
    CLIENT_ERROR = "CLIENT_ERROR"
    SERVER_ERROR = "SERVER_ERROR"
    TIMEOUT_EFFECT_CREATED = "TIMEOUT_EFFECT_CREATED"
    TIMEOUT_NO_EFFECT = "TIMEOUT_NO_EFFECT"
    CONNECT_ERROR = "CONNECT_ERROR"
    UNCLASSIFIABLE = "UNCLASSIFIABLE"


@unique
class ResendOutcome(StrEnum):
    """What one ``notify_by`` attempt does.

    Named for the *effect* like :class:`CreateOutcome`, with one difference that matters: there
    is no ``TIMEOUT_MESSAGE_SENT`` / ``TIMEOUT_NOT_SENT`` pair, because for a resend the
    distinction is not observable by anything — not by the caller, and not by a later read. The
    fake cannot offer a ground-truth oracle for a resend for the same reason the provider
    cannot: nothing reports whether a notification was sent. ``TIMEOUT`` is therefore one
    member, and a test asserting behaviour after it is asserting behaviour under permanent
    ignorance, which is the real situation.
    """

    SUCCESS = "SUCCESS"
    ACK_FALSE = "ACK_FALSE"
    """200 with ``{"success": false}``. Undocumented surface, so it is unknown, not a no."""

    UNPARSEABLE = "UNPARSEABLE"
    """200 with a body that is not JSON. A proxy cut the response short."""

    RATE_LIMITED = "RATE_LIMITED"
    """429 from the provider's gateway. Definitive for a resend, and it still spends a
    customer-message increment."""

    CLIENT_ERROR = "CLIENT_ERROR"
    SERVER_ERROR = "SERVER_ERROR"
    TIMEOUT = "TIMEOUT"
    """Read timeout after the request was sent. Permanently unresolvable."""

    CONNECT_ERROR = "CONNECT_ERROR"
    """Nothing left the process, so nothing was sent. The one definitive network failure."""


@dataclass(frozen=True, slots=True)
class RecordedCall:
    """One call the fake received.

    ``arguments`` holds identifiers, integers and flags only — never customer contact.
    ``sequence`` is a monotonic counter across every operation, so a test can assert
    ordering ("the read happened after the create") and not only counts.
    """

    operation: str
    arguments: Mapping[str, object]
    sequence: int


@dataclass(frozen=True, slots=True)
class ProviderBehaviour:
    """The script. One object, consumed by all three operations.

    Defaults are the happy path, so a test that only cares about one failure names only
    that failure. Every count is "how many of the first attempts", which is how the two
    real risks — read-after-write lag on the listing, and a provider read being
    unavailable for a while — actually present.
    """

    create_outcomes: tuple[CreateOutcome, ...] = (CreateOutcome.SUCCESS,)
    """Consumed in order; the last entry repeats forever. Empty means always succeed."""

    resend_outcomes: tuple[ResendOutcome, ...] = (ResendOutcome.SUCCESS,)
    """Consumed in order per ``(payment_link_id, medium)``; the last entry repeats.

    Keyed per link and medium rather than globally, because that is what the provider's
    documented rate limit is per, so a test scripting a 429 for one medium can assert the other
    is unaffected."""

    listing_unavailable_reads: int = 0
    """How many of the first listing reads answer 5xx. Reconciliation must retry these
    rather than concluding anything."""

    listing_empty_reads: int = 0
    """How many of the first listing reads answer empty *even when the link exists*.
    This is the read-after-write lag the design marks [EVIDENCE INSUFFICIENT], and the
    reason reconciliation may only treat empty as ``FAILED`` on its final attempt."""

    payment_statuses: tuple[PaymentStatus, ...] = (PaymentStatus.CAPTURED,)
    """Consumed in order; the last entry repeats. A tuple rather than a single value so
    one field expresses three separate rows of the design's table: a read that
    disagrees with a success webhook, a state that changes between reads, and a delayed
    success arriving after the case reached a terminal state."""

    payment_unavailable_reads: int = 0
    """N consecutive unavailable reads, for the escalation path (R10.C7)."""

    payment_amount: int = DEFAULT_PAYMENT_AMOUNT

    payment_amount_refunded: int | None = None
    """Refunded amount on a read, or ``None`` to derive it from the status.

    Exists so a *partial* payment can be scripted. A partly-recovered payment is a capture
    whose refunded amount is strictly between zero and the full amount — ``partially_paid`` is
    a payment-link status, not a payment status — and that is not derivable from the status
    alone."""

    window_contact: str | None = None
    window_email: str | None = None
    """Contact fields on payments returned by ``list_payments``.

    Off by default. A backfilled payment is only actionable if the read carried a contact, and
    defaulting these to a value would hide the case where it did not — which is the case that
    decides whether a backfilled case can be acted on or only seen."""

    window_payments: tuple[tuple[str, PaymentStatus], ...] = ()
    """What ``list_payments`` reports for the window, as ``(payment_id, status)`` pairs.

    Pairs rather than built entities so a test names only what the backfill actually
    branches on. The fake paginates over these honestly — respecting ``count`` and
    ``skip`` — because the endpoint caps a page at 100 and a backfill that forgets to
    page would silently ignore everything past the first page, which is the same
    detection gap it was written to close."""

    listing_window_unavailable_reads: int = 0
    """How many of the first ``list_payments`` reads answer 5xx. The backfill must
    retry rather than conclude the window was empty."""

    # -- readable constructors for the design's behaviour table ------------

    @classmethod
    def always_succeeding(cls) -> ProviderBehaviour:
        return cls()

    @classmethod
    def timeout_with_effect_created(cls) -> ProviderBehaviour:
        """The critical case: the caller sees a timeout, the link exists.

        Reconciliation must find it by ``reference_id`` and confirm, and must never
        issue a second create.
        """
        return cls(create_outcomes=(CreateOutcome.TIMEOUT_EFFECT_CREATED,))

    @classmethod
    def timeout_with_no_effect(cls) -> ProviderBehaviour:
        """Indistinguishable from the above to the caller; nothing was created.

        Reconciliation must eventually mark the intent ``FAILED`` — and only on its
        final attempt, because an early empty read cannot be told from lag.
        """
        return cls(create_outcomes=(CreateOutcome.TIMEOUT_NO_EFFECT,))

    @classmethod
    def resend_rate_limited(cls) -> ProviderBehaviour:
        """The provider's gateway declines the resend. Definitive; nothing was delivered."""
        return cls(resend_outcomes=(ResendOutcome.RATE_LIMITED,))

    @classmethod
    def resend_unverifiable(cls) -> ProviderBehaviour:
        """The resend times out after sending. Nothing can establish what happened."""
        return cls(resend_outcomes=(ResendOutcome.TIMEOUT,))

    @classmethod
    def listing_lag(cls, empty_reads: int) -> ProviderBehaviour:
        """The link exists but the listing does not show it for ``empty_reads`` reads."""
        return cls(
            create_outcomes=(CreateOutcome.TIMEOUT_EFFECT_CREATED,),
            listing_empty_reads=empty_reads,
        )

    @classmethod
    def payment_status(cls, status: PaymentStatus) -> ProviderBehaviour:
        return cls(payment_statuses=(status,))

    @classmethod
    def missed_failures(cls, count: int) -> ProviderBehaviour:
        """``count`` failed payments sit in the window that no webhook ever delivered.

        The webhook-disabled scenario: sustained delivery failure disables the endpoint,
        so detection stops entirely and the only way to find these is the backfill.
        """
        return cls(
            window_payments=tuple(
                (f"pay_MISSED{index:08d}", PaymentStatus.FAILED) for index in range(count)
            )
        )

    @classmethod
    def read_disagreeing_with_success_webhook(cls) -> ProviderBehaviour:
        """A success webhook arrived; the authoritative read says the payment failed.

        R10.C13's conflict path. The read wins — a webhook is a claim, a fetch is
        evidence — so no recovery may be declared, and the case holds.
        """
        return cls(payment_statuses=(PaymentStatus.FAILED,))

    @classmethod
    def unavailable_payment_reads(cls, count: int) -> ProviderBehaviour:
        return cls(payment_unavailable_reads=count)

    @classmethod
    def delayed_success(cls, *, failed_reads: int) -> ProviderBehaviour:
        """Failure for ``failed_reads`` reads, then capture — R10.C14 delayed recovery."""
        statuses = (PaymentStatus.FAILED,) * failed_reads + (PaymentStatus.CAPTURED,)
        return cls(payment_statuses=statuses)


def provider_behaviour() -> st.SearchStrategy[ProviderBehaviour]:
    """A Hypothesis strategy over the whole behaviour catalogue.

    Bounded on purpose. The counts stay small because the properties that consume this
    are bounded by ``MAX_EXECUTION_RECONCILIATION_ATTEMPTS`` (6) and
    ``MAX_PAYMENT_STATE_READ_ATTEMPTS`` (5); generating 400 consecutive unavailable
    reads would explore a region where every property is trivially true because the
    case escalated long before, and would spend the example budget doing it.

    Every ``CreateOutcome`` is reachable, and the status tuple is drawn from all five
    verified statuses, so a run of this strategy covers every row of the design's Fake
    Providers table that concerns the client.
    """
    return st.builds(
        ProviderBehaviour,
        create_outcomes=st.lists(st.sampled_from(CreateOutcome), min_size=1, max_size=3).map(tuple),
        listing_unavailable_reads=st.integers(min_value=0, max_value=3),
        listing_empty_reads=st.integers(min_value=0, max_value=3),
        payment_statuses=st.lists(
            st.sampled_from(PaymentStatus), min_size=1, max_size=3
        ).map(tuple),
        payment_unavailable_reads=st.integers(min_value=0, max_value=3),
        payment_amount=st.integers(min_value=1, max_value=10_000_000),
    )


class FakeRazorpay:
    """Satisfies ``providers.razorpay.PaymentProviderClient``, backed by a script.

    Structurally typed rather than a subclass, so the production client has no
    knowledge that a fake exists and no test-only branch in it.

    Thread-safe. Not decoration: the execution properties that need this fake are the
    concurrency ones, and a counter that could be lost to a race would turn "at most
    one create per key" from a failed assertion into an intermittent one.
    """

    __slots__ = ("_behaviour", "_calls", "_create_attempts", "_fetch_attempts", "_links",
                 "_listing_attempts", "_lock", "_notify_attempts", "_payment_amounts",
                 "_payment_scripts", "_sequence", "_window_attempts")

    def __init__(self, behaviour: ProviderBehaviour | None = None) -> None:
        self._behaviour = behaviour if behaviour is not None else ProviderBehaviour()
        self._lock = threading.Lock()
        self._calls: list[RecordedCall] = []
        self._sequence = 0
        self._create_attempts: dict[str, int] = {}
        self._listing_attempts: dict[str, int] = {}
        self._fetch_attempts: dict[str, int] = {}
        self._notify_attempts: dict[str, int] = {}
        self._window_attempts = 0
        self._links: dict[str, PaymentLinkEntity] = {}
        # Per-payment overrides. Empty for every existing test, which is why the read path
        # below falls through to the behaviour object when a payment is not in here.
        self._payment_amounts: dict[str, int] = {}
        self._payment_scripts: dict[str, tuple[PaymentStatus, ...]] = {}

    # -- the call log ------------------------------------------------------

    @property
    def calls(self) -> tuple[RecordedCall, ...]:
        """Every call, in order. The evidence behind a "zero calls" assertion."""
        with self._lock:
            return tuple(self._calls)

    @property
    def call_count(self) -> int:
        with self._lock:
            return len(self._calls)

    def calls_for(self, operation: str) -> tuple[RecordedCall, ...]:
        """Every call of one operation. Pass an ``OPERATION_*`` constant, not a literal."""
        return tuple(call for call in self.calls if call.operation == operation)

    def create_call_count_for(self, reference_id: str) -> int:
        """How many create calls were issued under one idempotency key.

        Property 3's assertion is that this never exceeds one, on any interleaving of
        crashes, timeouts and reconciliation passes. Counted here rather than derived
        from the call log by each test, so every test counts it the same way.
        """
        with self._lock:
            return self._create_attempts.get(reference_id, 0)

    def notify_call_count_for(self, payment_link_id: str, medium: NotifyMedium) -> int:
        """How many resends were issued against one link on one medium.

        The counterpart to :meth:`create_call_count_for`, and needed for the same kind of
        assertion in the opposite direction: after an ``UNCERTAIN`` resend the count must stay
        where it is forever, because nothing — not a sweep, not a restart, not a further
        decision cycle on the same key — is permitted to send that message again.
        """
        with self._lock:
            return self._notify_attempts.get(_notify_key(payment_link_id, medium), 0)

    def created_link_exists(self, reference_id: str) -> bool:
        """Whether the external effect exists on the fake provider side.

        The test-only oracle. A test asserting the timeout-with-effect-created case can
        check the ground truth the system under test is not allowed to see.
        """
        with self._lock:
            return reference_id in self._links

    def reset_calls(self) -> None:
        """Clear the call log, keeping created links. For a test with two phases."""
        with self._lock:
            self._calls.clear()

    # -- the three operations ---------------------------------------------

    # -- per-payment scripting ---------------------------------------------

    def script_payment(
        self,
        payment_id: str,
        *,
        amount: int,
        statuses: tuple[PaymentStatus, ...],
    ) -> None:
        """Pin what ``fetch_payment`` answers for **one** payment id.

        Added for the Demonstration_Loader, which drives a thousand cases through one worker
        and one provider client. :attr:`ProviderBehaviour.payment_amount` and
        :attr:`ProviderBehaviour.payment_statuses` are process-wide, and both are wrong for a
        batch: the amount on an authoritative read has to equal the *case's* ``payment_amount``
        or the Outcome_Monitor correctly treats the read as a partial capture and declares no
        recovery, and a batch whose whole point is a mix of terminal states needs some payments
        to answer captured and others to stay failed.

        The overrides are per id, so scripting one payment says nothing about any other, and a
        payment nobody scripted still answers from the behaviour object exactly as before —
        which is what keeps every existing test unchanged.

        Raises:
            ValueError: on an empty status tuple. An empty script would silently fall back to
                the behaviour's global sequence, which is the one outcome a caller reaching for
                this method cannot have meant.
        """
        if not statuses:
            raise ValueError(
                f"script_payment({payment_id!r}) needs at least one status; an empty tuple "
                "would silently fall back to the process-wide sequence"
            )
        with self._lock:
            self._payment_amounts[payment_id] = int(amount)
            self._payment_scripts[payment_id] = statuses
    def create_payment_link(self, request: PaymentLinkRequest) -> ProviderResult[PaymentLinkEntity]:
        """Create a link, or fail in whichever way the script says.

        Records the two flags that carry correctness weight — ``accept_partial`` and
        ``reminder_enable`` — so a test can assert on what was *sent*, not only on what
        the builder produced. No customer contact is recorded.
        """
        with self._lock:
            attempt = self._create_attempts.get(request.reference_id, 0) + 1
            self._create_attempts[request.reference_id] = attempt
            self._record(
                OPERATION_CREATE_PAYMENT_LINK,
                {
                    "reference_id": request.reference_id,
                    "case_id": request.case_id,
                    "amount": int(request.amount),
                    "currency": request.currency,
                    "expire_by": request.expire_by,
                    "accept_partial": request.accept_partial,
                    "reminder_enable": request.reminder_enable,
                },
            )
            outcome = self._outcome_for(attempt)
            entity = _link_entity(request)

            match outcome:
                case CreateOutcome.SUCCESS:
                    self._links[request.reference_id] = entity
                    return Success(entity=entity, http_status=200)
                case CreateOutcome.TIMEOUT_EFFECT_CREATED:
                    # The effect exists and the caller cannot know it. The whole point.
                    self._links[request.reference_id] = entity
                    return Timeout(phase=CallPhase.AFTER_SEND, detail="ReadTimeout")
                case CreateOutcome.TIMEOUT_NO_EFFECT:
                    return Timeout(phase=CallPhase.AFTER_SEND, detail="ReadTimeout")
                case CreateOutcome.CONNECT_ERROR:
                    # Definitive: nothing left the process, so nothing was created.
                    return Timeout(phase=CallPhase.CONNECT, detail="ConnectError", attempts=2)
                case CreateOutcome.CLIENT_ERROR:
                    return ClientError(
                        code="BAD_REQUEST_ERROR",
                        reason="invalid_request",
                        source=ResultSource.PROVIDER,
                        http_status=400,
                        description="The amount must be at least 100",
                    )
                case CreateOutcome.SERVER_ERROR:
                    return ServerError(http_status=500, raw='{"error":{"code":"SERVER_ERROR"}}')
                case CreateOutcome.UNCLASSIFIABLE:
                    return Unclassifiable(
                        raw=_UNPARSEABLE_BODY,
                        detail="body is not valid JSON",
                        http_status=200,
                    )
            # Unreachable while every member is handled above. Present so that adding a
            # member without handling it fails loudly here rather than returning None
            # into a caller that expects one of five results.
            raise AssertionError(f"unhandled create outcome {outcome!r}")

    def notify_by(
        self, payment_link_id: str, medium: NotifyMedium
    ) -> ProviderResult[PaymentLinkResendAck]:
        """Re-notify against an existing link, or fail in whichever way the script says.

        **No link is created on any path, and no oracle is offered.** Both are the provider's
        behaviour rather than a shortcut: the endpoint re-notifies an existing link, and nothing
        reports whether a notification went out. A fake exposing ``notification_was_sent`` would
        let a test assert something the system under test can never observe, and the test would
        then be checking a fact the production code has no access to.

        The 429 is built here rather than left to a status code, so the fake and the real client
        agree on the ``ClientError`` a rate limit produces — a fake that returned an
        ``Unclassifiable`` for it would make the resend's one deviation from the generic table
        untested against the code that ships.
        """
        with self._lock:
            key = _notify_key(payment_link_id, medium)
            attempt = self._notify_attempts.get(key, 0) + 1
            self._notify_attempts[key] = attempt
            self._record(
                OPERATION_NOTIFY_BY,
                {"payment_link_id": payment_link_id, "medium": medium.value},
            )
            outcomes = self._behaviour.resend_outcomes or (ResendOutcome.SUCCESS,)
            outcome = outcomes[min(attempt - 1, len(outcomes) - 1)]

            match outcome:
                case ResendOutcome.SUCCESS:
                    return Success(entity=PaymentLinkResendAck(success=True), http_status=200)
                case ResendOutcome.ACK_FALSE:
                    return Unclassifiable(
                        raw='{"success": false}',
                        detail="success: resend response reports success false",
                        http_status=200,
                    )
                case ResendOutcome.UNPARSEABLE:
                    return Unclassifiable(
                        raw=_UNPARSEABLE_BODY,
                        detail="body is not valid JSON",
                        http_status=200,
                    )
                case ResendOutcome.RATE_LIMITED:
                    return ClientError(
                        code=RATE_LIMITED,
                        reason=RATE_LIMITED_REASON,
                        source=ResultSource.PROVIDER,
                        http_status=RATE_LIMITED_STATUS,
                    )
                case ResendOutcome.CLIENT_ERROR:
                    return ClientError(
                        code="BAD_REQUEST_ERROR",
                        reason="invalid_request",
                        source=ResultSource.PROVIDER,
                        http_status=400,
                        description="The payment link is not in a notifiable state",
                    )
                case ResendOutcome.SERVER_ERROR:
                    return ServerError(http_status=500, raw='{"error":{"code":"SERVER_ERROR"}}')
                case ResendOutcome.TIMEOUT:
                    return Timeout(phase=CallPhase.AFTER_SEND, detail="ReadTimeout")
                case ResendOutcome.CONNECT_ERROR:
                    return Timeout(phase=CallPhase.CONNECT, detail="ConnectError", attempts=2)
            # Unreachable while every member is handled. Present so a new member fails loudly
            # here instead of returning None into a caller expecting one of five results.
            raise AssertionError(f"unhandled resend outcome {outcome!r}")

    def find_payment_links_by_reference_id(
        self, reference_id: str
    ) -> ProviderResult[PaymentLinkList]:
        """The reconciliation read, with unavailability first and then lag.

        Order matters: an unavailable read is a read that did not happen, so it must not
        consume a lag budget. A test scripting one of each gets 5xx, then empty, then
        the truth — which is the sequence a real reconciliation loop would face.
        """
        with self._lock:
            attempt = self._listing_attempts.get(reference_id, 0) + 1
            self._listing_attempts[reference_id] = attempt
            self._record(OPERATION_FIND_PAYMENT_LINKS, {"reference_id": reference_id})

            if attempt <= self._behaviour.listing_unavailable_reads:
                return ServerError(http_status=503, raw='{"error":{"code":"SERVER_ERROR"}}')
            if attempt <= self._behaviour.listing_unavailable_reads + (
                self._behaviour.listing_empty_reads
            ):
                # Empty although the link may exist. Indistinguishable, by design, from
                # "it was never created" — which is why an early empty result may not be
                # treated as failure.
                return Success(entity=PaymentLinkList(()), http_status=200)

            link = self._links.get(reference_id)
            found = (link,) if link is not None else ()
            return Success(entity=PaymentLinkList(found), http_status=200)

    def fetch_payment(self, payment_id: str) -> ProviderResult[PaymentEntity]:
        """The authoritative read, walking the scripted status sequence.

        ``captured`` is derived from the status rather than scripted separately, because
        a provider that reported ``status == "captured"`` with ``captured == false``
        would be a contradiction, and a fake able to produce one would let a test pass
        against behaviour the provider cannot exhibit.
        """
        with self._lock:
            attempt = self._fetch_attempts.get(payment_id, 0) + 1
            self._fetch_attempts[payment_id] = attempt
            self._record(OPERATION_FETCH_PAYMENT, {"provider_payment_id": payment_id})

            if attempt <= self._behaviour.payment_unavailable_reads:
                return ServerError(http_status=503, raw='{"error":{"code":"SERVER_ERROR"}}')

            index = attempt - self._behaviour.payment_unavailable_reads - 1
            # A per-payment script wins over the process-wide sequence. See
            # :meth:`script_payment` for why a batch needs one.
            statuses = (
                self._payment_scripts.get(payment_id)
                or self._behaviour.payment_statuses
                or (PaymentStatus.CAPTURED,)
            )
            status = statuses[min(index, len(statuses) - 1)]
            return Success(
                entity=_payment_entity(
                    payment_id,
                    status,
                    self._payment_amounts.get(payment_id, self._behaviour.payment_amount),
                    amount_refunded=self._behaviour.payment_amount_refunded,
                ),
                http_status=200,
            )

    def list_payments(
        self,
        *,
        from_ts: int,
        to_ts: int,
        count: int = MAX_PAYMENTS_PAGE_SIZE,
        skip: int = 0,
    ) -> ProviderResult[PaymentList]:
        """The backfill read. Paginates the scripted window honestly.

        The bounds are enforced here exactly as the real client enforces them, so a
        caller that passes an out-of-range window fails identically against both. A fake
        that were more permissive than the real thing would let the backfill ship with a
        window the provider rejects — and a rejected backfill is an undetected outage.
        """
        if not MIN_PAYMENT_WINDOW_TS <= from_ts <= MAX_PAYMENT_WINDOW_TS:
            return refused_for_invalid_request(
                "list_payments", f"from={from_ts} outside the provider's accepted range"
            )
        if to_ts < from_ts:
            return refused_for_invalid_request("list_payments", "to precedes from")
        if not 1 <= count <= MAX_PAYMENTS_PAGE_SIZE:
            return refused_for_invalid_request(
                "list_payments", f"count={count} outside 1..{MAX_PAYMENTS_PAGE_SIZE}"
            )
        if skip < 0:
            return refused_for_invalid_request("list_payments", f"skip={skip} is negative")

        with self._lock:
            self._window_attempts += 1
            attempt = self._window_attempts
            self._record(
                OPERATION_LIST_PAYMENTS,
                {
                    "window_from": from_ts,
                    "window_to": to_ts,
                    "count": count,
                    "skip": skip,
                },
            )

            if attempt <= self._behaviour.listing_window_unavailable_reads:
                return ServerError(http_status=503, raw='{"error":{"code":"SERVER_ERROR"}}')

            page = self._behaviour.window_payments[skip : skip + count]
            return Success(
                entity=PaymentList(
                    payments=tuple(
                        _payment_entity(
                            payment_id,
                            status,
                            self._behaviour.payment_amount,
                            contact=self._behaviour.window_contact,
                            email=self._behaviour.window_email,
                        )
                        for payment_id, status in page
                    ),
                    page_count=len(page),
                ),
                http_status=200,
            )

    # -- internals ---------------------------------------------------------

    def _record(self, operation: str, arguments: Mapping[str, object]) -> None:
        """Append to the call log. Called with ``_lock`` already held."""
        self._sequence += 1
        self._calls.append(
            RecordedCall(operation=operation, arguments=dict(arguments), sequence=self._sequence)
        )

    def _outcome_for(self, attempt: int) -> CreateOutcome:
        outcomes = self._behaviour.create_outcomes or (CreateOutcome.SUCCESS,)
        return outcomes[min(attempt - 1, len(outcomes) - 1)]

    def __repr__(self) -> str:
        return f"FakeRazorpay(calls={self.call_count}, links={len(self._links)})"


def _notify_key(payment_link_id: str, medium: NotifyMedium) -> str:
    """The attempt-count key: one link, one medium.

    Composed with a character no identifier and no medium contains, so two different pairs
    cannot collide into one count — a collision here would silently satisfy a "no second
    message was sent" assertion.
    """
    return f"{payment_link_id}|{medium.value}"


def _link_entity(request: PaymentLinkRequest) -> PaymentLinkEntity:
    """A link entity derived deterministically from the request.

    Deterministic so that two executions of the same key produce the same ids, which is
    what lets a test assert "reconciliation confirmed the *same* link" rather than
    merely "some link".
    """
    digest = hashlib.sha256(request.reference_id.encode("utf-8")).hexdigest()[:14]
    return PaymentLinkEntity(
        id=f"plink_{digest}",
        short_url=f"{FAKE_SHORT_URL_PREFIX}{digest}",
        status="created",
        reference_id=request.reference_id,
        amount=int(request.amount),
        amount_paid=0,
        currency=request.currency,
        description=request.description,
        expire_by=request.expire_by,
    )


def _payment_entity(
    payment_id: str,
    status: PaymentStatus,
    amount: int,
    *,
    amount_refunded: int | None = None,
    contact: str | None = None,
    email: str | None = None,
) -> PaymentEntity:
    """A payment entity derived from the script.

    ``amount_refunded`` is overridable because that is how a *partial* payment presents on a
    payment entity: ``partially_paid`` is a payment-**link** status and not a valid payment
    status, so a partly-recovered payment is a capture whose refunded amount sits strictly
    between zero and the full amount. Without a way to script that, the "a partial is not
    recovery" test cannot be written against the fake at all.

    ``contact`` and ``email`` default to ``None`` and are opt-in. A read that carries them is
    what makes a backfilled payment actionable, and a fake that supplied them unconditionally
    would hide the case where the provider does not.
    """
    refunded = (
        amount_refunded
        if amount_refunded is not None
        else (amount if status is PaymentStatus.REFUNDED else 0)
    )
    return PaymentEntity(
        id=payment_id,
        status=status.value,
        captured=status is PaymentStatus.CAPTURED,
        amount=amount,
        amount_refunded=refunded,
        currency="INR",
        contact=contact,
        email=email,
    )


def as_provider_client(fake: FakeRazorpay) -> PaymentProviderClient:
    """Return ``fake`` typed as the client protocol.

    A ``Protocol`` is only checked where it is used, so without a site like this the
    fake could drift out of shape and the failure would surface in whichever test
    happened to pass it somewhere typed. This makes the conformance claim explicit and
    checkable, and costs one function call.
    """
    return fake
