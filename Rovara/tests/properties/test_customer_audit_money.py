"""Task 40.5. The customer surface's audit trail is exact, and its money is integers.

Two properties, two tiers, and the tier split is the whole reason they share a file: they are the
same claim about the same write seen from two sides. P59 says *what was recorded* about a write, and
P60 says *what the figures in it are made of*.

**Property 60 is ``pure``.** Every currency figure the customer service produces is an integer count
of minor units, with no fractional or floating value at any step. That is decided by the projection
dataclass and one formatter, so it needs no database and runs at the microsecond tier alongside the
rest of the money discipline. ``scripts/check_no_float.py`` already makes the *lexical* prohibition
structural for ``revora/customer/``; this is the behavioural half — the guard would not catch an
integer divided by an integer and rendered wrong, and this would.

**Property 59 is ``pg`` in both halves, and the design assigns its first half to ``model``.** The
deviation is deliberate and it is the same one task 38 made for P63, P64 and P66: the honest
placement is the tier where a property actually runs.

``record_signal`` writes through four concrete repositories and an ``AuditWriter``, all constructed
from a ``Session``, and its guarantee *is* the transaction boundary — "all four writes or none".
A ``model``-tier version would need a fake session that implements ``FOR UPDATE``, a conditional
``UPDATE ... RETURNING``, a partial unique index on ``job``, and rollback. At that point the fake is
a database with fewer guarantees than the one in the container, and a passing test against it would
be evidence about the fake. The one thing this costs is examples per second, and it is paid once:
the sequences are short and the assertions are batched into one query per claim.

**The injected audit-write failure** is the second half, and it is what makes "all four or none"
falsifiable rather than merely stated. The failure is injected where the requirement puts it —
``AUDIT_WRITE_TIMEOUT`` — by setting a zero ``statement_timeout`` immediately before the audit write
and asserting that afterwards the signal count, the submission count and the job queue are all
exactly where they were.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from hypothesis import given, settings
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError, OperationalError
from sqlalchemy.orm import Session, sessionmaker

from revora.api.routers.customer import _render_amount
from revora.audit.events import CUSTOMER_SIGNAL_RECORDED
from revora.cases.review import CASE_REVIEW_KIND
from revora.customer.projection import CustomerCaseProjection, as_document
from revora.customer.signals import (
    DelayReasonSubmission,
    PartialArrangementSubmission,
    PromiseSubmission,
    record_signal,
)
from revora.customer.tokens import TokenService, VerifiedToken
from revora.domain.actions import CandidateAction
from revora.domain.enums import DelayReason
from revora.persistence.repositories.session import tenant_transaction
from revora.platform.config import default_configuration
from tests.fakes.customer import installed_signing_secrets
from tests.pg_support import insert_merchant
from tests.strategies.customer import case_projections

_CONFIG = default_configuration()
_START = datetime(2025, 6, 1, 12, 0, 0, tzinfo=UTC)

# No module-level marker: P60 needs nothing at all and belongs in the tier that runs on every
# commit, while P59 needs a migrated PostgreSQL. A module-level `pg` would drag the cheap one into
# the slow selection and out of the fast one.


# ---------------------------------------------------------------------------
# `pure` — Property 60
# ---------------------------------------------------------------------------


@pytest.mark.pure
@given(projection=case_projections())
@settings(max_examples=500)
def test_p60_every_customer_currency_figure_is_an_integer_minor_unit_count(
    projection: CustomerCaseProjection,
) -> None:
    """Feature: Customer Response Loop. Property 60 — every currency figure the
    Customer_Response_Service produces is an integer count of minor currency units, and no
    fractional or floating value appears at any step.

    Four assertions, and each one is a different way the property could be true on the surface and
    false underneath.

    ``amount`` on the projection is an ``int`` and **not a ``bool``**. The explicit ``bool``
    exclusion is not pedantry: ``bool`` is a subclass of ``int`` in Python, so an amount that
    became ``True`` somewhere would satisfy every ``isinstance(..., int)`` check and render as
    ``₹0.01``.

    ``minor`` on the wire is the *same* integer, unchanged. A figure that survives the dataclass and
    is then coerced on the way out is a figure the browser and the database disagree about, and this
    is the surface where the disagreement is read by the payer.

    ``formatted`` is derived from that integer and **agrees with it**, recomputed here from
    ``Decimal`` arithmetic — exact by construction — rather than compared against another call to
    the same formatter, which would only prove the formatter is deterministic.

    And nothing anywhere in the document is a ``float``. Walked recursively, because the amount
    envelope is nested and a future field could nest deeper.
    """
    assert isinstance(projection.amount, int) and not isinstance(projection.amount, bool), (
        f"the projection's amount is {type(projection.amount).__name__}, not an integer count of "
        "minor units. bool is excluded explicitly because it is a subclass of int and would pass "
        "every isinstance check while rendering as one paisa"
    )

    document = as_document(projection, render_amount=_render_amount)
    envelope = document["amount"]
    assert isinstance(envelope, dict)
    minor = envelope["minor"]
    assert isinstance(minor, int) and not isinstance(minor, bool)
    assert minor == int(projection.amount), (
        "the wire figure is not the stored figure. Every currency value on this surface is derived "
        "from the stored integer on the server (R19.C3), so a coercion between the two is a "
        "second source of truth for the one number the customer is being asked to pay"
    )

    # The formatted string, recomputed exactly. `Decimal` because the check must not itself
    # introduce the error it is looking for.
    magnitude = Decimal(abs(minor)).scaleb(-2)
    digits = f"{magnitude:.2f}"
    formatted = envelope["formatted"]
    assert isinstance(formatted, str)
    assert formatted.endswith(digits[-3:]), (
        f"the rendered string {formatted!r} does not end in the minor units of {minor}, so the "
        "string and the integer beside it disagree in the last digit"
    )

    _assert_no_float(document, path="document")


def _assert_no_float(value: object, *, path: str) -> None:
    """Walk a wire document refusing any ``float``, wherever it is nested."""
    if isinstance(value, dict):
        for key, item in value.items():
            _assert_no_float(item, path=f"{path}.{key}")
        return
    if isinstance(value, list | tuple):
        for index, item in enumerate(value):
            _assert_no_float(item, path=f"{path}[{index}]")
        return
    assert not isinstance(value, float), (
        f"{path} is a float. Money in Revora is an integer count of minor units, and this is the "
        "one surface where a rounding error is read by the person paying rather than by an operator"
    )


# ---------------------------------------------------------------------------
# `pg` — Property 59
# ---------------------------------------------------------------------------


_FAILURE_TRIGGER = "_task40_audit_write_timeout"
"""Name of the temporary trigger that makes the audit ``INSERT`` fail.

