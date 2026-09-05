"""The timeline is a projection: it writes nothing, repeats identically, and asserts nothing absent.

Feature: Case_Timeline Read Model (task 50, R26.C1 through R26.C11, R30.C14).

Property: P56 — for any Audit_Record sequence, Recovery_Case row and Customer_Signal set, two
projections of one unchanged input are equal, and the projection performs no write.

Property: P57 — for Audit_Record sequences with stages present, absent and out of order, every
Timeline_Stage holding ``DONE`` has its completing Audit_Record, and every stage without one holds
``UPCOMING`` or ``SKIPPED`` with a recorded reason.

Property: P58 — every deterministic stage sentence is identical with and without a
``DECISION_EXPLANATION`` record, and any AI paragraph present is marked ``AI_GENERATED``.

**``pure`` tier, and the reason is the shape of the function rather than a preference.**
:func:`~revora.timeline.stages.project` takes frozen dataclasses and returns one. There is no
database to migrate, no fixture to build and no transaction to open, so the whole of R26's
correctness claim is checkable at 500 examples in the time one container round trip would take.
``revora.timeline`` importing nothing from ``revora.persistence`` is what makes that true, and the
``layering`` contract in ``.importlinter`` is what keeps it true.

**How P56's no-write half is actually asserted, and why it is not circular.** The projection has no
session parameter, so there is nowhere to hand it one — which is a stronger guarantee than any test
can give and also, on its own, an untested one. :func:`_exploding_session` is the test's answer: a
stand-in whose every attribute access raises, installed as the module global of every name in
``revora.timeline.stages`` that could plausibly be a session or a repository. If the projection ever
grew a way to reach persistence, the reach would be through a module global, and this fails on it.
The assertion that the *signature* holds no session is separate and is
:func:`test_p56_the_projection_has_no_session_to_write_with`.

**On the oracle for P57.** The property compares the projected status against the completion rules
read out of the implementation, which would ordinarily be a test asserting a function equals itself.
It is not, and the reason is that the rules are a *declaration* and the statuses are the result of
applying them in a particular order with several special cases — ``EXECUTED`` keyed on a state
transition, ``OUTCOME_VERIFIED`` on four routes, ``CUSTOMER_RESPONDED``'s condition-based skip. What
P57 checks is that the order and the special cases never produce a ``DONE`` the declaration does not
license. ``tests.strategies.timeline.COMPLETING_EVENT_TYPES`` restates the event-type list
independently for the same reason, and :func:`test_the_generator_and_the_rules_agree` asserts the
two lists have not drifted — so a stage silently dropped from the production table cannot silently
vanish from the generator as well.
"""

from __future__ import annotations

import inspect
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from hypothesis import given
from hypothesis import strategies as st

from revora.api.rendering import SELECTED_ACTION_LABELS
from revora.api.rendering import WAITING_AND_WATCHING as RENDERING_WAITING_AND_WATCHING
from revora.audit import events
from revora.domain.enums import TimelineStage, TimelineStageStatus
from revora.domain.transitions import TERMINAL_STATES
from revora.timeline import stages as stages_module
from revora.timeline import templates
from revora.timeline.stages import (
    STAGE_ORDER,
    AuditRecordView,
    CaseTimeline,
    CaseView,
    FigureView,
    IntentView,
    SignalView,
    project,
    sequence_integrity,
)
from tests.strategies.timeline import (
    COMPLETING_EVENT_TYPES,
    SKIP_EVENT_TYPES,
    audit_sequences,
    case_views,
    figure_views,
    intent_views,
    signal_views,
    timeline_inputs,
)

pytestmark = pytest.mark.pure

NOW = datetime(2026, 9, 2, 12, tzinfo=UTC)
"""The instant every projection in this file is asked about.

A constant rather than a drawn value, because ``now`` is an *argument* to the projection and the
properties are about what the projection does with a fixed one. P56's equality claim is specifically
"a caller supplying the same ``now`` gets the same timeline", so varying it inside one example would
test the opposite. :func:`~tests.strategies.timeline.case_views` draws ``next_review_at`` on both
sides of this instant, which is where the time-dependent branch actually gets explored."""


