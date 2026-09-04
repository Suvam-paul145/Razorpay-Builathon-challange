"""Execution intent reservation and the reconciliation claim.

:meth:`ExecutionIntentRepository.reserve` is the exactly-once gate. It inserts the
intent *before* the provider is called, with ``ON CONFLICT DO NOTHING`` on
``(merchant_id, idempotency_key)``. A returned id means this caller owns the attempt
and may make the call. ``None`` means an intent for that key already exists, and the
correct response is to reconcile it — never to call the provider again.

That ordering is deliberately pessimistic. A crash between the insert and the call
burns an attempt that never reached the provider. The alternative — call first,
record after — risks a crash loop that issues provider calls while consuming zero
attempts, which can exceed the cap and message a customer twice. Given the choice,
the design under-attempts.

:meth:`ExecutionIntentRepository.live_payment_link` is here rather than beside either of its
callers because it has two, on layers that cannot see each other: the execution engine routes a
promise follow-up on it, and the candidate builder excludes ``PAYMENT_LINK`` on it. One query,
one definition of "live" — see the method for why that matters more than where it lives.

:meth:`ExecutionIntentRepository.claim_unresolved` uses ``FOR UPDATE SKIP LOCKED``
so two reconciliation sweeps running at once take different intents instead of
waiting on each other and then both resolving the same one. It also filters on
``effect_kind``, and that clause is a correctness mechanism rather than an index hint —
see the method.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from revora.domain.enums import ExecutionEffectKind, IntentState
from revora.persistence.models import ExecutionIntent, PaymentStateRead, RecoveryOutcome
from revora.persistence.repositories.base import MerchantScopedRepository
from revora.persistence.repositories.session import for_update_skip_locked

__all__ = [
    "ExecutionIntentRepository",
    "PaymentStateReadRepository",
    "RecoveryOutcomeRepository",
]

_UNRESOLVED: tuple[str, ...] = (IntentState.ATTEMPTED.value, IntentState.UNCERTAIN.value)

_RECONCILABLE = ExecutionEffectKind.PAYMENT_LINK_CREATE.value
"""The only effect a provider read can answer a question about.

A resend response carries a success boolean and no identifier, and no endpoint reports
whether a notification was sent, so there is nothing for a read to ask about. Rendered
from the enum because ``ix_execution_intent_unresolved``'s predicate is rendered from the
same member, and the query and the index have to agree exactly or the index is unused."""


class ExecutionIntentRepository(MerchantScopedRepository[ExecutionIntent]):
    """The exactly-once record's repository."""

    model = ExecutionIntent

    def reserve(
        self,
        merchant_id: uuid.UUID,
        *,
        idempotency_key: str,
        values: dict[str, object],
    ) -> uuid.UUID | None:
        """Claim the right to make one external call, or discover it is already claimed.

        Returns the new intent's id, or ``None`` when an intent for this
        idempotency key already exists. ``None`` is a normal outcome on a retry.
        """
        statement = (
            insert(ExecutionIntent)
            .values(merchant_id=merchant_id, idempotency_key=idempotency_key, **values)
            .on_conflict_do_nothing(
                index_elements=[ExecutionIntent.merchant_id, ExecutionIntent.idempotency_key]
            )
            .returning(ExecutionIntent.id)
        )
        return self.session.execute(statement).scalar_one_or_none()

    def get_by_idempotency_key(
        self, merchant_id: uuid.UUID, idempotency_key: str
    ) -> ExecutionIntent | None:
        """The intent for a key, within one merchant."""
        statement = self.scoped(merchant_id).where(
            ExecutionIntent.idempotency_key == idempotency_key
        )
        return self.session.execute(statement).scalar_one_or_none()

    def list_for_case(
        self, merchant_id: uuid.UUID, case_id: uuid.UUID
    ) -> Sequence[ExecutionIntent]:
        """Every attempt on a case, in attempt order."""
        statement = (
            self.scoped(merchant_id)
            .where(ExecutionIntent.case_id == case_id)
            .order_by(ExecutionIntent.attempt_ordinal)
        )
        return list(self.session.execute(statement).scalars())

    def live_payment_link(
        self, merchant_id: uuid.UUID, case_id: uuid.UUID
    ) -> ExecutionIntent | None:
        """The live payment link this case already holds, as the intent that created it.

        **The single definition of "this case has a live payment link", and it has two readers on
        two different layers.** :func:`revora.execution.engine._live_link_target` asks it to route
        R24.C10's resend branch — re-notify the link the case has, create no second one — and
        :mod:`revora.estimation.candidates` asks it to exclude ``PAYMENT_LINK`` from the candidate
        set with ``LIVE_PAYMENT_LINK_EXISTS`` on the same ground. It lives here because those two
        packages sit on opposite sides of the layering contract and cannot import each other, and
        because two readers deriving "live" separately is precisely how one of them comes to
        create a duplicate the other believed impossible.

        Live, and each clause earns its place:

        * ``effect_kind = PAYMENT_LINK_CREATE`` — a resend created no object, so it is not a link
          the case holds. This also excludes every row whose ``provider_response_id`` is a
          Revora-composed resend token rather than a provider identifier.
        * ``state = CONFIRMED`` — the provider acknowledged the object. An ``ATTEMPTED``,
          ``UNCERTAIN`` or ``FAILED`` creation is not a link anybody can be shown, and treating
          one as live would strand the case with nothing payable.
        * a non-empty ``provider_response_id`` — a confirmed row with no identifier is a link
          nothing can name, so nothing can re-notify it either.
        * **newest first** — a case can hold more than one confirmed creation across its
          attempts, and the customer's most recent link is the one they still have.

        **Expiry is not a fourth clause, and its absence is the load-bearing part.** A link's
        ``expire_by`` is clamped to the case's ``window_end_at`` when it is built
        (:func:`revora.providers.payment_link.clamp_expire_by`), and a case whose window has
        closed is expired by the lifecycle sweeper rather than decided again — so there is no
        state in which a case is still choosing actions while holding a link that has expired.
        That is why the answer can be given from Revora's own rows with **no provider read**: a
        fetch here would be a second external call on a path whose whole discipline is one call
        per idempotency key, asking a question the record already answers.

        ``None`` means the case holds nothing live, and it is not a failure. It routes the
        follow-up to R24.C11's create-a-link fallback, and it leaves ``PAYMENT_LINK`` a normal
        competing candidate.
        """
        statement = (
            self.scoped(merchant_id)
            .where(
                ExecutionIntent.case_id == case_id,
                ExecutionIntent.effect_kind == ExecutionEffectKind.PAYMENT_LINK_CREATE.value,
                ExecutionIntent.state == IntentState.CONFIRMED.value,
                ExecutionIntent.provider_response_id.is_not(None),
                ExecutionIntent.provider_response_id != "",
            )
            .order_by(ExecutionIntent.attempt_ordinal.desc())
            .limit(1)
        )
        return self.session.execute(statement).scalar_one_or_none()

    def claim_unresolved(
        self,
        merchant_id: uuid.UUID,
        *,
        started_before: datetime,
        limit: int,
    ) -> Sequence[ExecutionIntent]:
        """Claim unresolved intents for reconciliation, skipping locked rows.

        Served by ``ix_execution_intent_unresolved``, the partial index that exists
        for exactly this scan. Oldest first, because an intent that has been
        ``UNCERTAIN`` longest is the one blocking a case from progressing.

        **The ``effect_kind`` clause is the mechanism that keeps a resend out of
        reconciliation, and it is not an optimization.** A resend has no provider object to
        read, so an ``UNCERTAIN`` resend is unresolvable rather than pending; it escalates
        once and is never touched again. The clause matches the index predicate exactly,
        which means a resend row is not *skipped* by the sweep — it is absent from the set
        the sweep reads, and every caller inherits that without remembering to check. A
        future reader who removes the clause loses the partial index, gets a sequential scan
        and a failing performance assertion, and finds out before a customer gets a second
        message.
        """
        statement = for_update_skip_locked(
            select(ExecutionIntent)
            .where(
                ExecutionIntent.merchant_id == merchant_id,
                ExecutionIntent.state.in_(_UNRESOLVED),
                ExecutionIntent.effect_kind == _RECONCILABLE,
                ExecutionIntent.attempt_started_at <= started_before,
            )
            .order_by(ExecutionIntent.attempt_started_at)
            .limit(limit)
        )
        return list(self.session.execute(statement).scalars())


