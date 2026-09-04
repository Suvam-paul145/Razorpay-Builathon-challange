"""Properties 47 and 48 — partial money is never recovery, and a request moves no figure.

Task 43.2, ``model`` tier. Two properties, and between them they are the whole of R22's money
story: partial money never becomes recovery (P47), and an arrangement request leaves the full
amount unresolved exactly once and still lets a full capture reconcile the case (P48).

**Why ``model`` and not ``pg``, and what that costs.** The four revenue figures are SQL
aggregates — ``observed_recovered_revenue`` and ``natural_recovered_revenue`` sum
``recovery_outcome.recovered_amount``, and both incremental figures are gated on the experiment
analysis reading the same table — so "contributes zero" cannot be *summed* here without a
database. What can be established without one is stronger in the direction that matters and is
the reason the tier is right rather than merely cheaper: every one of those figures is a sum over
``recovery_outcome``, that table has ``verified_by_read_id NOT NULL`` and ``UNIQUE (case_id)``,
and the only code that inserts into it is ``_declare_recovery``. So *zero contribution* reduces to
*no row*, and *no row* reduces to *the monitor did not reach the declaration* — which is a
decision made by two pure functions and one branch order, all three of which are checkable here.
The driven end-to-end version lives in the ``pg`` lifecycle machine, where a real capture really
does produce a real row; this file is about whether the decision is right, not about whether
PostgreSQL adds up.

**Three mechanisms appear below, and each is used where it is the strongest available form.**

* *Generated inputs* over the pure decision functions — ``is_recovered`` and ``is_partial`` — and
  over the payment-link request builder. Hypothesis is doing real work in both: the partial
  boundary is an inequality with three interesting edges, and the builder clamps and validates.
* *Declaration reading* — the transition table, ``UNRESOLVED_STATES``, ``FEATURE_KEYS``, the
  ``RecoveryOutcome`` table arguments. These are data, and asserting against the data is what
  makes the assertion fail when somebody changes the system rather than when somebody changes a
  copy of it.
* *AST inspection*, for the two claims that are about what code **cannot** do: no module on the
  arrangement path assigns ``payment_amount``, ``currency`` or ``window_end_at``, and nothing in
  the tree sets ``accept_partial`` to anything but false. A substring search would fail on the
  prose — these modules explain at length which fields they must not touch, and several of them
  name all three in a docstring — so the walk is over names the parser sees and not over text.
  The same reason ``test_customer_signal_authority.py`` reads the AST.

**What ``partially_paid`` is, precisely, because it is easy to assert against the wrong type.**
It is a Payment **Link** status. ``PaymentEntity.status`` is validated against ``PAYMENT_STATUSES``
— the five ``PaymentStatus`` members — so a payment entity carrying ``partially_paid`` is not
merely uncommon, it is unconstructible. That is asserted below rather than assumed, because it is
the load-bearing half of "a partially paid link is never recovery": ``is_recovered`` tests for
``captured``, and a status that cannot exist on the entity cannot pass that test by any route.

**One reading is recorded here rather than resolved.** P47's phrase *a captured amount below
``payment_amount``* has two candidate meanings, and this file asserts the one the implementation
already documents: a capture is partial when the amount that stayed captured is below the amount
at risk, which ``revora/outcome/reads.py`` implements as ``0 < amount_refunded < amount`` and
comments as "a capture for less than the amount at risk is partial in substance whatever it is
called". The other reading — comparing ``PaymentEntity.amount`` against
``recovery_case.payment_amount`` — is **deliberately not asserted**, because R10.C3 makes the
provider's read the authority on the recovered figure and
``tests/persistence/test_outcome_monitor.py`` asserts on purpose that a read reporting 180000
against a case at risk 250000 declares recovery of 180000. Two requirements would have to be
reconciled to change that, and doing it inside a property test would be changing money behaviour
by way of a test file.
"""

from __future__ import annotations

import ast
import inspect
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError
from sqlalchemy import UniqueConstraint