# ---------------------------------------------------------------------------
# P56 — two projections of one unchanged input are equal, and nothing is written
# ---------------------------------------------------------------------------


class _WriteAttemptedError(AssertionError):
    """Raised the moment anything in the projection touches a would-be persistence name."""


class _ExplodingSession:
    """A stand-in for anything the projection could write through. Every access raises.

    Not a mock and not a spy: there is nothing to inspect afterwards, because the failure mode being
    guarded is a single attribute access. ``__getattr__`` covers ``session.add``, ``session.flush``,
    ``session.execute`` and every repository method alike, and ``__call__`` covers the case where
    the name is used as a constructor — ``RecoveryCaseRepository(session)`` — which is how a
    repository would actually be reached from a module that had acquired one.
    """

    def __getattr__(self, name: str) -> object:
        raise _WriteAttemptedError(
            f"the timeline projection reached {name!r} on a session-like object; "
            "R26.C6 says it performs no write of any kind, and it is supposed to have "
            "nothing to write with"
        )

    def __call__(self, *args: object, **kwargs: object) -> object:
        raise _WriteAttemptedError(
            "the timeline projection called a session-like object; the projection takes "
            "frozen dataclasses and returns one"
        )


_FORBIDDEN_GLOBALS: tuple[str, ...] = (
    "Session",
    "session",
    "tenant_transaction",
    "AuditRecordRepository",
    "RecoveryCaseRepository",
    "CustomerSignalRepository",
    "ExecutionIntentRepository",
)
"""Names that, if they existed in ``revora.timeline.stages``, would be the route to a write.

Enumerated rather than derived, and the enumeration is checkable: the ``layering`` contract already
forbids ``revora.timeline`` from importing ``revora.persistence`` at all, so none of these can be
present today and :func:`_exploding_session` asserts that too. The list is what turns the contract
into a runtime assertion — a contract can be edited, and this fails in the same commit if it is."""


@contextmanager
def _exploding_session() -> Iterator[None]:
    """Install an exploding stand-in under every name a write could travel through.

    Asserts first that none of the names exists, which is the stronger claim and the one the
    layering contract makes; then binds each of them anyway, so that the projection running under
    this manager is running in a module where *reaching* persistence raises rather than merely being
    unavailable. Both halves matter: the first would pass if somebody added an import and the second
    would pass if somebody added an import they never called.

    Restores in ``finally``, so a failing assertion inside the block cannot leave a poisoned module
    global for the rest of the session — the same reason ``test_cost_split._recorded_rounding`` is a
    context manager rather than a fixture, and the ``pure`` tier takes no fixtures anyway.
    """
    present = [name for name in _FORBIDDEN_GLOBALS if hasattr(stages_module, name)]
    assert not present, (
        f"revora.timeline.stages holds {present}, which the layering contract forbids: "
        "the projection is pure because it has no session and no repository in scope"
    )

    stand_in = _ExplodingSession()
    for name in _FORBIDDEN_GLOBALS:
        setattr(stages_module, name, stand_in)
    try:
        yield
    finally:
        for name in _FORBIDDEN_GLOBALS:
            delattr(stages_module, name)


@given(inputs=timeline_inputs(now=NOW))
def test_p56_two_projections_of_one_unchanged_input_are_equal(
    inputs: tuple[
        tuple[AuditRecordView, ...],
        CaseView,
        tuple[SignalView, ...],
        tuple[IntentView, ...],
        FigureView,
    ],
) -> None:
    """Feature: Case_Timeline Read Model. Property 56 — for any Audit_Record sequence, Recovery_Case
    row and Customer_Signal set, two projections of one unchanged input produce an identical
    Case_Timeline (R26.C7).

    Compared as whole values, not field by field. Every view and every part of
    :class:`~revora.timeline.stages.CaseTimeline` is a frozen dataclass, so ``==`` is structural
    equality over the entire projection — nine stages, every status, every instant, both sentences,
    every field set and the integrity finding. A field-by-field comparison would be a list of the
    things somebody remembered to check.

    The wire document is compared too. A projection that were equal while its ``as_document`` was
    not would be identical in a way no consumer could observe, and the consumer is the whole point
    of a read model.
    """
    first = project(*inputs, NOW)
    second = project(*inputs, NOW)

    assert first == second
    assert first.as_document() == second.as_document()


