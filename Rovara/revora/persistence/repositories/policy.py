"""Policy decisions, their twelve check results, and the intent reads policy depends on.

Two of the reads here exist purely to answer the duplicate-action check, and they are the
reason the check can be trusted.

:meth:`PolicyDecisionRepository.open_intent_exists` asks whether any execution intent for
the case is still unresolved — ``ATTEMPTED`` or ``UNCERTAIN``. Both mean a provider call
may already have happened and its outcome is unknown. While that is true, authorizing a
second external effect is how one failed payment becomes two customer messages, so the
check has to fail rather than proceed.

:meth:`PolicyDecisionRepository.intent_exists_for_key` is the narrower question: has *this
exact action at this exact attempt ordinal* already been attempted? The idempotency key is
derived deterministically at decision time, so a retried decision recomputes the same key
and this read is what stops it minting a second authorization for the same effect.

:meth:`PolicyDecisionRepository.insert_check_results` takes all twelve rows in one call and
there is no method to add one. A partially recorded evaluation would look, in the record,
exactly like an evaluation that ran fewer checks and approved — which is the one way this
table could actively mislead.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence

from sqlalchemy import func, select

from revora.domain.enums import IntentState, PolicyVerdict
from revora.persistence.models.execution import ExecutionIntent
from revora.persistence.models.policy import PolicyCheckResult, PolicyDecision
from revora.persistence.repositories.base import MerchantScopedRepository

__all__ = ["UNRESOLVED_INTENT_STATES", "PolicyDecisionRepository"]

UNRESOLVED_INTENT_STATES: tuple[str, ...] = (
    IntentState.ATTEMPTED.value,
    IntentState.UNCERTAIN.value,
)
"""Intent states that mean an external effect may exist but has not been confirmed.

