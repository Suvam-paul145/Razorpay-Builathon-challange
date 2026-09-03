"""Tasks 39.5 and 40.5. One token reaches one case, spends a bounded number of writes, leaks
nothing, and discloses exactly the fields the projection declares.

The Customer_Access_Token is the only credential in Revora that is not backed by a session, and
its whole design is an argument about what holding one is worth: one case's amount and one
recovery opportunity. These properties are that argument in executable form.

**Why three tiers in one file.** The design's Testing Strategy table assigns P31, P33 and the
read half of P32 to ``model`` — the service against in-memory fakes — and P32's row-lock bound
to ``pg``. A file-level marker would drag the wire-format and constant-time facts, which need
nothing at all, into the slow selection; so the markers are per test and the tiers are stated in
the section headers below.

* ``pure`` — the wire form round-trips, and the verification loop still has the shape that makes
  it constant time. The second one is a source-level assertion and it is the most valuable test
  in this file relative to its size: the loop is three lines and the four ways to break it are
  all a one-character edit.
* ``model`` — P31, P33, and the whole of P32 except its concurrency clause. These are claims
  about scoping and disclosure, decided entirely above the repository, so a real database would
  make them slower and no stronger.
* ``pg`` — P32's durable bound under genuine concurrency, and the revoke-then-mint ordering
  against the real partial unique index. Neither is stateable against a fake: the first needs two
  overlapping transactions and the second needs the index to actually exist.

**Task 40.5 adds Property 34 at the end of the file**, in the ``model`` tier the design assigns it,
plus the ``pure`` fact about the plain-language cause table. P34 is a claim about the *disclosure
surface* of the read projection, decided entirely by a dataclass declaration and one serializer, so
a database would make it slower and no stronger — which is the same argument that put P31 and P33
here. It lives beside them rather than in a file of its own because "what is one token worth" and
"what does that token let somebody read" are two halves of one answer.
"""

from __future__ import annotations

import ast
import inspect
import io
import json
import logging
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict
from dataclasses import fields as dataclass_fields
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from hypothesis import assume, given, settings
from sqlalchemy import Engine, text
from sqlalchemy.orm import sessionmaker

from revora.api.routers.customer import _render_amount
from revora.audit.events import (
    CREDENTIAL_UNAVAILABLE,
    CUSTOMER_TOKEN_EXPIRED,
    CUSTOMER_TOKEN_ISSUED,
    CUSTOMER_TOKEN_KEY_RETIRED,
    CUSTOMER_TOKEN_REJECTED,
)
from revora.customer import projection as projection_module
from revora.customer import tokens as tokens_module
from revora.customer.projection import (
    NO_CAUSE_RECORDED,
    PLAIN_LANGUAGE_CAUSE,
    PROJECTION_FIELDS,
    CustomerCaseProjection,
    as_document,
)
from revora.customer.tokens import (
    SECRET_LENGTH,
    TOKEN_ID_LENGTH,
    TOKEN_PREFIX,
    MintedToken,
    TokenIssueFailure,
    TokenRejection,
    TokenService,
    wire_token,
)
from revora.domain.actions import CandidateAction
from revora.domain.enums import RiskCause, TokenRevocationReason
from revora.persistence.models.customer import SECRET_HASH_BYTES
from revora.persistence.repositories.customer import CustomerAccessTokenRepository
from revora.persistence.repositories.session import tenant_transaction
from revora.platform.config import default_configuration
from revora.platform.logging import configure_logging, get_logger
from tests.fakes.customer import (
    FakeTokenStore,
    RecordingAuditSink,
    installed_signing_secrets,
)
from tests.pg_support import insert_merchant
from tests.strategies.customer import (
    TokenOperation,
    TokenPlan,
    case_projections,
    customer_tokens,
    token_operation_sequences,
    token_secrets,
    wire_shaped_garbage,
)

_CONFIG = default_configuration()
_START = datetime(2025, 6, 1, 12, 0, 0, tzinfo=UTC)


def _service(store: FakeTokenStore, audit: RecordingAuditSink) -> TokenService:
    return TokenService(store, audit, _CONFIG)


def _mint(
    service: TokenService, plan: TokenPlan, case_id: uuid.UUID, *, moment: datetime = _START
) -> MintedToken:
    """Mint for one case, failing the test loudly rather than returning ``None``."""
    outcome = service.mint(
        plan.merchant_id,
        case_id=case_id,
        window_end_at=moment + plan.window_offset,
        approved_action=plan.approved_action,
        moment=moment,
    )
    assert outcome.token is not None, f"mint refused with {outcome.failure}"
    return outcome.token


# ---------------------------------------------------------------------------
# `pure` — the wire form, and the shape that makes verification constant time
# ---------------------------------------------------------------------------


@pytest.mark.pure
@given(secret=token_secrets())
@settings(max_examples=200)
def test_the_wire_form_round_trips_and_carries_the_declared_widths(secret: str) -> None:
    """The wire form parses back to its two halves, at the declared widths.

    Not decoration. ``token_id`` is 26 fixed-width characters and that is the *only* reason
    ``HMAC(key, token_id ‖ secret)`` cannot be made to collide by moving the boundary between
    the two halves — with a variable-width handle, ``("ab", "cd")`` and ``("a", "bcd")`` sign
    the same bytes. So the widths are a property of the credential and not a formatting choice.
    """
    token_id = tokens_module._random_token_id()
    assert len(token_id) == TOKEN_ID_LENGTH
    assert len(secret) == SECRET_LENGTH

    presented = wire_token(token_id, secret)
    assert presented.startswith(TOKEN_PREFIX)

    parsed = tokens_module._parse(presented)
    assert parsed.well_formed
    assert (parsed.token_id, parsed.secret) == (token_id, secret)

    # A header value is accepted unchanged, so no caller has to re-implement the split.
    from_header = tokens_module._parse(f"Bearer {presented}")
    assert (from_header.token_id, from_header.secret) == (token_id, secret)


@pytest.mark.pure
def test_a_malformed_presentation_becomes_a_decoy_rather_than_an_early_return() -> None:
    """Every unparseable presentation yields a decoy of the right shape (R29.C6).

    The decoy is what makes "this is not even a token" cost the same as "this is a token I do
    not have". Returning early on a malformed input would let an attacker learn the wire format
    from response timing before guessing a single byte of a real handle — a disclosure the
    status-code table is careful to prevent and a timing side channel would hand back.
    """
    for presented in ("", "nonsense", "rvc_nodot", "rvc_TOOSHORT.x", f"{TOKEN_PREFIX}."):
        parsed = tokens_module._parse(presented)
        assert not parsed.well_formed, presented
        assert len(parsed.token_id) == TOKEN_ID_LENGTH, presented
        assert len(parsed.secret) == SECRET_LENGTH, presented

    assert len(tokens_module._ABSENT_ROW_HASH) == SECRET_HASH_BYTES, (
        "the decoy hash a missing row compares against is not the length of a real one, so a "
        "missing row does measurably less work than a present one"
    )