@given(inputs=timeline_inputs(now=NOW))
def test_p56_the_projection_performs_no_write(
    inputs: tuple[
        tuple[AuditRecordView, ...],
        CaseView,
        tuple[SignalView, ...],
        tuple[IntentView, ...],
        FigureView,
    ],
) -> None:
    """Feature: Case_Timeline Read Model. Property 56 — the projection performs no write, asserted
    by a session that raises on flush (R26.C6).

    Raises on flush *and* on every other attribute, because a write is not only a flush: ``add``,
    ``execute``, ``merge`` and constructing a repository are all routes to one, and a guard that
    watched a single method would be a guard against the one mistake nobody makes.

    Also asserts the inputs are unchanged afterwards. A projection cannot write to the database and
    it could still mutate its arguments, which would break R26.C7 in the only way the equality test
    above cannot see — the second projection would receive different input and might agree with the
    first for the wrong reason.
    """
    with _exploding_session():
        timeline = project(*inputs, NOW)

    assert isinstance(timeline, CaseTimeline)
    assert project(*inputs, NOW) == timeline


def test_p56_the_projection_has_no_session_to_write_with() -> None:
    """Feature: Case_Timeline Read Model. Property 56 — the projection's signature admits no
    session, no repository and no clock, so a write is not expressible inside it (R26.C6).

    The structural half of P56, and the half that is not an observation about one run. Every
    parameter is checked against the six the design declares: a seventh named ``session`` would be
    caught here in the commit that added it, before any behaviour existed to test.

    ``now`` being a parameter rather than a call is the other half, and it is asserted by name
    because it is what makes R26.C7 hold: a projection reading its own clock could not promise two
    identical results, and the difference between the two designs is exactly this parameter
    existing.
    """
    parameters = inspect.signature(project).parameters

    assert tuple(parameters) == ("records", "case", "signals", "intents", "figures", "now")
    for forbidden in ("session", "repository", "config", "clock", "engine"):
        assert forbidden not in parameters

    source = inspect.getsource(stages_module)
    assert "def now(" not in source
    assert "datetime.now(" not in source
    assert "utcnow(" not in source


# ---------------------------------------------------------------------------
# P57 — no stage is DONE without its completing record
# ---------------------------------------------------------------------------


def _completing_types(stage: TimelineStage) -> frozenset[str]:
    """The event types the implementation declares complete ``stage``.

    Read off the production table so the property checks the *applied* rules against the *declared*
    ones rather than against a copy in this file that could be wrong in the same direction. The
    generator's independent restatement is what stops that being circular — see the module docstring
    and :func:`test_the_generator_and_the_rules_agree`.
    """
    return frozenset(stages_module._COMPLETION_RULES[stage])


def _witnessed(records: Sequence[AuditRecordView], stage: TimelineStage) -> bool:
    """Whether some record in ``records`` licenses ``stage`` being ``DONE``.

    An independent reading of the design's completion table, written as a predicate over records
    rather than as a table lookup, so the two special cases are stated in the terms the requirement
    states them:

    ``EXECUTED`` — a ``STATE_TRANSITION`` whose ``new_state`` is ``WAITING_FOR_OUTCOME``. The design
    is explicit that this rather than the intent row is the key, because the transition exists on
    the fast path and the reconciliation path alike.

    ``OUTCOME_VERIFIED`` — one of the three recovery event types, or a ``STATE_TRANSITION`` into any
    Terminal_State. A case that ended without recovering has a verified outcome: the outcome is that
    it did not recover.
    """
    if stage is TimelineStage.EXECUTED:
        return any(
            record.event_type == events.STATE_TRANSITION
            and record.new_state == "WAITING_FOR_OUTCOME"
            for record in records
        )
    if stage is TimelineStage.OUTCOME_VERIFIED:
        terminal = {state.value for state in TERMINAL_STATES}
        return any(
            record.event_type in _completing_types(stage)
            or (record.event_type == events.STATE_TRANSITION and record.new_state in terminal)
            for record in records
        )
    return any(record.event_type in _completing_types(stage) for record in records)