A database-side failure rather than a patched ``AuditWriter``, because patching the writer would
prove that a Python exception rolls a transaction back — which nobody doubts. What has to be shown
is that the *database* refusing the audit statement leaves the other three writes absent, and only a
real failure on a real transaction shows that.

It raises with ``SQLSTATE 57014`` — ``query_canceled``, the code a genuine ``statement_timeout``
produces — so psycopg maps it to ``QueryCanceled`` and SQLAlchemy wraps it as ``OperationalError``,
which is exactly the exception class the router's ``except`` clause is written against. The
injection therefore exercises the production error path rather than a lookalike.
"""


def _install_audit_write_failure(engine: Engine) -> None:
    """Make every ``audit_record`` insert fail as a cancelled statement.

    Dropped first as well as last: a crashed run would otherwise leave the trigger behind and every
    subsequent test in the session would fail on its first audit write, several files away from the
    cause.
    """
    _remove_audit_write_failure(engine)
    with engine.begin() as connection:
        connection.execute(
            text(
                f"""
                CREATE FUNCTION {_FAILURE_TRIGGER}() RETURNS trigger AS $$
                BEGIN
                    RAISE EXCEPTION 'audit write exceeded AUDIT_WRITE_TIMEOUT'
                        USING ERRCODE = '57014';
                END
                $$ LANGUAGE plpgsql
                """
            )
        )
        connection.execute(
            text(
                f"CREATE TRIGGER {_FAILURE_TRIGGER} BEFORE INSERT ON audit_record "
                f"FOR EACH ROW EXECUTE FUNCTION {_FAILURE_TRIGGER}()"
            )
        )


def _remove_audit_write_failure(engine: Engine) -> None:
    """Remove the trigger and its function. Idempotent, so the ``finally`` cannot fail."""
    with engine.begin() as connection:
        connection.execute(
            text(f"DROP TRIGGER IF EXISTS {_FAILURE_TRIGGER} ON audit_record")
        )
        connection.execute(text(f"DROP FUNCTION IF EXISTS {_FAILURE_TRIGGER}()"))


def _seed_case(
    engine: Engine, merchant_id: uuid.UUID, *, state: str, moment: datetime
) -> uuid.UUID:
    """One case in a named state. Written directly, because how it got there is not under test."""
    case_id = uuid.uuid4()
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO recovery_case (
                    id, merchant_id, state, provider_payment_id, payment_amount, currency,
                    customer_key, detected_at, window_end_at, next_review_at,
                    decision_cycle_count, created_at
                ) VALUES (
                    :id, :m, :state, :pid, 249900, 'INR', :ck, :detected, :window_end,
                    :review, 1, now()
                )
                """
            ),
            {
                "id": str(case_id),
                "m": str(merchant_id),
                "state": state,
                "pid": f"pay_{case_id.hex[:14]}",
                "ck": f"ck-{case_id}",
                "detected": moment,
                "window_end": moment + timedelta(days=7),
                "review": moment + timedelta(hours=12) if state == "POLICY_CHECK" else None,
            },
        )
    return case_id


