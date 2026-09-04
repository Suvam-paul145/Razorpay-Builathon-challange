"""The startup schema-revision check.

A worker running against an older schema than the API is a class of bug that is
very hard to diagnose, because it does not look like a schema problem. It looks
like a wrong recovery number, or a case that never leaves a state, and the
investigation starts in the wrong place.

So both roles verify at startup that the migration revision in the database matches
the revision this build expects, and refuse to serve on a mismatch. Refusing is the
whole point: a process that starts and then behaves subtly differently is worse than
one that does not start, because the first failure is silent and the second is
loud and immediate.

:data:`EXPECTED_REVISION` is the revision this build was written against. It is
updated by whoever adds a migration, in the same commit as the migration, which is
what makes the check meaningful — a revision read from the migration directory at
runtime would agree with itself no matter what the database said.
"""

from __future__ import annotations

from typing import Final

from sqlalchemy import Engine, text

__all__ = [
    "EXPECTED_REVISION",
    "SchemaRevisionMismatchError",
    "current_revision",
    "verify_schema_revision",
]

EXPECTED_REVISION: Final[str] = "0018"
"""The head this build expects. Bump in the same commit as a new migration.

``0005`` added ``experiment_result`` and ``memory_observation.diagnosis_method``; ``0006``
adds ``merchant_session``; ``0007`` widens the three duration-in-milliseconds columns to
``bigint``, because ``integer`` milliseconds runs out after 24.8 days and a detection
backfill over a longer window crashed on the very events it existed to rescue; ``0008``
adds the customer response loop's four tables, ``recovery_case.next_review_at``,
``execution_intent.effect_kind``, ``ai_invocation.call_kind``, and splits the blended
``action_cost`` into ``financial_cost`` and ``communication_cost``.

``0009`` seeds ``PAYMENT_LINK_FINANCIAL_COST`` and ``MESSAGE_COMMUNICATION_COST`` as
``app_config`` rows for the sentinel tenant (R31.C11). It is a data-only migration, so a
build expecting ``0009`` against an ``0008`` database would not crash — it would load both
bounds from their placeholders and report them in ``Configuration.defaulted_keys``, and the
API and worker bootstrap call ``load_strict``, which refuses. Bumping the constant makes the
refusal happen at startup with a message that names the missing revision, rather than at the
first configuration load with one that names a bound.

``0010`` seeds ``WAIT_REVIEW_INTERVAL`` and ``REVIEW_SWEEP_INTERVAL`` for the same tenant
(R30.C3, R30.C5), and it is data-only for the same reason and with the same consequence: a
build expecting ``0010`` against an ``0009`` database is refused at startup rather than
discovering the two missing rows when the first case chooses restraint.

``0011`` seeds ``CUSTOMER_TOKEN_LIFETIME`` and ``CUSTOMER_TOKEN_MAX_SUBMISSIONS`` (R18.C2,
R18.C9), also data-only. The consequence of *not* bumping here is worse than for the two
before it: both bounds govern a credential reachable without a session, and a build that fell
back to their placeholders would mint tokens under a lifetime nobody configured and bound
submissions by a number the settings screen could not show. Refusing at startup is the
difference between a misconfiguration and a silently weaker credential.

``0012`` seeds the public customer surface's four bounds — ``CUSTOMER_PAGE_RATE_LIMIT``,
``CUSTOMER_PAGE_SOURCE_RATE_LIMIT``, ``MAX_CUSTOMER_SIGNALS_PER_CASE`` and
``DELAY_NOTE_MAX_LENGTH`` (R29.C1, R19.C7, R20.C2) — and is data-only like the three before it.
Two of the four govern the only endpoint reachable without a session, and the other two bound
what one customer can write and how much free text is retained about them, so a build falling
back to placeholders would be enforcing numbers the settings screen could not show on the one
surface where an operator most needs to see them.

``0014`` seeds the three sweep intervals the ticker role needs —
``DETECTION_GAP_BACKFILL_INTERVAL``, ``CALIBRATION_REPORT_INTERVAL`` and
``CUSTOMER_DATA_RETENTION_SWEEP_INTERVAL`` — and is data-only like the five before it. The
consequence of not bumping here is a new shape: the ticker prices every periodic sweep from a
bound and *refuses* a kind it cannot price, so a build expecting ``0014`` against an ``0013``
database would load all three from their placeholders, ``load_strict`` would refuse, and the
ticker would not start. Which is the correct outcome and the reason to bump — a schedule that
started and silently ran three of its seven sweeps on a guessed interval is exactly the failure
the whole role exists to remove, and it would present as "the retention sweep is late" rather
than as "the database was not migrated".

``0016`` seeds the five bounds the Promise_To_Pay capture needs —
``PROMISE_MIN_LEAD_TIME``, ``PROMISE_FOLLOW_UP_OFFSET``, ``PROMISE_WINDOW_SAFETY_MARGIN``,
``MAX_PROMISES_PER_CASE`` and ``PROMISE_SWEEP_INTERVAL`` (R23.C2, C3, C7, C13) — and is
data-only like the six seeds before it. The consequence of not bumping here is the sharpest of
the set, because two of the five decide what is *storable* rather than what is permitted:
``follow_up_at < window_end_at_snapshot`` is a ``CHECK`` from ``0008``, and the safety margin is
what keeps the computed Follow_Up_Instant inside it. A build that loaded the margin from a
placeholder would be computing a clamp against a number nobody configured on the one endpoint
reachable without a session, and the failure would present as a 503 to a well-formed promise
rather than as "the database was not migrated". ``load_strict`` refuses first, which is why the
bump is the fix.

``0017`` widens ``ck_recommendation_candidate_exclusion_reason_enum`` with
``NO_PROMISE_RECORDED`` and ``PROMISE_DATE_NOT_REACHED``, the two grounds R24 excludes
``PROMISE_TO_PAY_FOLLOW_UP`` on. It is the first schema-changing revision since ``0015`` and it
is *not* data-only, which changes the failure mode rather than only the message: a build
expecting ``0017`` against an ``0016`` database would rank the follow-up, exclude it correctly,
and then fail the ``INSERT`` that records why — inside the decision cycle's transaction, so a
case holding a promise would stall its whole cycle. Refusing at startup is the difference
between that and one loud message naming the missing revision.

``0018`` widens the same ``CHECK`` once more, with ``LIVE_PAYMENT_LINK_EXISTS`` — the ground on
which ``PAYMENT_LINK`` is withheld from a case that already holds a live link (R24.C10 read at
system scope). Schema-changing like ``0017`` and with the same failure mode, but reachable on a
much wider set of cases: *every* case on its second decision cycle after a confirmed link asks
this question, not only the ones holding a promise. A build expecting ``0018`` against an ``0017``
database would exclude the duplicate correctly and then fail the ``INSERT`` recording why, inside
the decision cycle's transaction — so the cycle stalls on exactly the cases where the alternative
to stalling is sending a customer a second payable link.

``0008`` is the bump that matters most so far, because the ``action_cost`` drop makes the
schemas mutually incompatible in both directions: a build expecting ``0007`` writing
against an ``0008`` database fails on the first estimate insert, and a build expecting
``0008`` against ``0007`` fails on the first read. Refusing to start is the difference
between one loud failure and a worker that looks healthy while every decision cycle it
touches dies mid-pipeline.

The bump was forgotten when 0005 was written and the pg tier caught it immediately, which
is the whole reason this constant is hand-maintained rather than read from the migration
directory: a value derived from the files present can never disagree with them, and so can
never detect a database that has not been migrated."""