from revora.customer.arrangements import (
    FEATURE_PARTIAL_ARRANGEMENT,
    PARTIAL_ARRANGEMENT_KIND,
    ArrangementRequest,
    arrangement_feature,
)
from revora.customer.signals import PartialArrangementSubmission
from revora.domain.actions import CandidateAction
from revora.domain.enums import CaseState, OutcomeClass, TerminalReason
from revora.domain.money import Minor
from revora.domain.segments import FEATURE_KEYS
from revora.domain.transitions import TERMINAL_STATES, is_legal, rule_for
from revora.jobs import pipeline as pipeline_module
from revora.metrics.unresolved import UNRESOLVED_STATES
from revora.outcome import monitor as monitor_module
from revora.outcome.monitor import OutcomeAssessment, OutcomeVerdict
from revora.outcome.reads import PARTIAL_STATUSES, is_partial, is_recovered
from revora.persistence.models.execution import RecoveryOutcome
from revora.providers.classification import (
    PAYMENT_STATUSES,
    PaymentEntity,
    PaymentLinkEntity,
)
from revora.providers.payment_link import CustomerContact, build_payment_link_request

_REPO_ROOT = Path(__file__).resolve().parents[2]
_REVORA = _REPO_ROOT / "revora"

_NOW = datetime(2026, 3, 1, tzinfo=UTC)
_CONTACT = CustomerContact(contact="+919876500000", email="buyer@example.com")
_MAX_MESSAGE_LENGTH = 300

_SOME_CASE_ID = uuid.UUID("c0000000-0000-4000-8000-0000000000cc")
"""A fixed identifier for the record shapes constructed below.

Fixed rather than generated. Every assertion that uses it is about a dataclass's defaults or about
which keys a document has, and drawing a random identifier would suggest the property depended on
it."""

_MONEY_FIELDS: tuple[str, ...] = ("payment_amount", "currency", "window_end_at")
"""The three case columns R22.C7 requires an arrangement request to leave alone.

Named as a tuple so the two AST tests below assert over the same list, and so adding a fourth
untouchable column is one edit. ``window_end_at`` is in here rather than in a window-specific test
because R22.C7 groups the three: the amount, what it is denominated in, and how long there is left
to recover it are the three facts a customer asking to pay differently might expect to move, and
the requirement's answer is the same for all three."""


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------


def _amounts() -> st.SearchStrategy[int]:
    """Positive minor-unit amounts, weighted to the boundaries the partial test turns on.

    ``2`` is the smallest amount that *has* a strict interior — a partial capture needs a refund
    strictly between zero and the amount, which is impossible at 1 — so it is the boundary case
    that a naive ``<=`` would get wrong, and it is drawn explicitly rather than left to chance.
    """
    return st.one_of(
        st.just(2),
        st.just(100),
        st.integers(min_value=2, max_value=50_000_000),
    )


@st.composite
def _partial_captures(draw: st.DrawFn) -> tuple[int, PaymentEntity]:
    """A case's amount at risk, and a captured read that kept less than that amount.

    The refund is drawn strictly inside ``(0, amount)`` — the interior, not the closed interval —
    because both ends are *not* partial and both are covered by their own test below: a zero
    refund is a full capture and is recovery, and a full refund is a reversal and is a different
    thing again. Generating the interior only is what keeps this test about the property rather
    than about the strategy's edges.
    """
    amount = draw(_amounts())
    refunded = draw(st.integers(min_value=1, max_value=amount - 1))
    entity = PaymentEntity(
        id=f"pay_{draw(st.integers(min_value=0, max_value=10**14)):014d}",
        status="captured",
        captured=True,
        amount=amount,
        amount_refunded=refunded,
    )
    return amount, entity


def _link_statuses() -> st.SearchStrategy[str]:
    return st.sampled_from(sorted(PARTIAL_STATUSES))


# ---------------------------------------------------------------------------
# AST helpers. The same argument as `test_customer_signal_authority.py`: these modules
# explain in prose which fields they must not write, so a text search fails on the
# explanation rather than on a write.
# ---------------------------------------------------------------------------


def _assigned_attributes(tree: ast.AST) -> set[str]:
    """Every attribute name that appears as an assignment target, at any depth.

    Covers ``x.y = ...``, ``x.y += ...`` and ``x.y: T = ...``, because all three write a column
    and only the first looks like a write at a glance. Walrus and starred forms cannot target an
    attribute in a way the other three miss.
    """
    written: set[str] = set()
    for node in ast.walk(tree):
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, ast.AugAssign | ast.AnnAssign):
            targets = [node.target]
        for target in targets:
            for inner in ast.walk(target):
                if isinstance(inner, ast.Attribute):
                    written.add(inner.attr)
    return written


def _tree_of(path: Path) -> ast.AST:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _function_tree(function: object) -> ast.AST:
    """The AST of one module-level function's own source.

    No dedent: every function read here is defined at module level, so its source starts at column
    zero and ``ast.parse`` accepts it as written. A dedent would be the wrong tool anyway — it
    would silently make a *method* parseable and the position claims below are about a plain
    function's statement list.
    """
    return ast.parse(inspect.getsource(function))  # type: ignore[arg-type]