@given(inputs=timeline_inputs(now=NOW))
def test_p57_every_done_stage_has_its_completing_record(
    inputs: tuple[
        tuple[AuditRecordView, ...],
        CaseView,
        tuple[SignalView, ...],
        tuple[IntentView, ...],
        FigureView,
    ],
) -> None:
    """Feature: Case_Timeline Read Model. Property 57 — across Audit_Record sequences with stages
    present, absent and out of order, every Timeline_Stage holding ``DONE`` has its completing
    Audit_Record, and every stage without one holds ``UPCOMING`` or ``SKIPPED`` with a recorded
    reason (R26.C2, R26.C5, R26.C11).

    Both directions are asserted, and the second is the one that catches the interesting bug. A
    ``DONE`` without a record is the failure the requirement names; a stage that has its record and
    is *not* ``DONE`` is a stage that will never complete, which reads to a merchant as a case that
    stalled at a step it actually finished.

    The generated inputs are deliberately inconsistent — figures without their records, intents
    without their transition, a terminal case with no terminal record — because a projection that
    inferred a stage's completion from a *figure* being present would pass on every input a real
    system produces. Independence in the generator is what makes the inference detectable.
    """
    records, case, signals, intents, figures = inputs
    timeline = project(*inputs, NOW)

    assert len(timeline.stages) == len(STAGE_ORDER)
    assert tuple(stage.stage for stage in timeline.stages) == STAGE_ORDER

    for projection in timeline.stages:
        witnessed = _witnessed(records, projection.stage)

        if projection.status is TimelineStageStatus.DONE:
            assert witnessed, (
                f"{projection.stage.value} is DONE with no completing record; "
                f"records were {[record.event_type for record in records]}"
            )
            # A DONE stage's instant is its completing record's, so it has to be one of the
            # instants actually present in the sequence. Anything else is a clock read.
            assert projection.instant in {record.occurred_at for record in records}
            assert projection.decision_sentence is not None
            assert projection.evidence_sentence is not None
            assert projection.skip_reason is None
            continue

        # No completing record: the status must be one of the three honest alternatives, never a
        # substituted DONE and never a sentence about a decision nothing recorded.
        assert projection.status in {
            TimelineStageStatus.UPCOMING,
            TimelineStageStatus.IN_PROGRESS,
            TimelineStageStatus.SKIPPED,
        }
        assert projection.instant is None
        assert projection.decision_sentence is None
        assert projection.evidence_sentence is None

        if projection.status is TimelineStageStatus.SKIPPED:
            # R26.C5. The reason is named from the persisted records, so it is an audit event type
            # — for CUSTOMER_RESPONDED, the terminal transition qualified by the state reached.
            assert projection.skip_reason is not None
            assert projection.skip_reason.split(":")[0] in (
                *SKIP_EVENT_TYPES,
                events.STATE_TRANSITION,
            )

    del case, signals, intents, figures


@given(records=audit_sequences(), case=case_views(now=NOW), figures=figure_views())
def test_p57_record_order_changes_nothing(
    records: tuple[AuditRecordView, ...], case: CaseView, figures: FigureView
) -> None:
    """Feature: Case_Timeline Read Model. Property 57 — the projection of an out-of-order
    Audit_Record sequence equals the projection of the same records in ascending sequence order
    (R26.C1).

    The generator already shuffles, so this is the claim stated directly rather than relied upon:
    the same records sorted, reversed and shuffled give one timeline. Stage order is a module-level
    tuple and the completion rules are membership tests, so record order can only affect *which*
    records satisfy which rule — and since the set is the same, nothing may move.

    Without this, ``STAGE_ORDER`` could quietly become "order of first completing record" and every
    other property here would still pass: the statuses would be right and only the reading order
    would be wrong, on the surface whose entire value is that the order is the explanation.
    """
    ascending = tuple(sorted(records, key=lambda record: record.seq))
    descending = tuple(reversed(ascending))

    baseline = project(ascending, case, (), (), figures, NOW)

    assert project(descending, case, (), (), figures, NOW) == baseline
    assert project(records, case, (), (), figures, NOW) == baseline


