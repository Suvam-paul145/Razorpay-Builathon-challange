"""Generated Customer_Access_Token universes, for the three properties about what one is worth.

The token is a credential whose whole design is a bound: one case, one tenant, one expiry, one
submission count. Each of P31, P32 and P33 attacks a different one of those, and each needs a
different shape of input, so this module produces three.

**:func:`customer_tokens` generates a *set* of cases, not a token.** P31 is worded as "every
read returns fields of that one case; a request naming a second case id returns no field of it",
and a universe with one case in it cannot fail that — there is no second case for the token to
reach. So the plan carries several case ids and names which one the token is for, and the
interesting draws are the ones where the subject is not the first: an implementation that
returned "the merchant's only case" would pass a single-case test and fail here.

**:func:`token_operation_sequences` generates operations *and clock steps*, not instants.** P32
is a claim about sequences "crossing both instants" — the expiry and the revocation — and the
failures worth finding live exactly at the boundary. A uniform draw over a week would step past
an expiry almost every time and land on it almost never, and ``>=`` versus ``>`` differ only
there. So the steps are drawn from a small catalogue built around the configured lifetime, the
same discipline ``tests/strategies/clocks.py`` applies to the case lifecycle.

**:func:`token_secrets` generates secrets that look real, plus ones that do not.** P33's input
is "arbitrary token secrets", and the point of an arbitrary one is that a substring search for
it in the audit log must find nothing. A secret drawn only from the real alphabet would miss the
case a careless implementation actually leaks: a *rejected* presentation, where the string came
from an attacker and no row was ever created for it.

**:func:`delay_reason_notes` generates a stranger's free text, adversarially.** P38 is task 41's,
and its claim is a universal negative — nothing about the system's behaviour depends on the note's
contents. A universal negative is only tested by a generator whose draws would *reveal* a
dependency, so this one includes markup, already-escaped markup, SQL, control characters,
right-to-left overrides, four scripts, and text that names in words each of the five things R20.C3
forbids deriving from a note. It is deliberately separate from :func:`plain_notes`; that function
is the write path's generator and the reason both exist is argued at the second one.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum, unique

from hypothesis import strategies as st

from revora.customer.projection import (
    NO_CAUSE_RECORDED,
    PLAIN_LANGUAGE_CAUSE,
    CustomerCaseProjection,
    PromiseView,
)
from revora.customer.tokens import SECRET_LENGTH, TOKEN_ID_LENGTH, TOKEN_PREFIX
from revora.domain.actions import CUSTOMER_VISIBLE_ACTIONS, EXECUTABLE_ACTIONS, CandidateAction
from revora.domain.enums import DelayReason, PromiseStatus
from revora.domain.money import Minor
from revora.persistence.models.customer import (
    DELAY_NOTE_MAX_LENGTH as SCHEMA_DELAY_NOTE_MAX_LENGTH,
)
from revora.platform.config import Configuration

__all__ = [
    "MINTABLE_ACTIONS",
    "TokenOperation",
    "TokenPlan",
    "case_projections",
    "customer_tokens",
    "delay_reason_notes",
    "plain_notes",
    "promise_dates",
    "signal_submissions",
    "token_operation_sequences",
    "token_secrets",
    "undeclared_fields",
    "wire_shaped_garbage",
]

MINTABLE_ACTIONS: tuple[CandidateAction, ...] = tuple(
    sorted(CUSTOMER_VISIBLE_ACTIONS & EXECUTABLE_ACTIONS, key=lambda action: action.value)
)
"""The actions a token can actually accompany.

