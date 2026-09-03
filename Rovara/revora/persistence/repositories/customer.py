"""The customer response loop's four repositories.

Every function here takes ``merchant_id`` as a required first argument, like every
other read and write in this package. That matters more on these four tables than on
any others: they are the only ones a request with no session can cause rows in, so the
tenant on a customer request comes from the ``recovery_case`` the token names and from
nothing the request carries. **There is deliberately no cross-merchant function to
call by accident** — not a "find this token anywhere" helper, not a global sweep read.

Four operations are the load-bearing ones and are worth reading before the rest.

:meth:`CustomerAccessTokenRepository.increment_accepted_submissions` is the durable
bound of R19.C5. It is a single conditional ``UPDATE ... RETURNING`` that compares
against the configured maximum inside the statement, so the check and the increment
cannot be separated by a concurrent request. The rate limiter is a coarse flood guard
on the read path and is per process; this is the bound no number of replicas can
exceed. ``None`` means the bound was already reached and is a normal answer, not a
failure.

:meth:`CustomerAccessTokenRepository.revoke_for_case` returns how many rows it changed,
so a second revoke of an already-revoked case is distinguishable from the first. That is
what stops a repeated terminal transition writing a second audit record claiming a
second revocation.

:meth:`ContactSuppressionRepository.insert_if_absent` is an ``INSERT ... ON CONFLICT DO
NOTHING`` against ``uq_contact_suppression_merchant_id_scope_key``. A second hard stop
on the same scope is idempotent rather than a second row nobody reconciles, and two
concurrent submissions reach it together and exactly one wins — the guarantee is the
index, not a check-then-insert that would race. :meth:`PromiseToPayRepository.
insert_if_absent` is the same shape against ``uq_promise_to_pay_merchant_id_case_id``,
which is ``MAX_PROMISES_PER_CASE = 1`` as a backstop behind the application check.

:meth:`ContactSuppressionRepository.in_force` is on the policy hot path — every decision
that could produce a customer-visible action does this lookup — and reads only the
unreleased rows, served by ``ix_contact_suppression_in_force``. A released suppression is
history and must not be read here.

**Nothing in this module transitions a case, evaluates policy or calls a provider.** The
reads and writes are the persistence side of the loop; every consequence is applied by
the worker through ``apply_transition``, which stays the only writer of
``recovery_case.state``.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert

from revora.domain.enums import CustomerSignalKind, PromiseStatus, TokenRevocationReason
from revora.persistence.models.customer import (
    ContactSuppression,
    CustomerAccessToken,
    CustomerSignal,
    PromiseToPay,
)
from revora.persistence.repositories.base import MerchantScopedRepository, rows_affected
from revora.persistence.repositories.session import for_update, for_update_skip_locked

__all__ = [
    "ContactSuppressionRepository",
    "CustomerAccessTokenRepository",
    "CustomerSignalRepository",
    "PromiseToPayRepository",
]

_FOLLOW_UP_PENDING: tuple[str, ...] = (
    PromiseStatus.RECORDED.value,
    PromiseStatus.FOLLOW_UP_SCHEDULED.value,
)
"""The two statuses ``ix_promise_to_pay_due_for_follow_up`` covers, rendered from the
enum so the query and the index predicate cannot disagree about which rows exist."""


class CustomerAccessTokenRepository(MerchantScopedRepository[CustomerAccessToken]):
    """The second credential's repository: one indexed lookup and a bounded counter."""

    model = CustomerAccessToken

    # -- writes ----------------------------------------------------------------

    def insert(
        self, merchant_id: uuid.UUID, *, values: Mapping[str, object]
    ) -> CustomerAccessToken:
        """Stage one token row and flush it so its id is available.

        Flushed rather than left pending because the caller mints inside
        ``execute_approved_action``'s first transaction, alongside the intent insert and
        before the provider call, and needs the row to exist before it builds the URL
        the message carries. A failed mint rolls that transaction back, so no intent
        exists, no counter moved and no call went out.
        """
        row = CustomerAccessToken(**dict(values))
        self.add(merchant_id, row)
        self.session.flush()
        return row

    def revoke_for_case(
        self,
        merchant_id: uuid.UUID,
        case_id: uuid.UUID,
        *,
        moment: datetime,
        reason: TokenRevocationReason,
    ) -> int:
        """Revoke every live token of one case. Returns how many were revoked.

        The bulk revoke behind R18.C8 and R21.C10: a case entering a Terminal_State, or
        a ``contact_suppression`` row covering it, ends the customer's access. Served by
        ``ix_customer_access_token_merchant_id_case_id``.

        Only the unrevoked rows are touched, so the reason already recorded on an
        earlier revocation is never overwritten and the returned count is the number of
        tokens this call actually ended. A second pass returns zero, which is what lets
        the caller write one audit record rather than one per attempt.
        """
        return rows_affected(
            self.session.execute(
                update(CustomerAccessToken)
                .where(
                    CustomerAccessToken.merchant_id == merchant_id,
                    CustomerAccessToken.case_id == case_id,
                    CustomerAccessToken.revoked_at.is_(None),
                )
                .values(revoked_at=moment, revocation_reason=reason.value)
            )
        )

    def increment_accepted_submissions(
        self, merchant_id: uuid.UUID, token_id: str, *, max_submissions: int
    ) -> int | None:
        """Count one accepted submission, or refuse because the bound is reached.

        Returns the new count, or ``None`` when the token already sits at
        ``max_submissions`` — a normal answer that the caller turns into a refusal, not
        an error. The comparison lives in the ``WHERE`` clause so the check and the
        increment are one statement: two concurrent submissions cannot both read four
        and both write five.

        ``max_submissions`` is passed in rather than read here because
        ``CUSTOMER_TOKEN_MAX_SUBMISSIONS`` is a versioned configuration row, and a
        repository that resolved it would make raising the bound invisible at the call
        site that depends on it. It is deliberately not a check constraint for the same
        reason: encoding today's value of 5 in the schema would make raising it a
        migration.
        """
        if max_submissions < 0:
            raise ValueError("max_submissions must not be negative")
        statement = (
            update(CustomerAccessToken)
            .where(
                CustomerAccessToken.merchant_id == merchant_id,
                CustomerAccessToken.token_id == token_id,
                CustomerAccessToken.accepted_submission_count < max_submissions,
            )
            .values(
                accepted_submission_count=CustomerAccessToken.accepted_submission_count + 1
            )
            .returning(CustomerAccessToken.accepted_submission_count)
        )
        allocated = self.session.execute(statement).scalar_one_or_none()
        return None if allocated is None else int(allocated)

    # -- reads -----------------------------------------------------------------

    def by_token_id(
        self, merchant_id: uuid.UUID, token_id: str
    ) -> CustomerAccessToken | None:
        """The one row for a handle, or ``None``.

        The verification lookup, served by
        ``uq_customer_access_token_merchant_id_token_id``. Exactly one indexed read:
        the caller then loops over every active signing secret accumulating with ``|=``
        and never breaking early, so the time taken is independent of which secret
        matched and of whether any did.

        ``None`` and a bad signature must fold into one branch at the caller returning
        identical status and body. A missing row is not a distinguishable outcome.
        """
        statement = self.scoped(merchant_id).where(CustomerAccessToken.token_id == token_id)
        return self.session.execute(statement).scalar_one_or_none()

    def lock_by_token_id(
        self, merchant_id: uuid.UUID, token_id: str
    ) -> CustomerAccessToken | None:
        """Read a token and hold its row for the rest of the transaction.

        The submission path takes this before it counts anything, so the bound check,
        the counter increment and the signal insert are one serialized unit. Distinct
        from :meth:`increment_accepted_submissions`, which is the atomic bounded
        increment on its own — a caller that also has to read the case and write a
        signal needs the row held across all of it.
        """
        statement = for_update(
            self.scoped(merchant_id).where(CustomerAccessToken.token_id == token_id)
        )
        return self.session.execute(statement).scalar_one_or_none()

    def live_for_case(
        self, merchant_id: uuid.UUID, case_id: uuid.UUID
    ) -> CustomerAccessToken | None:
        """The one unrevoked token for a case, if there is one.

        At most one can exist — ``one_live_token_per_case`` makes a second
        uncommittable — so this returns a single row rather than a list, and
        ``scalar_one_or_none`` is allowed to raise if that ever stops being true. A
        silent ``first()`` would hide a broken invariant.

        Unrevoked, **not unexpired**: expiry needs ``now()`` and so cannot be in the
        index predicate. The caller compares ``expires_at`` itself, and mints a
        replacement only after revoking the expired predecessor with
        ``EXPIRED_SUPERSEDED`` in the same transaction (R18.C14).
        """
        statement = self.scoped(merchant_id).where(
            CustomerAccessToken.case_id == case_id,
            CustomerAccessToken.revoked_at.is_(None),
        )
        return self.session.execute(statement).scalar_one_or_none()

    def list_for_case(
        self, merchant_id: uuid.UUID, case_id: uuid.UUID, *, limit: int
    ) -> Sequence[CustomerAccessToken]:
        """Every token a case has ever had, newest first.

        History, for the case-detail view and for answering "when did this customer's
        link stop working". ``limit`` is required, like every list read in this package.
        """
        if limit <= 0:
            raise ValueError("limit must be positive")
        statement = (
            self.scoped(merchant_id)
            .where(CustomerAccessToken.case_id == case_id)
            .order_by(CustomerAccessToken.issued_at.desc())
            .limit(limit)
        )
        return list(self.session.execute(statement).scalars())