@pytest.mark.pure
def test_verification_compares_every_secret_and_never_breaks_early() -> None:
    """R18.C4 and R29.C6, asserted on the source of the loop rather than by timing it.

    Timing assertions are the obvious approach and they are the wrong one here: a wall-clock
    comparison on a shared CI runner is noise, so it would either be flaky or be loose enough to
    pass an implementation with a ``break`` in it. What actually makes the loop constant time is
    four structural facts, and each is a one-character edit away from being false:

    1. the loop runs over **every** configured secret and does not filter by ``key_version``;
    2. it accumulates with ``|=`` rather than assigning, so a later non-match cannot clear an
       earlier match and an earlier match cannot let a later comparison be skipped;
    3. there is no ``break`` and no ``return`` inside it;
    4. the missing-row case substitutes a decoy hash instead of skipping the comparison.

    So the loop is read out of the AST and all four are asserted. This is the test that fails
    when somebody "optimizes" the verification, which is the change most likely to be made and
    least likely to be noticed — a ``break`` makes every existing behavioural test still pass.
    """
    source = inspect.getsource(TokenService.verify)
    tree = ast.parse(inspect.cleandoc(source))
    loops = [node for node in ast.walk(tree) if isinstance(node, ast.For)]
    assert len(loops) == 1, (
        f"expected exactly one loop in `verify`, found {len(loops)}. The comparison loop is the "
        "constant-time guarantee; a second loop beside it is a second thing to reason about"
    )
    loop = loops[0]

    disqualifying = [
        node
        for node in ast.walk(loop)
        if isinstance(node, ast.Break | ast.Return | ast.Continue)
    ]
    assert not disqualifying, (
        "the comparison loop contains an early exit, so the time it takes now depends on which "
        "signing secret matched — which is exactly the distinction R29.C6 forbids the response "
        "from making and a timing channel would restore"
    )

    augmented = [node for node in ast.walk(loop) if isinstance(node, ast.AugAssign)]
    assert len(augmented) == 1 and isinstance(augmented[0].op, ast.BitOr), (
        "the loop no longer accumulates its result with `|=`. Plain assignment means the last "
        "secret's answer wins, so a token minted under an older active secret stops verifying "
        "the moment a newer one is configured"
    )
    assert "compare_digest" in source, (
        "the per-secret comparison is not `hmac.compare_digest`, so it is a byte-by-byte "
        "comparison that reveals how much of a wrong secret was right"
    )
    assert "_ABSENT_ROW_HASH" in source, (
        "the missing-row case no longer substitutes a decoy hash, so a handle that matches no "
        "row is answered faster than one that does"
    )


# ---------------------------------------------------------------------------
# `model` — Property 31
# ---------------------------------------------------------------------------


@pytest.mark.model
@given(plan=customer_tokens(_CONFIG))
def test_p31_a_token_reaches_exactly_the_case_it_was_minted_for(plan: TokenPlan) -> None:
    """Feature: Customer Response Loop. Property 31 — for any set of Recovery_Cases and a
    Customer_Access_Token minted for one of them, every read through that token returns fields
    of that one Recovery_Case, and a request naming any other Recovery_Case identifier returns
    no field of it.

    The generated universe holds up to four cases and the token belongs to one of them, chosen
    by the generator rather than fixed at the first. That matters: an implementation that
    answered "this merchant's only case" would pass a single-case test, and one that answered
    "this merchant's first case" would pass a test whose subject was always index zero.

    Every other case gets a token of its own, so the assertion is not merely "the other cases
    are unreachable" — it is "the other cases exist, are reachable by *their* tokens, and are
    still not reachable by this one". A store with one row in it cannot tell those apart.
    """
    store = FakeTokenStore()
    audit = RecordingAuditSink()
    with installed_signing_secrets(*plan.signing_versions):
        service = _service(store, audit)
        minted = {case_id: _mint(service, plan, case_id) for case_id in plan.case_ids}
        subject = minted[plan.subject]
        assert subject.wire_token is not None

        outcome = service.verify(plan.merchant_id, subject.wire_token, moment=_START)

    assert outcome.verified and outcome.token is not None, (
        f"a token minted moments ago did not verify: {outcome.rejection}"
    )
    verified = outcome.token

    assert verified.case_id == plan.subject
    assert verified.merchant_id == plan.merchant_id
    assert verified.token_id == subject.token_id
    assert verified.authorizes(plan.subject)

    # R18.C5 / R29.C2: naming another case returns no field of it. Asserted two ways — the
    # authorization answer, and the absence of every other id from the whole verified value, so
    # a future field that quietly carried a second case id would fail here rather than in
    # whatever endpoint eventually serialized it.
    rendered = repr(sorted(str(value) for value in asdict(verified).values()))
    for other in plan.others:
        assert not verified.authorizes(other), (
            f"the token for {plan.subject} authorizes {other}; one token is one case's worth of "
            "authority and nothing else (R18.C10)"
        )
        assert str(other) not in rendered, (
            f"the verified token carries {other}, a case it does not grant access to"
        )


@pytest.mark.model
@given(plan=customer_tokens(_CONFIG))
def test_p31_a_write_through_one_token_mutates_only_that_case(plan: TokenPlan) -> None:
    """Feature: Customer Response Loop. Property 31, write half — every write through a
    Customer_Access_Token mutates that one Recovery_Case's token state and no other's.

    The write available on the token itself is the accepted-submission count, which is the
    durable bound behind every other write (R18.C9). So the assertion is that spending it moves
    exactly one row's counter, and that revoking the subject's access — as a terminal transition
    or a suppression does — leaves every other case's token live.

    Revocation is included here rather than in P32 because it is the one write that operates on
    a *case* rather than on a token, which makes it the one most likely to be written with the
    case filter missing. A bulk update with no ``case_id`` predicate would end every customer's
    access for that merchant at once, and nothing else in this file would notice.
    """
    store = FakeTokenStore()
    audit = RecordingAuditSink()
    with installed_signing_secrets(*plan.signing_versions):
        service = _service(store, audit)
        minted = {case_id: _mint(service, plan, case_id) for case_id in plan.case_ids}

        allocated = service.accept_submission(plan.merchant_id, minted[plan.subject].token_id)
        assert allocated == 1

        for other in plan.others:
            row = store.by_token_id(plan.merchant_id, minted[other].token_id)
            assert row is not None
            assert int(row.accepted_submission_count) == 0, (
                f"a submission counted against {plan.subject} also moved {other}'s counter"
            )

        revoked = service.revoke(
            plan.merchant_id,
            plan.subject,
            reason=TokenRevocationReason.CASE_TERMINAL,
            moment=_START,
        )
        assert revoked == 1

        for other in plan.others:
            row = store.by_token_id(plan.merchant_id, minted[other].token_id)
            assert row is not None
            assert row.revoked_at is None, (
                f"revoking {plan.subject} also revoked {other}; a bulk revoke without the "
                "case predicate ends every customer's access for the merchant at once"
            )


@pytest.mark.model
@given(plan=customer_tokens(_CONFIG), presented=wire_shaped_garbage())
def test_p31_a_token_that_is_not_one_returns_no_field_of_any_case(
    plan: TokenPlan, presented: str
) -> None:
    """Feature: Customer Response Loop. Property 31 — a presentation that is not a live token
    returns no field of any Recovery_Case, whichever of the ways of not being one it is.

    Five shapes, one answer. R29.C6 requires a failed signature and an unknown handle to be
    indistinguishable in status and body, and the generator adds three more that a careless
    parse would answer differently: no prefix, no separator, and a handle outside base32. All of
    them are 404 with no token attached to the outcome.
    """
    assume(presented.strip() != "")
    store = FakeTokenStore()
    audit = RecordingAuditSink()
    with installed_signing_secrets(*plan.signing_versions):
        service = _service(store, audit)
        _mint(service, plan, plan.subject)

        outcome = service.verify(plan.merchant_id, presented, moment=_START)

    assert not outcome.verified
    assert outcome.token is None
    assert outcome.rejection is TokenRejection.NOT_FOUND
    assert outcome.status_code == 404, (
        "a rejected presentation was answered with something other than 404, which discloses "
        "which of the ways of being wrong it was (R29.C6)"
    )
    assert CUSTOMER_TOKEN_REJECTED in audit.event_types()


