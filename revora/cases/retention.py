"""Customer-data retention: delete the contact, keep the numbers.

R17.C11 requires contact data to be deleted or irreversibly masked within 24 hours of
``CUSTOMER_DATA_RETENTION`` elapsing, while retaining the non-identifying fields the metrics need,
and recording the retention configuration version that was applied.

The last clause is the one that is easy to skip and it is load-bearing: "we deleted it on time" is a
claim about the bound that was in force, and the bound is per-merchant configurable. Without the
version on the record, a sweep that ran under a 180-day bound is indistinguishable from one that ran
under a 30-day bound, and the compliance statement is unverifiable in either direction.

**What is destroyed and what survives, stated explicitly because the split is the whole design.**

Destroyed, irreversibly:

* ``recovery_case.customer_contact_masked`` — the partial contact kept for support. Already masked,
  and the disclosed tail is still customer data, so it goes.
* ``webhook_event.raw_payload_ciphertext`` and its ``raw_payload_nonce`` — the AES-GCM ciphertext
  of the raw body, which is the only place a full contact ever existed. Both are cleared, not just
  the ciphertext: a nonce left behind is not itself sensitive, but leaving it makes the row look
  decryptable and the next person to read the schema has to work out why it is not. The row itself
  survives, because its ``provider_event_id`` is what makes duplicate detection work and deleting
  it would let a redelivery from a year ago open a second case.

Survived, deliberately:

* ``recovery_case.customer_key`` — the keyed non-reversible hash. **This is a judgement call and it
  is worth arguing with.** Keeping it preserves the cross-case opt-out join, which is the mechanism
  that stops an opted-out customer being contacted again; destroying it would silently revoke every
  historical opt-out, which is a privacy regression dressed as a privacy measure. It is not
  reversible without the HMAC secret, and it identifies nobody on its own.
* every amount, timestamp, state, cause and outcome. A retention sweep that also destroyed these
  would make every historical figure irreproducible — a different failure from a privacy one, and
  just as real, because a merchant's finance team cannot reconcile a quarter that has been
  partially erased.

**Terminal cases only.** A live case may still need to send a payment link, which needs the
ciphertext. In practice the recovery window is 168 hours and the retention bound is 180 days, so a
non-terminal case older than the bound is a stuck case rather than an active one — but relying on
that would make a privacy sweep depend on a configuration coincidence. Filtering on terminal state
makes it depend on the state machine instead.

**The Delay_Reason_Note pass is a second scan in the same sweep, and it is not filtered on case
state** (R29.C10). Three things follow from that and each is a decision rather than an omission.

*Its clock is the note's own.* R29.C10 measures the age of *the note*, so the cutoff is compared
against ``customer_signal.submitted_at`` and not against the case's ``detected_at``. A case detected
a year ago whose customer answered last week holds a note that is a week old, and redacting it
because the case is old would delete data inside its retention period — which is a different
compliance failure from keeping it too long, and a worse one, because it is unrecoverable.

*It does not filter on terminal state.* The reason the contact pass does is that a live case may
still need the ciphertext to send a payment link. Nothing needs a note: it is evidence a decision
cycle already read, the cause it refined is recorded on the diagnosis row, and R20.C4 reads the
signal's *reason* rather than its note. So there is no live-case dependency to protect, and adding
the filter would leave a note on a stuck non-terminal case unredacted forever.

*It scans the partial index and only the partial index.*
``ix_customer_signal_notes_for_retention`` covers ``(merchant_id, submitted_at)`` over
``delay_reason_note IS NOT NULL``, so the scan reads only rows that have work to do. Most signals
carry no note — a page view never does, a promise never does — and a full index on
``submitted_at`` would make every sweep read every signal in the tenant to discover it had nothing
to redact. That is also why
:func:`~revora.customer.signals._note_for_storage` turns an empty or whitespace-only note into
``NULL`` rather than into ``''``: an empty string satisfies ``IS NOT NULL`` and would enter this
index permanently.

**What survives a note redaction** is every other column on the row: the kind, the stated
Delay_Reason, the submission instant, the truncation flag, the token handle and the provenance. The
Delay_Reason is what Recovery_Memory segments on (R25.C3) and what the Metrics_Engine counts
cohorts by (R25.C11), and it is not identifying — it is one of six enumerated members. The note is
the only free text a stranger typed, so the note is the only thing that goes.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Final

from sqlalchemy import select, update

from revora.audit.events import CUSTOMER_DATA_REDACTED
from revora.audit.writer import AuditEntry, AuditWriter
from revora.domain.transitions import TERMINAL_STATES
from revora.persistence.models import CustomerSignal, RecoveryCase, WebhookEvent
from revora.persistence.repositories.base import rows_affected
from revora.persistence.repositories.session import tenant_transaction
from revora.platform.clock import now
from revora.platform.logging import get_logger

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sqlalchemy.orm import Session, sessionmaker

    from revora.platform.config import Configuration

__all__ = ["RETENTION_BATCH_LIMIT", "RetentionReport", "sweep_customer_data_retention"]

_logger = get_logger(__name__)

RETENTION_BATCH_LIMIT: Final[int] = 500
"""Cases redacted per sweep.

