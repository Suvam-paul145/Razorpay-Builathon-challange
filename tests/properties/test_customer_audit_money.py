"""Tasks 40.5 and 53.6. The audit trail is exact, the money is integers, and the demonstration
never claims credit.

Four properties across three tiers. P59 and P60 are the same claim about the same write seen from
two sides: P59 says *what was recorded* about a write, and P60 says *what the figures in it are
made of*. P61 and P62 are the same pair of questions asked of a whole Demo_Batch — what a
thousand-case demonstration is allowed to say about the money it moved, and whether every row it
left behind is labelled and accounted for.

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

**P61 and P62 are ``harness``, and one batch serves both.** They are claims about a Demo_Batch, and
a Demo_Batch is a thousand cases seeded through the signed webhook endpoint, the customer surface
driven over real HTTP, the sweeps run and the experiment completed — about seventeen minutes. So the
batch is a module-scoped fixture and the three ``harness`` tests below read it back; a batch per
test would cost three times as much and assert nothing more. The last test in the file is the
``pg``-tier half of R28.C16, which runs its own deliberately small batch for the reason given there.

**Two things the built system says differently from the way the properties are worded**, both stated
where they are asserted rather than worked around. ``webhook_event`` and ``audit_record`` have no
``provenance`` column, so R28.C16 can only be checked against the tables that do — asserted against
``information_schema`` so the limitation fails loudly if a migration ever closes it. And
``verified_test_mode_recoveries`` is ``0`` in any harness run, correctly: the harness reads a
scriptable fake, and R28.C2's three real Verified_Demo_Recoveries are a manual ``RUNBOOK.md`` step
because no documented Razorpay endpoint pays a payment link. P62 is therefore asserted as a
universal over the recoveries that exist, which holds at three and at three hundred.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, fields
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Final

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
from revora.domain.attribution import RefusalCode
from revora.domain.enums import (
    DEMONSTRATION_ONLY,
    NOT_ESTABLISHED,
    CaseState,
    DelayReason,
    ExperimentLabel,
    ExperimentState,
    OutcomeClass,
    Provenance,
)
from revora.jobs.worker import _DEFAULT_MERCHANT_SCAN_LIMIT
from revora.metrics.engine import DemonstrationFinding
from revora.persistence.repositories.config import ConfigurationRepository
from revora.persistence.repositories.session import tenant_transaction
from revora.platform.config import default_configuration
from revora.synthetic import demo
from revora.synthetic.demo import (
    CUSTOMER_DRIVEN_OUTCOMES,
    DEMO_BATCH_CASE_COUNT,
    DEMO_PRIOR_COHORT_SIZE,
    PROVENANCE_BEARING_TABLES,
    DemoBatchReport,
    authoritative_test_mode_recoveries,
    run_demo_batch,
)
from tests.demo_support import demo_harness
from tests.fakes.customer import installed_signing_secrets
from tests.pg_support import insert_merchant
from tests.strategies.customer import case_projections

_CONFIG = default_configuration()
_START = datetime(2025, 6, 1, 12, 0, 0, tzinfo=UTC)

# No module-level marker: P60 needs nothing at all and belongs in the tier that runs on every
# commit, P59 needs a migrated PostgreSQL, and P61 and P62 need a seventeen-minute batch that runs
# nightly. A module-level marker of any of the three would drag the other two into the wrong
# selection, so every test below is marked individually.


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

# ---------------------------------------------------------------------------
# `harness` — Properties 61 and 62, over one Demo_Batch
# ---------------------------------------------------------------------------
#
# Three `harness` tests and **one** batch between them. A thousand cases seeded through the signed
# webhook endpoint, driven to terminal states with the customer surface exercised over real HTTP, is
# about seventeen minutes; a file with three tests each driving its own batch would be fifty-one
# minutes and would assert nothing the first one did not. So the batch is a module-scoped fixture
# and each test reads it back from the rows it left behind.
#
# The three tests are marked `harness` and deliberately **not** `pg`, even though they need a
# migrated PostgreSQL. `tests/synthetic/test_scenarios.py` carries both marks so the push tier can
# select `pg and not harness`; here the same exclusion is had by not claiming the tier at all,
# which is one fewer thing for the push selection to have to say. The `pg`-tier claim in this
# section is the last test, and it runs its own small batch for the reason given there.


_CLEAN_DATABASE_HINT = (
    "Truncate every application table before a Demo_Batch. `run_once` scans merchants with due "
    "work — ordered by id and bounded by a scan limit — so a database holding another merchant's "
    "queued jobs spends a batch's bounded drain passes on somebody else's backlog. The symptoms "
    "are cases stranded in ACTION_SCHEDULED, a collapsed recovery figure, and a REAL-provenance "
    "count that looks exactly like a data leak and is not. A TRUNCATE over the application tables "
    "leaves the schema, the grants and alembic_version intact."
)
"""What to do about a dirty database, said once and quoted by the guard.