@given(records=audit_sequences(with_gap=True), case=case_views(now=NOW), figures=figure_views())
def test_p57_a_gapped_sequence_still_projects_and_names_its_missing_numbers(
    records: tuple[AuditRecordView, ...], case: CaseView, figures: FigureView
) -> None:
    """Feature: Case_Timeline Read Model. Property 57 / R26.C11 — where the Audit_Record sequence
    holds a gap, the Timeline_Stages are still presented together with an indication naming the
    missing sequence numbers, and no stage is asserted ``DONE`` on the strength of an absent record.

    Three claims, and the third is the one that makes the banner honest rather than decorative. A
    reader is being told the trail is incomplete; if the timeline had also filled the hole in —
    marked a stage ``DONE`` because the stages either side of it were — the banner would be an
    admission beside a fabrication.

    The missing numbers are named rather than counted, because "one record is missing" is not
    actionable and "sequence 7 is missing" can be reconciled against the writer's own logs.
    """
    timeline = project(records, case, (), (), figures, NOW)

    assert len(timeline.stages) == len(STAGE_ORDER)
    assert not timeline.integrity.complete
    assert timeline.integrity.missing
    assert timeline.integrity.as_document()["detail"] is not None

    present = {record.seq for record in records}
    assert set(timeline.integrity.missing).isdisjoint(present)

    for projection in timeline.stages:
        if projection.status is TimelineStageStatus.DONE:
            assert _witnessed(records, projection.stage)


@given(records=audit_sequences(with_gap=False))
def test_p57_a_whole_sequence_reports_no_gap(records: tuple[AuditRecordView, ...]) -> None:
    """Feature: Case_Timeline Read Model. Property 57 / R26.C11 — a gap-free Audit_Record sequence
    reports complete and names no missing number.

    The anti-vacuity half of the gap check, and it is not optional. A ``sequence_integrity`` that
    returned ``complete=False`` unconditionally would pass the gapped property above and put a
    "the allocation was bypassed" banner on every case in the system — which, because the banner is
    a serious claim, is a worse failure than missing a real gap.

    The empty sequence is included and reports complete. A case whose ``CASE_DETECTED`` record has
    not been read is not a case with a gap.
    """
    integrity = sequence_integrity(records)

    assert integrity.complete
    assert integrity.missing == ()
    assert integrity.as_document()["detail"] is None
    if records:
        assert integrity.starts_at_one
        assert integrity.record_count == len(records)


# ---------------------------------------------------------------------------
# P58 — the deterministic sentences do not depend on the reasoning layer
# ---------------------------------------------------------------------------


def _sentences(timeline: CaseTimeline) -> tuple[tuple[str | None, str | None], ...]:
    """Every stage's two sentences, in stage order. The thing R26.C9 says must not move."""
    return tuple(
        (projection.decision_sentence, projection.evidence_sentence)
        for projection in timeline.stages
    )


@given(
    records=audit_sequences(),
    case=case_views(now=NOW),
    signals=signal_views(),
    intents=intent_views(),
    figures=figure_views(with_ai_explanation=False),
)
def test_p58_every_sentence_is_identical_with_and_without_an_explanation(
    records: tuple[AuditRecordView, ...],
    case: CaseView,
    signals: tuple[SignalView, ...],
    intents: tuple[IntentView, ...],
    figures: FigureView,
) -> None:
    """Feature: Case_Timeline Read Model. Property 58 — every deterministic stage sentence is
    identical with and without a recorded ``DECISION_EXPLANATION`` response, and any AI paragraph
    present is marked ``AI_GENERATED`` (R26.C9).

    **Both branches are genuinely covered.** The pair is constructed here from one drawn view — the
    same input, with and without the paragraph — because nothing in ``revora`` writes
    ``ai_invocation`` today: ``revora.reasoning`` holds contracts and schemas and no module imports
    it. Without constructing the record, this property would compare the absent case against itself
    and read as though it had checked something.

    The comparison is over the whole nine-stage sentence set *and* the field sets, because R26.C9
    names criteria 3 **and** 4 — the sentences and the presented fields. Comparing only the
    sentences would leave a paragraph free to change a field, which is the same claim broken one
    level down.

    The label is asserted on the paragraph, not on the stage. ``AI_GENERATED`` beside advisory prose
    is the difference between an explanation and evidence, and this is the one place in the timeline
    where a model's words could be read as a recorded fact.
    """
    without = project(records, case, signals, intents, figures, NOW)
    with_explanation = project(
        records,
        case,
        signals,
        intents,
        replace(figures, ai_explanation_text="Chose the link because it was worth the most."),
        NOW,
    )

    assert _sentences(with_explanation) == _sentences(without)
    assert [projection.fields for projection in with_explanation.stages] == [
        projection.fields for projection in without.stages
    ]
    assert [projection.status for projection in with_explanation.stages] == [
        projection.status for projection in without.stages
    ]

    assert without.ai_explanation is None
    assert with_explanation.ai_explanation is not None
    assert with_explanation.ai_explanation_label == "AI_GENERATED"
    assert with_explanation.as_document()["ai_explanation_label"] == "AI_GENERATED"
    # The key travels either way, so a client has one rendering path rather than two.
    assert "ai_explanation_label" in without.as_document()