Derived from the two sets rather than listed, so an action becoming executable or becoming
customer-visible changes what these properties explore without anybody editing this file.
That happened: ``PROMISE_TO_PAY_FOLLOW_UP`` was customer-visible and not executable, so it was
absent here, and R24.C1 moved it into ``EXECUTABLE_ACTIONS`` — which added it to this tuple with
no edit to this file, which is the arrangement working rather than a coincidence."""


@dataclass(frozen=True, slots=True)
class TokenPlan:
    """One token, the case it is for, and the other cases it must not reach."""

    merchant_id: uuid.UUID
    case_ids: tuple[uuid.UUID, ...]
    subject_index: int
    window_offset: timedelta
    """``window_end_at - issued_at``. Drawn across the configured lifetime so the expiry is
    sometimes the lifetime and sometimes the window — R18.C2 says the earlier of the two, and a
    generator that only ever produced one of them would test half the clause."""

    approved_action: CandidateAction
    signing_versions: tuple[int, ...]
    """The configured active signing secrets. More than one is a rotation in progress; the
    highest is the one a mint uses and all of them are tried on verification (R29.C14)."""

    @property
    def subject(self) -> uuid.UUID:
        """The case the token is minted for."""
        return self.case_ids[self.subject_index]

    @property
    def others(self) -> tuple[uuid.UUID, ...]:
        """Every case the token must not reach."""
        return tuple(
            case_id for index, case_id in enumerate(self.case_ids) if index != self.subject_index
        )


def customer_tokens(
    config: Configuration, *, max_cases: int = 4
) -> st.SearchStrategy[TokenPlan]:
    """A universe of cases with one token minted for one of them.

    ``config`` is passed in rather than read here for the reason
    ``tests/strategies/clocks.py`` gives about the cooldown: the window offsets below are built
    *around* ``CUSTOMER_TOKEN_LIFETIME``, so hard-coding a duration would silently stop probing
    the boundary the moment somebody tuned the bound.
    """
    lifetime = config.CUSTOMER_TOKEN_LIFETIME
    offsets = st.sampled_from(
        (
            # Window closes first, so it is the expiry (R18.C2's second term).
            lifetime // 4,
            lifetime - timedelta(seconds=1),
            # Exactly equal: the two terms tie and either answer is the same instant.
            lifetime,
            # Lifetime closes first, so it is the expiry (R18.C2's first term).
            lifetime + timedelta(seconds=1),
            lifetime * 3,
        )
    )
    return st.builds(
        TokenPlan,
        merchant_id=st.uuids(version=4),
        case_ids=st.lists(
            st.uuids(version=4), min_size=1, max_size=max_cases, unique=True
        ).map(tuple),
        subject_index=st.integers(min_value=0),
        window_offset=offsets,
        approved_action=st.sampled_from(MINTABLE_ACTIONS),
        signing_versions=st.lists(
            st.integers(min_value=1, max_value=5), min_size=1, max_size=3, unique=True
        ).map(lambda versions: tuple(sorted(versions))),
    ).map(_normalize_subject)


def _normalize_subject(plan: TokenPlan) -> TokenPlan:
    """Bring ``subject_index`` into range without ``filter``.

    Drawing an unbounded integer and folding it is cheaper for Hypothesis than filtering on a
    condition that depends on another field, and it shrinks towards zero — so a minimal
    counterexample names the first case, which is the one a reader can hold in their head.
    """
    return TokenPlan(
        merchant_id=plan.merchant_id,
        case_ids=plan.case_ids,
        subject_index=plan.subject_index % len(plan.case_ids),
        window_offset=plan.window_offset,
        approved_action=plan.approved_action,
        signing_versions=plan.signing_versions,
    )


@unique
class TokenOperation(StrEnum):
    """What a generated step does to, or with, a token."""

    READ = "READ"
    """Verify and read the projection. Permitted until expiry even at the submission cap."""

    WRITE = "WRITE"
    """Verify and attempt one accepted submission. Bounded three ways at once."""

    ADVANCE = "ADVANCE"
    """Move the clock by the step's delta and do nothing else."""

    REVOKE = "REVOKE"
    """Revoke the case's tokens, as a terminal transition or a suppression would."""


def token_operation_sequences(
    config: Configuration, *, min_size: int = 4, max_size: int = 14
) -> st.SearchStrategy[tuple[tuple[TokenOperation, timedelta], ...]]:
    """Operations paired with clock steps, long enough to cross expiry and revocation.

    ``min_size`` is four because the property is about a sequence that crosses a boundary: a
    shorter run can exhaust the submission cap or reach expiry, but not both, and P32 asserts
    all three of its clauses over one sequence.

    The deltas come from a catalogue keyed to ``CUSTOMER_TOKEN_LIFETIME`` — nothing, a fraction,
    just under, exactly, and just over — so "at or after expiry" is actually landed on rather
    than jumped over. ``REVOKE`` is generated at a low weight: it is absorbing, so a uniform
    draw would spend most examples on sequences that revoke in step one and then assert nothing
    interesting about the cap.
    """
    lifetime = config.CUSTOMER_TOKEN_LIFETIME
    deltas = st.sampled_from(
        (
            timedelta(0),
            lifetime // 8,
            lifetime - timedelta(seconds=1),
            lifetime,
            lifetime + timedelta(seconds=1),
        )
    )
    operations = st.sampled_from(
        (
            TokenOperation.READ,
            TokenOperation.WRITE,
            TokenOperation.WRITE,
            TokenOperation.WRITE,
            TokenOperation.ADVANCE,
            TokenOperation.REVOKE,
        )
    )
    return st.lists(
        st.tuples(operations, deltas), min_size=min_size, max_size=max_size
    ).map(tuple)