class PaymentStateReadRepository(MerchantScopedRepository[PaymentStateRead]):
    """Authoritative provider reads, kept as history."""

    model = PaymentStateRead

    def latest_for_case(
        self, merchant_id: uuid.UUID, case_id: uuid.UUID
    ) -> PaymentStateRead | None:
        """The newest authoritative read for a case.

        Newest by ``read_at``, not by ``created_at``: a read can be persisted after a
        later one when reconciliation runs concurrently, and the question being asked
        is what the provider said most recently.
        """
        statement = (
            self.scoped(merchant_id)
            .where(PaymentStateRead.case_id == case_id)
            .order_by(PaymentStateRead.read_at.desc())
            .limit(1)
        )
        return self.session.execute(statement).scalar_one_or_none()

    def list_for_case(
        self, merchant_id: uuid.UUID, case_id: uuid.UUID
    ) -> Sequence[PaymentStateRead]:
        """Every read for a case, oldest first, so a conflict can be reconstructed."""
        statement = (
            self.scoped(merchant_id)
            .where(PaymentStateRead.case_id == case_id)
            .order_by(PaymentStateRead.read_at)
        )
        return list(self.session.execute(statement).scalars())


class RecoveryOutcomeRepository(MerchantScopedRepository[RecoveryOutcome]):
    """The verified outcome, at most one per case."""

    model = RecoveryOutcome

    def for_case(self, merchant_id: uuid.UUID, case_id: uuid.UUID) -> RecoveryOutcome | None:
        """The outcome for a case, if it has been verified."""
        statement = self.scoped(merchant_id).where(RecoveryOutcome.case_id == case_id)
        return self.session.execute(statement).scalar_one_or_none()

    def list_in_window(
        self,
        merchant_id: uuid.UUID,
        *,
        start: datetime,
        end: datetime,
    ) -> Sequence[RecoveryOutcome]:
        """Outcomes recovered inside a time window, for cohort aggregation.

        The aggregate is computed by summing these integers in the caller rather than
        by a SQL ``SUM`` over a cast, so the reported total is exactly the sum of the
        rows it is built from — see ``domain.money.sum_exact``.
        """
        statement = (
            self.scoped(merchant_id)
            .where(
                RecoveryOutcome.recovery_timestamp >= start,
                RecoveryOutcome.recovery_timestamp < end,
            )
            .order_by(RecoveryOutcome.recovery_timestamp)
        )
        return list(self.session.execute(statement).scalars())