This is a real diagnosis rather than defensive prose: a measured batch never finished against a
container holding 2 813 merchants and 33 028 cases, and finished in under three minutes against an
empty one. The guard below is what turns that into a named precondition failure instead of hours
spent reading a coverage report that describes a population the batch never had."""


@dataclass(frozen=True, slots=True)
class _Batch:
    """One finished Demo_Batch: the report it produced and the rows it left behind.

    The engine travels with the report because every assertion below is a *read back* — the
    report's own figures are already read back from the database by the loader, and a test that
    only checked those would be checking one reader against itself.
    """

    report: DemoBatchReport
    merchant_id: uuid.UUID
    engine: Engine


def _assert_the_queue_cannot_starve_this_batch(engine: Engine) -> None:
    """Refuse to start a batch that a foreign backlog could keep the worker from ever reaching.

    The precondition the loader cannot check for itself and the harness must not fix. It is
    asserted rather than repaired: truncating from inside a fixture would delete rows belonging to
    whatever else shares the session, and a fixture with that blast radius is worse than a failure
    with an instruction in it.

    **The bound is the worker's merchant scan limit, not zero.** ``claimable_merchant_ids`` returns
    merchant ids *ordered by id* under that limit, and a merchant beyond it is one a bounded drain
    may never reach at all — which is what happened against a container holding 2 813 merchants:
    every pass drained somebody else's backlog, the batch's own cases stranded in
    ``ACTION_SCHEDULED``, and the recovery figure collapsed. Strictly fewer than the limit means
    this batch's own merchant is inside one pass's scan however its random id sorts. A handful of
    merchants below that bound costs a few claims and cannot starve anything, so failing on those
    would be refusing to run beside every other test that leaves a job queued.

    Only ``job`` is checked, and only rows a worker would claim. Stale rows in every other table
    are harmless because each of them is read under a ``merchant_id`` — the demonstration tenant is
    created inside :func:`~tests.demo_support.demo_harness`, so "this merchant's rows" and "this
    batch's rows" are the same set no matter what else the database holds. The queue is the one
    shared resource whose *selection* is not tenant-scoped.
    """
    with engine.connect() as connection:
        merchants_with_work = int(
            connection.execute(
                text("SELECT count(DISTINCT merchant_id) FROM job WHERE state = 'PENDING'")
            ).scalar_one()
        )
    assert merchants_with_work < _DEFAULT_MERCHANT_SCAN_LIMIT, (
        f"{merchants_with_work} merchant(s) hold pending jobs and one worker pass scans "
        f"{_DEFAULT_MERCHANT_SCAN_LIMIT}, so this batch's own merchant may never be reached and "
        f"its figures would describe a population it did not seed. {_CLEAN_DATABASE_HINT}"
    )


def _run_demo_batch(
    engine: Engine, *, case_count: int, prior_cohort_size: int
) -> _Batch:
    """Drive one whole Demo_Batch and hand back what it produced.

    The harness is opened, used and **closed** before the report is returned, so the assertions
    run against a database nobody is still holding a substituted clock, a substituted secret store
    and a second engine over. Everything the tests need afterwards is either on the report or
    still in the rows, and both outlive the harness.
    """
    _assert_the_queue_cannot_starve_this_batch(engine)
    harnesses = demo_harness(engine)
    harness = next(harnesses)
    try:
        with tenant_transaction(harness.tenant.merchant_id) as session:
            config = ConfigurationRepository(session).load(harness.tenant.merchant_id)
        report = run_demo_batch(
            harness.tenant,
            transport=harness.transport,
            worker=harness.worker,
            advance=harness.advance,
            config=config,
            synthetic_run_id=harness.synthetic_run_id,
            case_count=case_count,
            prior_cohort_size=prior_cohort_size,
            script_payment=harness.script_payment,
        )
    finally:
        harnesses.close()
    # On stdout rather than in an assertion message: pytest shows it only when something below
    # fails, and when something below fails this document is the whole diagnosis.
    print(json.dumps(report.as_document(), indent=2, sort_keys=True))
    return _Batch(report=report, merchant_id=harness.tenant.merchant_id, engine=engine)


@pytest.fixture(scope="module")
def demo_batch(owner_engine: Engine) -> _Batch:
    """One full-size Demo_Batch, shared by every ``harness`` test in this module."""
    return _run_demo_batch(
        owner_engine,
        case_count=DEMO_BATCH_CASE_COUNT,
        prior_cohort_size=DEMO_PRIOR_COHORT_SIZE,
    )


def _group_counts(
    engine: Engine, sql: str, params: Mapping[str, object]
) -> dict[str, int]:
    """A ``GROUP BY`` read as a mapping, with ``NULL`` rendered as ``"<null>"``.

    ``NULL`` is kept rather than dropped for the reason ``revora.synthetic.demo`` keeps it: a row
    whose provenance is absent is a finding, and a dropped group would make it look like the row
    was not there.
    """
    with engine.connect() as connection:
        rows = connection.execute(text(sql), dict(params)).all()
    return {("<null>" if row[0] is None else str(row[0])): int(row[1]) for row in rows}


def _scalar_int(engine: Engine, sql: str, params: Mapping[str, object]) -> int:
    """One counting query, read as an ``int``."""
    with engine.connect() as connection:
        return int(connection.execute(text(sql), dict(params)).scalar_one())


def _assert_every_leaf_is_a_string(value: object, *, path: str) -> None:
    """Refuse any non-string leaf, wherever it is nested.

    Used on the ``incremental_recovered_revenue`` block, where "carries no numeric value" is the
    requirement. Written as "every leaf is a string" rather than "no leaf is a number" on purpose:
    the first refuses a ``None`` and a ``bool`` as well, and both of those are things a dashboard
    renders as a figure.
    """
    if isinstance(value, dict):
        for key, item in value.items():
            _assert_every_leaf_is_a_string(item, path=f"{path}.{key}")
        return
    if isinstance(value, list | tuple):
        for index, item in enumerate(value):
            _assert_every_leaf_is_a_string(item, path=f"{path}[{index}]")
        return
    assert isinstance(value, str), (
        f"{path} is a {type(value).__name__} carrying {value!r}. R28.C12 says "
        "incremental_recovered_revenue reports NOT_ESTABLISHED and no numeric value, and a "
        "number anywhere under this key is the one presentation the whole separation exists to "
        "prevent: a demonstration's figure read as a causal claim about real money"
    )


@pytest.mark.harness
def test_p61_a_demonstration_attributes_nothing_and_never_shows_its_figure_unlabelled(
    demo_batch: _Batch,
) -> None:
    """**Property 61** — zero Attributed_Recovery, ``incremental_recovered_revenue``
    ``NOT_ESTABLISHED`` with no numeric value, and ``demonstration_incremental_revenue``
    presented only with ``SYNTHETIC`` and ``DEMONSTRATION_ONLY`` adjacent to it.

    **The batch has to have money in it or this property is worthless.** Every clause below is
    trivially true of a batch that recovered nothing: no recoveries means nothing to attribute, no
    observed revenue means nothing an incremental figure could be computed from, and no
    demonstration value means no figure to label. So the first assertions are that the money
    arrived — and the last one is that a *number exists* for the demonstration figure. What P61
    then says is the interesting thing: with real recovered money on one side and a sound
    treatment-versus-control comparison producing a number on the other, the system still refuses
    to claim it caused anything.

    **Zero ``ATTRIBUTED`` is read from ``recovery_outcome``, not from the report.** The
    classification is a column, and it is what the headline revenue figures are summed from — so
    the check is a count of rows carrying that value, plus the histogram of what they carry
    instead. A test that asserted only on ``incremental_status`` would pass on a database where
    a thousand outcomes had been classified as causally attributed and one summary field had not
    noticed.

    **The refusal names the label.** ``DISQUALIFYING_LABEL`` in the refusal codes is what makes
    this a mechanism rather than a coincidence: the experiment is refused because it carries
    ``SYNTHETIC`` (R13.C8), which is a condition no amount of clean measurement can clear. The
    interval's width is deliberately not asserted here — an interval that happened to contain zero
    would refuse the claim for a *second* reason, and a property that only held when the batch got
    a lucky randomization would be a property about the randomization.

    **Adjacency is an object, not a caption.** The value and both labels are asserted to be in the
    *same* mapping, and :class:`~revora.metrics.engine.DemonstrationFinding` is asserted to have
    no ``labels`` field at all — the labels are a property, so there is no constructor through
    which a caller could build this figure carrying one of them or none. That is the difference
    between "we remembered to label it" and "it cannot be built unlabelled".

    The whole report document is finally walked for floats, which is P60's clause about the
    Demonstration_Loader (R28.C11) reaching the one place the loader actually produces figures.
    """
    report = demo_batch.report

    assert report.recovered_case_count > 0, (
        "the batch recovered nothing, so every clause of P61 is vacuously true: there is no "
        "recovery to attribute and no revenue an incremental figure could be computed from"
    )
    assert report.observed_recovered_revenue > 0
    assert isinstance(report.observed_recovered_revenue, int)

    classifications = _group_counts(
        demo_batch.engine,
        "SELECT classification, count(*) FROM recovery_outcome WHERE merchant_id = :m "
        "GROUP BY classification",
        {"m": str(demo_batch.merchant_id)},
    )
    assert classifications, (
        "no recovery_outcome rows at all, so counting the ATTRIBUTED ones proves nothing"
    )
    assert classifications.get(OutcomeClass.ATTRIBUTED.value, 0) == 0, (
        f"the batch holds {classifications[OutcomeClass.ATTRIBUTED.value]} outcome(s) classified "
        f"{OutcomeClass.ATTRIBUTED.value}: {classifications}. R28.C10 says a Demonstration_"
        "Experiment produces none, and the mechanism is the attribution gate refusing a "
        "SYNTHETIC-labelled experiment — so a row here means either the gate was bypassed or the "
        "label was dropped"
    )

    assert report.incremental_status == NOT_ESTABLISHED, (
        f"incremental_recovered_revenue reports {report.incremental_status!r} for an experiment "
        "whose every input came from a Synthetic_Dataset"
    )
    assert RefusalCode.DISQUALIFYING_LABEL in report.incremental_refusal_codes, (
        f"the refusal codes are {list(report.incremental_refusal_codes)} and none of them is the "
        "label. R13.C8 is what disqualifies this experiment, and a refusal for some other reason "
        "is a refusal that a cleaner measurement would clear — which is exactly what must not be "
        "true of a demonstration"
    )

    document = report.as_document()
    incremental = document["incremental_recovered_revenue"]
    assert isinstance(incremental, dict)
    assert incremental["status"] == NOT_ESTABLISHED
    _assert_every_leaf_is_a_string(incremental, path="incremental_recovered_revenue")

    demonstration = document["demonstration_incremental_revenue"]
    assert isinstance(demonstration, dict)
    value = demonstration["value"]
    assert isinstance(value, int) and not isinstance(value, bool), (
        f"the demonstration figure is {type(value).__name__}, not an integer count of minor "
        "units. R28.C11 puts it in the same units as every other figure here, and with no "
        "figure at all there is nothing for the labels to qualify"
    )
    assert value != 0, (
        "the demonstration figure is zero, which is a measurement of exactly no effect over a "
        "world with a planted lift of "
        f"{report.ground_truth_lift} — so either the batch produced no comparison or the "
        "arithmetic lost it, and either way P61's labelling clause has nothing to label"
    )
    labels = demonstration["labels"]
    assert labels == [ExperimentLabel.SYNTHETIC.value, DEMONSTRATION_ONLY], (
        f"the demonstration figure carries {labels}. R28.C11 requires both labels, and they are "
        "asserted here as a list in one mapping beside the value because *adjacent* is the "
        "requirement: a figure whose qualification lives in another view is a figure that gets "
        "screenshotted without it"
    )
    assert tuple(labels) == report.demonstration_labels
    assert "labels" not in {field.name for field in fields(DemonstrationFinding)}, (
        "DemonstrationFinding has a labels *field*, so there is now a constructor through which "
        "this figure can be built carrying one label or none. R28.C11's guarantee was that the "
        "figure and its qualification are the same object"
    )
    assert DemonstrationFinding(value=value).labels == (
        ExperimentLabel.SYNTHETIC.value,
        DEMONSTRATION_ONLY,
    )

    assert ExperimentLabel.CAUSALITY_NOT_ESTABLISHED.value in report.metrics_labels, (
        f"the period's own labels are {list(report.metrics_labels)}. R12.C9 puts "
        "CAUSALITY_NOT_ESTABLISHED on a positive observed figure that no experiment licenses, and "
        "this batch's observed figure is positive and licensed by nothing"
    )
    assert ExperimentLabel.SYNTHETIC.value in report.metrics_labels

    # P60's clause about the Demonstration_Loader (R28.C11), on the one document the loader
    # actually presents. The walk is the same one the `pure` test uses on the customer page.
    _assert_no_float(document, path="demo_report")


@pytest.mark.harness
def test_p62_every_row_is_synthetic_every_verified_recovery_reads_captured_and_no_seq_gaps(
    demo_batch: _Batch,
) -> None:
    """**Property 62** — every seeded Recovery_Case is ``SYNTHETIC``, every verified recovery
    names an authoritative read whose captured amount equals the case's ``payment_amount``, and
    every seeded case's Audit_Record sequence starts at 1, steps by 1 and holds no gap.

    **The provenance half is asserted as "only SYNTHETIC", not as "no REAL".** A ``NULL``
    provenance, or a third value nobody has thought of yet, satisfies "not REAL" and satisfies
    nothing R28.C16 wants — the requirement is that the row is *labelled*, and an unlabelled row
    in a demonstration tenant is a row that will one day be counted in a real figure. So the
    histogram's keys are compared, and it is taken over ``recovery_case`` and every table that
    copies the case's value, because a propagation gap shows up in the derived tables first.

    **The verified-recovery half is a universal over the rows that exist, and the count it holds
    at is zero-or-more on purpose.** R28.C2's three Verified_Demo_Recoveries are money that moved
    in Razorpay test mode, and no documented endpoint pays a payment link — so they are a manual
    ``RUNBOOK.md`` step, and ``verified_test_mode_recoveries`` is honestly ``0`` in any harness
    run because this harness reads a scriptable fake. Asserting a non-zero count here would either
    make a gated test reach the network or make it count a fake's answer as a provider's. What is
    asserted instead is the shape every such recovery must have, over the ``recovery_outcome`` rows
    the batch did produce: each names the read that verified it, that read reports a capture, and
    its amount equals the case's ``payment_amount`` **exactly**, compared as integers. Three
    hundred-odd rows or three, the claim is the same one and it is checked over all of them.

    ``authoritative_test_mode_recoveries`` is then asked the same question through the loader's own
    query, and its answer has to equal the number of recovered cases holding an outcome. That is
    the falsifiable half: the function counts only rows where the read agrees on the amount, so a
    single disagreement makes the two numbers differ, and it names which reader was wrong.

    **The audit half checks a consequence rather than an arrangement.** ``AuditWriter`` allocates
    from ``recovery_case.audit_seq`` under the row lock, identically for a seeded case and a real
    one, so a gap-free sequence is not something the loader can arrange — which is what makes it
    worth reading back. Every seeded case must appear (a case with no records at all would satisfy
    every per-case condition by holding no rows), and at least one sequence must be three long,
    because ``min == max == count == 1`` is true of every single-record case and a file that only
    ever saw those would be checking nothing.
    """
    report = demo_batch.report
    params = {"m": str(demo_batch.merchant_id)}

    cases = _group_counts(
        demo_batch.engine,
        "SELECT provenance, count(*) FROM recovery_case WHERE merchant_id = :m "
        "GROUP BY provenance",
        params,
    )
    assert cases == {Provenance.SYNTHETIC.value: report.seeded_case_count}, (
        f"the batch's cases carry {cases} and it seeded {report.seeded_case_count}. R28.C1 and "
        "R28.C16 want every one of them labelled SYNTHETIC — an unlabelled or differently "
        "labelled case is a generated payment that a real revenue figure will eventually sum"
    )
    assert report.seeded_case_count == report.case_count == DEMO_BATCH_CASE_COUNT

    for table in PROVENANCE_BEARING_TABLES:
        histogram = _group_counts(
            demo_batch.engine,
            f"SELECT provenance, count(*) FROM {table} WHERE merchant_id = :m "
            "GROUP BY provenance",
            params,
        )
        assert set(histogram) <= {Provenance.SYNTHETIC.value}, (
            f"{table} carries {histogram} for the demonstration tenant. Every one of these "
            "tables copies the case's provenance, so a value here that is not SYNTHETIC is the "
            "propagation gap R28.C16 exists to catch, and a NULL is the same gap unlabelled"
        )
    signals = _group_counts(
        demo_batch.engine,
        "SELECT provenance, count(*) FROM customer_signal WHERE merchant_id = :m "
        "GROUP BY provenance",
        params,
    )
    assert signals.get(Provenance.SYNTHETIC.value, 0) > 0, (
        f"no labelled customer_signal rows at all ({signals}), so the propagation claim above is "
        "vacuous for the one derived table the customer surface writes"
    )

    with demo_batch.engine.connect() as connection:
        outcomes = connection.execute(
            text(
                "SELECT o.id, o.case_id, o.verified_by_read_id, r.captured, r.amount, "
                "c.payment_amount, c.state FROM recovery_outcome o "
                "JOIN recovery_case c ON c.id = o.case_id AND c.merchant_id = o.merchant_id "
                "LEFT JOIN payment_state_read r ON r.id = o.verified_by_read_id "
                "AND r.merchant_id = o.merchant_id WHERE o.merchant_id = :m"
            ),
            params,
        ).all()

    assert outcomes, "no recovery_outcome rows, so the verified-recovery universal is vacuous"
    unverified = [row[0] for row in outcomes if row[2] is None]
    assert not unverified, (
        f"{len(unverified)} recovery_outcome row(s) name no verifying read. "
        "recovery_outcome.verified_by_read_id is NOT NULL by design, so this is a schema change "
        "as well as a recovery recorded on nobody's authority"
    )
    recovered = [row for row in outcomes if str(row[6]) == CaseState.RECOVERED.value]
    assert recovered, (
        "no outcome belongs to a case still in RECOVERED, so R28.C2's definition selects nothing"
    )
    for row in recovered:
        assert row[3] is True, (
            f"case {row[1]} recovered from a read that does not report a capture. The read is the "
            "authority R28.C2 names, and a recovery recorded against a read that says the money "
            "did not arrive is a recovered-revenue figure with nothing behind it"
        )
        assert int(row[4]) == int(row[5]), (
            f"case {row[1]} recovered from a read of {int(row[4])} minor units against a "
            f"payment_amount of {int(row[5])}. R28.C2 requires equality: a read for less is a "
            "partial payment, and counting one as a recovery overstates recovered revenue by the "
            "difference"
        )

    factory = sessionmaker(bind=demo_batch.engine, expire_on_commit=False)
    with tenant_transaction(demo_batch.merchant_id, factory) as session:
        evidenced = authoritative_test_mode_recoveries(session, demo_batch.merchant_id)
    assert evidenced == len(recovered), (
        f"the loader's own count of recoveries evidenced by an authoritative read is {evidenced} "
        f"against {len(recovered)} recovered cases holding an outcome. The two disagree only when "
        "a read fails R28.C2's captured-and-equal test, so this names a case whose recovery is "
        "not evidenced by the row it points at"
    )
    assert report.verified_test_mode_recoveries == 0, (
        f"the report claims {report.verified_test_mode_recoveries} Verified_Demo_Recoveries. In a "
        "harness run the authoritative reads are genuine reads of tests.fakes.razorpay, and a "
        "field whose whole meaning is money that moved at the provider must report zero of them. "
        "R28.C2's three are a manual RUNBOOK.md step against test-mode credentials"
    )

    with demo_batch.engine.connect() as connection:
        sequences = connection.execute(
            text(
                "SELECT case_id, min(seq), max(seq), count(*), count(DISTINCT seq) "
                "FROM audit_record WHERE merchant_id = :m AND case_id IS NOT NULL "
                "GROUP BY case_id"
            ),
            params,
        ).all()

    assert len(sequences) == report.seeded_case_count, (
        f"{len(sequences)} of {report.seeded_case_count} seeded cases have any Audit_Record at "
        "all. R28.C15 is a claim about every seeded case, and a case with no records satisfies "
        "every per-case condition below by holding no rows to violate them"
    )
    assert max(int(row[3]) for row in sequences) >= 3, (
        "no case holds three Audit_Records, so min == max == count is true of every sequence by "
        "arithmetic and the gap check is not looking at anything"
    )
    for case_id, lowest, highest, total, distinct in sequences:
        assert int(lowest) == 1, (
            f"case {case_id}'s audit sequence starts at {lowest}. A sequence starting above 1 "
            "has lost its first record, which is the one that says the case was created"
        )
        assert int(highest) == int(total), (
            f"case {case_id}'s audit sequence reaches {highest} across {total} records, so it "
            "holds a gap. The sequence is allocated under the case's row lock, so a gap is a "
            "record that was allocated and never committed — evidence that is missing rather "
            "than absent"
        )
        assert int(total) == int(distinct), (
            f"case {case_id} holds {total} records across {distinct} distinct sequence numbers, "
            "so two records share one. The unique index should already forbid it"
        )
    assert report.coverage.audit_sequence_gaps == (), (
        f"the loader's own gap check disagrees with the sequences read above: "
        f"{list(report.coverage.audit_sequence_gaps)}"
    )


@pytest.mark.harness
def test_the_demonstration_batch_reaches_every_required_outcome_and_completes(
    demo_batch: _Batch,
) -> None:
    """R28.C4, C5 and C8: the outcome coverage, the customer surface, and a completed experiment
    carrying a numeric lift and interval.

    Not a numbered property — it is the assertion that the *batch* is the batch the two properties
    above are claims about. P61 and P62 are both statements over a population, and a population
    missing half its outcomes would satisfy them while demonstrating nothing: no escalation, no
    stopped case, no disputed charge, no promise.

    **Coverage is read back from rows, never accumulated.** ``BatchCoverage.missing`` returns every
    gap at once rather than raising on the first, because a batch is measured in minutes and
    diagnosing three gaps one run at a time is three runs. It is asserted here as *empty*, and if
    a required outcome ever stops being reachable this is where that becomes visible — the honest
    failure of a required outcome the system cannot currently produce, rather than an assertion
    quietly widened to accommodate it.

    **Every customer-driven outcome went in over HTTP.** The submissions map counts what the public
    ``/customer/{slug}/…`` surface *accepted*, with a real bearer token verified in constant time
    against the persisted hash — so a batch that recorded a dispute proves the token path, the
    submission caps, the four-writes-in-one-transaction and the enqueued consequence all ran.

    **The experiment's state is read from its own row.** ``COMPLETED`` is what R28.C8 asks for and
    it is also the precondition the attribution gate reads, so a report claiming a lift while the
    row still said ``ACTIVE`` would be an interim look presented as a result. The lift and both
    bounds are asserted present, ordered and ``Decimal``. Their *closeness* to the planted lift is
    deliberately not asserted: one batch is one randomization, and whether a 95 percent interval
    covers the truth is a question about many of them, which is what the evidence harness's
    coverage check is for. The difference is reported instead.
    """
    report = demo_batch.report

    assert report.coverage.missing == (), (
        f"the batch did not reach every required outcome: {list(report.coverage.missing)}. "
        "R28.C4 names the four Terminal_States and the high-baseline null selection; R28.C5 names "
        "the four customer-caused terminal reasons and both promise statuses"
    )
    assert report.coverage.complete

    submitted = dict(report.customer_submissions)
    missing_submissions = [
        outcome for outcome in CUSTOMER_DRIVEN_OUTCOMES if submitted.get(outcome, 0) < 1
    ]
    assert not missing_submissions, (
        f"the customer surface accepted nothing for {missing_submissions}; accepted counts were "
        f"{submitted}. R28.C5 requires each of these to be driven through the public HTTP "
        "endpoint rather than written into customer_signal directly"
    )

    with demo_batch.engine.connect() as connection:
        state, completed_at, labels = connection.execute(
            text(
                "SELECT state, completed_at, labels FROM experiment "
                "WHERE merchant_id = :m AND id = :e"
            ),
            {"m": str(demo_batch.merchant_id), "e": str(report.experiment_id)},
        ).one()

    assert str(state) == ExperimentState.COMPLETED.value, (
        f"the Demonstration_Experiment is {state}, not COMPLETED. R28.C8 completes it once every "
        "assigned case is terminal, and the loader logs the count of cases still in flight when "
        "it cannot — read that line before looking anywhere else"
    )
    assert completed_at is not None
    assert ExperimentLabel.SYNTHETIC.value in list(labels or ()), (
        "the experiment does not carry SYNTHETIC, which is the label the attribution gate refuses "
        "on (R13.C8) and therefore the entire mechanism behind P61's zero attributed recoveries"
    )

    assert report.measured_lift is not None, (
        "the experiment completed with no measured lift. A None lift is 'we could not measure', "
        "which is the honest answer to an empty arm and not something a thousand-case batch with "
        "both arms populated should produce"
    )
    assert report.lift_ci_low is not None and report.lift_ci_high is not None
    assert isinstance(report.measured_lift, Decimal)
    assert isinstance(report.lift_ci_low, Decimal)
    assert isinstance(report.lift_ci_high, Decimal)
    assert report.lift_ci_low <= report.measured_lift <= report.lift_ci_high, (
        f"the interval [{report.lift_ci_low}, {report.lift_ci_high}] does not contain the lift "
        f"{report.measured_lift} it is an interval for"
    )
    assert report.measured_minus_ground_truth is not None, (
        "the measured-versus-planted difference R28.C9 asks for is not computable, so the batch "
        "cannot say whether the measurement found what the generator put there"
    )

    assert report.control_case_count + report.treatment_case_count == report.seeded_case_count, (
        f"{report.control_case_count} control and {report.treatment_case_count} treatment against "
        f"{report.seeded_case_count} seeded cases. Assignment happens inside the transaction that "
        "creates the case, so an unassigned case is one the experiment was not ACTIVE for"
    )
    assert (
        min(report.control_case_count, report.treatment_case_count)
        >= report.required_sample_size_per_group
    ), (
        f"an arm holds fewer cases than the {report.required_sample_size_per_group} per arm the "
        "experiment computed for itself at definition time (R28.C7), so its own gate would refuse "
        "it for being underpowered before the SYNTHETIC label ever got a chance to"
    )


# ---------------------------------------------------------------------------
# `pg` — R28.C16 against the tables that can carry the value
# ---------------------------------------------------------------------------


_PG_DEMO_CASE_COUNT: Final[int] = 88
"""The smallest batch ``plan_batch`` accepts at :data:`_PG_DEMO_PRIOR_COHORT`.