class CustomerSignalRepository(MerchantScopedRepository[CustomerSignal]):
    """What the customer said, as evidence. Inserted, counted, read, redacted."""

    model = CustomerSignal

    # -- writes ----------------------------------------------------------------

    def insert(self, merchant_id: uuid.UUID, *, values: Mapping[str, object]) -> CustomerSignal:
        """Stage one signal row and flush it so its id is available.

        Flushed because ``contact_suppression.customer_signal_id`` and
        ``promise_to_pay.customer_signal_id`` are ``NOT NULL`` foreign keys written in
        the same transaction, and the alternative — relying on SQLAlchemy's insert
        ordering — makes a schema-level correctness property depend on the unit of
        work's internal sort.
        """
        row = CustomerSignal(**dict(values))
        self.add(merchant_id, row)
        self.session.flush()
        return row

    def redact_note(
        self,
        merchant_id: uuid.UUID,
        signal_id: uuid.UUID,
        *,
        moment: datetime,
        retention_config_version: str,
    ) -> bool:
        """Erase one delay-reason note, recording when and under which config version.

        The note is set to ``NULL`` rather than to a placeholder, because
        ``redacted_note_is_absent`` refuses a row that claims a redaction while still
        holding the text — a marking without the erasure would let the retention sweep
        report work it had not done (R29.C10).

        Returns whether anything changed, so a second sweep pass over an
        already-redacted row does not write a second audit record. Only rows still
        holding a note are touched, which is what makes the pass idempotent.
        """
        return (
            rows_affected(
                self.session.execute(
                    update(CustomerSignal)
                    .where(
                        CustomerSignal.merchant_id == merchant_id,
                        CustomerSignal.id == signal_id,
                        CustomerSignal.delay_reason_note.is_not(None),
                    )
                    .values(
                        delay_reason_note=None,
                        note_redacted_at=moment,
                        retention_config_version=retention_config_version,
                    )
                )
            )
            > 0
        )

    # -- reads -----------------------------------------------------------------

    def list_for_case(
        self, merchant_id: uuid.UUID, case_id: uuid.UUID, *, limit: int
    ) -> Sequence[CustomerSignal]:
        """Every signal for a case, oldest first.

        Oldest first because the sequence is the evidence: "viewed the page, then stated
        a reason, then promised a date" is a different history from the reverse, and
        Recovery_Memory and the timeline both read it in order. Served by
        ``ix_customer_signal_merchant_id_case_id_submitted_at``.
        """
        if limit <= 0:
            raise ValueError("limit must be positive")
        statement = (
            self.scoped(merchant_id)
            .where(CustomerSignal.case_id == case_id)
            .order_by(CustomerSignal.submitted_at, CustomerSignal.created_at)
            .limit(limit)
        )
        return list(self.session.execute(statement).scalars())

    def latest_delay_reason(
        self, merchant_id: uuid.UUID, case_id: uuid.UUID
    ) -> CustomerSignal | None:
        """The most recent stated reason for a case, or ``None`` if there is none.

        The input to R20.C4's cause refinement. **Most recent, not first**: a customer who
        submits a second reason has corrected the first, and diagnosing the next cycle on
        the superseded one would make the correction pointless. The signal cap of R19.C7
        bounds how many corrections there can be, so this is not an unbounded revision
        history — it is at most a handful of rows, and the last one is the current account.

        Only ``DELAY_REASON``-kind rows are considered. ``delay_reason`` is ``NULL`` on a
        promise by ``promise_carries_no_delay_reason`` and on a page view, so filtering on
        the kind and on the column being present says the same thing twice — deliberately,
        because the first is the intent and the second is what makes the row usable without
        a ``None`` check at the call site.

        Served by ``ix_customer_signal_merchant_id_case_id_submitted_at``, read in reverse.
        ``created_at`` breaks a tie on ``submitted_at``, which is reachable: the instant is
        the request's, so two submissions inside one clock tick sort by insertion instead of
        arbitrarily.
        """
        statement = (
            self.scoped(merchant_id)
            .where(
                CustomerSignal.case_id == case_id,
                CustomerSignal.kind == CustomerSignalKind.DELAY_REASON.value,
                CustomerSignal.delay_reason.is_not(None),
            )
            .order_by(CustomerSignal.submitted_at.desc(), CustomerSignal.created_at.desc())
            .limit(1)
        )
        return self.session.execute(statement).scalar_one_or_none()

    def count_for_case(self, merchant_id: uuid.UUID, case_id: uuid.UUID) -> int:
        """How many signals a case holds, for the ``MAX_CUSTOMER_SIGNALS_PER_CASE``
        check of R19.C7.

        A count rather than a stored counter: there is no column to drift, and the read
        is served by the same index the per-case list uses.
        """
        statement = (
            select(func.count())
            .select_from(CustomerSignal)
            .where(
                CustomerSignal.merchant_id == merchant_id,
                CustomerSignal.case_id == case_id,
            )
        )
        return int(self.session.execute(statement).scalar_one())

    def claim_notes_for_retention(
        self,
        merchant_id: uuid.UUID,
        *,
        submitted_before: datetime,
        limit: int,
    ) -> Sequence[CustomerSignal]:
        """Claim notes older than the retention bound, skipping locked rows.

        Served by ``ix_customer_signal_notes_for_retention``, the partial index that
        exists for exactly this scan — most signals carry no note, and a full index
        would make the sweep read every row it has no work to do on.

        ``FOR UPDATE SKIP LOCKED`` so two retention passes running at once take
        different rows instead of waiting on each other and then both redacting the
        same one. Oldest first, because the note that has been retained longest is the
        one furthest past the bound.
        """
        if limit <= 0:
            raise ValueError("limit must be positive")
        statement = for_update_skip_locked(
            select(CustomerSignal)
            .where(
                CustomerSignal.merchant_id == merchant_id,
                CustomerSignal.delay_reason_note.is_not(None),
                CustomerSignal.submitted_at <= submitted_before,
            )
            .order_by(CustomerSignal.submitted_at)
            .limit(limit)
        )
        return list(self.session.execute(statement).scalars())