``ATTEMPTED`` is committed before the provider call, so it covers the crash window between
the commit and the call. ``UNCERTAIN`` is committed after a timeout or an unclassifiable
response, which is the case where the call definitely went out and the answer is unknown.
``CONFIRMED`` and ``FAILED`` are resolved and do not block a further action."""


class PolicyDecisionRepository(MerchantScopedRepository[PolicyDecision]):
    """Decision writes, the twelve check rows, and the intent reads policy consults."""

    model = PolicyDecision

    # -- writes ----------------------------------------------------------------

    def insert(
        self, merchant_id: uuid.UUID, *, values: Mapping[str, object]
    ) -> PolicyDecision:
        """Stage one decision and flush it so its id is available to the check rows.

        Flushed rather than left pending because ``policy_check_result.policy_decision_id``
        is a ``NOT NULL`` foreign key, and depending on the unit of work's insert ordering
        would make a schema guarantee rest on SQLAlchemy's internal sort.
        """
        row = PolicyDecision(**dict(values))
        self.add(merchant_id, row)
        self.session.flush()
        return row

    def insert_check_results(
        self, merchant_id: uuid.UUID, *, rows: Sequence[Mapping[str, object]]
    ) -> Sequence[PolicyCheckResult]:
        """Stage all twelve check rows. See the module docstring for why it is all twelve."""
        created = [PolicyCheckResult(**dict(values)) for values in rows]
        for row in created:
            row.merchant_id = merchant_id
            self.session.add(row)
        self.session.flush()
        return created

    # -- reads -----------------------------------------------------------------

    def for_cycle(
        self, merchant_id: uuid.UUID, case_id: uuid.UUID, decision_cycle: int
    ) -> Sequence[PolicyDecision]:
        """Every decision made in one cycle, oldest first.

        A list, not a single row: one cycle can legitimately hold several decisions — a
        deferred one followed by an approved one once the cooldown elapsed, or a blocked
        one for a customer-visible action followed by an approved ``DO_NOTHING``.
        """
        statement = (
            self.scoped(merchant_id)
            .where(
                PolicyDecision.case_id == case_id,
                PolicyDecision.decision_cycle == decision_cycle,
            )
            .order_by(PolicyDecision.evaluated_at)
        )
        return list(self.session.execute(statement).scalars())

    def latest_approved_unconsumed(
        self, merchant_id: uuid.UUID, case_id: uuid.UUID
    ) -> PolicyDecision | None:
        """The newest ``APPROVED`` decision that has not authorized an effect yet.

        What the execution engine looks for. ``consumed_by_intent_id IS NULL`` is the
        unconsumed part, and ``one_intent_per_decision`` is what makes "one approval, at
        most one effect" a database fact rather than a convention (R8.C15). Freshness
        against ``expires_at`` is checked by execution, not here — this read answers "is
        there an approval", and staleness is a separate refusal with its own reason.
        """
        statement = (
            self.scoped(merchant_id)
            .where(
                PolicyDecision.case_id == case_id,
                PolicyDecision.verdict == PolicyVerdict.APPROVED.value,
                PolicyDecision.consumed_by_intent_id.is_(None),
            )
            .order_by(PolicyDecision.evaluated_at.desc())
            .limit(1)
        )
        return self.session.execute(statement).scalars().first()

    def list_approved_unconsumed(
        self, merchant_id: uuid.UUID, case_id: uuid.UUID
    ) -> Sequence[PolicyDecision]:
        """Every ``APPROVED`` decision on a case that has not authorized an effect yet.

        The plural of :meth:`latest_approved_unconsumed`, and the two are not
        interchangeable. Execution wants *the* approval to act on, so it takes the
        newest. R21.C6 wants *every* action a Contact_Suppression cancels, one audit
        record each, so it needs the whole set — and the set can hold more than one,
        because a case that was approved, left unexecuted and later re-decided has an
        approval per cycle that nothing consumed.

        ``consumed_by_intent_id IS NULL`` is exactly R21.C6's "for which no
        execution-intent record exists", and ``one_intent_per_decision`` makes that
        correspondence one-to-one rather than approximate — so a count of these rows is a
        count of cancelled actions and not an estimate of one.

        Oldest first, unlike the singular read. These are written to the audit log in
        order, and a record sequence that ran newest-first would read as though the
        cancellations happened backwards.
        """
        statement = (
            self.scoped(merchant_id)
            .where(
                PolicyDecision.case_id == case_id,
                PolicyDecision.verdict == PolicyVerdict.APPROVED.value,
                PolicyDecision.consumed_by_intent_id.is_(None),
            )
            .order_by(PolicyDecision.evaluated_at)
        )
        return list(self.session.execute(statement).scalars())

    def check_results_for(
        self, merchant_id: uuid.UUID, policy_decision_id: uuid.UUID
    ) -> Sequence[PolicyCheckResult]:
        """The twelve check rows of one decision, in evaluation order."""
        statement = (
            select(PolicyCheckResult)
            .where(
                PolicyCheckResult.merchant_id == merchant_id,
                PolicyCheckResult.policy_decision_id == policy_decision_id,
            )
            .order_by(PolicyCheckResult.check_order)
        )
        return list(self.session.execute(statement).scalars())

    def open_intent_exists(self, merchant_id: uuid.UUID, case_id: uuid.UUID) -> bool:
        """Whether an unresolved execution intent exists for the case (check 3)."""
        statement = (
            select(func.count())
            .select_from(ExecutionIntent)
            .where(
                ExecutionIntent.merchant_id == merchant_id,
                ExecutionIntent.case_id == case_id,
                ExecutionIntent.state.in_(UNRESOLVED_INTENT_STATES),
            )
        )
        return int(self.session.execute(statement).scalar_one()) > 0

    def intent_exists_for_key(self, merchant_id: uuid.UUID, idempotency_key: str) -> bool:
        """Whether any intent already exists for an idempotency key (check 3).

        Any state, including ``CONFIRMED`` and ``FAILED``. A confirmed intent for this key
        means the effect already happened, and a failed one means this exact attempt
        ordinal has been used — a retry has to be a new ordinal with a new key, not a
        second run of the same one.
        """
        statement = (
            select(func.count())
            .select_from(ExecutionIntent)
            .where(
                ExecutionIntent.merchant_id == merchant_id,
                ExecutionIntent.idempotency_key == idempotency_key,
            )
        )
        return int(self.session.execute(statement).scalar_one()) > 0