@pytest.mark.model
@given(plan=customer_tokens(_CONFIG))
def test_p31_a_token_presented_under_another_tenant_finds_nothing(plan: TokenPlan) -> None:
    """R17.C2 extended to the token credential (R29.C2), as the 404 path.

    A token is scoped by ``(merchant_id, token_id)`` and there is deliberately no repository
    function that finds one without a tenant. So presenting a perfectly valid token under a
    different merchant reaches no row and takes the same path as a forgery — which is the right
    answer, and worth asserting because the tempting "optimization" is a global lookup on a
    handle that is, after all, 128 bits of randomness.
    """
    store = FakeTokenStore()
    audit = RecordingAuditSink()
    stranger = uuid.uuid4()
    assume(stranger != plan.merchant_id)
    with installed_signing_secrets(*plan.signing_versions):
        service = _service(store, audit)
        minted = _mint(service, plan, plan.subject)
        assert minted.wire_token is not None

        outcome = service.verify(stranger, minted.wire_token, moment=_START)

    assert outcome.rejection is TokenRejection.NOT_FOUND
    assert outcome.status_code == 404


# ---------------------------------------------------------------------------
# `model` — Property 32, everything except the concurrency clause
# ---------------------------------------------------------------------------


@pytest.mark.model
@given(plan=customer_tokens(_CONFIG), operations=token_operation_sequences(_CONFIG))
def test_p32_writes_are_bounded_by_the_cap_the_expiry_and_the_revocation(
    plan: TokenPlan, operations: tuple[tuple[TokenOperation, timedelta], ...]
) -> None:
    """Feature: Customer Response Loop. Property 32 — over any sequence of reads and writes
    crossing the expiry and revocation instants, accepted writes never exceed
    ``CUSTOMER_TOKEN_MAX_SUBMISSIONS``, no operation is accepted at or after the expiry instant,
    and none is accepted after revocation.

    Three bounds over one sequence, which is the point of generating a sequence at all: each is
    easy to hold alone and the failures are in their interaction. A cap enforced by reading the
    counter and then incrementing it passes a test that only counts writes; an expiry checked
    with ``>`` instead of ``>=`` passes every test that never lands exactly on the instant; and
    a revocation honoured on the read path but not the write path passes both of those.

    ``READ`` is asserted separately and in the opposite direction: R18.C9 says the projection
    keeps being served until expiry *even at the submission cap*, so a read refused because the
    cap was reached would be a bug in the other direction — the customer would lose the page
    telling them what they owe as a consequence of having explained themselves five times.
    """
    store = FakeTokenStore()
    audit = RecordingAuditSink()
    maximum = _CONFIG.CUSTOMER_TOKEN_MAX_SUBMISSIONS

    with installed_signing_secrets(*plan.signing_versions):
        service = _service(store, audit)
        minted = _mint(service, plan, plan.subject)
        assert minted.wire_token is not None
        expires_at = minted.expires_at

        instant = _START
        revoked_at: datetime | None = None
        accepted = 0
        accepted_at_or_after_expiry = 0
        accepted_after_revocation = 0
        served_after_the_end = 0
        reads_served_at_the_cap = 0

        for operation, delta in operations:
            instant = instant + delta

            if operation is TokenOperation.ADVANCE:
                continue

            if operation is TokenOperation.REVOKE:
                service.revoke(
                    plan.merchant_id,
                    plan.subject,
                    reason=TokenRevocationReason.CASE_TERMINAL,
                    moment=instant,
                )
                if revoked_at is None:
                    revoked_at = instant
                continue

            dead = revoked_at is not None or instant >= expires_at
            outcome = service.verify(plan.merchant_id, minted.wire_token, moment=instant)

            if outcome.verified:
                if dead:
                    served_after_the_end += 1
                if operation is TokenOperation.READ:
                    if outcome.token is not None and service.submissions_remaining(
                        outcome.token
                    ) == 0:
                        reads_served_at_the_cap += 1
                    continue
                if service.accept_submission(plan.merchant_id, minted.token_id) is not None:
                    accepted += 1
                    if instant >= expires_at:
                        accepted_at_or_after_expiry += 1
                    if revoked_at is not None:
                        accepted_after_revocation += 1
            else:
                assert dead, (
                    f"a live token was refused at {instant.isoformat()} with "
                    f"{outcome.rejection}; it expires at {expires_at.isoformat()} and was not "
                    "revoked, so nothing had ended"
                )
                expected = (
                    TokenRejection.REVOKED if revoked_at is not None else TokenRejection.EXPIRED
                )
                assert outcome.rejection is expected
                assert outcome.status_code == 410, (
                    "a customer holding a dead link must be told it is dead (R18.C7, R18.C8); "
                    f"got {outcome.status_code}"
                )

    assert accepted <= maximum, (
        f"{accepted} writes were accepted against a cap of {maximum}. The counter is the durable "
        "bound of R19.C5 — the rate limiter is a coarse flood guard and cannot substitute for it"
    )
    assert accepted_at_or_after_expiry == 0, (
        f"{accepted_at_or_after_expiry} writes were accepted at or after the expiry instant. "
        "R18.C7 is `at or after`, so a check written with `>` passes every test that never "
        "lands exactly on the instant and fails on the one that does"
    )
    assert accepted_after_revocation == 0, (
        f"{accepted_after_revocation} writes were accepted after revocation. A revoked token is "
        "the mechanism behind R18.C8 and R21.C10 — a customer who disputed a charge must not be "
        "able to keep writing to the case"
    )
    assert served_after_the_end == 0
    assert reads_served_at_the_cap >= 0  # counted for the anti-vacuity test below


@pytest.mark.model
def test_p32_the_cap_is_reachable_and_a_read_survives_it() -> None:
    """Property 32's anti-vacuity, and R18.C9's second clause, deterministically.

    The generated test above proves that nothing exceeds the three bounds. This one proves the
    bounds are reachable at all — an implementation that refused every write would satisfy every
    assertion there — and that reaching the submission cap does **not** stop the read.

    R18.C9 is explicit about the asymmetry: at the cap, no further signal is accepted and the
    projection is still served until expiry. The customer who has explained themselves five
    times must not lose the page telling them what they owe as a consequence.
    """
    merchant_id = uuid.uuid4()
    case_id = uuid.uuid4()
    store = FakeTokenStore()
    audit = RecordingAuditSink()
    maximum = _CONFIG.CUSTOMER_TOKEN_MAX_SUBMISSIONS

    with installed_signing_secrets(1):
        service = _service(store, audit)
        outcome = service.mint(
            merchant_id,
            case_id=case_id,
            window_end_at=_START + timedelta(days=30),
            approved_action=CandidateAction.PAYMENT_LINK,
            moment=_START,
        )
        assert outcome.token is not None
        minted = outcome.token
        assert minted.wire_token is not None
        assert minted.expires_at == _START + _CONFIG.CUSTOMER_TOKEN_LIFETIME, (
            "a window well past the lifetime should leave the lifetime as the expiry (R18.C2)"
        )

        allocated = [
            service.accept_submission(merchant_id, minted.token_id)
            for _ in range(maximum + 3)
        ]
        assert allocated == [*range(1, maximum + 1), None, None, None], (
            f"the counter did not stop at {maximum}: {allocated}"
        )

        just_inside = minted.expires_at - timedelta(seconds=1)
        at_the_cap = service.verify(merchant_id, minted.wire_token, moment=just_inside)
        assert at_the_cap.verified and at_the_cap.token is not None
        assert service.submissions_remaining(at_the_cap.token) == 0
        assert at_the_cap.status_code == 200, (
            "the read stopped being served once the submission cap was reached; R18.C9 keeps "
            "serving the projection until expiry"
        )

        exactly_at_expiry = service.verify(
            merchant_id, minted.wire_token, moment=minted.expires_at
        )
        assert exactly_at_expiry.rejection is TokenRejection.EXPIRED, (
            "the expiry instant itself was served. R18.C7 says `at or after`"
        )
        assert exactly_at_expiry.status_code == 410
        assert CUSTOMER_TOKEN_EXPIRED in audit.event_types()