@given(
    records=audit_sequences(),
    case=case_views(now=NOW),
    figures=figure_views(with_ai_explanation=True),
)
def test_p58_no_sentence_reads_the_explanation(
    records: tuple[AuditRecordView, ...], case: CaseView, figures: FigureView
) -> None:
    """Feature: Case_Timeline Read Model. Property 58 — no stage sentence contains any part of a
    recorded ``DECISION_EXPLANATION`` paragraph (R26.C9).

    The mechanism behind the invariance above, asserted directly. The equality test would pass if a
    sentence read the paragraph and the two drawn paragraphs happened to be equal; this cannot,
    because it checks that no sentence contains the text at all.

    Stated as a containment check over a distinctive paragraph rather than as a template inspection,
    so it holds regardless of *how* a future sentence might come to include it — a placeholder added
    to ``STAGE_TEMPLATES``, a concatenation, or an interpolation somewhere in between.
    """
    marker = "SENTINEL-EXPLANATION-TEXT"
    timeline = project(records, case, (), (), replace(figures, ai_explanation_text=marker), NOW)

    for projection in timeline.stages:
        assert marker not in (projection.decision_sentence or "")
        assert marker not in (projection.evidence_sentence or "")
        assert marker not in repr(projection.fields)

    assert timeline.ai_explanation == marker


# ---------------------------------------------------------------------------
# Example-based checks: the things a property should not be asked to cover
# ---------------------------------------------------------------------------


def test_the_generator_and_the_rules_agree() -> None:
    """The generator's event-type list has not drifted from the implementation's.

    The generator restates the completing event types instead of importing them, so that a stage
    dropped from the production table does not disappear from the generated inputs at the same time
    — which would leave P57 passing over a smaller world. That independence is only safe if a drift
    is caught, and this is where.

    Deliberately an example test rather than a property: it is a claim about two module-level
    constants, and there is nothing to generate.
    """
    declared = {
        event_type
        for types in stages_module._COMPLETION_RULES.values()
        for event_type in types
    }
    assert declared <= set(COMPLETING_EVENT_TYPES)

    skips = {
        event_type for types in stages_module._SKIP_RULES.values() for event_type in types
    }
    assert skips == set(SKIP_EVENT_TYPES)


def test_every_stage_has_a_declared_template_and_a_declared_rule() -> None:
    """All nine stages have a rule and a template, and ``STAGE_ORDER`` is the enumeration.

    Three totality claims that the import-time guards in ``stages.py`` and ``templates.py`` already
    enforce. Asserted again here because an import-time ``raise`` fails the whole suite with a
    collection error, which is loud but says nothing about which invariant broke — and because a
    ``# pragma: no cover`` guard is a guard nobody has watched fail.
    """
    assert set(STAGE_ORDER) == set(TimelineStage)
    assert len(STAGE_ORDER) == 9
    assert set(stages_module._COMPLETION_RULES) == set(TimelineStage)
    assert set(templates.STAGE_TEMPLATES) == set(TimelineStage)