def _python_files() -> list[Path]:
    return sorted(_REVORA.rglob("*.py"))


# ===========================================================================
# Property 47 — partial money is never recovery
# ===========================================================================


@pytest.mark.model
@given(captured=_partial_captures())
@settings(max_examples=150, deadline=None)
def test_p47_a_capture_that_kept_less_than_the_amount_at_risk_is_not_recovery(
    captured: tuple[int, PaymentEntity],
) -> None:
    """Feature: Customer Response Loop. Property 47, first clause — a captured amount below the
    amount at risk leaves the Recovery_Case unrecovered (R22.C4).

    Two functions decide this and both are asserted, because they answer different questions and
    the monitor consults them in a fixed order. ``is_recovered`` is allowed to say yes here — the
    status genuinely is ``captured`` — and that is exactly why ``is_partial`` has to say yes too
    and has to be consulted first. A test that only checked ``is_recovered`` would pass on a build
    that had deleted the partial branch entirely.

    The generated interval is strict on both ends, and the two ends are the two ways to get this
    wrong. A refund of zero is a full capture and *is* recovery; a refund equal to the amount is a
    reversal, not a partial payment. An implementation using ``<=`` at either end would either
    refuse every recovery or accept every reversal as one.
    """
    amount_at_risk, entity = captured

    assert entity.amount == amount_at_risk, "the strategy stopped generating what it claims to"
    assert 0 < entity.amount_refunded < entity.amount

    assert is_partial(entity), (
        f"a capture of {entity.amount} with {entity.amount_refunded} refunded was not read as "
        "partial. R22.C4 holds a Recovery_Case where a partial capture is reported, and "
        "revora/outcome/reads.py is the one place that decides it"
    )
    assert is_recovered(entity), (
        "the strategy is meant to produce a *captured* read, so that the partial test is what "
        "excludes it rather than the status being unrecognised. If this fails the test below it "
        "is no longer proving anything"
    )


@pytest.mark.model
@given(amount=_amounts())
@settings(max_examples=50, deadline=None)
def test_p47_a_full_capture_is_recovery_so_the_partial_test_is_not_vacuous(amount: int) -> None:
    """The control. A capture with nothing refunded is recovery, and must stay so.

    Included because every assertion in this file is of the form "this is not counted", and a
    build that counted *nothing* would satisfy all of them. This is the one test here that fails
    on that build.
    """
    entity = PaymentEntity(
        id="pay_00000000000001",
        status="captured",
        captured=True,
        amount=amount,
        amount_refunded=0,
    )
    assert is_recovered(entity)
    assert not is_partial(entity), (
        "a capture with nothing refunded was read as partial, so no case can ever recover"
    )


@pytest.mark.model
@given(status=_link_statuses())
@settings(max_examples=10, deadline=None)
def test_p47_a_partially_paid_provider_state_is_never_recovery(status: str) -> None:
    """Property 47, second clause — a Payment_Provider state of ``partially_paid`` is not
    recovery (R22.C4), and it is not recovery twice over.

    ``partially_paid`` is a Payment **Link** status, which is the fact that makes this clause
    hold at two independent layers rather than one:

    1. ``is_partial`` names it in :data:`PARTIAL_STATUSES` and returns ``True`` on it, so the
       monitor holds the case.
    2. ``PaymentEntity.status`` is validated against ``PAYMENT_STATUSES``, the five verified
       payment statuses, and ``partially_paid`` is not among them — so a *payment* entity
       carrying it cannot be constructed at all. ``is_recovered`` tests for ``captured``, and a
       status that cannot exist on the entity cannot reach that test by any route.

    The second is asserted rather than assumed because it is what makes the first sufficient. If
    ``partially_paid`` ever became a valid payment status, layer 1 alone would still be correct
    but would be the only thing standing between a partly-paid link and a recovery figure.
    """
    assert status in PARTIAL_STATUSES
    assert status not in PAYMENT_STATUSES, (
        f"{status!r} became a verified *payment* status. It is a payment-link status, and the "
        "partial-payment exclusion is layered on the assumption that a PaymentEntity cannot "
        "carry it"
    )

    link = PaymentLinkEntity(
        id="plink_0000000000001", short_url="https://rzp.io/i/abc123", status=status
    )
    assert is_partial(link), (  # type: ignore[arg-type]
        f"a payment link reporting {status!r} was not read as partial. Both entities carry a "
        "notion of partial payment and PARTIAL_STATUSES is the one rule that excludes both"
    )

    with pytest.raises(ValidationError):
        PaymentEntity(
            id="pay_00000000000001",
            status=status,
            captured=True,
            amount=100,
            amount_refunded=0,
        )