@pytest.mark.model
@given(plan=customer_tokens(_CONFIG))
def test_a_live_token_is_reused_and_an_expired_one_is_superseded(plan: TokenPlan) -> None:
    """R18.C14 and R18.C2's "never extended", which is where the partial index bites.

    Two claims that look like one. A further approved action on a case holding a live token
    **reuses** it with its expiry untouched, because a second live token doubles the credential
    surface and grants nothing extra. But a case holding an *expired* token gets a replacement,
    and the predecessor is revoked ``EXPIRED_SUPERSEDED`` in the same transaction — because
    ``one_live_token_per_case`` is partial over ``revoked_at IS NULL`` and expiry cannot be in
    the predicate, so an expired-but-unrevoked row is still in the index and an insert beside it
    fails. The fake raises the same ``IntegrityError`` the index would, so a mint that forgot to
    revoke first fails here too.

    The reuse path returns no wire token, and that is a consequence rather than an omission: the
    secret has no reversible representation anywhere (R18.C3), so a token minted in an earlier
    transaction can never have its URL rebuilt. Workable because the action that reuses one is a
    resend of the link the customer already holds.
    """
    store = FakeTokenStore()
    audit = RecordingAuditSink()
    with installed_signing_secrets(*plan.signing_versions):
        service = _service(store, audit)
        first = _mint(service, plan, plan.subject)

        reuse = service.mint(
            plan.merchant_id,
            case_id=plan.subject,
            window_end_at=_START + plan.window_offset,
            approved_action=plan.approved_action,
            moment=first.expires_at - timedelta(seconds=1),
        )
        assert reuse.token is not None
        assert reuse.token.reused
        assert reuse.token.token_id == first.token_id
        assert reuse.token.expires_at == first.expires_at, (
            "reuse extended the expiry; R18.C2 forbids extending an issued token's expiry"
        )
        assert reuse.token.wire_token is None
        assert len(store.rows) == 1, "a second token row was created for one case (R18.C14)"

        later = first.expires_at + timedelta(seconds=1)
        replacement = service.mint(
            plan.merchant_id,
            case_id=plan.subject,
            window_end_at=later + _CONFIG.CUSTOMER_TOKEN_LIFETIME,
            approved_action=plan.approved_action,
            moment=later,
        )
        assert replacement.token is not None
        assert not replacement.token.reused
        assert replacement.token.token_id != first.token_id

        predecessor = store.by_token_id(plan.merchant_id, first.token_id)
        assert predecessor is not None
        assert predecessor.revocation_reason == TokenRevocationReason.EXPIRED_SUPERSEDED.value, (
            "the expired predecessor was not marked superseded, so the supersession is only "
            "inferable from two timestamps rather than written down"
        )
        assert store.live_for_case(plan.merchant_id, plan.subject) is not None


@pytest.mark.model
@given(plan=customer_tokens(_CONFIG))
def test_a_window_that_has_already_closed_mints_nothing(plan: TokenPlan) -> None:
    """R18.C2's clamp at its degenerate end: no token, named reason, nothing written.

    The expiry is the earlier of the lifetime and ``window_end_at``, so a case whose window has
    elapsed has no expiry available that is after the issuance instant — and ``expires_at >
    issued_at`` is a database constraint. Refusing with a named reason is what turns that into
    an abandoned execution attempt (R18.C13) rather than a failed transaction.
    """
    store = FakeTokenStore()
    audit = RecordingAuditSink()
    with installed_signing_secrets(*plan.signing_versions):
        outcome = _service(store, audit).mint(
            plan.merchant_id,
            case_id=plan.subject,
            window_end_at=_START - timedelta(seconds=1),
            approved_action=plan.approved_action,
            moment=_START,
        )

    assert outcome.token is None
    assert outcome.failure is TokenIssueFailure.WINDOW_ALREADY_CLOSED
    assert store.rows == {}
    assert CUSTOMER_TOKEN_ISSUED not in audit.event_types()


@pytest.mark.model
@given(plan=customer_tokens(_CONFIG))
def test_a_retired_signing_key_takes_the_same_404_path_as_a_forgery(plan: TokenPlan) -> None:
    """R29.C14, implemented deliberately stronger than worded.

    The requirement asks for 410 on a token signed by a retired secret. This answers **404**,
    the same as a token that never existed, and the reason is the same one that makes the
    signature and unknown-handle cases indistinguishable: telling an attacker "that was signed by
    a key we retired" tells them their guess had the right shape. What the rotation costs
    operationally is not lost — ``CUSTOMER_TOKEN_KEY_RETIRED`` is the recorded category, so "how
    many customers did that rotation lock out" stays answerable from the audit log.

    Also asserted here: verification against *every* active secret. The token is minted under the
    newest version, a further version is added, and it keeps verifying — which is the rotation
    working rather than a rotation that invalidates every live link.
    """
    store = FakeTokenStore()
    audit = RecordingAuditSink()
    minting_version = max(plan.signing_versions)

    with installed_signing_secrets(*plan.signing_versions):
        service = _service(store, audit)
        minted = _mint(service, plan, plan.subject)
        assert minted.wire_token is not None
        assert minted.key_version == str(minting_version)

    # A newer secret is added. The token was signed by an older one that is still active.
    with installed_signing_secrets(*plan.signing_versions, minting_version + 10):
        survived = _service(store, RecordingAuditSink()).verify(
            plan.merchant_id, minted.wire_token, moment=_START
        )
    assert survived.verified, (
        "a token stopped verifying when a newer signing secret was added. R29.C14 requires "
        "verification against every active secret, so a rotation must not invalidate live links"
    )

    # The minting version is retired: removed from the configured set entirely.
    remaining = tuple(v for v in plan.signing_versions if v != minting_version)
    rotated = RecordingAuditSink()
    with installed_signing_secrets(*(remaining or (minting_version + 10,))):
        outcome = _service(store, rotated).verify(
            plan.merchant_id, minted.wire_token, moment=_START
        )

    assert outcome.rejection is TokenRejection.NOT_FOUND
    assert outcome.status_code == 404, (
        "a token signed by a retired key was answered with something other than 404, so the "
        "response distinguishes it from a token that never existed"
    )
    categories = [
        record.entry.decision.get("category")
        for record in rotated.records
        if record.entry.decision is not None
    ]
    assert CUSTOMER_TOKEN_KEY_RETIRED in categories, (
        "the retirement was not recorded, so the number of customers a rotation locked out is "
        "not answerable from the audit log"
    )