def _mint(
    factory: sessionmaker[Session],
    merchant_id: uuid.UUID,
    case_id: uuid.UUID,
    *,
    moment: datetime,
) -> VerifiedToken:
    """Mint through the real service and verify, so the test holds what a request would hold."""
    with installed_signing_secrets(1):
        with tenant_transaction(merchant_id, factory) as session:
            outcome = TokenService.on_session(session, _CONFIG).mint(
                merchant_id,
                case_id=case_id,
                window_end_at=moment + timedelta(days=7),
                approved_action=CandidateAction.PAYMENT_LINK,
                moment=moment,
            )
        assert outcome.token is not None and outcome.token.wire_token is not None
        with tenant_transaction(merchant_id, factory) as session:
            verified = TokenService.on_session(session, _CONFIG).verify(
                merchant_id, outcome.token.wire_token, moment=moment
            )
    assert verified.token is not None
    return verified.token


def _counts(engine: Engine, merchant_id: uuid.UUID, case_id: uuid.UUID) -> tuple[int, int, int]:
    """Signals, accepted submissions, pending reviews. One query each, read together.

    Read as a triple rather than asserted one at a time, because the property is about the three
    moving *together*: a test that checked them in sequence could pass on a state where the first
    two agreed and the third did not, and then report the failure against whichever assertion ran
    last.
    """
    with engine.connect() as connection:
        signals = int(
            connection.execute(
                text("SELECT count(*) FROM customer_signal WHERE case_id = :c"),
                {"c": str(case_id)},
            ).scalar_one()
        )
        accepted = int(
            connection.execute(
                text(
                    "SELECT coalesce(sum(accepted_submission_count), 0) "
                    "FROM customer_access_token WHERE case_id = :c"
                ),
                {"c": str(case_id)},
            ).scalar_one()
        )
        jobs = int(
            connection.execute(
                text(
                    "SELECT count(*) FROM job WHERE case_id = :c AND kind = :k "
                    "AND state = 'PENDING'"
                ),
                {"c": str(case_id), "k": CASE_REVIEW_KIND},
            ).scalar_one()
        )
    return signals, accepted, jobs