@pytest.mark.model
def test_p47_the_monitor_holds_on_a_partial_before_it_can_declare_recovery() -> None:
    """Property 47, third clause — zero contribution to all four revenue figures.

    Reduced to something a tier without a database can establish, in three steps, each asserted:

    1. **Every revenue figure is a sum over ``recovery_outcome``.** That table carries
       ``UNIQUE (case_id)`` and ``verified_by_read_id NOT NULL``, so a case contributes to the
       figures at most once and only against an authoritative read.
    2. **The only insert into it is ``_declare_recovery``**, and ``_assess`` reaches that call
       only *after* the partial branch, which returns. So a read the partial test claims can
       never produce a row.
    3. **The assessment the partial branch returns carries no money.** ``recovered_amount`` is
       ``None`` and ``classification`` is ``None``, and ``declared_recovery`` — documented as the
       only property the metrics layer should trust — is ``False``. A ``None`` classification
       matches neither ``OBSERVED`` nor ``NATURAL``, which are the two filters the revenue sums
       apply.

    Step 2 is read from the AST of ``_assess`` rather than asserted by driving it, because driving
    it needs a session and this tier has none. The check is deliberately narrow — the partial
    branch's body ends in a ``return``, and the ``_declare_recovery`` call comes after it in the
    same statement list — so it fails on the one refactor that would break the property (moving
    the declaration above the partial test, or dropping the ``return``) and not on cosmetic edits.
    """
    # 1.
    unique = [
        argument
        for argument in RecoveryOutcome.__table_args__
        if isinstance(argument, UniqueConstraint)
    ]
    keyed_on_case = [
        constraint
        for constraint in unique
        if tuple(column.name for column in constraint.columns) == ("case_id",)
    ]
    assert keyed_on_case, (
        "recovery_outcome lost UNIQUE (case_id). Every revenue figure sums this table, so a "
        "second row per case would double-count the money in all four"
    )
    assert RecoveryOutcome.__table__.c.verified_by_read_id.nullable is False, (
        "recovery_outcome.verified_by_read_id became nullable, so a recovery could be recorded "
        "with no authoritative read behind it"
    )

    # 2.
    tree = _function_tree(monitor_module._assess)
    body = next(
        node.body
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_assess"
    )
    statements = _flattened_statements(body)
    partial_index = _index_of_partial_guard(statements)
    declare_index = _index_of_declare_call(statements)
    assert partial_index is not None, (
        "no `if record.partial:` guard found in _assess. R22.C4's hold is that branch, and "
        "without it a partial capture falls through to the recovery declaration"
    )
    assert declare_index is not None, "no _declare_recovery call found in _assess"
    assert partial_index < declare_index, (
        f"_assess reaches _declare_recovery (statement {declare_index}) before it tests for a "
        f"partial capture (statement {partial_index}). Order is the whole guarantee here: the "
        "partial test is only an exclusion if it runs first"
    )

    # 3.
    assessment = OutcomeAssessment(OutcomeVerdict.PARTIAL_HELD, _SOME_CASE_ID)
    assert assessment.declared_recovery is False
    assert assessment.recovered_amount is None, (
        "the partial verdict carries a recovered amount. Even zero would be wrong: the metrics "
        "layer sums recovery_outcome rows, and an amount on this assessment invites a caller to "
        "write one"
    )
    assert assessment.classification is None
    assert assessment.classification not in (OutcomeClass.OBSERVED, OutcomeClass.NATURAL)


def _flattened_statements(body: list[ast.stmt]) -> list[ast.stmt]:
    """``_assess``'s statements, with the single ``with`` block's body spliced in.

    The whole function is one ``with tenant_transaction(...)`` block, so the ordering P47 cares
    about lives one level down. Flattening exactly one level rather than walking the tree keeps
    "before" and "after" meaningful — a full walk visits nested branches in an order that says
    nothing about execution.
    """
    flattened: list[ast.stmt] = []
    for statement in body:
        if isinstance(statement, ast.With):
            flattened.extend(statement.body)
        else:
            flattened.append(statement)
    return flattened