def token_secrets() -> st.SearchStrategy[str]:
    """A 22-character base64url secret, as a mint would produce one.

    Generated rather than taken from ``_random_secret`` so a counterexample is reproducible.
    The properties that need a *real* secret mint one; this is for the disclosure search, where
    what matters is that the string is distinctive enough that finding it in an audit field or a
    log line is unambiguous.
    """
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    return st.text(alphabet=alphabet, min_size=SECRET_LENGTH, max_size=SECRET_LENGTH)


def wire_shaped_garbage() -> st.SearchStrategy[str]:
    """A presentation that is not a real token, across every way of not being one.

    Five shapes, and each one exercises a different branch of the parse: no prefix, no
    separator, a handle of the wrong length, a handle with characters outside base32, and a
    fully well-formed token nobody ever minted. All five must answer 404 with an identical body
    (R29.C6), and the last one is the only one that reaches a real lookup — which is exactly
    why the other four must not be allowed to return early.
    """
    handles = st.text(
        alphabet="abcdefghijklmnopqrstuvwxyz234567",
        min_size=TOKEN_ID_LENGTH,
        max_size=TOKEN_ID_LENGTH,
    )
    return st.one_of(
        st.text(min_size=0, max_size=40),
        st.builds(lambda handle: f"{TOKEN_PREFIX}{handle}", handles),
        st.builds(lambda handle: f"{TOKEN_PREFIX}{handle[:-3]}.short", handles),
        st.builds(lambda handle: f"{TOKEN_PREFIX}{handle.upper()}.{'A' * SECRET_LENGTH}", handles),
        st.builds(
            lambda handle, secret: f"{TOKEN_PREFIX}{handle}.{secret}", handles, token_secrets()
        ),
    )


# ---------------------------------------------------------------------------
# Task 40: the read projection and the three write shapes
# ---------------------------------------------------------------------------


def plain_notes() -> st.SearchStrategy[str | None]:
    """Notes across every shape a stranger can type into a text box.

    ``None`` and the empty string are separate draws and they are separate cases: the writer
    turns both into a stored ``NULL``, because ``ix_customer_signal_notes_for_retention`` is
    partial over ``delay_reason_note IS NOT NULL`` and an empty string would put a row with
    nothing to redact into the retention sweep's scanned set for as long as the row exists.

    The markup, control-character and over-length draws are here rather than waiting for task
    41's P38 because the *write* path is what stores them, and the two facts this task owes are
    that the row lands at all and that the stored length respects the bound. Every one of these
    is a value that a hand-rolled length check or a naive escape-on-store would get wrong.
    """
    return st.one_of(
        st.none(),
        st.just(""),
        st.just("   "),
        st.text(max_size=40),
        st.just("<script>alert(1)</script>"),
        st.just("'; DROP TABLE customer_signal; --"),
        st.just("line\u2028break\u0000null"),
        st.just("नमस्ते, मैं ५ तारीख को भुगतान करूँगा"),
        st.text(min_size=501, max_size=900),
    )


def signal_submissions(*, note_strategy: st.SearchStrategy[str | None] | None = None):
    """One of the three declared write shapes, as the request body a customer would send.

    Wire bodies — plain ``dict`` — rather than constructed Pydantic models, because the property
    that matters is what the *endpoint* does with a body, and constructing the model in the test
    would validate the body twice and assert nothing about the first time.

    All three shapes are drawn, and the promise's date is always in the future: a past date is a
    422 and belongs in the rejection tests rather than in a sequence whose point is that accepted
    writes never acquire authority. Returns ``(path_suffix, body)``.
    """
    notes = plain_notes() if note_strategy is None else note_strategy
    delay = st.builds(
        lambda reason, note: (
            "delay-reason",
            {"delay_reason": reason.value}
            if note is None
            else {"delay_reason": reason.value, "note": note},
        ),
        st.sampled_from(tuple(DelayReason)),
        notes,
    )
    partial = st.builds(
        lambda note: (
            "partial-arrangement",
            {} if note is None else {"note": note},
        ),
        notes,
    )
    promise = st.builds(
        # Relative to the *real* clock rather than to a fixed base instant, because the guard the
        # writer applies compares the date against the submission instant — so a fixed 2025 base
        # would have been a future date when this generator was written and a 422 the following
        # year. The offset is what is generated; the origin is whenever the example runs.
        lambda days: (
            "promise",
            {"promise_date": (datetime.now(UTC) + timedelta(days=days)).isoformat()},
        ),
        st.integers(min_value=2, max_value=6),
    )
    return st.one_of(delay, partial, promise)


