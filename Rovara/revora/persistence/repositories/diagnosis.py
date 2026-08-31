"""Diagnosis reads, the one-active-row insert, and the coverage aggregate.

Two operations here carry weight beyond storage.

:meth:`DiagnosisRepository.insert_active` is the active-diagnosis invariant. It is a
single ``INSERT ... ON CONFLICT DO NOTHING`` against the
``one_active_diagnosis_per_cycle`` partial unique index, so "exactly one active
diagnosis per ``(case_id, decision_cycle)``" is a database fact rather than a promise
made by the service above it. ``None`` back means a diagnosis for that cycle already
exists, which on a retried job is the normal answer, not an error. Doing this as
``SELECT`` then ``INSERT`` would be a race two concurrent diagnosis jobs win together,
and two active causes for one cycle means two different actions could each claim to be
justified with nothing in the record saying which one the recommendation used.

:meth:`DiagnosisRepository.coverage` is the measurement the design owes. design.md
states, marked ``[INFERENCE]``, that the deterministic table covers the large majority
of real failures. That is a hypothesis. This aggregate is how it gets tested: the
match key and the matched-or-not flag are persisted in ``diagnosis.evidence`` by every
diagnosis, and this reads them back as counts. It is a query rather than a log line
because an operator needs to ask the question months later, and because the number
belongs on a dashboard next to the recovery figures it justifies.

The hit rate is computed as a ``Decimal`` quotient, never a float. It is a ratio of
two integers and it is displayed to a merchant, which is precisely the shape of value
that binary rounding makes embarrassing.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import ColumnElement, Select, func, select, text
from sqlalchemy.dialects.postgresql import insert

from revora.domain.enums import DiagnosisMethod, RiskCause
from revora.domain.failure_taxonomy import (
    EVIDENCE_ERROR_REASON,
    EVIDENCE_MATCH_KEY,
    EVIDENCE_MATCHED,
    EVIDENCE_OUTCOME,
    MatchOutcome,
)
from revora.persistence.models import Diagnosis
from revora.persistence.repositories.base import MerchantScopedRepository

__all__ = ["DeterministicCoverage", "DiagnosisRepository", "UnmappedReasonCount"]

#: The predicate of the ``one_active_diagnosis_per_cycle`` partial unique index,
#: spelt exactly as the model spells it. ``ON CONFLICT`` against a partial index must
#: repeat its ``WHERE`` clause, and the two spellings have to be textually identical
#: or Postgres will not recognize the index as the conflict target at all — it fails
#: at execution time with "no unique or exclusion constraint matching", not at import.
_ACTIVE_INDEX_WHERE = text("is_active")

_HIT_RATE_PLACES = Decimal("0.0001")
"""Four decimal places, matching the probability type discipline. A hit rate is not a
probability, but it is read alongside them and a different precision on the same
dashboard reads as a bug."""


@dataclass(frozen=True, slots=True)
class DeterministicCoverage:
    """How much of the real failure traffic the mapping table actually decides.

    Four disjoint counts over the active diagnoses in a window, plus the derived
    rate. ``not_at_risk`` is separated out on purpose: ``order_already_paid`` is a
    reason the table handles correctly while naming no cause, so folding it into
    either the hits or the gaps would misreport coverage in one direction or the
    other.
    """

    total: int
    deterministic_hits: int
    unmapped: int
    not_at_risk: int

    @property
    def hit_rate(self) -> Decimal:
        """Deterministic hits over the cases where a hit was possible.

        The denominator excludes ``not_at_risk`` for the reason above. Zero
        observations returns zero rather than raising: an operator reading a fresh
        deployment should see "no data yet" as a zero next to a zero total, not an
        exception in a dashboard endpoint.
        """
        denominator = self.total - self.not_at_risk
        if denominator <= 0:
            return Decimal("0")
        rate = Decimal(self.deterministic_hits) / Decimal(denominator)
        return rate.quantize(_HIT_RATE_PLACES, rounding=ROUND_HALF_UP)


@dataclass(frozen=True, slots=True)
class UnmappedReasonCount:
    """One provider reason the table does not handle, and how often it arrived.

    This is the actionable half of the metric. The hit rate says whether there is a
    problem; this says which line to add to
    ``domain.failure_taxonomy._REASON_GROUPS`` to fix it. ``reason`` is ``None`` for
    failures that carried no ``error_reason`` at all, which is a different problem
    and not one a table entry solves.
    """

    reason: str | None
    occurrences: int


class DiagnosisRepository(MerchantScopedRepository[Diagnosis]):
    """The active-diagnosis invariant, and the coverage metric built on evidence."""

    model = Diagnosis

    # -- the invariant ---------------------------------------------------------

    def insert_active(
        self,
        merchant_id: uuid.UUID,
        *,
        case_id: uuid.UUID,
        decision_cycle: int,
        values: dict[str, object],
    ) -> uuid.UUID | None:
        """Record the active diagnosis for a cycle, or discover one already exists.

        Returns the new row's id, or ``None`` when an active diagnosis is already
        recorded for ``(case_id, decision_cycle)``. ``None`` is not an error: a
        diagnosis job can be retried after a worker crash, and the correct response is
        to leave the existing diagnosis alone and continue the lifecycle on it.

        The conflict target repeats the partial index's ``WHERE is_active`` predicate,
        because ``ON CONFLICT`` against a partial index must — and because a superseded
        diagnosis from an earlier cycle deliberately does not conflict.
        """
        statement = (
            insert(Diagnosis)
            .values(
                merchant_id=merchant_id,
                case_id=case_id,
                decision_cycle=decision_cycle,
                is_active=True,
                **values,
            )
            .on_conflict_do_nothing(
                index_elements=[Diagnosis.case_id, Diagnosis.decision_cycle],
                index_where=_ACTIVE_INDEX_WHERE,
            )
            .returning(Diagnosis.id)
        )
        return self.session.execute(statement).scalar_one_or_none()

    # -- reads -----------------------------------------------------------------

    def active_for_cycle(
        self, merchant_id: uuid.UUID, case_id: uuid.UUID, decision_cycle: int
    ) -> Diagnosis | None:
        """The active diagnosis for one decision cycle, if there is one.

        ``scalar_one_or_none`` rather than ``first()``: at most one can exist because
        the partial unique index makes a second one uncommittable, and if that ever
        stops being true this should raise rather than quietly pick a row.
        """
        statement = self._active(merchant_id).where(
            Diagnosis.case_id == case_id,
            Diagnosis.decision_cycle == decision_cycle,
        )
        return self.session.execute(statement).scalar_one_or_none()

    def active_for_case(self, merchant_id: uuid.UUID, case_id: uuid.UUID) -> Diagnosis | None:
        """The newest active diagnosis for a case, across cycles.

        What the dashboard and the memory writer mean by "the diagnosis". Ordered by
        decision cycle descending because a later cycle's diagnosis supersedes an
        earlier one even while both remain active — the partial index scopes
        uniqueness to a cycle, not to a case.
        """
        statement = (
            self._active(merchant_id)
            .where(Diagnosis.case_id == case_id)
            .order_by(Diagnosis.decision_cycle.desc())
            .limit(1)
        )
        return self.session.execute(statement).scalars().first()

    def list_for_case(
        self, merchant_id: uuid.UUID, case_id: uuid.UUID
    ) -> Sequence[Diagnosis]:
        """Every diagnosis for a case, oldest cycle first, superseded ones included.

        The history is the point. A case whose cause changed between cycles is a case
        whose action set changed with it, and the explanation a merchant is owed
        includes the cause that was live at the time each decision was made.
        """
        statement = (
            self.scoped(merchant_id)
            .where(Diagnosis.case_id == case_id)
            .order_by(Diagnosis.decision_cycle, Diagnosis.created_at)
        )
        return list(self.session.execute(statement).scalars())

    # -- the measured facts ----------------------------------------------------

    def coverage(
        self,
        merchant_id: uuid.UUID,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> DeterministicCoverage:
        """Count deterministic hits, gaps and not-at-risk answers over a window.

        Read from ``evidence`` rather than inferred from ``method``. ``method`` says
        ``DETERMINISTIC`` for a hit, which would be enough for the rate alone, but it
        cannot distinguish an unmapped reason from an ``order_already_paid`` answer —
        both record ``FALLBACK_UNKNOWN`` — and conflating those two is exactly the
        misreport this metric exists to avoid. The taxonomy outcome is persisted for
        that reason.

        Superseded diagnoses are excluded. A re-diagnosis in a later cycle is a
        second observation of the same failure fields, and counting both would weight
        long-running cases more heavily than short ones in a metric about provider
        coverage.
        """
        outcome = Diagnosis.evidence[EVIDENCE_OUTCOME].astext
        matched = Diagnosis.evidence[EVIDENCE_MATCHED].astext
        statement = (
            select(
                func.count().label("total"),
                func.count().filter(matched == "true").label("hits"),
                func.count()
                .filter(outcome == MatchOutcome.UNMAPPED.value)
                .label("unmapped"),
                func.count()
                .filter(outcome == MatchOutcome.NOT_AT_RISK.value)
                .label("not_at_risk"),
            )
            .select_from(Diagnosis)
            .where(
                Diagnosis.merchant_id == merchant_id,
                Diagnosis.is_active,
                *self._window(start, end),
            )
        )
        row = self.session.execute(statement).one()
        return DeterministicCoverage(
            total=int(row.total),
            deterministic_hits=int(row.hits),
            unmapped=int(row.unmapped),
            not_at_risk=int(row.not_at_risk),
        )

    def unmapped_reasons(
        self,
        merchant_id: uuid.UUID,
        *,
        limit: int,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> Sequence[UnmappedReasonCount]:
        """The provider reasons that fell through, most frequent first.

        ``limit`` is required for the same reason it is on every list read: an
        unbounded query whose cost grows with a tenant's failure volume is a query
        that gets slower exactly when someone is trying to diagnose an incident.
        """
        if limit <= 0:
            raise ValueError("limit must be positive")
        outcome = Diagnosis.evidence[EVIDENCE_OUTCOME].astext
        reason = Diagnosis.evidence[EVIDENCE_ERROR_REASON].astext
        occurrences = func.count().label("occurrences")
        statement = (
            select(reason.label("reason"), occurrences)
            .select_from(Diagnosis)
            .where(
                Diagnosis.merchant_id == merchant_id,
                Diagnosis.is_active,
                outcome == MatchOutcome.UNMAPPED.value,
                *self._window(start, end),
            )
            .group_by(reason)
            .order_by(occurrences.desc())
            .limit(limit)
        )
        return [
            UnmappedReasonCount(reason=row.reason, occurrences=int(row.occurrences))
            for row in self.session.execute(statement)
        ]

    def match_key_counts(
        self,
        merchant_id: uuid.UUID,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> dict[str, int]:
        """How many diagnoses each lookup tier decided, keyed by match key.

        Answers a question the hit rate cannot: whether the reason table is carrying
        the traffic or whether the coarse ``error_code`` tier is quietly doing the work
        and labelling guesses as deterministic. A large ``error_code`` share is a
        signal to look at the reason table, not a reassurance.
        """
        match_key = Diagnosis.evidence[EVIDENCE_MATCH_KEY].astext
        occurrences = func.count().label("occurrences")
        statement = (
            select(match_key.label("match_key"), occurrences)
            .select_from(Diagnosis)
            .where(
                Diagnosis.merchant_id == merchant_id,
                Diagnosis.is_active,
                match_key.is_not(None),
                *self._window(start, end),
            )
            .group_by(match_key)
        )
        return {row.match_key: int(row.occurrences) for row in self.session.execute(statement)}

    def substituted_count(
        self,
        merchant_id: uuid.UUID,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> int:
        """How many active diagnoses had their cause replaced with ``UNKNOWN``.

        The rate of substitution is the honest measure of how much the diagnosis layer
        actually knows. A high count with the LLM path disabled means the table needs
        extending; a high count with it enabled means the model is producing
        low-confidence answers that are not worth the invocation.
        """
        statement = (
            select(func.count())
            .select_from(Diagnosis)
            .where(
                Diagnosis.merchant_id == merchant_id,
                Diagnosis.is_active,
                Diagnosis.substituted_to_unknown,
                *self._window(start, end),
            )
        )
        return int(self.session.execute(statement).scalar_one())

    def cause_counts(
        self,
        merchant_id: uuid.UUID,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> dict[RiskCause, int]:
        """The recorded cause distribution over active diagnoses.

        Recorded cause, so a substituted row counts as ``UNKNOWN`` — which is what the
        optimizer saw and therefore what explains the actions it proposed.
        """
        occurrences = func.count().label("occurrences")
        statement = (
            select(Diagnosis.cause.label("cause"), occurrences)
            .select_from(Diagnosis)
            .where(
                Diagnosis.merchant_id == merchant_id,
                Diagnosis.is_active,
                *self._window(start, end),
            )
            .group_by(Diagnosis.cause)
        )
        return {
            RiskCause(row.cause): int(row.occurrences)
            for row in self.session.execute(statement)
        }

    def method_counts(
        self,
        merchant_id: uuid.UUID,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> dict[DiagnosisMethod, int]:
        """The method distribution over active diagnoses.

        The AI-boundary audit in one query: a deployment with the reasoning layer off
        must show zero ``AI_ASSISTED`` and zero ``REJECTED_AI_OUTPUT`` rows, and that
        is checkable rather than assertable.
        """
        occurrences = func.count().label("occurrences")
        statement = (
            select(Diagnosis.method.label("method"), occurrences)
            .select_from(Diagnosis)
            .where(
                Diagnosis.merchant_id == merchant_id,
                Diagnosis.is_active,
                *self._window(start, end),
            )
            .group_by(Diagnosis.method)
        )
        return {
            DiagnosisMethod(row.method): int(row.occurrences)
            for row in self.session.execute(statement)
        }

    # -- internals -------------------------------------------------------------

    def _active(self, merchant_id: uuid.UUID) -> Select[tuple[Diagnosis]]:
        """Scoped select restricted to active rows."""
        return select(Diagnosis).where(
            Diagnosis.merchant_id == merchant_id, Diagnosis.is_active
        )

    @staticmethod
    def _window(
        start: datetime | None, end: datetime | None
    ) -> list[ColumnElement[bool]]:
        """The optional half-open ``created_at`` window, as ``WHERE`` conditions.

        Returned as conditions rather than applied to a statement so the aggregate
        queries below — which differ in their select lists but not in their filtering —
        share one definition of "in the window" and cannot drift apart.

        Half-open: ``start`` inclusive, ``end`` exclusive, so consecutive windows
        partition the rows exactly once each. An inclusive upper bound double-counts
        every row landing on a boundary, which for a daily report is every row written
        at midnight.
        """
        conditions: list[ColumnElement[bool]] = []
        if start is not None:
            conditions.append(Diagnosis.created_at >= start)
        if end is not None:
            conditions.append(Diagnosis.created_at < end)
        return conditions
