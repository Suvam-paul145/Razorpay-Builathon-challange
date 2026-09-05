"""``ai_invocation`` writes and the two counts the per-case bound is served by.

One row per invocation, **including the ones that produced nothing usable** — a timeout, a
schema rejection, a content rejection, a transport failure. A layer that only recorded its
successes would look more reliable than it is, and the deterministic-fallback rate is the
number that says whether the reasoning path is carrying its weight (R27.C12).

**The bound is counted from these rows rather than from a per-process counter** (R27.C13).
A counter in memory resets on a restart, and a case whose worker crashed mid-cycle would
then get a fresh allowance every time the job was redelivered — which is the opposite of a
bound. Counting committed rows means the allowance survives a restart, a redelivery and a
second worker, because the rows are the same rows all three of them can see.

There is no update method and no delete method. Unlike ``audit_record`` that is *not*
enforced by a grant: it is enforced by the fact that no caller needs one. A verdict is what
the invocation produced and ``influenced_recommendation`` is whether the caller used it, and
both are known at the moment the row is written — so a row that needed correcting afterwards
would mean the writer had guessed.
"""

from __future__ import annotations

import uuid

from sqlalchemy import func, select

from revora.persistence.models import AiInvocation
from revora.persistence.repositories.base import MerchantScopedRepository

__all__ = ["AiInvocationRepository"]


class AiInvocationRepository(MerchantScopedRepository[AiInvocation]):
    """One row per reasoning invocation, and the per-case count that bounds them."""

    model = AiInvocation

    def record(
        self,
        merchant_id: uuid.UUID,
        *,
        case_id: uuid.UUID | None,
        call_kind: str | None,
        prompt_contract_id: str,
        verdict: str,
        influenced_recommendation: bool,
        model_id: str | None = None,
        model_version: str | None = None,
        latency_ms: int | None = None,
        raw_response_truncated: str | None = None,
        correlation_id: uuid.UUID | None = None,
    ) -> AiInvocation:
        """Insert one invocation row and flush it so its id is available.

        Flushed rather than left to the unit of work, because ``diagnosis.ai_invocation_id``
        is a foreign key to this row and the caller needs the id to write it. Relying on
        SQLAlchemy's insert ordering would make a schema guarantee depend on an internal
        sort.

        Every argument after ``verdict`` is optional and each absence means something
        specific rather than "not filled in". ``model_id`` is absent where no request was
        issued; ``model_version`` is absent where the provider named none, and is never
        filled from ``model_id`` — the two answer "what did Revora ask for" and "what
        answered", and collapsing them would make a silent provider-side version change
        invisible in the one table built to make it visible. ``latency_ms`` is absent where
        nothing was waited for.
        """
        row = AiInvocation(
            case_id=case_id,
            call_kind=call_kind,
            prompt_contract_id=prompt_contract_id,
            model_id=model_id,
            model_version=model_version,
            latency_ms=latency_ms,
            verdict=verdict,
            influenced_recommendation=influenced_recommendation,
            raw_response_truncated=raw_response_truncated,
            correlation_id=correlation_id,
        )
        self.add(merchant_id, row)
        self.session.flush()
        return row

    def count_for_case(self, merchant_id: uuid.UUID, case_id: uuid.UUID) -> int:
        """How many invocations this case has already cost (R27.C13).

        Served by ``ix_ai_invocation_case_id``. Counts every verdict, not only the accepted
        ones: the bound exists to cap what a single case can spend, and a rejected response
        cost exactly as much to obtain as an accepted one.
        """
        statement = (
            select(func.count())
            .select_from(AiInvocation)
            .where(
                AiInvocation.merchant_id == merchant_id,
                AiInvocation.case_id == case_id,
            )
        )
        return int(self.session.execute(statement).scalar_one())

    def count_for_case_and_kind(
        self, merchant_id: uuid.UUID, case_id: uuid.UUID, *, call_kind: str
    ) -> int:
        """How many invocations of one kind this case has cost.

        Exists because ``call_kind`` is a column rather than a fragment of
        ``prompt_contract_id``: "how many ``CAUSE_HYPOTHESIS`` calls did this case make" is a
        ``WHERE`` on an indexed equality, not a ``LIKE`` over a version string that would
        also match a future ``cause-hypothesis/2``.
        """
        statement = (
            select(func.count())
            .select_from(AiInvocation)
            .where(
                AiInvocation.merchant_id == merchant_id,
                AiInvocation.case_id == case_id,
                AiInvocation.call_kind == call_kind,
            )
        )
        return int(self.session.execute(statement).scalar_one())