def undeclared_fields() -> st.SearchStrategy[str]:
    """Field names outside every declared write shape, including the three R22.C1 names.

    ``amount``, ``instalment_count`` and ``schedule`` are drawn explicitly and the rest is
    arbitrary, because R22.C1 is a claim about those three specifically and ``extra="forbid"`` is
    a claim about all of them. A generator that only produced arbitrary names would pass against
    an implementation that special-cased the three, and a generator that only produced the three
    would pass against one that listed them and forgot the general rule.
    """
    return st.one_of(
        st.sampled_from(("amount", "instalment_count", "schedule")),
        st.sampled_from(("currency", "case_id", "merchant_id", "token_id", "nickname")),
        st.from_regex(r"\A[a-z][a-z_]{2,12}\Z", fullmatch=True).filter(
            lambda name: name not in {"delay_reason", "note", "promise_date"}
        ),
    )


def case_projections() -> st.SearchStrategy[CustomerCaseProjection]:
    """An arbitrary :class:`CustomerCaseProjection`, across every field's range.

    Built directly rather than read out of a database, because P34 is a claim about the
    *disclosure surface* — the key set — and a database contributes nothing to it while making
    the property slow enough to run at a tenth of the examples. The ranges are chosen to cross the
    two boundaries that exist: ``pay_url`` and ``promise`` are each independently present or
    absent, so all four combinations are drawn, and ``signals_remaining`` reaches zero.

    ``amount`` is drawn as an integer over a range spanning four orders of magnitude, including
    the single minor unit and an amount past a crore, because the same eight keys must come back
    whatever the figure is — and because P60's "every currency figure is an integer minor-unit
    count" is asserted over the same draws.
    """
    return st.builds(
        CustomerCaseProjection,
        merchant_display_name=st.text(min_size=1, max_size=60),
        amount=st.integers(min_value=1, max_value=10_000_000_00).map(Minor),
        currency=st.sampled_from(("INR", "USD", "EUR", "GBP", "AED", "XTS")),
        reason=st.sampled_from(
            (*PLAIN_LANGUAGE_CAUSE.values(), NO_CAUSE_RECORDED)
        ),
        pay_url=st.one_of(st.none(), st.just("https://rzp.io/i/AbCdEfG")),
        window_end_at=st.datetimes(
            min_value=datetime(2025, 1, 1), max_value=datetime(2030, 1, 1)
        ).map(lambda moment: moment.replace(tzinfo=UTC)),
        promise=st.one_of(
            st.none(),
            st.builds(
                PromiseView,
                promise_date=st.datetimes(
                    min_value=datetime(2025, 1, 1), max_value=datetime(2030, 1, 1)
                ).map(lambda moment: moment.replace(tzinfo=UTC)),
                status=st.sampled_from(tuple(PromiseStatus)),
            ),
        ),
        signals_remaining=st.integers(min_value=0, max_value=5),
    )


# ---------------------------------------------------------------------------
# Task 41: Delay_Reason_Note content (P38)
# ---------------------------------------------------------------------------