@pytest.mark.model
@given(plan=customer_tokens(_CONFIG))
def test_an_absent_signing_secret_mints_nothing_verifies_nothing_and_answers_503(
    plan: TokenPlan,
) -> None:
    """R29.C13. A missing credential is a classifiable outcome, not a rejection and not a crash.

    503 rather than 404, and that distinction is the whole point: a rejection would be
    indistinguishable from a forgery, and the two demand different responses. A forgery is the
    system working; an unreadable signing secret is a deployment that must not be serving this
    endpoint at all, and answering 404 would hide it behind traffic that looks like probing.
    """
    store = FakeTokenStore()
    audit = RecordingAuditSink()
    with installed_signing_secrets():
        service = _service(store, audit)
        mint_outcome = service.mint(
            plan.merchant_id,
            case_id=plan.subject,
            window_end_at=_START + plan.window_offset,
            approved_action=plan.approved_action,
            moment=_START,
        )
        verify_outcome = service.verify(
            plan.merchant_id,
            wire_token("a" * TOKEN_ID_LENGTH, "B" * SECRET_LENGTH),
            moment=_START,
        )

    assert mint_outcome.token is None
    assert mint_outcome.failure is TokenIssueFailure.CREDENTIAL_UNAVAILABLE
    assert store.rows == {}, "a token was written without a signing secret to key its hash with"
    assert verify_outcome.rejection is TokenRejection.CREDENTIAL_UNAVAILABLE
    assert verify_outcome.status_code == 503
    assert audit.event_types().count(CREDENTIAL_UNAVAILABLE) == 2


# ---------------------------------------------------------------------------
# `model` — Property 33
# ---------------------------------------------------------------------------


@contextmanager
def captured_logs() -> Iterator[io.StringIO]:
    """The root logger writing masked JSON into a buffer, restored afterwards.

    The same arrangement ``tests/platform/test_logging.py`` uses, because P33 is a claim about
    what reaches the *output stream* and the masking happens in the formatter. Capturing at the
    handler rather than with ``caplog`` is deliberate: ``caplog`` records the message and fields
    before formatting, so it would see a value the emitted line does not contain and P33 would
    be asserted against the wrong artefact.

    A context manager rather than a fixture because these are property tests: a function-scoped
    fixture is set up once for the whole function, so every generated example would append to
    one buffer and a secret leaked by example three would be indistinguishable from one leaked
    by example thirty. Each example needs its own stream.
    """
    stream = io.StringIO()
    root = logging.getLogger()
    previous_handlers = list(root.handlers)
    previous_level = root.level
    try:
        configure_logging(level=logging.DEBUG, stream=stream)
        yield stream
    finally:
        for handler in list(root.handlers):
            root.removeHandler(handler)
        for handler in previous_handlers:
            root.addHandler(handler)
        root.setLevel(previous_level)


def _emitted(stream: io.StringIO) -> list[dict[str, Any]]:
    return [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]


@pytest.mark.model
@given(plan=customer_tokens(_CONFIG))
@settings(max_examples=50)
def test_p33_no_audit_field_and_no_log_record_contains_the_secret(plan: TokenPlan) -> None:
    """Feature: Customer Response Loop. Property 33 — for any token secret, no Audit_Record
    field and no log record contains the secret, and the token appears in both only as its
    ``token_id``.

    Driven over the whole lifecycle in one go — mint, a successful verification, a rejected
    presentation of the same handle with a wrong secret, and an expired presentation — because
    the leak worth finding is on a path somebody wrote in a hurry. The rejection path is the
    likeliest: it is the one where a developer reaches for "log what was presented" to debug a
    customer who cannot open their link.

    Both halves are asserted, and the second is not decoration. "No secret anywhere" is
    satisfiable by recording nothing at all, which would make every one of these requests
    untraceable; R18.C11 asks for the absence *and* the ``token_id``.
    """
    store = FakeTokenStore()
    audit = RecordingAuditSink()
    with captured_logs() as stream, installed_signing_secrets(*plan.signing_versions):
        service = _service(store, audit)
        minted = _mint(service, plan, plan.subject)
        assert minted.wire_token is not None
        secret = minted.wire_token.split(".", 1)[1]

        service.verify(plan.merchant_id, minted.wire_token, moment=_START)
        wrong = wire_token(minted.token_id, "Z" * SECRET_LENGTH)
        service.verify(plan.merchant_id, wrong, moment=_START)
        service.verify(plan.merchant_id, minted.wire_token, moment=minted.expires_at)
        emitted = _emitted(stream)

    rendered_audit = json.dumps(
        [asdict(record.entry) for record in audit.records], default=str
    )
    assert secret not in rendered_audit, (
        "a token secret reached an audit field. The audit log is the one store that cannot be "
        "rewritten, so a secret in it is a bearer capability retained for AUDIT_RETENTION_PERIOD"
    )
    assert minted.wire_token not in rendered_audit
    assert "Z" * SECRET_LENGTH not in rendered_audit, (
        "the secret from a *rejected* presentation was recorded. R18.C6 requires the rejection "
        "record to carry no part of the presented token, and this is the path where somebody "
        "reaches for `log what they sent` to debug a customer who cannot open their link"
    )
    assert minted.token_id in rendered_audit, (
        "no audit record identifies the token at all. R18.C11 asks for the handle, not for "
        "silence — a lifecycle nobody can trace is not a privacy improvement"
    )

    assert emitted, "the mint emitted no log record at all"
    rendered_logs = json.dumps(emitted)
    assert secret not in rendered_logs, "a token secret reached a log record (R29.C4)"
    assert minted.wire_token not in rendered_logs
    assert any(record.get("token_id") == minted.token_id for record in emitted), (
        "no log record carries the token_id, so a request cannot be joined to the token that "
        "made it"
    )


@pytest.mark.model
@given(secret=token_secrets())
@settings(max_examples=100)
def test_p33_a_token_handed_to_the_logger_by_name_is_masked_anyway(secret: str) -> None:
    """Property 33's backstop: the masking serializer covers the token by field name (39.1).

    Everything above tests that this module does not log a secret. This tests what happens when
    something *else* does — which is the case worth engineering for, because the field names in
    ``DEFAULT_FIELD_KINDS`` exist precisely because the values that leak are the ones nobody
    remembered to declare.

    ``CUSTOMER_ACCESS_TOKEN`` is also a zero-disclosure kind, so no trailing window survives.
    The four characters a contact is allowed to reveal are useful to an operator; the last four
    characters of a bearer secret are part of the capability. And ``token_id`` is asserted to be
    untouched in the same breath, because a registry entry that masked the handle would take the
    one identifier R18.C11 requires these records to carry.
    """
    logger = get_logger("revora.test.customer")
    presented = wire_token("b" * TOKEN_ID_LENGTH, secret)

    with captured_logs() as stream:
        logger.info(
            "probe",
            wire_token=presented,
            customer_access_token=presented,
            token_secret=secret,
            token_id="b" * TOKEN_ID_LENGTH,
        )
        emitted = _emitted(stream)

    assert emitted
    rendered = json.dumps(emitted)
    assert secret not in rendered, (
        "a value in a field declared CUSTOMER_ACCESS_TOKEN survived masking"
    )
    assert secret[-4:] not in rendered, (
        "the trailing characters of the secret survived. CUSTOMER_ACCESS_TOKEN is a "
        "zero-disclosure kind: the last four characters of a bearer secret are part of the "
        "capability, not a debugging aid"
    )
    assert emitted[-1]["token_id"] == "b" * TOKEN_ID_LENGTH, (
        "the token_id was masked. It is the handle R18.C11 requires every record to carry, and "
        "it is separately random, so masking it removes the trace and protects nothing"
    )