def test_the_shared_waiting_label_agrees_with_the_api_renderer() -> None:
    """R26.C14's shared label is one string, even though it is written in two places.

    ``revora.timeline.templates`` cannot import ``revora.api.rendering`` — the layering contract
    forbids the upward import — so ``WAITING_AND_WATCHING`` is duplicated and the duplication is
    guarded here. The guard is worth more than it looks: the requirement is that ``DO_NOTHING`` and
    ``WAIT`` render under *one* label, and a merchant moving from the case list to the timeline
    would be the first to notice two.

    The whole action table is compared, not just the shared string. A divergence on any action is
    the same failure, and comparing one entry would leave the other eight unguarded.
    """
    assert templates.WAITING_AND_WATCHING == RENDERING_WAITING_AND_WATCHING
    assert {
        member: pair.label for member, pair in templates.ACTION_LABELS.items()
    } == dict(SELECTED_ACTION_LABELS)


def test_a_skipped_stage_names_a_recorded_reason_rather_than_prose() -> None:
    """R26.C5, on the one stage skipped by a condition rather than by a purpose-written record.

    A terminal case with no ``CUSTOMER_SIGNAL_RECORDED`` is a customer who never answered, and
    nothing writes an audit record for a thing a person did not do. The reason therefore names the
    terminal transition *and* the state it reached — the bare event type would say only that the
    case moved, and which ending it moved to is the whole of what makes the silence explicable.

    An example test because it pins one exact string on one exact input, which is what a reader of
    R26.C5 wants to see.
    """
    records = (
        AuditRecordView(seq=1, event_type=events.CASE_DETECTED, occurred_at=NOW),
        AuditRecordView(
            seq=2, event_type=events.STATE_TRANSITION, occurred_at=NOW, new_state="EXPIRED"
        ),
    )
    case = CaseView(
        case_id="c1",
        state="EXPIRED",
        detected_at=NOW,
        decision_cycle_count=1,
        max_recovery_attempts=3,
        terminal_reason="RECOVERY_WINDOW_ELAPSED",
    )

    timeline = project(records, case, (), (), FigureView(), NOW)
    responded = timeline.stage(TimelineStage.CUSTOMER_RESPONDED)

    assert responded.status is TimelineStageStatus.SKIPPED
    assert responded.skip_reason == f"{events.STATE_TRANSITION}:EXPIRED"
    assert responded.decision_sentence is None

    # And the ended case still gets a verified outcome, with the R26.C14 label for its ending.
    outcome = timeline.stage(TimelineStage.OUTCOME_VERIFIED)
    assert outcome.status is TimelineStageStatus.DONE
    assert outcome.decision_sentence == (
        "Ended: the recovery window closed (RECOVERY_WINDOW_ELAPSED)."
    )


def test_the_decided_stage_is_in_progress_while_a_review_remains_permitted() -> None:
    """R30.C14 — a case awaiting a review shows ``DECIDED`` as ``IN_PROGRESS`` rather than ``DONE``.

    Three conditions decide it and each is checked by moving one of them: a future
    ``next_review_at`` with the cycle counter below the bound is ``IN_PROGRESS``; a past instant is
    not; a counter at the bound is not. Naming them separately is the point — a case at the cap
    claiming a future review is a promise R30.C10 will refuse, and a case with an overdue instant
    claiming one is worse, because the appointment has already been missed.

    ``now`` is passed explicitly on every call, which is also the demonstration that the branch is
    reachable without a clock.
    """
    records = (AuditRecordView(seq=1, event_type=events.CASE_DETECTED, occurred_at=NOW),)
    waiting = CaseView(
        case_id="c1",
        state="POLICY_CHECK",
        detected_at=NOW,
        decision_cycle_count=1,
        max_recovery_attempts=3,
        next_review_at=datetime(2026, 9, 3, tzinfo=UTC),
    )

    assert (
        project(records, waiting, (), (), FigureView(), NOW)
        .stage(TimelineStage.DECIDED)
        .status
        is TimelineStageStatus.IN_PROGRESS
    )
    assert (
        project(records, replace(waiting, next_review_at=None), (), (), FigureView(), NOW)
        .stage(TimelineStage.DECIDED)
        .status
        is TimelineStageStatus.UPCOMING
    )
    assert (
        project(records, replace(waiting, decision_cycle_count=3), (), (), FigureView(), NOW)
        .stage(TimelineStage.DECIDED)
        .status
        is TimelineStageStatus.UPCOMING
    )