_INJECTION_NOTES: tuple[str, ...] = (
    # Markup. The first is the canonical XSS payload; the second and third are the ones an escape
    # that handled only the angle brackets would let through, because both close an attribute value
    # instead of opening a tag.
    "<script>alert(1)</script>",
    '" onmouseover="alert(1)" x="',
    "' onfocus='alert(1)' autofocus='",
    "<img src=x onerror=alert(1)>",
    # Already-escaped text. It must survive as *text* — a stranger who literally typed `&lt;` wrote
    # four characters, and a reader must see four characters rather than a `<` somebody decoded.
    "&lt;script&gt;",
    "&amp;",
    # SQL. Inert because every write in the module binds parameters, and generated here anyway
    # because "inert" is the claim and a claim with no adversarial input behind it is an assertion.
    "'; DROP TABLE customer_signal; --",
    "1' OR '1'='1",
    # Values that name the four things R20.C3 forbids deriving from a note. If any derivation ever
    # reads the text, these are the draws that make it visible: a note *saying* the customer
    # disputes the charge must not produce a hard stop, and a note naming an amount must not
    # produce a currency figure.
    "DISPUTES_THE_CHARGE",
    "NO_LONGER_WANTS_THE_ORDER",
    "delay_reason: BANK_OR_CARD_PROBLEM",
    "promise_date=2030-01-01T00:00:00Z",
    "amount 249900 INR, instalment_count 3",
    "I will pay ₹2,499.00 on the 5th",
    # Control characters, all storable and all retained. R20.C3 forbids deriving a judgement about
    # the contents, so stripping these would be exactly that — and each one is a real thing a paste
    # from a phone keyboard or a word processor puts in a text box.
    "line\u2028break",
    "carriage\rreturn\nnewline",
    "tab\tseparated",
    "bell\x07and\x1bescape",
    "right\u202eto\u202cleft",
    "zero\u200bwidth\ufeffspace",
    # The one character PostgreSQL TEXT cannot hold, in the composition that produced a 503 before
    # the writer removed it. Kept as a fixed draw so the regression stays covered by name.
    "line\u2028break\x00null",
    "\x00",
    # Whitespace-only, in four flavours. All become a stored NULL, which is what keeps the partial
    # retention index small.
    "",
    "   ",
    "\t\n ",
    "\u00a0\u2003",
    # Non-ASCII, in scripts with different byte lengths and different grapheme behaviour, because a
    # length bound counted in bytes rather than characters truncates all three differently and only
    # the emoji draw splits a surrogate pair.
    "नमस्ते, मैं ५ तारीख को भुगतान करूँगा",
    "口座に残高がありませんでした",
    "sí, pagaré mañana — perdón",
    "🙏🏽 salary delayed 😞",
)
"""Fixed note draws, each covering a case a generated string would reach rarely or never.

Named constants rather than inline in the strategy so a counterexample shrinks to one of them and
a reader can see *which* adversarial shape failed, rather than to a minimal random string that
happens to contain a ``<``."""


def delay_reason_notes(*, max_over_length: int = 1_400) -> st.SearchStrategy[str | None]:
    """Notes across every shape P38 asserts over: whitespace, markup, control, non-ASCII, long.

    Distinct from :func:`plain_notes`, which task 40 wrote for the *write path* and which draws the
    handful of shapes needed to show a row lands and respects its bound. This one is the adversarial
    generator: it exists because P38's claim is that *nothing* is derived from the contents, and a
    generator whose interesting draws are rare makes that claim about a sample rather than about the
    space. Both are kept — replacing ``plain_notes`` with this one would make every task-40 property
    slower for no gain, since none of them reads the note at all.

    ``None`` is a draw and is the *reference* case for P38: every other draw's derived reason, hard
    stop, promise date, arrangement flag and currency figures are compared against what the same
    submission produces with the note absent. A generator without it would have nothing to compare
    against.

    Over-length draws go well past ``DELAY_NOTE_MAX_LENGTH`` rather than one character past it, and
    they are drawn as *text* rather than as ``"a" * n``: truncating at a character index inside a
    multi-byte grapheme is the failure mode, and a run of ASCII cannot find it. The boundary itself
    is covered by the exact-length draws below, which is where ``<=`` and ``<`` differ.

    Args:
        max_over_length: the longest note generated. Comfortably past the 500-character column
            check, so the truncation path is exercised rather than approached.
    """
    limit = SCHEMA_DELAY_NOTE_MAX_LENGTH
    return st.one_of(
        st.none(),
        st.sampled_from(_INJECTION_NOTES),
        # Ordinary text, including the empty string, which the writer turns into a stored NULL.
        st.text(max_size=80),
        # The boundary, from both sides and on it.
        st.text(min_size=limit - 1, max_size=limit - 1),
        st.text(min_size=limit, max_size=limit),
        st.text(min_size=limit + 1, max_size=limit + 1),
        # Well past it, in mixed scripts so the truncation index lands mid-grapheme.
        st.text(
            alphabet=st.characters(codec="utf-8"),
            min_size=limit + 2,
            max_size=max_over_length,
        ),
        # An over-length note built by repeating an adversarial fragment, so a truncation that cut
        # a payload in half is reachable — ``"<scrip"`` is the shape that would break an escape
        # applied after truncation rather than before.
        st.builds(
            lambda fragment, times: fragment * times,
            st.sampled_from(_INJECTION_NOTES),
            st.integers(min_value=30, max_value=90),
        ),
    )