def _index_of_partial_guard(statements: list[ast.stmt]) -> int | None:
    """The position of ``if record.partial:`` whose body returns, or ``None``."""
    for index, statement in enumerate(statements):
        if not isinstance(statement, ast.If):
            continue
        test = statement.test
        if (
            isinstance(test, ast.Attribute)
            and test.attr == "partial"
            and any(isinstance(inner, ast.Return) for inner in ast.walk(statement))
        ):
            return index
    return None


def _index_of_declare_call(statements: list[ast.stmt]) -> int | None:
    """The position of the statement containing the ``_declare_recovery`` call, or ``None``."""
    for index, statement in enumerate(statements):
        for node in ast.walk(statement):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "_declare_recovery"
            ):
                return index
    return None


@pytest.mark.model
@given(
    amount=_amounts(),
    currency=st.sampled_from(["INR", "inr", " usd ", "SGD"]),
    hours=st.integers(min_value=1, max_value=400),
    ordinal=st.integers(min_value=1, max_value=5),
    action=st.sampled_from([CandidateAction.PAYMENT_LINK, CandidateAction.CUSTOMER_MESSAGE]),
)
@settings(max_examples=100, deadline=None)
def test_p47_every_link_revora_creates_declines_partial_payment(
    amount: int,
    currency: str,
    hours: int,
    ordinal: int,
    action: CandidateAction,
) -> None:
    """Property 47, fourth clause — every payment link Revora created carries
    ``accept_partial = false`` (R22.C3).

    Asserted on **what the builder produces**, twice: on the request object and on the JSON body
    it serialises to. Both, because they are two places the flag has to survive and only the
    second reaches the provider — a field dropped from ``to_payload`` would leave the object
    correct and the wire silent, and the provider's own default is not something Revora may rely
    on.

    Generated across the inputs that could plausibly reach the flag: the amount, the currency in
    its unnormalised forms, an expiry that exercises the clamp both under and over the provider
    ceiling, the attempt ordinal, and the action. None of them should be able to, which is the
    property; enumerating them is how "should not be able to" is tested rather than asserted.

    ``build_payment_link_request`` is the only supported way to build one — the constructor does
    no clamping and no validation, so constructing directly is how an unvalidated request gets
    past the gates — and it passes no ``accept_partial`` at all, which is why the dataclass
    default is the answer rather than one of several places the value could come from.
    """
    request = build_payment_link_request(
        case_id="8f14e45f-ea0d-4c2b-9f7e-000000000001",
        action=action,
        attempt_ordinal=ordinal,
        amount=Minor(amount),
        currency=currency,
        description="Complete your payment",
        customer=_CONTACT,
        window_end=_NOW + timedelta(hours=hours),
        now=_NOW,
        max_message_length=_MAX_MESSAGE_LENGTH,
    )

    assert request.accept_partial is False
    payload = request.to_payload()
    assert payload["accept_partial"] is False, (
        "a created payment link would accept a partial payment. Revora has no notion of partial "
        "recovery, so a partly-paid link would sit at partially_paid holding the case while the "
        "customer believed they had settled it"
    )
    # The flag reaching the wire as a JSON boolean rather than as something truthy. `0` and `""`
    # are both falsey in Python and neither is `false` to a provider parsing JSON strictly.
    assert isinstance(payload["accept_partial"], bool)


@pytest.mark.model
def test_p47_nothing_in_the_tree_sets_partial_payment_acceptance_to_true() -> None:
    """Property 47's fourth clause from the other side: no code can set the flag.

    The generated test above shows the one supported builder produces ``false``. This shows there
    is no other producer, which is the half that survives somebody adding a second call site: a
    keyword argument or a dict entry setting ``accept_partial`` to anything other than a literal
    ``False`` fails here, wherever in ``revora/`` it appears.

    From the AST, because ``revora/providers/payment_link.py`` and ``revora/audit/events.py`` both
    discuss the flag in prose at length — one of them explains what would happen if it were true —
    and a text search would fail on the explanation.
    """
    offenders: list[str] = []
    for path in _python_files():
        for node in ast.walk(_tree_of(path)):
            if (
                isinstance(node, ast.keyword)
                and node.arg == "accept_partial"
                and not _is_literal_false(node.value)
            ):
                offenders.append(
                    f"{path.relative_to(_REPO_ROOT)}: accept_partial=<not False>"
                )
            if isinstance(node, ast.Dict):
                for key, value in zip(node.keys, node.values, strict=True):
                    if (
                        isinstance(key, ast.Constant)
                        and key.value == "accept_partial"
                        and not _is_literal_false(value)
                        and not _is_self_attribute(value, "accept_partial")
                    ):
                        offenders.append(
                            f"{path.relative_to(_REPO_ROOT)}: "
                            '"accept_partial": <not False and not self.accept_partial>'
                        )
    assert not offenders, (
        "partial-payment acceptance is set to something other than false:\n  "
        + "\n  ".join(offenders)
        + "\nR22.C3 forbids any Payment_Provider request that sets it true, on any link"
    )