@pytest.mark.pg
def test_p59_exactly_one_audit_record_per_write_with_the_token_as_actor(
    owner_engine: Engine,
) -> None:
    """**Property 59**, first half: one Audit_Record per accepted write, ``token_id`` as actor,
    correlation id shared with the write.

    Three writes of three different shapes, each with its own correlation id, and then the audit log
    is read once. Three shapes rather than three of one, because R19.C6 requires the record to carry
    the Customer_Signal_Kind and the submitted values — so a record written from a shared code path
    that lost the kind would pass a single-shape test.

    **Exactly one**, and the "exactly" is the load-bearing word. The timeline's
    ``CUSTOMER_RESPONDED`` stage keys on the presence of any ``CUSTOMER_SIGNAL_RECORDED`` record for
    the case, so a second record for one submission is not a duplicated log line — it is a case
    history claiming the customer said something twice, and Recovery_Memory reads that history as
    an observation feature.

    The **shared correlation id** is what makes the whole thing joinable: R29.C9 requires the record
    to carry the correlation id generated for the submission, and the signal row carries the same
    one, so "what did this customer send at 14:32" is one query rather than a search.
    """
    factory = sessionmaker(bind=owner_engine, expire_on_commit=False)
    merchant_id = insert_merchant(owner_engine, display_name="Customer audit exactness")
    moment = datetime.now(UTC)
    case_id = _seed_case(owner_engine, merchant_id, state="POLICY_CHECK", moment=moment)
    token = _mint(factory, merchant_id, case_id, moment=moment)

    submissions = (
        DelayReasonSubmission(delay_reason=DelayReason.SALARY_OR_CASHFLOW_TIMING, note="the 5th"),
        PartialArrangementSubmission(note="two parts?"),
        PromiseSubmission(promise_date=moment + timedelta(days=3)),
    )
    correlations = [uuid.uuid4() for _ in submissions]

    for submission, correlation_id in zip(submissions, correlations, strict=True):
        with tenant_transaction(merchant_id, factory) as session:
            outcome = record_signal(
                session, _CONFIG, token, submission, correlation_id=correlation_id
            )
        assert outcome.accepted, f"{submission.kind} was refused: {outcome.rejection}"

    with owner_engine.connect() as connection:
        records = connection.execute(
            text(
                "SELECT actor, correlation_id, decision->>'kind' AS kind, "
                "decision->>'signal_id' AS signal_id, decision::text AS body "
                "FROM audit_record WHERE merchant_id = :m AND case_id = :c "
                "AND event_type = :e ORDER BY seq"
            ),
            {"m": str(merchant_id), "c": str(case_id), "e": CUSTOMER_SIGNAL_RECORDED},
        ).all()
        signal_rows = connection.execute(
            text(
                "SELECT id, kind, correlation_id FROM customer_signal "
                "WHERE case_id = :c ORDER BY submitted_at"
            ),
            {"c": str(case_id)},
        ).all()

    assert len(records) == 3, (
        f"{len(records)} audit records for three accepted writes. R19.C6 says exactly one per "
        "signal, and the timeline's CUSTOMER_RESPONDED stage keys on their presence — so a second "
        "one is a case history claiming the customer said something twice"
    )
    assert {str(row[0]) for row in records} == {token.token_id}, (
        "an audit record for a customer write does not carry the token_id as its actor. R29.C9 "
        "extends R17.C9 by admitting a credential as an actor where no Merchant_User initiated the "
        "operation, and without it these writes have no attributable author at all"
    )
    assert [str(row[1]) for row in records] == [str(c) for c in correlations], (
        "the audit records do not carry the correlation ids of the submissions that caused them, "
        "in order, so a customer's report of what they sent cannot be joined to what was recorded"
    )
    assert [row[2] for row in records] == [s.kind.value for s in submissions], (
        "an audit record does not name the Customer_Signal_Kind of the write it describes (R19.C6)"
    )
    assert [str(row[0]) for row in signal_rows] == [row[3] for row in records], (
        "the audit records name signal ids that are not the rows that were inserted, so the record "
        "and the evidence it describes are not the same object"
    )
    assert [str(row[2]) for row in signal_rows] == [str(c) for c in correlations], (
        "the persisted signals do not carry the submission correlation id, so only half of the "
        "trail is joinable"
    )


@pytest.mark.pg
def test_p59_an_audit_write_failure_leaves_no_signal_and_an_unchanged_count(
    owner_engine: Engine,
) -> None:
    """**Property 59**, second half: on audit failure, zero signal rows and an unchanged count.

    This is the executable form of "all four writes or none" (R19.C5, R29.C12), and the injection
    point is chosen to match the requirement rather than to be convenient. R29.C12 is worded about
    ``AUDIT_WRITE_TIMEOUT``, so the failure is a **statement timeout** set to zero immediately
    before the audit ``INSERT`` — the same mechanism the production path uses to bound the write,
    triggered at the same statement.

    A ``before_cursor_execute`` listener rather than a patched ``AuditWriter``: patching the writer
    would prove that a Python exception rolls the transaction back, which nobody doubts. What has to
    be true is that the *database* cancelling the audit statement leaves the other three writes
    absent, and only a real cancellation on a real transaction demonstrates that.

    The counts are taken before and after and compared as a triple. All three have to be unchanged,
    and they are three different stores: ``customer_signal`` has no row, the token's
    ``accepted_submission_count`` did not move even though its ``UPDATE`` had already succeeded
    inside the transaction, and no ``case_review`` job is pending even though the enqueue had
    already returned an id. The middle one is the interesting assertion — a conditional ``UPDATE``
    that has already matched a row is exactly the kind of write somebody would expect to survive.
    """
    factory = sessionmaker(bind=owner_engine, expire_on_commit=False)
    merchant_id = insert_merchant(owner_engine, display_name="Customer audit failure")
    moment = datetime.now(UTC)
    case_id = _seed_case(owner_engine, merchant_id, state="POLICY_CHECK", moment=moment)
    token = _mint(factory, merchant_id, case_id, moment=moment)

    before = _counts(owner_engine, merchant_id, case_id)
    assert before == (0, 0, 0), f"the fixture is not clean: {before}"

    _install_audit_write_failure(owner_engine)
    try:
        with pytest.raises(OperationalError) as raised, tenant_transaction(
            merchant_id, factory
        ) as session:
            record_signal(
                session,
                _CONFIG,
                token,
                DelayReasonSubmission(delay_reason=DelayReason.OTHER, note="anything"),
                correlation_id=uuid.uuid4(),
            )
    finally:
        _remove_audit_write_failure(owner_engine)

    assert type(raised.value.orig).__name__ == "QueryCanceled", (
        "the injected failure did not arrive as a cancellation, so this test is no longer "
        f"exercising the AUDIT_WRITE_TIMEOUT path: {type(raised.value.orig).__name__}"
    )
    assert isinstance(raised.value, DBAPIError), (
        "the failure did not arrive as a DBAPIError, so the router's except clause would not have "
        "caught it and R29.C12's 503 would have been a 500"
    )

    after = _counts(owner_engine, merchant_id, case_id)
    assert after == (0, 0, 0), (
        f"an audit-write failure left {after[0]} signal row(s), an accepted-submission count of "
        f"{after[1]} and {after[2]} pending review(s). R29.C12 requires all four writes to commit "
        "together or not at all — the submission count is the one to look at first, because its "
        "conditional UPDATE had already matched a row before the audit statement was cancelled"
    )