def promise_dates(
    *,
    relative_to: datetime,
    window_end_at: datetime,
    config: Configuration,
) -> st.SearchStrategy[datetime]:
    """Promise_Dates spanning every boundary R23's clamp has, plus the far past and far future.

    Task 44.4's generator. Uniform dates over a range would spend almost every example in the
    ordinary interior and almost none at the four places the clamp changes answer, so this is a
    union of boundary-anchored offsets and a broad interval rather than one interval — the same
    argument :func:`~tests.strategies.clocks.boundary_deltas` makes for the lifecycle clock.

    The boundaries it deliberately lands on, and what each is for:

    * **``relative_to`` exactly, and just either side of it.** ``promise_date > recorded_at`` is a
      ``CHECK`` on ``promise_to_pay``, so ``==`` must be refused and ``+1μs`` must not be refused
      *for that reason*. A generator that never produced the equal case would leave the degenerate
      guard of R23.C2 untested at the only value it exists for.
    * **``relative_to + PROMISE_MIN_LEAD_TIME``, and either side.** The lead-time boundary is
      inclusive at the bound, so all three of ``-1μs``, ``==`` and ``+1μs`` have distinct correct
      answers and only a generator aimed at the bound finds them.
    * **``window_end_at`` exactly, and either side.** R23.C5 is "at or after", so ``==`` escalates
      and ``-1μs`` does not. This is the single most valuable point in the whole space: getting the
      comparison inclusive-vs-exclusive wrong here is the difference between an escalation and a
      ``follow_up_at`` the schema refuses.
    * **``window_end_at - PROMISE_WINDOW_SAFETY_MARGIN - PROMISE_FOLLOW_UP_OFFSET``.** The date at
      which the clamp changes which of its two arguments wins. Either side of it, one branch of
      ``min`` is taken and the other is not, and ``clamped`` flips.
    * **The far past and the far future** — years, not seconds. R23 has no upper bound on a
      Promise_Date and Property 41 asserts the window is unmoved *for any date*, so a generator
      capped near the window would be asserting the property only where it is easy.

    ``window_end_at`` is passed in rather than derived from ``relative_to`` and
    ``RECOVERY_WINDOW_DURATION``, because a caller exploring R23.C6 needs a window with less than
    ``PROMISE_WINDOW_SAFETY_MARGIN`` left — which is a window that opened long before
    ``relative_to`` and cannot be produced by adding the configured duration to it.

    Every value is timezone-aware UTC. A naive datetime would be a different test: ``plan_promise``
    calls ``ensure_utc`` on its inputs, and generating naive values here would exercise that call
    rather than the clamp, which :func:`tests.properties.test_promise` asserts separately.
    """
    tick = timedelta(microseconds=1)
    lead = config.PROMISE_MIN_LEAD_TIME
    margin = config.PROMISE_WINDOW_SAFETY_MARGIN
    offset = config.PROMISE_FOLLOW_UP_OFFSET
    pivot = window_end_at - margin - offset

    anchors: tuple[datetime, ...] = (
        relative_to,
        relative_to - tick,
        relative_to + tick,
        relative_to + lead - tick,
        relative_to + lead,
        relative_to + lead + tick,
        window_end_at - tick,
        window_end_at,
        window_end_at + tick,
        pivot - tick,
        pivot,
        pivot + tick,
    )
    # A broad interval either side of the window, so the interior is covered and so the far past
    # and far future are reachable. Expressed in seconds and converted, rather than as a
    # ``datetimes`` strategy with bounds, because the bounds that matter here are relative to two
    # instants a caller chose and ``datetimes`` takes absolute ones.
    spread = st.integers(
        min_value=-(3 * 365 * 24 * 3600), max_value=3 * 365 * 24 * 3600
    ).map(lambda seconds: relative_to + timedelta(seconds=seconds))

    return st.one_of(st.sampled_from(anchors), spread)