# ---------------------------------------------------------------------------
# `pg` — Property 32's row-lock bound, and the real partial index
# ---------------------------------------------------------------------------


def _seed_case(engine: Engine, merchant_id: uuid.UUID, *, moment: datetime) -> uuid.UUID:
    """One non-terminal case, written directly.

    Direct rather than driven through the pipeline because what is under test here is a
    conditional ``UPDATE`` and a partial unique index, neither of which cares how the case came
    to exist. The claim that the pipeline produces such a row belongs to the integration tier.
    """
    case_id = uuid.uuid4()
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO recovery_case (
                    id, merchant_id, state, provider_payment_id, payment_amount, currency,
                    customer_key, detected_at, window_end_at, created_at
                ) VALUES (
                    :id, :merchant_id, 'POLICY_CHECK', :payment_id, 5000, 'INR',
                    :customer_key, :detected_at, :window_end, now()
                )
                """
            ),
            {
                "id": str(case_id),
                "merchant_id": str(merchant_id),
                "payment_id": f"pay_{case_id.hex[:14]}",
                "customer_key": f"ck-{case_id}",
                "detected_at": moment,
                "window_end": moment + timedelta(days=30),
            },
        )
    return case_id


@pytest.mark.pg
def test_p32_the_submission_bound_holds_under_genuine_concurrency(
    owner_engine: Engine,
) -> None:
    """**Property 32**, the clause a fake cannot state: the cap holds across transactions.

    The sequential case is already covered in the ``model`` tier and it is the easy one — the
    second call reads a committed counter and declines. What the requirement actually has to
    survive is many submissions *in flight at once*, because that is what two worker replicas or
    two browser tabs produce, and it is the case a read-then-write guard cannot handle: both read
    four, both write five.

    The mechanism is that the comparison lives in the ``WHERE`` clause of the same ``UPDATE``
    that increments, so PostgreSQL serializes the contending updates on the row and each one
    re-evaluates the predicate against the committed value. Six threads make twelve attempts
    between them; exactly ``CUSTOMER_TOKEN_MAX_SUBMISSIONS`` succeed.

    This is also the reason the design says the rate limiter is *not* the bound. That counter is
    process-local, so two replicas admit twice the configured rate; this one cannot be exceeded
    by any number of replicas.
    """
    factory = sessionmaker(bind=owner_engine, expire_on_commit=False)
    merchant_id = insert_merchant(owner_engine, display_name="Token submission bound")
    moment = datetime.now(UTC)
    case_id = _seed_case(owner_engine, merchant_id, moment=moment)
    maximum = _CONFIG.CUSTOMER_TOKEN_MAX_SUBMISSIONS

    with installed_signing_secrets(1), tenant_transaction(merchant_id, factory) as session:
        outcome = TokenService.on_session(session, _CONFIG).mint(
            merchant_id,
            case_id=case_id,
            window_end_at=moment + timedelta(days=30),
            approved_action=CandidateAction.PAYMENT_LINK,
            moment=moment,
        )
        assert outcome.token is not None
        token_id = outcome.token.token_id

    successes: list[int] = []
    lock = threading.Lock()
    start = threading.Barrier(6)

    def contend() -> None:
        start.wait(timeout=20)
        for _ in range(2):
            with tenant_transaction(merchant_id, factory) as session:
                allocated = CustomerAccessTokenRepository(
                    session
                ).increment_accepted_submissions(
                    merchant_id, token_id, max_submissions=maximum
                )
            if allocated is not None:
                with lock:
                    successes.append(allocated)

    threads = [
        threading.Thread(target=contend, name=f"submitter-{index}", daemon=True)
        for index in range(6)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert not any(thread.is_alive() for thread in threads)

    assert len(successes) == maximum, (
        f"{len(successes)} of 12 concurrent submissions were accepted against a cap of "
        f"{maximum}. A read-then-write guard produces exactly this failure, and the fix is the "
        "comparison living inside the UPDATE rather than beside it"
    )
    assert sorted(successes) == list(range(1, maximum + 1)), (
        f"the allocated counts were {sorted(successes)}; two submissions were handed the same "
        "ordinal, which means the increment was not serialized on the row"
    )

    with owner_engine.connect() as connection:
        final = connection.execute(
            text(
                "SELECT accepted_submission_count FROM customer_access_token "
                "WHERE merchant_id = :m AND token_id = :t"
            ),
            {"m": str(merchant_id), "t": token_id},
        ).scalar_one()
    assert int(final) == maximum


@pytest.mark.pg
def test_revoke_then_mint_satisfies_the_real_partial_unique_index(
    owner_engine: Engine,
) -> None:
    """The revoke-then-mint ordering, against ``one_live_token_per_case`` itself.

    The ``model`` tier asserts the same ordering against a fake that raises the same error, which
    is enough to catch the mistake — but not enough to establish that the index the fake is
    imitating is really shaped the way the fake assumes. This runs both statements in one real
    transaction and then asks the database how many live rows exist.

    Two facts, and the second is what makes the first non-obvious: the predecessor is revoked
    ``EXPIRED_SUPERSEDED`` before the insert, and there is exactly one live row afterwards. If
    the order were reversed the insert would violate the index and the transaction would abort —
    which is why the revoke has to be a written act rather than something inferred from the
    expiry the index cannot see.
    """
    factory = sessionmaker(bind=owner_engine, expire_on_commit=False)
    merchant_id = insert_merchant(owner_engine, display_name="Token supersession")
    moment = datetime.now(UTC)
    case_id = _seed_case(owner_engine, merchant_id, moment=moment)

    with installed_signing_secrets(1):
        with tenant_transaction(merchant_id, factory) as session:
            first = TokenService.on_session(session, _CONFIG).mint(
                merchant_id,
                case_id=case_id,
                window_end_at=moment + timedelta(days=30),
                approved_action=CandidateAction.PAYMENT_LINK,
                moment=moment,
            )
            assert first.token is not None

        after_expiry = first.token.expires_at + timedelta(seconds=1)
        with tenant_transaction(merchant_id, factory) as session:
            replacement = TokenService.on_session(session, _CONFIG).mint(
                merchant_id,
                case_id=case_id,
                window_end_at=after_expiry + timedelta(days=30),
                approved_action=CandidateAction.PAYMENT_LINK,
                moment=after_expiry,
            )
        assert replacement.token is not None
        assert replacement.token.token_id != first.token.token_id

    with owner_engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT token_id, revocation_reason FROM customer_access_token "
                "WHERE merchant_id = :m AND case_id = :c ORDER BY issued_at"
            ),
            {"m": str(merchant_id), "c": str(case_id)},
        ).all()
        live = connection.execute(
            text(
                "SELECT count(*) FROM customer_access_token "
                "WHERE merchant_id = :m AND case_id = :c AND revoked_at IS NULL"
            ),
            {"m": str(merchant_id), "c": str(case_id)},
        ).scalar_one()

    assert [reason for _token_id, reason in rows] == [
        TokenRevocationReason.EXPIRED_SUPERSEDED.value,
        None,
    ]
    assert int(live) == 1, (
        "the case holds more than one live token, which one_live_token_per_case is supposed to "
        "make uncommittable"
    )


@pytest.mark.pg
def test_a_terminal_transition_revokes_every_live_token_and_later_requests_get_410(
    owner_engine: Engine,
) -> None:
    """Task 39.3 against a real case: entering a Terminal_State ends the customer's access.

    R18.C8's first half, and it is asserted through ``apply_locked_transition`` rather than
    through a direct call to the revoke, because the requirement is about a *case entering a
    terminal state* and the point of putting the revoke at the one writer of
    ``recovery_case.state`` is that no terminal edge can bypass it. Calling the revoke directly
    would test the revoke and prove nothing about the coupling.

    The 410 afterwards is the consequence the customer sees, and it is checked through the same
    ``verify`` a request would take — so the token is genuinely dead rather than merely marked.
    """
    from revora.cases.manager import apply_locked_transition
    from revora.domain.enums import CaseState, TerminalReason
    from revora.persistence.repositories.cases import RecoveryCaseRepository

    factory = sessionmaker(bind=owner_engine, expire_on_commit=False)
    merchant_id = insert_merchant(owner_engine, display_name="Token terminal revoke")
    moment = datetime.now(UTC)
    case_id = _seed_case(owner_engine, merchant_id, moment=moment)

    with installed_signing_secrets(1):
        with tenant_transaction(merchant_id, factory) as session:
            outcome = TokenService.on_session(session, _CONFIG).mint(
                merchant_id,
                case_id=case_id,
                window_end_at=moment + timedelta(days=30),
                approved_action=CandidateAction.PAYMENT_LINK,
                moment=moment,
            )
            assert outcome.token is not None
        minted = outcome.token
        assert minted.wire_token is not None

        with tenant_transaction(merchant_id, factory) as session:
            case = RecoveryCaseRepository(session).lock_for_update(merchant_id, case_id)
            assert case is not None
            result, rejection = apply_locked_transition(
                session,
                merchant_id,
                case,
                expected_version=int(case.version),
                target_state=CaseState.STOPPED,
                reason="attempts exhausted",
                actor="test",
                terminal_reason=TerminalReason.MAX_ATTEMPTS_REACHED,
            )
            assert rejection is None, f"the terminal transition was refused: {rejection}"
            assert result.applied

        with tenant_transaction(merchant_id, factory) as session:
            after = TokenService.on_session(session, _CONFIG).verify(
                merchant_id, minted.wire_token, moment=moment + timedelta(minutes=1)
            )

    assert after.rejection is TokenRejection.REVOKED, (
        "a token survived its case entering a Terminal_State. R18.C8 ends the customer's access "
        "when the case ends, and the revoke lives at the one writer of recovery_case.state so "
        "that no terminal edge can forget it"
    )
    assert after.status_code == 410

    with owner_engine.connect() as connection:
        reason = connection.execute(
            text(
                "SELECT revocation_reason FROM customer_access_token "
                "WHERE merchant_id = :m AND token_id = :t"
            ),
            {"m": str(merchant_id), "t": minted.token_id},
        ).scalar_one()
        recorded = connection.execute(
            text(
                "SELECT decision FROM audit_record WHERE merchant_id = :m AND case_id = :c "
                "AND event_type = 'STATE_TRANSITION' ORDER BY seq DESC LIMIT 1"
            ),
            {"m": str(merchant_id), "c": str(case_id)},
        ).scalar_one()

    assert reason == TokenRevocationReason.CASE_TERMINAL.value
    assert recorded.get("customer_tokens_revoked") == 1, (
        "the transition record does not say the customer's access was withdrawn, so the only "
        "trace of it is a column on a table nobody reading the case history looks at"
    )


@pytest.mark.pg
def test_the_verification_lookup_is_served_by_the_handle_index(owner_engine: Engine) -> None:
    """The one indexed lookup of R18.C4 really is indexed, and by the handle.

    Every customer request begins with this read, so a sequential scan here is a scan of every
    token a merchant has ever issued, on the one endpoint an unauthenticated caller can reach at
    ``CUSTOMER_PAGE_RATE_LIMIT`` per token.

    **Two hundred spare rows are inserted first, and that is not padding.** The table carries two
    indexes whose leading column is ``merchant_id`` — the unique one on ``(merchant_id,
    token_id)`` and the case index on ``(merchant_id, case_id)`` — and on a merchant with a single
    token they cost the same, so the planner picks either. A test written against one row asserts
    whichever one PostgreSQL happened to prefer that day, which is how it fails later for a
    reason that has nothing to do with the query. With two hundred rows under one merchant the
    case index is no longer selective for a ``merchant_id``-only bound and the unique index is
    the only cheap answer, so the assertion becomes a statement about the query rather than about
    the planner's tie-break.

    The spares are revoked, because ``one_live_token_per_case`` permits exactly one live token
    per case and revoked rows are still in both indexes — which is what makes them usable as
    volume without needing two hundred case rows.

    ``enable_seqscan = off`` for the ``EXPLAIN`` only, for the reason the review-sweeper's index
    test gives: two hundred rows still fit on a couple of pages, so a sequential scan is
    genuinely cheaper and the question this test means to ask is whether the index is *available*
    for the query at all.
    """
    merchant_id = insert_merchant(owner_engine, display_name="Token index")
    moment = datetime.now(UTC)
    case_id = _seed_case(owner_engine, merchant_id, moment=moment)
    factory = sessionmaker(bind=owner_engine, expire_on_commit=False)

    with installed_signing_secrets(1), tenant_transaction(merchant_id, factory) as session:
        outcome = TokenService.on_session(session, _CONFIG).mint(
            merchant_id,
            case_id=case_id,
            window_end_at=moment + timedelta(days=30),
            approved_action=CandidateAction.PAYMENT_LINK,
            moment=moment,
        )
        assert outcome.token is not None
        token_id = outcome.token.token_id

    with owner_engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO customer_access_token (
                    id, merchant_id, case_id, token_id, secret_hash, key_version, issued_at,
                    expires_at, accepted_submission_count, revoked_at, revocation_reason,
                    approved_action, created_at
                ) VALUES (
                    gen_random_uuid(), :m, :c, 'spare' || lpad((:g)::text, 21, '0'),
                    decode(repeat('00', 32), 'hex'), '1', :now, :end, 0, :now,
                    :reason, 'PAYMENT_LINK', now()
                )
                """
            ),
            [
                {
                    "m": str(merchant_id),
                    "c": str(case_id),
                    "now": moment,
                    "end": moment + timedelta(days=30),
                    "reason": TokenRevocationReason.CASE_TERMINAL.value,
                    "g": index,
                }
                for index in range(200)
            ],
        )
        connection.execute(text("ANALYZE customer_access_token"))

    with owner_engine.connect() as connection:
        connection.execute(text("SET enable_seqscan = off"))
        plan = "\n".join(
            str(row[0])
            for row in connection.execute(
                text(
                    "EXPLAIN SELECT * FROM customer_access_token "
                    "WHERE merchant_id = :m AND token_id = :t"
                ),
                {"m": str(merchant_id), "t": token_id},
            ).all()
        )

    assert "uq_customer_access_token_merchant_id_token_id" in plan, (
        f"the verification lookup is not served by its unique index. Plan:\n{plan}"
    )
    assert "Seq Scan" not in plan