class ContactSuppressionRepository(MerchantScopedRepository[ContactSuppression]):
    """The permanent end to contact, and the named release that is the only way back."""

    model = ContactSuppression

    # -- writes ----------------------------------------------------------------

    def insert_if_absent(
        self, merchant_id: uuid.UUID, *, scope_key: str, values: Mapping[str, object]
    ) -> uuid.UUID | None:
        """Suppress a scope, returning the new id, or ``None`` if it already is.

        ``None`` is not an error. It means this scope is already suppressed — including
        by a suppression that has since been released, because the unique constraint
        covers every row — and the caller's correct response is to leave the existing
        record alone. A second hard stop on one scope must not produce a second row
        nobody reconciles, and two concurrent submissions reach this together and
        exactly one wins.
        """
        statement = (
            insert(ContactSuppression)
            .values(merchant_id=merchant_id, scope_key=scope_key, **dict(values))
            .on_conflict_do_nothing(
                index_elements=[
                    ContactSuppression.merchant_id,
                    ContactSuppression.scope_key,
                ]
            )
            .returning(ContactSuppression.id)
        )
        return self.session.execute(statement).scalar_one_or_none()

    def release(
        self,
        merchant_id: uuid.UUID,
        scope_key: str,
        *,
        moment: datetime,
        released_by_user_id: uuid.UUID,
        release_config_version: str,
    ) -> bool:
        """Lift a suppression, naming the person who lifted it. Returns whether it moved.

        ``released_by_user_id`` is a required argument and not an optional one, because
        ``release_names_a_user`` refuses a release with no person attached (R21.C2) —
        an anonymous lifting of a suppression is not a thing this system can record, and
        a default here would turn that schema refusal into a runtime surprise.

        Only a suppression still in force is touched, so a repeated release does not
        overwrite who actually performed it or when.
        """
        return (
            rows_affected(
                self.session.execute(
                    update(ContactSuppression)
                    .where(
                        ContactSuppression.merchant_id == merchant_id,
                        ContactSuppression.scope_key == scope_key,
                        ContactSuppression.released_at.is_(None),
                    )
                    .values(
                        released_at=moment,
                        released_by_user_id=released_by_user_id,
                        release_config_version=release_config_version,
                    )
                )
            )
            > 0
        )

    # -- reads -----------------------------------------------------------------

    def in_force(self, merchant_id: uuid.UUID, scope_key: str) -> ContactSuppression | None:
        """The suppression covering a scope right now, or ``None``.

        The policy hot path: every decision that could produce a customer-visible
        action asks this. Served by ``ix_contact_suppression_in_force``, partial over
        the unreleased rows precisely so a released suppression — which is history —
        cannot be read here.

        ``None`` means contact is permitted by *this* control and says nothing about
        the other five checks.
        """
        statement = self.scoped(merchant_id).where(
            ContactSuppression.scope_key == scope_key,
            ContactSuppression.released_at.is_(None),
        )
        return self.session.execute(statement).scalar_one_or_none()

    def for_scope(self, merchant_id: uuid.UUID, scope_key: str) -> ContactSuppression | None:
        """The suppression record for a scope, released or not.

        Distinct from :meth:`in_force`, and the difference is what each is for: the hot
        path must not see a released row, while "has this scope ever been suppressed,
        and by whom was it lifted" is the question the case-detail view and an audit
        answer. ``UNIQUE (merchant_id, scope_key)`` means there is at most one.
        """
        statement = self.scoped(merchant_id).where(ContactSuppression.scope_key == scope_key)
        return self.session.execute(statement).scalar_one_or_none()

    def list_in_force(
        self, merchant_id: uuid.UUID, *, limit: int
    ) -> Sequence[ContactSuppression]:
        """Suppressions still in force for one merchant, newest first.

        For the operator view and for the downgrade pre-assertion's application-side
        counterpart. ``limit`` is required, like every list read in this package.
        """
        if limit <= 0:
            raise ValueError("limit must be positive")
        statement = (
            self.scoped(merchant_id)
            .where(ContactSuppression.released_at.is_(None))
            .order_by(ContactSuppression.suppressed_at.desc())
            .limit(limit)
        )
        return list(self.session.execute(statement).scalars())