Bounded so one sweep cannot hold a long transaction over a table the pipeline is writing to. The
sweep runs on an interval and picks up where it left off — the ``WHERE`` clause is self-limiting
because a redacted case no longer matches it — so a backlog drains over several runs rather than in
one lock."""

_REDACTED_CIPHERTEXT: Final[bytes] = b""
"""What a redacted payload becomes. Empty, not a placeholder string.

An empty ``BYTEA`` cannot be decrypted into anything, which is what "irreversibly" requires. A
placeholder like ``b'REDACTED'`` would decrypt-fail with an authentication error indistinguishable
from a corrupted row or a rotated key, and the operator investigating would have no way to tell a
compliance action from a data-loss incident."""

_TERMINAL_VALUES: Final[tuple[str, ...]] = tuple(
    sorted(state.value for state in TERMINAL_STATES)
)


@dataclass(frozen=True, slots=True)
class RetentionReport:
    """What one sweep did. Every figure is a count, and the version is what was applied."""

    cases_redacted: int
    payloads_redacted: int
    notes_redacted: int
    """Delay_Reason_Notes deleted by this pass (R29.C10).

    Counted separately from ``cases_redacted`` rather than folded into it, because the two passes
    select on different clocks over different tables and a single figure would make "180 notes and
    no cases" — a perfectly ordinary state, since a note ages from its own submission — look like
    an arithmetic error in the report."""

    retention_seconds: int
    config_version: str
    cutoff: datetime
    more_remaining: bool

    @property
    def redacted_anything(self) -> bool:
        """Whether this sweep did any work at all.

        What decides the audit record. Read as a property rather than as three comparisons at the
        call site, so a fourth pass added later cannot be forgotten in the one place that says
        whether there is anything to record."""
        return bool(self.cases_redacted or self.payloads_redacted or self.notes_redacted)

    def as_document(self) -> dict[str, object]:
        return {
            "cases_redacted": self.cases_redacted,
            "payloads_redacted": self.payloads_redacted,
            "notes_redacted": self.notes_redacted,
            "retention_seconds": self.retention_seconds,
            "retention_config_version": self.config_version,
            "cutoff": self.cutoff.isoformat(),
            "more_remaining": self.more_remaining,
        }


def sweep_customer_data_retention(
    merchant_id: uuid.UUID,
    *,
    config: Configuration,
    correlation_id: uuid.UUID | None = None,
    moment: datetime | None = None,
    limit: int = RETENTION_BATCH_LIMIT,
    factory: sessionmaker[Session] | None = None,
) -> RetentionReport:
    """Redact contact data and Delay_Reason_Notes older than ``CUSTOMER_DATA_RETENTION``.

    Two passes in one transaction (R17.C11, R29.C10), and they select differently: the contact
    pass takes terminal cases whose ``detected_at`` is past the cutoff, the note pass takes signals
    whose ``submitted_at`` is, at any case state. The module docstring argues both.

    One transaction rather than two so a crash cannot leave the tenant half-swept with one audit
    record claiming the whole of it. One audit record for both, because it is one compliance action
    with two figures in it — and the record is written only when something was actually redacted,
    so the trail's count of redactions stays a count of redactions rather than of sweeps.

    Returns a report whose ``more_remaining`` tells the caller a further sweep is due immediately
    rather than at the next interval — which is what keeps the 24-hour bound in R17.C11 and R29.C10
    satisfiable on a merchant with a large backlog, where waiting for the next tick would miss it.
    It is the disjunction of the two passes' backlogs: either alone is enough to need another run.

    Must be called outside a transaction; opens its own.
    """
    when = moment or now()
    cutoff = when - config.CUSTOMER_DATA_RETENTION

    with tenant_transaction(merchant_id, factory) as session:
        due = list(
            session.execute(
                select(RecoveryCase.id, RecoveryCase.source_event_id)
                .where(
                    RecoveryCase.merchant_id == merchant_id,
                    RecoveryCase.state.in_(_TERMINAL_VALUES),
                    RecoveryCase.detected_at < cutoff,
                    # The self-limiting condition. A case whose contact is already gone does not
                    # match, so a re-run is cheap and idempotent rather than rewriting the same
                    # rows and writing a second audit record claiming a second redaction.
                    RecoveryCase.customer_contact_masked.is_not(None),
                )
                .order_by(RecoveryCase.detected_at)
                .limit(limit + 1)
            ).all()
        )
        cases_remaining = len(due) > limit
        batch = due[:limit]

        case_ids = [row.id for row in batch]
        event_ids = [row.source_event_id for row in batch if row.source_event_id is not None]

        if case_ids:
            session.execute(
                update(RecoveryCase)
                .where(RecoveryCase.merchant_id == merchant_id, RecoveryCase.id.in_(case_ids))
                .values(customer_contact_masked=None)
            )

        payloads = 0
        if event_ids:
            payloads = rows_affected(
                session.execute(
                    update(WebhookEvent)
                    .where(
                        WebhookEvent.merchant_id == merchant_id,
                        WebhookEvent.id.in_(event_ids),
                        WebhookEvent.raw_payload_ciphertext != _REDACTED_CIPHERTEXT,
                    )
                    .values(
                        raw_payload_ciphertext=_REDACTED_CIPHERTEXT,
                        raw_payload_nonce=_REDACTED_CIPHERTEXT,
                    )
                )
            )

        notes, notes_remaining = _redact_notes(
            session,
            merchant_id,
            cutoff=cutoff,
            moment=when,
            config_version=config.version,
            limit=limit,
        )

        report = RetentionReport(
            cases_redacted=len(case_ids),
            payloads_redacted=payloads,
            notes_redacted=notes,
            retention_seconds=int(config.CUSTOMER_DATA_RETENTION.total_seconds()),
            config_version=config.version,
            cutoff=cutoff,
            more_remaining=cases_remaining or notes_remaining,
        )

        # No work, no record. A sweep that found nothing is not a redaction, and writing a record
        # for it would make the audit trail's count of redactions a count of sweeps instead —
        # which is what makes the idempotence of a re-run checkable at all.
        if not report.redacted_anything:
            return report

        AuditWriter(
            session,
            disclosure_length=config.MASK_DISCLOSURE_LENGTH,
            max_field_length=config.MAX_AUDIT_FIELD_LENGTH,
        ).write_unattached(
            merchant_id,
            AuditEntry(
                event_type=CUSTOMER_DATA_REDACTED,
                actor="retention_sweep",
                decision={
                    **report.as_document(),
                    "retained_fields": [
                        "customer_key",
                        "payment_amount",
                        "currency",
                        "detected_at",
                        "state",
                        "terminal_reason",
                        "risk_cause",
                        "recovered_amount",
                        # R29.C10's "non-identifying Customer_Signal fields required for metrics
                        # and for Recovery_Memory", named so the claim is checkable against this
                        # record rather than against the code that produced it.
                        "customer_signal.kind",
                        "customer_signal.delay_reason",
                        "customer_signal.submitted_at",
                        "customer_signal.note_truncated",
                    ],
                    "note": (
                        "customer_key is retained deliberately: it is a keyed non-reversible "
                        "hash and it is what makes a historical opt-out apply to a future case. "
                        "Destroying it would revoke every recorded opt-out. delay_reason is "
                        "retained for the same kind of reason: it is one of six enumerated "
                        "members, it identifies nobody, and Recovery_Memory segments and the "
                        "Metrics_Engine cohort counts are computed from it."
                    ),
                },
            ),
            correlation_id=correlation_id,
        )

    _logger.info(
        "customer data retention sweep completed",
        merchant_id=str(merchant_id),
        **report.as_document(),
    )
    return report


def _redact_notes(
    session: Session,
    merchant_id: uuid.UUID,
    *,
    cutoff: datetime,
    moment: datetime,
    config_version: str,
    limit: int,
) -> tuple[int, bool]:
    """Delete every Delay_Reason_Note submitted before ``cutoff``. Returns count and backlog.

    R29.C10's whole mechanism, and it is four statements' worth of decisions.

    **The note is set to ``NULL``, not to a placeholder.** ``redacted_note_is_absent`` — the
    ``CHECK (note_redacted_at IS NULL OR delay_reason_note IS NULL)`` migration ``0008``
    installed — refuses a row that claims a redaction while still holding text. So "irreversibly
    mask" is enforced by the database rather than asserted by this function: a future edit that
    wrote ``'[redacted]'`` alongside ``note_redacted_at`` would fail the insert, not ship a
    sweep that marks without erasing. That constraint is why the requirement's "delete **or**
    irreversibly mask" is resolved here as *delete*: with the ``CHECK`` in place, the only
    storable mask is the absence.

    **``note_redacted_at`` and ``retention_config_version`` are written in the same statement.**
    Not in a second pass, because a crash between them would leave a row whose note is gone and
    whose record of *which bound removed it* is missing — and R29.C10's last clause is precisely
    that the applied configuration version is recorded. One ``UPDATE``, one transaction, all
    three columns or none.

    **``delay_reason_note IS NOT NULL`` is in the predicate as well as in the index.** It makes
    the pass idempotent: a row already redacted does not match, so a re-run redacts nothing,
    reports zero, and writes no second audit record claiming a second redaction. It is also what
    lets the planner use ``ix_customer_signal_notes_for_retention``, whose own predicate is the
    same condition — a scan without it would be a sequential read of every signal the tenant has
    ever recorded.

    **The subquery selects ids first rather than updating in one statement.** ``UPDATE ... WHERE
    submitted_at < :cutoff LIMIT n`` is not expressible in PostgreSQL, and the batch bound is not
    optional: without it one sweep on a tenant with a large backlog holds a long transaction over
    a table the customer surface is inserting into. So the ids are read under the partial index,
    ordered oldest-first so the rows closest to breaching the 24-hour deadline go first, and the
    ``UPDATE`` is keyed on them.

    Ordered oldest-first for that reason and not for determinism. If a backlog cannot be cleared
    in one pass, the rows that have been over the bound longest are the ones R29.C10's 24-hour
    window is already tightest on, and ``more_remaining`` on the report is what tells the caller
    to run again immediately rather than at the next interval.

    A bulk ``UPDATE`` here rather than
    :meth:`~revora.persistence.repositories.customer.CustomerSignalRepository.redact_note` in a
    loop, which is the same statement per row: that method exists for the single-row case and
    returns whether it changed anything, and calling it five hundred times would be five hundred
    round trips to do one table's worth of work. Both write the same three columns, and the
    ``CHECK`` is what keeps them honest rather than the shared code path.

    Returns:
        ``(notes_redacted, more_remaining)``. ``more_remaining`` is ``True`` when a further note
        was waiting beyond the batch, which the caller folds into the report so the sweep is
        re-run rather than deferred.
    """
    due = list(
        session.execute(
            select(CustomerSignal.id)
            .where(
                CustomerSignal.merchant_id == merchant_id,
                CustomerSignal.submitted_at < cutoff,
                CustomerSignal.delay_reason_note.is_not(None),
            )
            .order_by(CustomerSignal.submitted_at)
            .limit(limit + 1)
        ).scalars()
    )
    if not due:
        return 0, False

    more_remaining = len(due) > limit
    batch = due[:limit]
    redacted = rows_affected(
        session.execute(
            update(CustomerSignal)
            .where(
                CustomerSignal.merchant_id == merchant_id,
                CustomerSignal.id.in_(batch),
                CustomerSignal.delay_reason_note.is_not(None),
            )
            .values(
                delay_reason_note=None,
                note_redacted_at=moment,
                retention_config_version=config_version,
            )
        )
    )
    return redacted, more_remaining