def test_a_review_is_presented_with_its_trigger_and_both_actions() -> None:
    """R30.C14 — every ``CASE_REVIEWED`` record appears at the ``DECIDED`` stage with its
    Review_Trigger, its previous selected action and its new one.

    Two reviews, both re-selecting a null action, because that is the case the requirement was
    written about: a review that changed nothing produces no state visible anywhere else — same
    state, same action, one more cycle spent — so without both actions on the record, "we
    re-examined and still think waiting is right" and "we forgot about it" are the same row.

    Both actions carry their label and their stored member (R26.C14), so ``DO_NOTHING`` followed by
    ``WAIT`` reads as one situation rather than as a change of course.
    """
    records = (
        AuditRecordView(seq=1, event_type=events.RECOMMENDATION_RECORDED, occurred_at=NOW),
        AuditRecordView(
            seq=2,
            event_type=events.CASE_REVIEWED,
            occurred_at=NOW,
            review_trigger="SCHEDULED_REVIEW",
            previous_selected_action="DO_NOTHING",
            new_selected_action="WAIT",
        ),
        AuditRecordView(
            seq=3,
            event_type=events.CASE_REVIEWED,
            occurred_at=NOW,
            review_trigger="EVENT_ATTACHED",
            previous_selected_action="WAIT",
            new_selected_action="PAYMENT_LINK",
        ),
    )
    case = CaseView(
        case_id="c1",
        state="POLICY_CHECK",
        detected_at=NOW,
        decision_cycle_count=2,
        max_recovery_attempts=3,
    )

    reviews = project(records, case, (), (), FigureView(), NOW).stage(
        TimelineStage.DECIDED
    ).fields["reviews"]

    assert isinstance(reviews, list)
    assert [review["review_trigger"] for review in reviews] == [
        "SCHEDULED_REVIEW",
        "EVENT_ATTACHED",
    ]
    assert reviews[0]["previous_selected_action"] == {
        "label": templates.WAITING_AND_WATCHING,
        "member": "DO_NOTHING",
    }
    assert reviews[0]["new_selected_action"] == {
        "label": templates.WAITING_AND_WATCHING,
        "member": "WAIT",
    }


def test_a_sentence_refuses_a_substitution_set_that_does_not_match_its_template() -> None:
    """``templates.render`` refuses both a missing key and a surplus one (R26.C3).

    The missing case would fail anyway; the surplus case is the one this exists for. ``format_map``
    ignores a key the template has no slot for, so a caller computing a value the declared sentence
    does not present would be silently tolerated — and that is how a second, undeclared sentence
    starts being assembled beside the declared one.
    """
    template = "Policy verdict {verdict}."

    assert templates.render(template, {"verdict": "Approved (APPROVED)"}) == (
        "Policy verdict Approved (APPROVED)."
    )
    with pytest.raises(templates.TemplateError):
        templates.render(template, {})
    with pytest.raises(templates.TemplateError):
        templates.render(template, {"verdict": "Approved", "primary_reason": "x"})


@given(member=st.text(max_size=8))
def test_an_unknown_enumeration_member_keeps_its_stored_value(member: str) -> None:
    """A value no label table holds renders as the not-recorded label with the member preserved.

    R26.C14 asks for the label *and* the stored value, and the honest failure direction for a value
    this build does not know is to lose the label rather than the value: a label nobody chose is
    worse than none, and losing the row's own value as well would leave a reader nothing to check
    against the database.

    Reachable in practice on any deployment that renames an enumeration member without migrating the
    rows that hold the old one, which is exactly when somebody is reading a timeline to find out
    what happened.
    """
    pair = templates.labelled(templates.ACTION_LABELS, member)

    if member in templates.ACTION_LABELS:
        assert pair == templates.ACTION_LABELS[member]
    else:
        assert pair.label == templates.NOT_RECORDED
        assert pair.member == member