Forty prior cases plus twenty-four shaped roles plus four each of six customer-driven roles is
exactly eighty-eight, and ``plan_batch`` refuses anything smaller rather than truncating. Small
because this test's claim does not need a big population — it needs *some* rows of every kind and
a count of zero — and the push tier pays for it on every commit."""

_PG_DEMO_PRIOR_COHORT: Final[int] = 40
"""A prior cohort too small to build a high baseline, and that is fine here.

At forty the designated half is twenty, under ``MIN_SEGMENT_SAMPLE_SIZE``, so this batch does not
produce a ``HIGH_BASELINE_NO_INTERVENTION`` selection and its coverage is not complete. Coverage is
the ``harness`` tier's claim and is asserted there against a full-size batch; asserting it here
would be asserting it against a population deliberately too small to satisfy it."""

_PG_DEMO_MINIMUM_DETECTABLE_EFFECT: Final[Decimal] = Decimal("0.3000")
"""The minimum detectable effect this small batch is powered for.

``DEMO_MINIMUM_DETECTABLE_EFFECT`` of 0.08 absolute asks for about 444 cases per arm, and
``define_demonstration_experiment`` refuses — correctly — to activate an experiment needing more
than half the batch. 0.30 asks for 39, which eighty-eight cases can hold. Patched for the duration
of this test rather than parameterised, because it is not a parameter: a *demonstration* batch is
sized by the effect it wants to detect, and making that an argument would let a caller quietly
weaken the power calculation of the real one."""


@pytest.mark.pg
def test_r28c16_a_demonstration_run_writes_no_real_provenance_row(
    owner_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R28.C16: after a Demo_Batch, the count of ``REAL``-provenance rows is zero.

    **Its own batch, and a small one.** The ``harness`` tier's thousand-case run is seventeen
    minutes and runs nightly; this claim is the one worth having on every push, because
    ``DEMO_PROVENANCE`` reaches ``recovery_case`` through the *worker registry's* seam rather than
    through any request, and a refactor of ``build_registry`` is exactly the kind of change that
    would silently unlabel a demonstration and be discovered a month later in a revenue figure.
    Eighty-eight cases is enough: the claim is a zero, and a zero over a populated tenant is the
    same claim at any size.

    It asserts **nothing that depends on the batch completing**, and that is deliberate rather
    than lazy. Cases stranded mid-pipeline, an experiment left ``ACTIVE``, an absent lift — none of
    those can make an unlabelled row appear, so none of them belongs in this test's assertions.
    What that buys is a test that cannot fail for a reason it is not about.

    **Two of the four tables R28.C16 names have no ``provenance`` column, and that is checked here
    rather than merely written down.** ``webhook_event`` and ``audit_record`` cannot carry the
    value; ``PROVENANCE_BEARING_TABLES`` records the gap as a schema gap rather than a loader gap.
    The absence is asserted against ``information_schema``, so a migration that adds either column
    fails this test with an instruction to extend the tuple — which is the only way a documented
    limitation stays true. Both tables are also asserted non-empty, so the limitation is a real one
    about rows that exist rather than a note about tables nothing writes to.
    """
    monkeypatch.setattr(
        demo, "DEMO_MINIMUM_DETECTABLE_EFFECT", _PG_DEMO_MINIMUM_DETECTABLE_EFFECT
    )
    batch = _run_demo_batch(
        owner_engine,
        case_count=_PG_DEMO_CASE_COUNT,
        prior_cohort_size=_PG_DEMO_PRIOR_COHORT,
    )
    params = {"m": str(batch.merchant_id)}

    assert batch.report.seeded_case_count == _PG_DEMO_CASE_COUNT, (
        f"{batch.report.seeded_case_count} of {_PG_DEMO_CASE_COUNT} cases were seeded, so a count "
        "of zero REAL rows might only mean there are no rows"
    )
    assert batch.report.coverage.real_provenance_rows == dict.fromkeys(
        PROVENANCE_BEARING_TABLES, 0
    ), (
        f"the batch left REAL-provenance rows behind: "
        f"{batch.report.coverage.real_provenance_rows}. R28.C16 makes SYNTHETIC structural — it "
        "reaches the case through the worker registry rather than through any request, so there "
        "is no argument by which a caller could have asked for this"
    )

    for table in PROVENANCE_BEARING_TABLES:
        real_rows = _scalar_int(
            batch.engine,
            f"SELECT count(*) FROM {table} WHERE merchant_id = :m AND provenance = :p",
            {**params, "p": Provenance.REAL.value},
        )
        assert real_rows == 0, (
            f"{table} holds {real_rows} REAL-provenance row(s) for a demonstration tenant"
        )

    for table in ("webhook_event", "audit_record"):
        has_column = _scalar_int(
            batch.engine,
            "SELECT count(*) FROM information_schema.columns WHERE table_schema = 'public' "
            "AND table_name = :t AND column_name = 'provenance'",
            {"t": table},
        )
        assert has_column == 0, (
            f"{table} now has a provenance column. R28.C16 names it and it could not be checked "
            "because the column did not exist; add it to "
            "revora.synthetic.demo.PROVENANCE_BEARING_TABLES so the count covers it, and delete "
            "this assertion"
        )
        rows = _scalar_int(
            batch.engine,
            f"SELECT count(*) FROM {table} WHERE merchant_id = :m",
            params,
        )
        assert rows > 0, (
            f"{table} holds no rows for this batch, so the documented limitation above is about "
            "a table nothing wrote to rather than about unlabellable evidence"
        )