@pytest.mark.pg
def test_p59_a_refused_write_is_audited_and_persists_nothing_else(owner_engine: Engine) -> None:
    """R29.C9's other half: a *rejected* write is audited too, and changes nothing.

    R29.C9 says "accepts **or rejects**" — one record either way — and the reason that matters is
    asymmetric: a customer complaining that they cannot submit anything is answerable only if the
    refusals are in the log beside the acceptances. Without it, the audit trail of a customer who
    was refused five times is indistinguishable from that of a customer who never opened the page.

    A terminal case is the refusal chosen here because it is the one that must leave the *case*
    untouched as well (R19.C8): no state change, no counter movement, no signal, no queued
    consequence. The 409 is asserted through the outcome's own status code, read from
    ``SIGNAL_REJECTION_STATUS`` rather than restated.
    """
    factory = sessionmaker(bind=owner_engine, expire_on_commit=False)
    merchant_id = insert_merchant(owner_engine, display_name="Customer refusal audited")
    moment = datetime.now(UTC)
    case_id = _seed_case(owner_engine, merchant_id, state="POLICY_CHECK", moment=moment)
    token = _mint(factory, merchant_id, case_id, moment=moment)

    # Terminal after the token was minted, which is the real sequence: a case ends between the
    # customer's read and their write.
    with owner_engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE recovery_case SET state = 'EXPIRED', "
                "terminal_reason = 'RECOVERY_WINDOW_ELAPSED' WHERE id = :c"
            ),
            {"c": str(case_id)},
        )

    with tenant_transaction(merchant_id, factory) as session:
        outcome = record_signal(
            session,
            _CONFIG,
            token,
            PartialArrangementSubmission(note="please"),
            correlation_id=uuid.uuid4(),
        )

    assert not outcome.accepted
    assert outcome.status_code == 409, (
        f"a write to a terminal case answered {outcome.status_code}; R19.C8 says 409 and names the "
        "Terminal_State"
    )
    assert outcome.detail == "EXPIRED", (
        "the refusal does not name the Terminal_State. It is the one thing the customer is "
        "entitled to be told: the case ended, and nothing they write now will be read"
    )
    assert _counts(owner_engine, merchant_id, case_id) == (0, 0, 0)

    with owner_engine.connect() as connection:
        rejected = connection.execute(
            text(
                "SELECT actor, decision->>'outcome', decision->>'detail' FROM audit_record "
                "WHERE merchant_id = :m AND case_id = :c AND event_type = "
                "'CUSTOMER_SIGNAL_REJECTED'"
            ),
            {"m": str(merchant_id), "c": str(case_id)},
        ).all()
        state = connection.execute(
            text(
                "SELECT state, executed_action_count, customer_message_count, "
                "decision_cycle_count FROM recovery_case WHERE id = :c"
            ),
            {"c": str(case_id)},
        ).one()

    assert len(rejected) == 1
    assert str(rejected[0][0]) == token.token_id
    assert rejected[0][1] == "CASE_TERMINAL"
    assert rejected[0][2] == "EXPIRED"
    assert tuple(state) == ("EXPIRED", 0, 0, 1), (
        f"the case moved: {tuple(state)}. R19.C8 requires the state and every counter to be left "
        "unchanged by a refused write"
    )