# ---------------------------------------------------------------------------
# `model` — Property 34, the read projection's disclosure surface (task 40.5)
# ---------------------------------------------------------------------------
#
# **On the declared count.** The design's prose says "nine fields" and then enumerates *eight* —
# in its JSON sample, in its bullet list, and again in the task breakdown. The requirements
# enumerate the same eight (R19.C1's seven presented items, with the amount's currency counted
# separately). So these tests assert **eight**, from ``PROJECTION_FIELDS``, which is derived from
# the dataclass rather than restated. Asserting nine would mean inventing a field to satisfy an
# arithmetic error, and a disclosure decided by counting is exactly how this list must not grow.


@pytest.mark.model
@given(projection=case_projections())
@settings(max_examples=300)
def test_p34_the_projection_discloses_exactly_the_declared_fields(
    projection: CustomerCaseProjection,
) -> None:
    """Feature: Customer Response Loop. Property 34 — for arbitrary Recovery_Case states,
    Promise_To_Pay records and candidate sets, the Customer_Response_Page projection's key set is
    exactly the declared fields and contains no other field.

    Asserted at both ends of the same declaration, which is the whole point of the projection being
    a purpose-built dataclass rather than a filtered view of the dashboard model:

    * the **dataclass** has exactly the declared fields, so ``frozen`` and ``slots`` mean the
      disclosure surface is fixed at class definition rather than at each call site;
    * the **wire document** has exactly the same keys, so nothing is added between the read and the
      response.

    Then the exclusion list of R19.C2 and R29.C3 is checked by name against the rendered document —
    every probability, every cost term, the net value, the contact identifier, the instrument
    reference, the policy decision, the Merchant_User identifier. That looks redundant beside the
    key-set assertion and it is not: the key-set assertion fails when a field is *added*, while
    this one names what must never be there, so a reader can check the requirement against the test
    instead of against the requirements document. It is also what catches a field arriving under a
    different name — ``probability`` inside the ``promise`` object, say.
    """
    assert frozenset(f.name for f in dataclass_fields(projection)) == PROJECTION_FIELDS

    document = as_document(projection, render_amount=_render_amount)
    assert frozenset(document) == PROJECTION_FIELDS, (
        "the customer document's key set is not the declared projection fields. Every field here "
        "is a disclosure decision, and a key set that can drift is a disclosure decided by "
        "whoever last edited a serializer"
    )

    # Checked against the document's **keys**, gathered recursively, rather than against its
    # serialized text. A generated merchant display name of "RECOMMENDATION" found the difference:
    # R19.C2 and R29.C3 exclude *fields*, and a merchant legitimately called "Audit & Recommendation
    # Ltd" must still be able to have their name on the page. Recursive because the amount envelope
    # is nested and a future field could nest deeper — which is the case a top-level key check would
    # miss.
    keys = {key.lower() for key in _all_keys(document)}
    for forbidden in (
        "baseline_recovery_probability",
        "intervention_recovery_probability",
        "incremental_probability",
        "expected_incremental_revenue",
        "financial_cost",
        "communication_cost",
        "risk_cost",
        "customer_cost",
        "total_action_cost",
        "net_recovery_value",
        "customer_contact",
        "customer_key",
        "instrument",
        "policy_decision",
        "verdict",
        "merchant_user",
        "audit",
        "recommendation",
        "config_version",
    ):
        assert not any(forbidden in key for key in keys), (
            f"the projection carries a field matching {forbidden!r}: {sorted(keys)}. R19.C2 and "
            "R29.C3 are written as a list of exclusions, and this is that list — the internal "
            "economics are the Merchant's commercial reasoning, and showing a customer that their "
            "payment was priced would damage the recovery it exists to achieve"
        )