def _is_literal_false(node: ast.expr) -> bool:
    return isinstance(node, ast.Constant) and node.value is False


def _is_self_attribute(node: ast.expr, name: str) -> bool:
    """``self.<name>``, which is how ``to_payload`` forwards the dataclass default."""
    return (
        isinstance(node, ast.Attribute)
        and node.attr == name
        and isinstance(node.value, ast.Name)
        and node.value.id == "self"
    )


# ===========================================================================
# Property 48 — the full amount, once, and a later capture still reconciles
# ===========================================================================


@pytest.mark.model
def test_p48_the_arrangement_reason_lands_in_exactly_one_unresolved_group() -> None:
    """Feature: Customer Response Loop. Property 48, first clause — the full ``payment_amount``
    appears exactly once in ``unresolved_revenue`` (R22.C5).

    "Exactly once" is a claim about the grouping's shape, and it decomposes into three facts that
    are all readable from declarations:

    1. The terminal reason terminates the case ``ESCALATED``. That is what the handler applies,
       and ``ESCALATED`` is in :data:`TERMINAL_STATES`.
    2. ``ESCALATED`` appears in :data:`UNRESOLVED_STATES` **once**. The grouping is a ``GROUP BY``
       over ``state`` merged onto that tuple, so a state listed twice would emit the same money in
       two rows and the footer would stop being their sum.
    3. There is no group keyed on the terminal *reason*. This is the assertion that fails on the
       tempting implementation of R22.C9 — a sixth group for arrangement requests — which would
       split the escalated money across two rows. R22.C9 is satisfied by a breakdown *inside* the
       escalated row, which is what ``revora/api/views.py`` builds and what the ``Unresolved``
       route renders.

    The ``unresolved_revenue`` sum itself is one ``SUM(payment_amount) FILTER (state != RECOVERED)``
    over the case rows of a cohort, and a case is one row — so a case contributes its amount once
    by virtue of being a row, not by virtue of anything this feature does. Which is the point: the
    arrangement path adds no second sum and no second table to be summed.
    """
    assert CaseState.ESCALATED in TERMINAL_STATES
    assert UNRESOLVED_STATES.count(CaseState.ESCALATED) == 1, (
        f"ESCALATED appears {UNRESOLVED_STATES.count(CaseState.ESCALATED)} times in "
        "UNRESOLVED_STATES. Every occurrence emits the same money as another row"
    )
    assert CaseState.RECOVERED not in UNRESOLVED_STATES, (
        "RECOVERED entered the unresolved grouping, so recovered money is being reported as "
        "unresolved"
    )
    # No group is keyed on a terminal reason. The grouping is by state and only by state.
    reason_values = {reason.value for reason in TerminalReason}
    assert not reason_values & {state.value for state in UNRESOLVED_STATES}, (
        "a Terminal_State and a Terminal_Reason share a spelling, so a reader cannot tell which "
        "of the two a grouping row is keyed on"
    )