class PromiseToPayRepository(MerchantScopedRepository[PromiseToPay]):
    """At most one promise per case, and the follow-up scan that finds it when due."""

    model = PromiseToPay

    # -- writes ----------------------------------------------------------------

    def insert_if_absent(
        self, merchant_id: uuid.UUID, *, case_id: uuid.UUID, values: Mapping[str, object]
    ) -> uuid.UUID | None:
        """Record a promise, returning its id, or ``None`` if the case already has one.

        ``None`` is not an error: it is ``MAX_PROMISES_PER_CASE`` being reached, and the
        caller's correct response is a rejection naming the bound (R23.C7). The
        application check against the configured value runs first;
        ``uq_promise_to_pay_merchant_id_case_id`` is the backstop behind it, and going
        through ``ON CONFLICT DO NOTHING`` rather than letting the constraint raise is
        what keeps two concurrent submissions from turning one refusal into a failed
        transaction.
        """
        statement = (
            insert(PromiseToPay)
            .values(merchant_id=merchant_id, case_id=case_id, **dict(values))
            .on_conflict_do_nothing(
                index_elements=[PromiseToPay.merchant_id, PromiseToPay.case_id]
            )
            .returning(PromiseToPay.id)
        )
        return self.session.execute(statement).scalar_one_or_none()

    def resolve(
        self,
        merchant_id: uuid.UUID,
        promise_id: uuid.UUID,
        *,
        expected_statuses: Sequence[PromiseStatus],
        values: Mapping[str, object],
    ) -> bool:
        """Move a promise out of a status it is expected to be in. Returns whether it moved.

        ``expected_statuses`` is required and is the whole point: every transition of a
        promise is conditional on the status it is leaving, so a follow-up sweep and a
        capture arriving at the same instant cannot both claim the promise — one of them
        gets ``False`` and does nothing. Without it, "mark kept" and "mark missed" would
        race and the losing write would silently overwrite the winner.

        The status pairs the schema insists on — ``kept_at`` with ``KEPT``, no
        ``follow_up_at`` with ``BEYOND_WINDOW_ESCALATED`` — are the caller's to supply
        in ``values``. They are checked by ``kept_at_iff_kept`` and
        ``escalated_schedules_nothing``, so an inconsistent pair fails the transaction
        rather than landing.
        """
        if not expected_statuses:
            raise ValueError("expected_statuses must name at least one status")
        return (
            rows_affected(
                self.session.execute(
                    update(PromiseToPay)
                    .where(
                        PromiseToPay.merchant_id == merchant_id,
                        PromiseToPay.id == promise_id,
                        PromiseToPay.status.in_(
                            [status.value for status in expected_statuses]
                        ),
                    )
                    .values(**dict(values))
                )
            )
            > 0
        )

    # -- reads -----------------------------------------------------------------

    def for_case(self, merchant_id: uuid.UUID, case_id: uuid.UUID) -> PromiseToPay | None:
        """The promise recorded against a case, if there is one.

        ``UNIQUE (merchant_id, case_id)`` means at most one, so this returns a single
        row and ``scalar_one_or_none`` is allowed to raise if that stops being true.
        """
        statement = self.scoped(merchant_id).where(PromiseToPay.case_id == case_id)
        return self.session.execute(statement).scalar_one_or_none()

    def claim_due_for_follow_up(
        self, merchant_id: uuid.UUID, *, now: datetime, limit: int
    ) -> Sequence[PromiseToPay]:
        """Claim promises whose Follow_Up_Instant has been reached (R23.C13).

        Served by ``ix_promise_to_pay_due_for_follow_up``, partial over the two statuses
        that can still carry an instant. ``FOR UPDATE SKIP LOCKED`` so two sweeps take
        different rows rather than both acting on one.

        ``now`` is passed in rather than read here, so a test that moves the clock does
        not have the repository consulting a different one. Ordered by ``follow_up_at``,
        because the promise that has been due longest is the one a customer is waiting
        on.
        """
        if limit <= 0:
            raise ValueError("limit must be positive")
        statement = for_update_skip_locked(
            select(PromiseToPay)
            .where(
                PromiseToPay.merchant_id == merchant_id,
                PromiseToPay.status.in_(_FOLLOW_UP_PENDING),
                PromiseToPay.follow_up_at.is_not(None),
                PromiseToPay.follow_up_at <= now,
            )
            .order_by(PromiseToPay.follow_up_at)
            .limit(limit)
        )
        return list(self.session.execute(statement).scalars())

    def list_by_status(
        self, merchant_id: uuid.UUID, status: PromiseStatus, *, limit: int
    ) -> Sequence[PromiseToPay]:
        """Promises in one status, newest first, within one merchant.

        For the operator view and for the promise-kept rate the metrics engine reports.
        """
        if limit <= 0:
            raise ValueError("limit must be positive")
        statement = (
            self.scoped(merchant_id)
            .where(PromiseToPay.status == status.value)
            .order_by(PromiseToPay.recorded_at.desc())
            .limit(limit)
        )
        return list(self.session.execute(statement).scalars())