class SchemaRevisionMismatchError(RuntimeError):
    """The database schema is not the one this build expects.

    Carries both revisions so the message is enough to decide what to do: if the
    database is behind, run migrations; if it is ahead, this build is the old one
    and should not be serving.
    """

    def __init__(self, found: str | None, expected: str) -> None:
        self.found = found
        self.expected = expected
        detail = "no alembic_version row" if found is None else f"found {found!r}"
        super().__init__(
            f"schema revision mismatch: expected {expected!r}, {detail}. "
            "Run migrations before starting this process."
        )


def current_revision(engine: Engine) -> str | None:
    """The revision recorded in ``alembic_version``, or ``None`` if unmigrated.

    Reads the table directly rather than through Alembic's own API so the check
    costs one query and no migration-environment import at startup.
    """
    with engine.connect() as connection:
        exists = connection.execute(
            text("SELECT to_regclass('public.alembic_version') IS NOT NULL")
        ).scalar_one()
        if not exists:
            return None
        row = connection.execute(text("SELECT version_num FROM alembic_version")).first()
        return None if row is None else str(row[0])


def verify_schema_revision(engine: Engine, *, expected: str = EXPECTED_REVISION) -> str:
    """Return the current revision, or refuse to continue.

    Raises:
        SchemaRevisionMismatchError: if the database is unmigrated or on another
            revision. Callers must not catch this and continue — it is the one
            startup failure that should stop the process.
    """
    found = current_revision(engine)
    if found != expected:
        raise SchemaRevisionMismatchError(found, expected)
    return found