@pytest.mark.model
def test_p48_nothing_on_the_arrangement_path_writes_a_money_field() -> None:
    """Property 48, second clause — the ``payment_amount`` and the currency are unchanged
    (R22.C7), and so is ``window_end_at``.

    Established structurally, at two levels, because "unchanged" is a claim about the absence of a
    write and no amount of driving proves an absence.

    **The path itself writes none of the three.** Every module an arrangement request travels
    through is walked for attribute assignments, and the three names must not appear among the
    targets. The AST is read rather than the text because these modules argue about the three
    fields at length — ``arrangements.py`` names all of them in prose to say it does not touch them
    — so a substring search would fail on the argument.

    **And the one writer of case state writes none of the three either.**
    ``apply_locked_transition`` is the only writer of ``recovery_case.state``, and the set of case
    attributes it assigns is the complete set a transition can move: the state, the version, the
    terminal reason, the three counters, ``last_outbound_at`` and ``next_review_at``. So R22.C7 is
    not a discipline the
    arrangement handler keeps — it is a property of the mechanism every terminal transition in the
    system goes through, which is why the handler contains no assertion restating it.
    """
    on_the_path = (
        _REVORA / "customer" / "arrangements.py",
        _REVORA / "customer" / "signals.py",
        _REVORA / "cases" / "manager.py",
    )
    for path in on_the_path:
        written = _assigned_attributes(_tree_of(path))
        offending = sorted(name for name in _MONEY_FIELDS if name in written)
        assert not offending, (
            f"{path.name} assigns {offending}. R22.C7 leaves the payment amount, the currency "
            "and the recovery window end untouched when a Partial_Arrangement_Request is "
            "persisted, and the mechanism is that no writer of them exists on this path"
        )

    handler_written = _assigned_attributes(
        _function_tree(pipeline_module.handle_partial_arrangement)
    )
    offending = sorted(name for name in _MONEY_FIELDS if name in handler_written)
    assert not offending, (
        f"handle_partial_arrangement assigns {offending}, so escalating an arrangement request "
        "moves money"
    )


@pytest.mark.model
def test_p48_a_later_full_capture_still_reconciles_the_case_and_counts_once() -> None:
    """Property 48, third clause — a subsequent full capture reconciles the Recovery_Case to
    ``RECOVERED``, counting the recovered amount exactly once (R22.C8, R10.C14).

    This is the clause that makes R22.C8 worth having. Leaving the payment link live is only
    useful if paying through it still resolves the case, and the case is already terminal by then
    — so the reconciliation edge out of ``ESCALATED`` has to exist, has to require a verified
    capture, and has to be usable at most once.

    All three come off the transition table, which is generated from five declarations rather than
    hand-written. So this reads the same declaration the case manager enforces, instead of reading
    a copy of it.

    "Counting once" has two independent guards and both are asserted: ``at_most_once_per_case`` on
    the edge, and ``UNIQUE (case_id)`` on ``recovery_outcome``. Two, because they fail in different
    directions — the edge stops a second *transition*, the constraint stops a second *row* — and
    the money is summed from the rows.
    """
    assert is_legal(CaseState.ESCALATED, CaseState.RECOVERED), (
        "a case that ended ESCALATED can no longer be reconciled to RECOVERED, so a customer who "
        "chose to pay the full amount after an arrangement request was escalated would have their "
        "payment go uncounted"
    )
    rule = rule_for(CaseState.ESCALATED, CaseState.RECOVERED)
    assert rule is not None
    assert rule.requires_verified_capture, (
        "the reconciliation edge stopped requiring a verified capture, so a case could be moved "
        "to RECOVERED on a webhook"
    )
    assert rule.at_most_once_per_case, (
        "the reconciliation edge may now be taken more than once per case, which is a second "
        "count of the same money"
    )
    # And the terminal reason this feature applies really does leave the case in that state.
    assert TerminalReason.CUSTOMER_REQUESTED_PARTIAL_ARRANGEMENT.value in {
        reason.value for reason in TerminalReason
    }


@pytest.mark.model
def test_p48_the_request_carries_no_amount_at_any_layer() -> None:
    """R22.C1 restated where P48 depends on it: there is no proposed amount to leak into a figure.

    P48 says the *full* amount is what appears in ``unresolved_revenue``, and the only way a
    smaller number could appear instead is if one existed anywhere. It does not, at three layers:
    the request model declares no such field, the record shape the worker reads declares none, and
    the observation feature contains none.

    Asserted over ``model_fields`` and ``__slots__`` rather than by attempting an assignment,
    because the interesting claim is that the names are *absent* — a rejected assignment would
    also be produced by a frozen model that had the field.
    """
    forbidden = ("amount", "instalment_count", "instalments", "schedule")

    declared = set(PartialArrangementSubmission.model_fields)
    assert declared == {"note"}, (
        f"PartialArrangementSubmission declares {sorted(declared)}. R22.C1's 422 is the schema's "
        "default behaviour and only while the schema declares nothing else"
    )
    for name in forbidden:
        assert name not in declared

    record_fields = set(ArrangementRequest.__slots__)
    for name in forbidden:
        assert name not in record_fields, (
            f"ArrangementRequest carries {name!r}. Nothing persists one, so the field could only "
            "ever be filled with a value the reader invented"
        )

    feature = arrangement_feature(
        ArrangementRequest(
            signal_id=_SOME_CASE_ID,
            requested_at=_NOW,
            note="can I split this",
            note_truncated=False,
            note_redacted_at=None,
        )
    )
    body = feature[FEATURE_PARTIAL_ARRANGEMENT]
    assert isinstance(body, dict)
    for name in forbidden:
        assert name not in body