def _all_keys(value: object) -> set[str]:
    """Every mapping key in a wire document, at any depth."""
    found: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            found.add(str(key))
            found |= _all_keys(item)
    elif isinstance(value, list | tuple):
        for item in value:
            found |= _all_keys(item)
    return found


@pytest.mark.model
def test_p34_the_projection_is_not_a_filtered_view_of_the_dashboard_model() -> None:
    """The *mechanism* behind P34: adding a dashboard field cannot leak it here.

    A key-set property is satisfiable by a filter over a wider object, and a filter is a thing that
    drifts — the day somebody adds ``net_recovery_value`` to the dashboard's case document, a
    filtered projection either leaks it or needs a second edit nobody will connect to this
    requirement. So the structural claim is asserted directly: the customer projection module
    imports nothing from ``revora.api.views``, and the dataclass inherits from nothing.

    ``revora.api.rendering`` is *also* unreachable from it — ``revora.customer`` sits below
    ``revora.api`` in the layering contract, which ``lint-imports`` enforces — which is why the
    amount renderer is injected rather than imported. That inversion is the same guarantee seen
    from the other side: the projection cannot reach the dashboard's vocabulary even to borrow one
    formatter, so it certainly cannot inherit its fields.
    """
    tree = ast.parse(inspect.getsource(projection_module))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported.add(node.module)
    # Read out of the AST rather than grepped out of the source, because the module's own
    # docstring explains *why* ``revora.api.rendering`` is unreachable and a substring search
    # would fail on the explanation.
    assert not any(name.startswith("revora.api") for name in imported), (
        "the customer projection imports from revora.api, so it is on the same side of the "
        f"layering contract as the dashboard's read models. Imports: {sorted(imported)}"
    )
    assert CustomerCaseProjection.__mro__[1:] == (object,), (
        "CustomerCaseProjection inherits from something, so its field set is not local to its own "
        "declaration and a field added to the base would appear on the customer page"
    )


@pytest.mark.pure
def test_the_plain_language_cause_table_is_total_and_carries_no_provider_vocabulary() -> None:
    """R19.C1's ``reason``: a sentence per cause, and never the provider's error string.

    Totality is asserted rather than relied on, even though the module raises at import if the
    table is incomplete — the import-time check protects the running system and this protects the
    reason it exists, which is that a ``.get`` with a fallback would let a cause added tomorrow
    ship to a customer under a sentence nobody chose.

    Then the negative claim, which is the one with teeth: no sentence contains a provider reason
    code, a Razorpay identifier prefix, or the words a failure log uses. The provider's vocabulary
    is internal, it sometimes names our own integration, and it is written for an operator
    debugging a payment rail rather than for a person deciding whether they can afford to pay.
    """
    assert frozenset(PLAIN_LANGUAGE_CAUSE) == frozenset(RiskCause)

    forbidden = (
        "insufficient_funds",
        "payment_risk_check_failed",
        "compliance_violation",
        "gateway",
        "razorpay",
        "plink_",
        "pay_",
        "bad_request_error",
        "risk",
        "fraud",
        "error code",
    )
    for cause, sentence in PLAIN_LANGUAGE_CAUSE.items():
        lowered = sentence.lower()
        for token in forbidden:
            assert token not in lowered, (
                f"the sentence for {cause.value} contains {token!r}. It is a plain-language "
                "rendering for the person who owes the money, not the provider's vocabulary — and "
                "'risk' or 'fraud' in particular is either an accusation a template cannot "
                "substantiate or a hint telling a fraudster which control they tripped"
            )
        assert sentence.endswith("."), f"{cause.value} is not a sentence"
        assert sentence[0].isupper(), f"{cause.value} does not begin as a sentence"

    assert PLAIN_LANGUAGE_CAUSE[RiskCause.UNKNOWN] != NO_CAUSE_RECORDED, (
        "'no diagnosis yet' and 'the cause is UNKNOWN' render identically. They are different "
        "claims: the first means we have not finished looking, the second means we looked and "
        "could not tell, and a customer reading the first should keep waiting for a better answer"
    )