@pytest.mark.model
def test_p48_the_observation_feature_cannot_resegment_the_training_set() -> None:
    """R22.C6 without a side effect on any estimate.

    The observation feature R22.C6 requires lives in the same JSONB document the estimator probes
    by containment, so the question "does adding it move a baseline" has to be answered rather than
    assumed. It does not, for two reasons and both are asserted:

    1. The key is not one of :data:`FEATURE_KEYS`. The backoff levels are truncations of that one
       ordered tuple, so a sixth *segment* feature would extend every level at once and resegment
       every observation.
    2. Its value is a nested object. A containment probe is built from string values, and
       ``features @> '{"risk_cause": "..."}'`` cannot match a nested document — so the key cannot
       become a segment dimension by accident even if somebody later added its name to a probe.

    Also asserted: the note's **text** is absent. R29.C10 requires a Delay_Reason_Note past the
    retention bound to be deleted while the non-identifying Customer_Signal fields are retained, and
    a verbatim copy in ``memory_observation.features`` would be a copy the retention sweep does not
    scan and cannot redact. The feature carries the signal identifier instead, so the text is
    resolved through the row the sweep does reach — and disappears from here when it is redacted
    there.
    """
    note = "please can I pay 2000 now and the rest next month"
    feature = arrangement_feature(
        ArrangementRequest(
            signal_id=_SOME_CASE_ID,
            requested_at=_NOW,
            note=note,
            note_truncated=True,
            note_redacted_at=None,
        )
    )

    assert set(feature) == {FEATURE_PARTIAL_ARRANGEMENT}
    assert FEATURE_PARTIAL_ARRANGEMENT not in FEATURE_KEYS, (
        f"{FEATURE_PARTIAL_ARRANGEMENT!r} became a segment feature. LEVEL_FEATURES truncates "
        "FEATURE_KEYS, so every backoff level would gain it at once and every historical "
        "observation would stop matching its own segment"
    )
    body = feature[FEATURE_PARTIAL_ARRANGEMENT]
    assert isinstance(body, dict), (
        "the arrangement feature became a flat string value, so a containment probe naming this "
        "key would match it and the key would be a segment dimension in all but name"
    )

    flattened = repr(body)
    assert note not in flattened, (
        "the note's text is copied into the observation. R29.C10 requires it deletable, and this "
        "copy is in a table the retention sweep does not scan"
    )
    assert body["signal_id"] == str(_SOME_CASE_ID), (
        "the feature no longer names the signal, so the note it refers to cannot be resolved and "
        "the reference has become a dead end"
    )
    assert body["note_present"] is True
    assert body["note_length"] == len(note)
    assert body["note_truncated"] is True


@pytest.mark.model
def test_p48_an_absent_request_adds_no_key_to_the_observation() -> None:
    """The observation of a case with no arrangement request is byte-identical to before.

    Which is what keeps the pg test asserting the exact five-key feature set on an expired case
    honest, and it is the reason the writer returns an empty mapping rather than the key with a
    ``None`` value: a present-but-null key is a key a containment probe can match on.
    """
    from revora.memory import store as store_module

    source = inspect.getsource(store_module._arrangement_features)
    assert "return {}" in source, (
        "the arrangement feature writer no longer has an empty-mapping path, so every observation "
        "in the system now carries the key"
    )


@pytest.mark.model
def test_the_arrangement_job_kind_is_its_own_and_not_a_sweep() -> None:
    """One kind, event-driven, deduped per case.

    Asserted because the dedupe key is what makes "two requests queue one escalation" true, and
    because a kind that leaked into ``PERIODIC_SWEEP_KINDS`` would be a sweep the ticker prices
    from a configured interval that does not exist — which stops the ticker rather than the
    feature, and would be found somewhere unrelated.
    """
    from revora.jobs.scheduler import PERIODIC_SWEEP_KINDS
    from revora.jobs.worker import build_registry

    assert PARTIAL_ARRANGEMENT_KIND not in PERIODIC_SWEEP_KINDS
    assert PARTIAL_ARRANGEMENT_KIND in build_registry(provider=None), (
        "the arrangement job kind has no handler, so every escalation would dead-letter and every "
        "case whose customer asked for a person would stay in the pipeline"
    )
