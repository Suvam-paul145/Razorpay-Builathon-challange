"""Task 49.5, Properties 49 to 55. A model may explain and may hypothesise. It may do nothing else.

Requirement 27 adds one component and takes nothing away. The seven properties here are the
seven separate ways that could stop being true, and each one is stated over the *whole* space of
provider answers rather than over a happy path: ``tests.strategies.reasoning.reasoning_responses``
produces valid, schema-invalid, absent, timed-out and adversarial outcomes, and the real adapter
turns each of them into a real result over ``httpx.MockTransport`` with all four gates running.

**Every property drives production code from a real provider outcome, never from a constructed
result.** That is the single most important decision in this file. Building an ``Accepted``
directly would let a property assert a mapping over values the adapter cannot produce — a schema
rejection carrying a usable cause, an accepted description that never passed
``validate_description`` — and the property would then be a statement about the test's
imagination. So the unit generated is what the *wire* does, and the classification into one of
the five result variants is the adapter's, which is the component under test.

What each property prevents, and how it fails if the guarantee breaks:

* **P49 — a model answer moves no policy outcome.** The reference is the ``None`` case: a
  decision cycle that asked nothing. Every response other than an accepted, above-floor cause
  must leave the twelve check outcomes byte-identical to it, and an accepted one must move
  *exactly one* ``PolicyInput`` field — the recorded cause, which is the diagnosis layer's output
  after the ceiling and the floor substitution, not the model's answer. It fails the day
  ``_proposal_from`` lets a timeout carry a cause, the day the floor substitution is dropped, or
  the day a second field of ``PolicyInput`` starts carrying anything the adapter produced.
* **P50 — an explanation moves no figure.** The selection is integer arithmetic over a candidate
  set, and prose has no field to arrive in. It fails if ``select`` gains a parameter, if a
  candidate type gains a mutable attribute, or if ``_explain_decision`` ever records
  ``influenced_recommendation`` as anything but a literal ``False``.
* **P51 — the absent credential and the rejected response are the same system.** Both hand the
  pure components ``None``, and nothing waits either way. It fails if the credential-absent path
  ever issues a request, waits, or introduces a state.
* **P52 — one row per invocation, and a bound counted from rows.** It fails on an off-by-one in
  the bound check, on a result variant whose row cannot be built, and on a result that produces
  neither a row nor a named Audit_Record.
* **P53 — the transmitted key set is a subset of the declared set.** Driven with adversarial
  values, including ones that carry JSON separators, because the interesting question is whether
  a *value* can add a *key*. It fails if the payload builder ever iterates the caller's keys
  instead of the contract's.
* **P54 — the confidence ceiling.** ``1.000`` is reachable only through ``DETERMINISTIC``, which
  is what lets a reader of the ``diagnosis`` table read the method off the confidence. It fails
  if the cap moves, if it is folded into the output schema as a range bound, or if a rejected
  answer starts carrying its returned confidence.
* **P55 — a refused draft is a substitution, never a non-execution.** The reviewed template goes
  out, the draft is retained, and the customer-message counter cannot see either. It fails if a
  content rule stops firing, or if the counter's single writer ever learns where a description
  came from.

**Tiering follows the design's table and is per test.** P53 and P54 are ``pure``: they are claims
about ``build_request_payload`` and about two ``Decimal`` functions, and a database would make
them slower without making them stronger. The other five are ``model``: they drive the adapter,
the policy engine, the optimizer and an in-memory fake of the invocation repository.

**None of them is ``pg``, and that is a real limitation worth naming rather than hiding.** P51's
"cases created, decisions, executions, recovered amounts and projected timelines are identical"
and P52's "exactly one ``ai_invocation`` row" are, in their fullest form, claims about rows. What
is asserted here is the half that determines them: that the *value handed to every pure
component* is identical, and that exactly one complete row is *derivable* from every variant. A
component handed an identical argument computes an identical answer — that is what "pure" means,
and ``test_policy.py``'s R8.C14 property already pins it — and a row that could not be built is a
row the timeout never got. The row-level confirmation belongs beside the other ``pg`` reasoning
tests in ``tests/persistence/test_reasoning_wiring.py``.
"""

from __future__ import annotations

import ast
import contextlib
import dataclasses
import inspect
import json
import uuid
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from functools import partial
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from sqlalchemy.orm import Session

from revora.cases import manager as manager_module
from revora.diagnosis.service import (
    AiCauseProposal,
    RecordedDiagnosis,
    capped_ai_confidence,
    resolve_ai_diagnosis,
    resolve_recorded_diagnosis,
)
from revora.domain.actions import CandidateAction
from revora.domain.enums import CaseState, DiagnosisMethod, ReasoningCallKind, RiskCause
from revora.domain.money import Minor
from revora.domain.probability import AI_CONFIDENCE_CEILING, Probability
from revora.execution.messages import description_for, template_for_action
from revora.jobs import pipeline
from revora.optimizer.arithmetic import CandidateInput
from revora.optimizer.selection import SelectionResult, Thresholds, select
from revora.platform.config import Configuration, default_configuration
from revora.platform.secrets import EnvironmentSecretResolver, SecretStore, set_secret_store
from revora.policy.engine import evaluate
from revora.policy.input import PolicyInput
from revora.policy.rules import default_rule_set
from revora.providers.payment_link import validate_description
from revora.reasoning.adapter import (
    CONTENT_REJECTED,
    DEFAULT_MODEL,
    Accepted,
    ContentRule,
    PromptContractViolationError,
    ReasoningAdapter,
    ReasoningResult,
    ReasoningVerdict,
    RejectedContent,
    RejectedSchema,
    TimedOut,
    Unavailable,
    UnavailableReason,
    _wire_body,
    audit_event_type_for,
    build_request_payload,
    extract_transmitted_payload,
    verdict_of,
)
from revora.reasoning.contracts import CONTRACTS, FORBIDDEN_NAME_FRAGMENTS, contract_for
from revora.reasoning.schemas import CauseHypothesisOutput
from tests.strategies.candidates import candidate_estimate_set
from tests.strategies.policy import policy_input
from tests.strategies.reasoning import (
    ReasoningResponse,
    ResponseShape,
    adversarial_case_values,
    ai_confidences,
    forbidden_field_names,
    reasoning_responses,
)

# ---------------------------------------------------------------------------
# Fixed bounds, restated rather than shared
# ---------------------------------------------------------------------------

_CONFIG = default_configuration()
_FLOOR = _CONFIG.DIAGNOSIS_CONFIDENCE_FLOOR
_MERCHANT_ID = uuid.UUID("00000000-0000-4000-8000-000000000001")
_CASE_ID = uuid.UUID("00000000-0000-4000-8000-000000000002")
_FAKE_CREDENTIAL = "fake-llm-credential"
_MERCHANT_NAME = "Acme Retail"
_RESPONSE_PAGE_URL = "https://pay.revora.test/r/abc123"

_EPOCH = datetime(2025, 6, 1, 12, 0, 0, tzinfo=UTC)
"""One fixed evaluation instant. The policy engine takes it as an argument rather than reading a
clock, which is what makes "identical inputs give an identical decision" literally true — and it
is the same instant ``tests/strategies/policy.py`` builds its draws around, so a constructed
input and a generated one are comparable."""

_RULES = default_rule_set(
    max_recovery_attempts=3,
    max_customer_messages=2,
    cooldown_interval=_CONFIG.COOLDOWN_INTERVAL,
    policy_decision_validity=_CONFIG.POLICY_DECISION_VALIDITY,
    risk_reason_codes=frozenset({"payment_risk_check_failed", "compliance_violation"}),
    min_net_value_threshold=Minor(5_000),
    min_incremental_probability=Decimal("0.05"),
)

_THRESHOLDS = Thresholds(
    min_net_value=Minor(5_000),
    min_incremental_probability=Decimal("0.05"),
    max_cost_to_value_ratio=Decimal("0.30"),
    high_baseline=Decimal("0.80"),
)
"""The optimizer's four bounds, the same values ``tests/properties/test_optimizer.py`` uses.

Restated rather than imported. A shared constant between two property files makes one file's
failure message describe the other file's subject, which is the argument
``test_customer_signal_authority.py`` already makes about ``_ASSIGNMENT_REFUSED``."""

_ASSIGNMENT_REFUSED = (dataclasses.FrozenInstanceError, AttributeError, TypeError)
"""What a refused attribute assignment on a frozen, slotted dataclass may raise.

Three types because the mechanism differs by case and by CPython version:
``FrozenInstanceError`` for a declared field, ``TypeError`` or ``AttributeError`` for a name the
slots layout has no room for. The property is that the assignment cannot succeed; pinning the
type would make the test brittle about something it does not care about."""

_REVORA_ROOT = Path(inspect.getfile(pipeline)).resolve().parents[1]

_EXPECTED_CASE_STATES: frozenset[str] = frozenset(
    {
        "NEW",
        "DETECTED",
        "DIAGNOSED",
        "DECISION_PENDING",
        "POLICY_CHECK",
        "ACTION_SCHEDULED",
        "EXECUTING",
        "WAITING_FOR_OUTCOME",
        "RECOVERED",
        "STOPPED",
        "BLOCKED",
        "EXPIRED",
        "ESCALATED",
        "FAILED",
    }
)
"""The fourteen states, pinned by name so a fifteenth has to be noticed here.

Pinned rather than derived, which is the whole point: R27.C7 says no Recovery_Case may be held in
a state that waits on a reasoning response, and the strongest form of that claim is that the state
machine gained no member at all. A derived set would agree with itself forever."""

_MODEL_WORDS: frozenset[str] = frozenset(
    {"AI", "MODEL", "REASONING", "REASON", "EXPLANATION", "EXPLAIN", "LLM", "DRAFT", "PROMPT"}
)
"""Words that would name a model-waiting state, matched as whole words rather than substrings.

Whole words because the first version of this check was a substring search and ``FAILED`` contains
``AI``. That is not a near miss worth tolerating — a substring rule that fires on an unrelated
state is a rule nobody trusts, and the next person's fix is to delete it. State names are
``UPPER_SNAKE_CASE``, so splitting on the underscore is the exact granularity the claim needs."""


# ---------------------------------------------------------------------------
# Driving the real adapter from a generated provider outcome
# ---------------------------------------------------------------------------


def _config(**overrides: str) -> Configuration:
    """The placeholder configuration with named bounds overridden.

    Built from ``as_raw`` and reparsed rather than mutated, so an override goes through the same
    parser a stored row would and a bound expressed wrongly fails here rather than surviving as a
    value no deployment could hold.
    """
    raw = dict(default_configuration().as_raw())
    raw.update(overrides)
    return Configuration.from_values(raw, version="test", strict=True)


_FAST_CONFIG = _config(REASONING_TIMEOUT="0.02", REASONING_RETRY_COUNT="1")
"""A twenty-millisecond timeout, so a generated timeout costs two attempts rather than twenty
seconds. The bound under test in these properties is never the wait itself — that is
``tests/test_reasoning_adapter.py``'s subject — so shortening it changes what the properties cost
and not what they assert."""


@contextlib.contextmanager
def _credential(*, present: bool) -> Iterator[None]:
    """Install or withhold the reasoning credential for the duration of a block.

    The store is a module global, so leaving a fake installed would leak into every test after
    this one. Resolver-backed rather than environment-backed, which is also what keeps these
    properties passing in the deployed reality where ``REVORA_LLM_CREDENTIAL`` is unset.
    """
    resolver = EnvironmentSecretResolver(
        {"REVORA_LLM_CREDENTIAL": _FAKE_CREDENTIAL} if present else {}
    )
    previous = set_secret_store(SecretStore(resolver))
    try:
        yield
    finally:
        set_secret_store(previous)


@dataclasses.dataclass(frozen=True, slots=True)
class _Invocation:
    """One invocation, driven end to end: what came back, and what was sent to get it."""

    result: ReasoningResult[Any]
    requests: tuple[httpx.Request, ...]


def _invoke(
    response: ReasoningResponse,
    *,
    config: Configuration | None = None,
    payment_amount_formatted: str = "INR 2,500.00",
    approved_link: str = pipeline.NO_APPROVED_LINK,
) -> _Invocation:
    """Run one generated provider outcome through the real adapter. No socket, no database.

    The adapter is constructed with an ``httpx.MockTransport`` because the adapter *takes* a
    transport — a monkeypatch would leave the production path untested and this one approximately
    tested. The credential is installed or withheld around the call rather than for the module,
    because the absent-credential shape is one of the five and has to be reachable from inside a
    generated sequence rather than only from a separate test.
    """
    seen: list[httpx.Request] = []
    settings_ = config if config is not None else _FAST_CONFIG
    with (
        _credential(present=response.credential_present),
        ReasoningAdapter(
            transport=httpx.MockTransport(response.handler(seen)), config=settings_
        ) as adapter,
    ):
        result = _call(
            adapter,
            response,
            config=settings_,
            payment_amount_formatted=payment_amount_formatted,
            approved_link=approved_link,
        )
    return _Invocation(result=result, requests=tuple(seen))


def _call(
    adapter: ReasoningAdapter,
    response: ReasoningResponse,
    *,
    config: Configuration,
    payment_amount_formatted: str,
    approved_link: str,
) -> ReasoningResult[Any]:
    """The typed wrapper for one call kind, with the arguments a real call site passes.

    The ``LINK_DESCRIPTION`` branch passes ``validate_description`` with ``MAX_MESSAGE_LENGTH``
    already bound, which is exactly what ``pipeline._draft_link_description`` does — not an
    approximation of it. The adapter *requires* the validator, which is what makes "sent without
    the length and control-character rules" unreachable rather than discouraged, and a test that
    supplied a permissive stand-in would be exercising a gate that does not exist.
    """
    match response.call_kind:
        case ReasoningCallKind.CAUSE_HYPOTHESIS:
            return adapter.propose_cause(
                provider_error_code="BAD_REQUEST_ERROR",
                provider_error_reason="payment_failed",
                delay_reason_note="the customer said they would pay on Friday",
                case_id=str(_CASE_ID),
            )
        case ReasoningCallKind.DECISION_EXPLANATION:
            return adapter.explain_decision(
                risk_cause=RiskCause.ABANDONMENT,
                baseline_probability=Decimal("0.1200"),
                selected_action=CandidateAction.PAYMENT_LINK,
                selected_net_recovery_value=123_400,
                selection_reason="HIGHEST_NET_VALUE",
                currency="INR",
                explanation_max_length=pipeline.REASONING_EXPLANATION_MAX_LENGTH,
                runner_up_action=CandidateAction.CUSTOMER_MESSAGE,
                runner_up_net_recovery_value=98_700,
                case_id=str(_CASE_ID),
            )
        case _:
            return adapter.draft_link_description(
                merchant_display_name=_MERCHANT_NAME,
                payment_amount_formatted=payment_amount_formatted,
                currency="INR",
                risk_cause=RiskCause.ABANDONMENT,
                approved_link=approved_link,
                length_validator=partial(
                    validate_description, max_length=config.MAX_MESSAGE_LENGTH
                ),
                case_id=str(_CASE_ID),
            )


def _recorded_from(result: ReasoningResult[Any]) -> RecordedDiagnosis:
    """The diagnosis the pipeline would record for ``result``, through production code only.

    Two production calls and no third: ``_proposal_from`` maps the five result variants onto the
    three permitted methods, and ``resolve_ai_diagnosis`` applies R27.C4's ceiling and R3.C8's
    substitution. Both are the functions ``handle_diagnosis`` uses, so what this returns is what
    would be written rather than a reconstruction of it.
    """
    proposal = pipeline._proposal_from(cast("ReasoningResult[CauseHypothesisOutput]", result))
    return resolve_ai_diagnosis(proposal, confidence_floor=_FLOOR)


def _reference_policy_input() -> PolicyInput:
    """One evaluable ``PolicyInput``, for the structural half of P49.

    Constructed rather than drawn because the assertions it carries are about the *type* — that it
    has no ``__dict__`` and refuses every name a reasoning result carries — and a generated value
    would explore a space those two claims do not depend on.
    """
    return PolicyInput(
        case_id=_CASE_ID,
        merchant_id=_MERCHANT_ID,
        decision_cycle=1,
        selected_action=CandidateAction.PAYMENT_LINK,
        case_state=CaseState.POLICY_CHECK,
        case_version=1,
        payment_amount=Minor(249_900),
        customer_key="ck-p49",
        verified_payment_captured=False,
        verified_payment_status="failed",
        customer_opted_out=False,
        contact_suppressed=False,
        consent_expires_at=_EPOCH + _CONFIG.RECOVERY_WINDOW_DURATION,
        consent_recorded=True,
        risk_flagged=False,
        diagnosed_cause=RiskCause.UNKNOWN,
        human_owner_user_id=None,
        window_end_at=_EPOCH + _CONFIG.RECOVERY_WINDOW_DURATION,
        executed_action_count=0,
        customer_message_count=0,
        last_outbound_at=None,
        open_intent_exists=False,
        intent_exists_for_key=False,
        evaluated_at=_EPOCH,
        rules_version="v1-assumption-baseline",
    )


# ---------------------------------------------------------------------------
# Fingerprints: everything that must not move
# ---------------------------------------------------------------------------


def _policy_fingerprint(evaluation: Any) -> tuple[object, ...]:
    """Verdict, primary reason, and all twelve ordered check outcomes."""
    return (
        evaluation.verdict,
        evaluation.primary_reason,
        tuple((check.order, check.check, check.outcome) for check in evaluation.checks),
    )


def _selection_fingerprint(result: SelectionResult) -> tuple[object, ...]:
    """The selected action and every per-candidate figure R7.C8 requires be reported.

    Every numeric field, not a summary of them. P50 says "every reported per-candidate figure",
    and a fingerprint over the winner alone would pass an implementation that let prose move a
    *rejected* alternative's net value — which is a figure the dashboard shows and a merchant
    reads.
    """
    return (
        result.selected.action,
        result.selection_reason,
        result.divergence_reason,
        result.qualifying_actions,
        tuple(
            (
                item.action,
                item.availability,
                item.unavailable_reason,
                item.intervention_probability.value,
                item.incremental_probability.value,
                int(item.expected_incremental_revenue),
                int(item.financial_cost),
                int(item.communication_cost),
                int(item.risk_cost),
                int(item.customer_cost),
                int(item.total_cost),
                int(item.net_recovery_value),
                item.cost_ratio,
                item.excluded,
                item.exclusion_reason,
                item.rank,
            )
            for item in result.candidates
        ),
    )


def _result_value_names(result: ReasoningResult[Any]) -> frozenset[str]:
    """Every attribute name a reasoning result exposes, including its parsed output's.

    The set of names a value produced by the reasoning layer could arrive under. Descending into
    ``output`` matters: the adapter's own field names are transport vocabulary — ``latency_ms``,
    ``http_status`` — and the names that would actually be tempting to a caller are the parsed
    ones, ``cause`` and ``confidence`` and ``explanation`` and ``description``.
    """
    names = {field.name for field in dataclasses.fields(result)}
    output = getattr(result, "output", None)
    if output is not None:
        names |= set(type(output).model_fields)
    return frozenset(names)


# ---------------------------------------------------------------------------
# Reading the source, for the claims a behavioural test cannot make
# ---------------------------------------------------------------------------


def _parsed(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _function(module: object, name: str) -> ast.FunctionDef:
    """One top-level function's AST node, by name."""
    tree = _parsed(Path(inspect.getfile(cast("Any", module))))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"no function named {name!r}; this test has stopped finding its subject")


def _referenced_names(node: ast.AST) -> set[str]:
    """Every identifier the *code* under ``node`` refers to, ignoring prose.

    From the AST, and that is not a stylistic preference. These modules are heavily documented and
    several of them explain in prose exactly which value they must not read — ``_draft_link_
    description``'s docstring promises "nothing here touches the customer-message counter" — so a
    substring search over the source fails on the promise rather than on a reference. The same
    approach, and the same argument, as ``test_customer_signal_authority.py``'s.
    """
    names: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Name):
            names.add(child.id)
        elif isinstance(child, ast.Attribute):
            names.add(child.attr)
        elif isinstance(child, ast.arg | ast.keyword) and child.arg is not None:
            names.add(child.arg)
        elif isinstance(child, ast.Constant) and isinstance(child.value, str):
            # A string constant counts as a reference: reaching a value by name through
            # ``getattr`` would otherwise slip past an AST walk entirely.
            names.add(child.value)
    return names


def _assigned_attributes(tree: ast.Module) -> set[str]:
    """Attribute names this module assigns to, augmented assignment included."""
    assigned: set[str] = set()
    for node in ast.walk(tree):
        targets: Sequence[ast.expr] = ()
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AugAssign):
            targets = (node.target,)
        for target in targets:
            if isinstance(target, ast.Attribute):
                assigned.add(target.attr)
    return assigned


def _imported_modules(tree: ast.Module) -> set[str]:
    """Every module name imported by one file, from the AST."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            names.add(node.module)
    return names


# ===========================================================================
# Property 49 — no reasoning response moves a policy outcome
# ===========================================================================


@pytest.mark.model
@settings(max_examples=150)
@given(
    candidate=policy_input(),
    response=reasoning_responses(call_kind=ReasoningCallKind.CAUSE_HYPOTHESIS),
)
def test_p49_only_a_trusted_cause_moves_anything_and_it_moves_exactly_one_field(
    candidate: PolicyInput, response: ReasoningResponse
) -> None:
    """Feature: revora-customer-response-loop, Property 49: for any reasoning response — valid,
    schema-invalid, absent, timed out or adversarial — the Policy_Decision's verdict, primary
    reason and all twelve ordered check outcomes are identical to the ``None`` case, except that a
    response accepted at or above ``DIAGNOSIS_CONFIDENCE_FLOOR`` may move the single field
    ``PolicyInput.diagnosed_cause`` and nothing else.

    **The exception is the requirement rather than a weakening of it, and it is worth being exact
    about.** R27.C11 excludes "every field the Reasoning_Adapter produced". The recorded cause is
    not one of those: it is the Diagnosis_Engine's output after R27.C4's ceiling and R3.C8's
    substitution have run, and it is the same column the deterministic taxonomy writes. Adding
    cause coverage where the provider's error field resolved nothing is the entire benefit R27's
    user story asks for — "adding AI adds explanation and cause coverage" — so a property
    asserting the cause could never move would be asserting the feature does nothing.

    What must never move is everything else, and that is what makes this falsifiable rather than
    tautological. The returned confidence, the evidence summary, the refused raw body, the
    latency, the model version: none of them reaches a field, and the assertion is a *set
    difference* over the two inputs rather than a spot check — so a field added to ``PolicyInput``
    next year is covered by this test without anybody remembering to extend it.

    The reference is ``RiskCause.UNKNOWN``, and that is the honest reference rather than a
    convenient one. R27.C16 means a ``CAUSE_HYPOTHESIS`` invocation happens only where neither
    mapping table produced a ``DETERMINISTIC`` diagnosis, so the recorded cause on the
    ``ai_proposal=None`` path — for exactly the cases that reach a model at all — is ``UNKNOWN``.

    The failures this catches, each of which has happened somewhere in some system:

    * a timeout or a transport failure mapped onto the model's *last* answer rather than onto
      ``FALLBACK_UNKNOWN`` — the shape of a cache that outlived its evidence;
    * a schema rejection recorded with the cause it named before validation failed;
    * the confidence floor applied to the deterministic path but not to the AI one;
    * ``PolicyInput`` gaining an ``ai_confidence`` field because a check "needed more signal".
    """
    result = _invoke(response).result
    recorded = _recorded_from(result)

    reference = replace(candidate, diagnosed_cause=RiskCause.UNKNOWN)
    actual = replace(candidate, diagnosed_cause=recorded.cause)

    moved = {
        field.name
        for field in dataclasses.fields(PolicyInput)
        if getattr(reference, field.name) != getattr(actual, field.name)
    }
    assert moved <= {"diagnosed_cause"}, (
        f"the reasoning response moved {sorted(moved)} on the policy input. Only the recorded "
        "cause may differ between an asked and an unasked decision cycle; every other field is "
        f"deterministic input R27.C11 excludes the adapter from. The draw was "
        f"{response.shape.value} ({response.detail})"
    )

    trusted = (
        recorded.method is DiagnosisMethod.AI_ASSISTED and not recorded.substituted_to_unknown
    )
    if trusted:
        # Only an accepted response may produce a trusted cause, and the cause must be the one it
        # named. Without these two the property would be satisfied by an implementation that let a
        # timeout carry the model's *previous* answer — the trusted branch would simply be taken,
        # and every assertion above it is about the fields that did not move.
        assert isinstance(result, Accepted), (
            f"a {type(result).__name__} from a {response.shape.value} draw ({response.detail}) "
            "produced a trusted cause. Only a response that passed the declared output schema may "
            "name one; a timeout or a rejected body must record UNKNOWN"
        )
        assert recorded.cause is result.output.cause
        assert recorded.confidence >= _FLOOR
        assert recorded.confidence <= AI_CONFIDENCE_CEILING.value
        return

    assert recorded.cause is RiskCause.UNKNOWN, (
        f"a {response.shape.value} response ({response.detail}) produced the recorded cause "
        f"{recorded.cause.value} with method {recorded.method.value}. Only an accepted answer at "
        "or above DIAGNOSIS_CONFIDENCE_FLOOR may name a cause; everything else takes the "
        "deterministic fallback, which records UNKNOWN"
    )
    assert _policy_fingerprint(evaluate(actual, _RULES)) == _policy_fingerprint(
        evaluate(reference, _RULES)
    ), (
        "the twelve check outcomes moved for a response that produced no trusted cause, so "
        "something other than the recorded cause reached the evaluation"
    )


@pytest.mark.model
@settings(max_examples=100)
@given(response=reasoning_responses())
def test_p49_a_reasoning_result_names_no_field_the_policy_engine_reads(
    response: ReasoningResponse,
) -> None:
    """Property 49's structural half: there is nowhere for a model's answer to sit.

    Stronger than the behavioural half and complementary to it. A generated sequence shows that
    the responses drawn moved nothing; this shows that none *can*, because the names a result
    carries and the names ``PolicyInput`` declares are disjoint sets and the type admits no
    attribute outside its declared list.

    The near miss is the point. ``CauseHypothesisOutput`` declares ``cause`` and ``PolicyInput``
    declares ``diagnosed_cause`` — two different names for two genuinely different things, the
    model's answer and the recorded one. The day somebody unifies them by renaming is the day the
    recorded cause stops being the diagnosis layer's output, and this test fails on that rename,
    which is a rename a reviewer might otherwise wave through as tidying.
    """
    result = _invoke(response).result
    declared = {field.name for field in dataclasses.fields(PolicyInput)}
    carried = _result_value_names(result)

    assert carried.isdisjoint(declared), (
        f"a {type(result).__name__} carries {sorted(carried & declared)}, which PolicyInput also "
        "declares. Sharing a name is how a value the adapter produced starts arriving as "
        "deterministic input"
    )

    candidate = _reference_policy_input()
    assert not hasattr(candidate, "__dict__"), (
        "PolicyInput has gained a __dict__, so an arbitrary attribute can now be attached to an "
        "evaluation's input at runtime"
    )
    for name in sorted(carried):
        with pytest.raises(_ASSIGNMENT_REFUSED):
            setattr(candidate, name, "APPROVED")


@pytest.mark.model
def test_p49_the_policy_engine_takes_no_reasoning_argument() -> None:
    """Property 49 at the signature: ``evaluate`` has two parameters and neither is a result.

    The cheapest and least escapable form of the claim. A parameter is the one route into a pure
    function that no amount of frozen-dataclass discipline closes, and it is also the route
    somebody would take in good faith — "the engine needs the confidence to decide" is a sentence
    that sounds reasonable right up until a model can move an authorization.
    """
    parameters = list(inspect.signature(evaluate).parameters)
    assert parameters == ["candidate", "rules"], (
        f"evaluate now takes {parameters}. R27.C11 requires every Policy_Decision be derived "
        "exclusively from the declared deterministic input set, and a third parameter is where "
        "that stops being true"
    )


# ===========================================================================
# Property 50 — an explanation moves no selected action and no reported figure
# ===========================================================================


@pytest.mark.model
@settings(max_examples=100)
@given(
    plan=candidate_estimate_set(),
    amount=st.integers(min_value=1, max_value=5_000_000).map(Minor),
    response=reasoning_responses(call_kind=ReasoningCallKind.DECISION_EXPLANATION),
)
def test_p50_an_explanation_moves_no_selected_action_and_no_reported_figure(
    plan: tuple[Probability, tuple[CandidateInput, ...]],
    amount: Minor,
    response: ReasoningResponse,
) -> None:
    """Feature: revora-customer-response-loop, Property 50: for any candidate set, the selected
    Candidate_Action and every reported per-candidate figure are identical with and without a
    ``DECISION_EXPLANATION`` response.

    The ordering is what makes this true and it is worth naming: ``handle_optimizer`` commits the
    recommendation and applies the transition *before* it asks a model anything. So "the
    explanation held no influence on the selection" (R27.C8) is a fact about when the call happens
    and not only about what the code does with the answer — by the time prose exists, the
    comparison has been recorded and the policy job is already enqueued.

    Both halves are asserted because they fail differently. The behavioural half re-runs the
    selection after attempting to attach the stored explanation to every candidate input, every
    evaluated candidate, the result and the winner; the structural half is that all of those
    attempts are *refused*, which is what makes the re-run's equality mean something rather than
    restating that ``select`` is pure.

    The stored value is bounded here too, because R27.C8 asks for the stored explanation to be
    truncated to ``REASONING_EXPLANATION_MAX_LENGTH`` — and that bound is enforced twice, once by
    ``parse_decision_explanation`` rejecting anything longer and once by the slice at the store. A
    bound enforced only where the parsing happens is a bound the day somebody stores prose from
    somewhere else.
    """
    baseline, candidates = plan
    before = select(candidates, baseline=baseline, amount=amount, thresholds=_THRESHOLDS)

    result = _invoke(response).result
    stored = (
        result.output.explanation[: pipeline.REASONING_EXPLANATION_MAX_LENGTH]
        if isinstance(result, Accepted)
        else None
    )
    if stored is not None:
        assert len(stored) <= pipeline.REASONING_EXPLANATION_MAX_LENGTH

    targets: tuple[object, ...] = (*candidates, *before.candidates, before, before.selected)
    for target in targets:
        for name in ("ai_explanation_text", "explanation", "net_recovery_value", "rank"):
            with pytest.raises(_ASSIGNMENT_REFUSED):
                setattr(target, name, stored)

    after = select(candidates, baseline=baseline, amount=amount, thresholds=_THRESHOLDS)
    assert _selection_fingerprint(before) == _selection_fingerprint(after), (
        f"the selection moved after a {response.shape.value} DECISION_EXPLANATION "
        f"({response.detail}). Prose has no field to arrive in and the selection is integer "
        "arithmetic, so a difference here means one of those two stopped being true"
    )


@pytest.mark.model
def test_p50_the_optimizer_admits_no_explanation_and_records_no_influence() -> None:
    """Property 50's two structural halves: no parameter, and a literal ``influenced=False``.

    ``select``'s parameter list is checked for the same reason ``evaluate``'s is: a parameter is
    the route no immutability closes.

    The second half is the more interesting one. ``ai_invocation.influenced_recommendation`` is
    the queryable record that the prose held no influence on the selection, and R27.C8 makes it
    false *by construction* on this call kind rather than false-unless-set. Read out of the AST as
    a literal ``False`` at the call site, so a future edit that *computed* it — from
    ``isinstance(result, Accepted)``, say, which is the plausible mistake because that is exactly
    what the ``LINK_DESCRIPTION`` site correctly does — fails here rather than quietly making the
    column mean something different for one of the three kinds.
    """
    parameters = list(inspect.signature(select).parameters)
    assert parameters == ["candidates", "baseline", "amount", "thresholds"], (
        f"select now takes {parameters}; the optimizer's input set has grown"
    )

    explain = _function(pipeline, "_explain_decision")
    recording = [
        node
        for node in ast.walk(explain)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_record_reasoning_invocation"
    ]
    assert len(recording) == 1, (
        f"_explain_decision records {len(recording)} invocations; R27.C12 asks for exactly one "
        "per invocation"
    )
    influenced = [
        keyword.value for keyword in recording[0].keywords if keyword.arg == "influenced"
    ]
    assert len(influenced) == 1
    assert isinstance(influenced[0], ast.Constant) and influenced[0].value is False, (
        "the DECISION_EXPLANATION invocation row no longer records influenced=False as a literal. "
        "R27.C8 requires the record say the response held no influence on the selection, and a "
        "computed value is a value that can compute to True"
    )


# ===========================================================================
# Property 51 — the absent credential and the rejected response are one system
# ===========================================================================


@pytest.mark.model
@settings(max_examples=150)
@given(
    response=reasoning_responses(
        shapes=(
            ResponseShape.ABSENT,
            ResponseShape.SCHEMA_INVALID,
            ResponseShape.SCHEMA_INVALID,
            ResponseShape.TIMEOUT,
            ResponseShape.TIMEOUT,
        )
    )
)
def test_p51_a_rejected_response_hands_every_component_what_no_credential_hands_it(
    response: ReasoningResponse,
) -> None:
    """Feature: revora-customer-response-loop, Property 51: with the reasoning credential absent
    and with it present and every response rejected, the value handed to every pure component is
    identical — so the Recovery_Cases created, the decisions, the executions, the recovered
    amounts and the projected Case_Timelines are identical — and no Recovery_Case is held in a
    state that waits on a reasoning response.

    **This is the model-tier half of P51 and the boundary is worth being explicit about.** The
    property's fullest form is a claim about two runs of the whole pipeline producing identical
    rows. What is asserted here is the mechanism that determines it: every pure component takes
    the reasoning result as an ``| None`` argument, so two runs are identical exactly when the
    argument is identical — and this asserts the argument is. A component handed the same value
    computes the same answer, which is what "pure" means and what
    ``test_r8c14_identical_inputs_give_identical_decisions`` already pins.

    Both invocation sites that produce a value are checked, because they produce different kinds
    of nothing:

    * ``CAUSE_HYPOTHESIS`` hands ``run_diagnosis`` an ``AiCauseProposal`` whose recorded cause is
      ``UNKNOWN`` — the same cause the ``None`` path records — with a method drawn from the two
      untrusted ones. The *method* differs between the two runs and that is correct and
      deliberate: ``FALLBACK_UNKNOWN`` versus ``REJECTED_AI_OUTPUT`` versus no row at all is
      exactly the operational fact R27.C12 exists to preserve. What must not differ is the cause,
      because the cause is what policy reads.
    * ``LINK_DESCRIPTION`` hands ``execute_approved_action`` ``ai_description=None`` on every
      rejected path, so the engine composes the reviewed template exactly as it did before this
      layer existed.

    The failure this catches is the one that looks like an improvement: a rejected answer
    "partially" used — a cause kept at zero confidence, a truncated draft sent because it was
    nearly valid. Either would make the credential-present system behave differently from the
    credential-absent one, and the difference would appear in money.
    """
    invocation = _invoke(response)
    result = invocation.result

    assert not isinstance(result, Accepted), (
        f"a {response.shape.value} draw ({response.detail}) was accepted; this property's "
        "generator is restricted to the outcomes that take the deterministic path"
    )

    # The CAUSE_HYPOTHESIS argument: the recorded cause is the ``None`` path's cause.
    recorded = _recorded_from(result)
    assert recorded.cause is RiskCause.UNKNOWN
    assert recorded.method in {
        DiagnosisMethod.REJECTED_AI_OUTPUT,
        DiagnosisMethod.FALLBACK_UNKNOWN,
    }
    assert recorded.substituted_to_unknown is True

    # The LINK_DESCRIPTION argument, derived the way ``_draft_link_description`` derives it: an
    # ``Accepted`` yields the draft and every other variant yields ``None``.
    ai_description = result.output.description if isinstance(result, Accepted) else None
    assert ai_description is None

    if response.shape is not ResponseShape.ABSENT:
        return

    assert invocation.requests == (), (
        "a request was issued with no credential configured. R27.C7 requires none, and the "
        "transport never having been reached is the only externally visible evidence of it"
    )
    assert isinstance(result, Unavailable)
    assert result.reason is UnavailableReason.CREDENTIAL_ABSENT
    assert result.request_issued is False
    assert result.attempts == 0
    assert result.waited_ms == 0, (
        "the absent-credential path waited. Nothing may block, sleep or poll there: it is the "
        "deployed default, and R27.C7's 'no case waits on a reasoning response' holds by there "
        "being nothing to wait for"
    )


@pytest.mark.model
def test_p51_no_case_state_waits_on_a_model_and_no_adapter_is_built_without_a_credential() -> None:
    """Property 51's other half: R27.C7's "holding no Recovery_Case in a state that waits".

    A state machine cannot be shown to have no waiting state by driving it — the claim is about
    the *absence* of a member, so it is asserted against the enumeration. The fourteen names are
    pinned rather than derived, which is the whole point: a derived set agrees with itself
    forever, and what R27.C7 forbids is the fifteenth member.

    ``WAITING_FOR_OUTCOME`` is the one state whose name says it waits, and it is not the one this
    forbids: it waits on an authoritative provider read of a payment — an external effect with
    money behind it — and ``handle_outcome``, the handler that clears it, takes a ``provider`` and
    takes no adapter at all. Asserted rather than assumed, because "the waiting state is about the
    provider" is exactly the kind of claim that stays true in a comment while the code drifts.

    The last assertion is the coarsest and most valuable one available: with no credential
    configured, nothing is *constructed*. No client, no payload, no wait, no ``ai_invocation``
    row. That is the branch the deployed reality takes, so it is the branch that must cost
    nothing.
    """
    assert {state.value for state in CaseState} == _EXPECTED_CASE_STATES, (
        "the Recovery_Case state set has changed. R27.C7 forbids a state that waits on a "
        "reasoning response, and a new member is where one would appear"
    )
    for state in CaseState:
        words = set(state.value.split("_"))
        assert words.isdisjoint(_MODEL_WORDS), (
            f"{state.value} names {sorted(words & _MODEL_WORDS)}, which reads as a state waiting "
            "for a model rather than for an external effect"
        )
    waiting = {state.value for state in CaseState if "WAITING" in state.value.split("_")}
    assert waiting == {"WAITING_FOR_OUTCOME"}, (
        f"the states that wait are {sorted(waiting)}. Exactly one may wait, and it waits on an "
        "authoritative provider read of a payment"
    )

    outcome_parameters = inspect.signature(pipeline.handle_outcome).parameters
    assert "provider" in outcome_parameters
    assert "reasoning" not in outcome_parameters, (
        "handle_outcome takes a reasoning adapter, so the one state that waits could now be "
        "waiting on a model rather than on an authoritative payment read"
    )

    with _credential(present=False):
        assert pipeline.reasoning_adapter() is None
        assert pipeline._resolve_adapter(None) is None


# ===========================================================================
# Property 52 — one row per invocation, and a bound counted from rows
# ===========================================================================


class _ScalarOnly:
    """The one thing ``count_for_case`` asks of a result: ``scalar_one()``."""

    __slots__ = ("_value",)

    def __init__(self, value: int) -> None:
        self._value = value

    def scalar_one(self) -> int:
        return self._value


class _CountingSession:
    """An in-memory stand-in for the session ``AiInvocationRepository`` counts through.

    The whole fake, because ``count_for_case`` makes exactly one query and reads exactly one
    scalar off it. ``queries`` is recorded so the property can assert the count was actually
    taken — a bound check that silently stopped querying would otherwise pass every assertion
    about the number it returned.
    """

    __slots__ = ("queries", "rows")

    def __init__(self, rows: int) -> None:
        self.rows = rows
        self.queries = 0

    def execute(self, statement: object, *args: object, **kwargs: object) -> _ScalarOnly:
        self.queries += 1
        return _ScalarOnly(self.rows)


@pytest.mark.model
@settings(max_examples=50)
@given(
    attempts=st.integers(min_value=1, max_value=3),
    sequence=st.lists(reasoning_responses(), min_size=1, max_size=12),
)
def test_p52_the_per_case_invocation_count_stays_within_the_bound(
    attempts: int, sequence: list[ReasoningResponse]
) -> None:
    """Feature: revora-customer-response-loop, Property 52: for any sequence of invocations on one
    Recovery_Case — including ones that timed out, were rejected or returned a transport error —
    the committed ``ai_invocation`` count for that case never exceeds
    ``MAX_REASONING_CALLS_PER_CASE``.

    Driven through the real bound predicate, ``pipeline._within_call_bound`` over
    ``pipeline.reasoning_call_bound``, against an in-memory fake of the one query the repository
    makes. The bound is counted from *committed rows* rather than from a per-process counter
    (R27.C13), and that is exactly why the fake is a row count: a counter in memory resets on a
    restart, so a case whose worker crashed mid-cycle would get a fresh allowance on every
    redelivery, and a bound a crash loop can reset is not a bound.

    **The simulation credits a row to every permitted invocation, which is stricter than
    reality.** An absent credential and a payload the allow-list refused write no row, because
    nothing was sent; crediting them anyway makes the number asserted against the bound an upper
    bound on the real one, and it means this test holds no second copy of the pipeline's
    row-writing rule. A property that re-derived that rule would pass whenever the copy and the
    original agreed — including when both were wrong.

    ``MAX_RECOVERY_ATTEMPTS`` is drawn rather than left at its default so the bound is small
    enough to be *reached*: at three attempts the bound is nine and a twelve-long sequence
    reaches it, at one it is three and almost every sequence does. The two assertions after the
    loop are what make this more than "a counter did not run away" — the permitted count equals
    the bound exactly when the sequence is longer, and the surplus is refused rather than
    absorbed. An off-by-one in the predicate, ``<=`` where ``<`` belongs, gives one invocation too
    many and is invisible to an inequality on its own.
    """
    config = _config(MAX_RECOVERY_ATTEMPTS=str(attempts), REASONING_TIMEOUT="0.02")
    bound = pipeline.reasoning_call_bound(config)
    assert bound == len(ReasoningCallKind) * attempts

    rows = 0
    permitted = 0
    refused = 0
    for response in sequence:
        session = _CountingSession(rows)
        allowed = pipeline._within_call_bound(
            cast("Session", session), _MERCHANT_ID, _CASE_ID, config
        )
        assert session.queries == 1, (
            "the bound check made no query, so it is no longer counting committed rows and would "
            "survive a restart with a fresh allowance (R27.C13)"
        )
        if not allowed:
            refused += 1
            continue
        permitted += 1
        _invoke(response, config=config)
        rows += 1

    assert rows <= bound, (
        f"{rows} invocations were permitted against a bound of {bound}. R27.C13 caps what one "
        "Recovery_Case may spend across its whole lifetime"
    )
    assert permitted == min(len(sequence), bound), (
        f"{permitted} of {len(sequence)} invocations were permitted with a bound of {bound}. "
        "Fewer means allowance is being lost; more means the bound is checked against the wrong "
        "number"
    )
    assert refused == max(0, len(sequence) - bound)


@pytest.mark.model
@settings(max_examples=150)
@given(response=reasoning_responses())
def test_p52_every_invocation_yields_exactly_one_complete_record(
    response: ReasoningResponse,
) -> None:
    """Property 52's other half: one complete row per invocation, or a named Audit_Record.

    R27.C12 asks for one Audit_Record per invocation carrying nine named things, *for every
    invocation including the ones that timed out, were rejected or returned a transport error*. So
    the property is not "the accepted ones are recorded" — it is that no variant can produce a row
    the writer cannot build. A variant that could not name a ``prompt_contract_id`` would be a
    variant whose row silently went unwritten, and the deterministic-fallback rate would then be
    computed over the successes only, which is the one number this table exists to keep honest.

    The second assertion covers what the first cannot. Two results issue no request at all — an
    absent credential (R27.C7) and a payload the allow-list refused (R27.C2) — and both correctly
    write *no* ``ai_invocation`` row, because nothing was sent and a row claiming otherwise would
    inflate the reliability figure. What they must not do is vanish, so each one names an event
    type instead, and the invariant asserted is that **every invocation leaves at least one
    durable record**: a row, or a named Audit_Record, or both.

    Scoped to the three sanctioned call kinds, which is all this generator produces and all
    R27.C1 permits. ``UNKNOWN_CALL_KIND`` is the one ``Unavailable`` reason that names no event
    type, and it is unreachable through a typed call: every member of ``ReasoningCallKind`` has a
    declared contract, so that branch exists for a value arriving from a stored row or a
    deserialized job payload that predates or postdates the enumeration. A property that generated
    one would be asserting about a state the type system already forbids.
    """
    result = _invoke(response).result

    verdicts = [verdict for verdict in ReasoningVerdict if verdict_of(result) is verdict]
    assert len(verdicts) == 1, f"{type(result).__name__} maps onto {verdicts}, not one verdict"

    columns = pipeline._invocation_columns(result, fallback_model_id=DEFAULT_MODEL)
    assert set(columns) == {
        "call_kind",
        "contract_id",
        "model_id",
        "model_version",
        "latency_ms",
        "retained",
        "detail",
    }
    assert columns["contract_id"], (
        "a result produced no prompt_contract_id. The column is NOT NULL, so this is a row that "
        "would not be written at all"
    )
    assert columns["call_kind"] == response.call_kind.value
    assert columns["contract_id"] == contract_for(response.call_kind).contract_id

    row_written = not (isinstance(result, Unavailable) and not result.request_issued)
    assert row_written or audit_event_type_for(result) is not None, (
        f"a {response.shape.value} invocation ({response.detail}) produced neither an "
        "ai_invocation row nor a named Audit_Record, so it left no durable trace at all"
    )

    if isinstance(result, Accepted | RejectedSchema | RejectedContent):
        assert columns["model_id"], "a response arrived but no model is recorded against it"
    if isinstance(result, TimedOut):
        assert columns["model_id"] == DEFAULT_MODEL
        assert columns["model_version"] is None, (
            "model_version is what answered; filling it from what was asked for would hide a "
            "silent provider-side version change in the table built to expose it"
        )


# ===========================================================================
# Property 53 — the transmitted key set is a subset of the declared set
# ===========================================================================


def _encoded_body(call_kind: ReasoningCallKind, payload: Mapping[str, object]) -> bytes:
    """The request body as ``httpx`` would put it on the wire.

    Built through the adapter's own ``_wire_body`` and then encoded, so the round trip crosses a
    real serialization boundary. A test that handed the mapping straight back to the extractor
    would never encode anything, and "a value became a key" is a claim about encoding.
    """
    return json.dumps(_wire_body(call_kind, payload)).encode("utf-8")


@pytest.mark.pure
@settings(max_examples=500)
@given(values=adversarial_case_values())
def test_p53_the_transmitted_key_set_is_a_subset_of_the_declared_set(
    values: dict[str, object],
) -> None:
    """Feature: revora-customer-response-loop, Property 53: for any case row and any
    Delay_Reason_Note content, the key set of the payload transmitted for a Reasoning_Call_Kind is
    a subset of the field set the recorded Prompt_Contract version declares, and no transmitted
    key names a customer contact identifier, a payment instrument reference, a
    Customer_Access_Token, an authentication secret or a Merchant_User identifier.

    **The values are adversarial and the keys are not, and that asymmetry is the property.**
    R27.C2 and R27.C3 are claims about the transmitted *field set*, and the failure they guard
    against is a value smuggling in a key — a note containing ``", "customer_contact": "…``, a
    string that closes the JSON object it is inside, a nested document that survives
    serialization as structure rather than as text. So every value drawn here is one of those,
    and the assertion is over the keys that come back out of the encoded body.

    Asserted through the round trip rather than on the builder's return value.
    ``build_request_payload`` produces the mapping and ``extract_transmitted_payload`` reads it
    back out of the *encoded request envelope*, which is where a value that escaped its string
    would become a key. Checking the builder's output alone would miss exactly that.

    **What this property does not claim, stated so nobody reads it as claiming it.** A
    Delay_Reason_Note is free text a customer typed and it is transmitted verbatim, truncated to
    ``DELAY_NOTE_MAX_LENGTH``. A customer who writes their phone number into the note has put a
    contact identifier on the wire, and no arrangement of field names prevents that. R27.C3 is
    satisfied here because no contract *declares* such a field — which
    ``contracts._reject_forbidden_names`` enforces at import — and the residual question of note
    *contents* is a retention and masking question R29.C10 owns, not this one.
    """
    for call_kind, contract in CONTRACTS.items():
        # Only the names this contract declares. A value for another kind's field is an
        # undeclared field, and that is the next test's subject rather than this one's.
        supplied = {name: value for name, value in values.items() if contract.declares(name)}
        payload = build_request_payload(
            call_kind, supplied, delay_note_limit=_CONFIG.DELAY_NOTE_MAX_LENGTH
        )
        assert frozenset(payload) <= contract.fields, (
            f"{sorted(frozenset(payload) - contract.fields)} was transmitted for "
            f"{contract.contract_id} without being declared"
        )

        transmitted = extract_transmitted_payload(_encoded_body(call_kind, payload))
        assert frozenset(transmitted) <= contract.fields, (
            f"{sorted(frozenset(transmitted) - contract.fields)} appeared as a key in the encoded "
            f"request body for {contract.contract_id}. A value has escaped its string and become "
            "a field"
        )
        for name in transmitted:
            offending = sorted(
                fragment for fragment in FORBIDDEN_NAME_FRAGMENTS if fragment in name.lower()
            )
            assert not offending, (
                f"the transmitted key {name!r} matches forbidden name fragment(s) {offending}; "
                "R27.C3 forbids transmitting a contact identifier, an instrument reference, a "
                "Customer_Access_Token, an authentication secret or a Merchant_User identifier"
            )

        note = transmitted.get("delay_reason_note")
        if isinstance(note, str):
            assert len(note) <= _CONFIG.DELAY_NOTE_MAX_LENGTH, (
                "the delay note was transmitted past DELAY_NOTE_MAX_LENGTH; R20.C11's truncation "
                "happens in the adapter, which is the component holding both the value and the "
                "bound"
            )


@pytest.mark.pure
@settings(max_examples=300)
@given(
    call_kind=st.sampled_from(list(ReasoningCallKind)),
    name=forbidden_field_names(),
    value=st.text(max_size=40),
)
def test_p53_an_undeclared_field_blocks_transmission_and_builds_nothing(
    call_kind: ReasoningCallKind, name: str, value: str
) -> None:
    """Property 53's other half: an undeclared name raises rather than being dropped silently.

    Dropping would be worse than it looks, and R27.C2 says so: it asks for blocked transmission
    and a record naming the offending field, not for silent filtering. A caller whose field was
    quietly removed would believe it had transmitted something it had not — and the belief would
    be invisible, because the request would succeed.

    The generated names include four that R27.C3 does *not* forbid: ``payment_id``, ``order_id``,
    ``case_id`` and ``net_recovery_value``. They must be refused too, and for a different reason —
    the allow-list is a declared set, not a blocklist of bad ideas, so a name being harmless is
    not a reason to transmit it. A test that only drew forbidden names would pass against an
    implementation that checked a blocklist, which is the weaker mechanism.
    """
    contract = contract_for(call_kind)
    assert not contract.declares(name), (
        f"{name!r} is declared by {contract.contract_id}, so this generator is no longer "
        "producing undeclared names and the property has gone vacuous"
    )

    with pytest.raises(PromptContractViolationError) as caught:
        build_request_payload(
            call_kind, {name: value}, delay_note_limit=_CONFIG.DELAY_NOTE_MAX_LENGTH
        )

    assert caught.value.fields == frozenset({name})
    assert caught.value.contract_id == contract.contract_id


# ===========================================================================
# Property 54 — the confidence ceiling
# ===========================================================================


@pytest.mark.pure
@settings(max_examples=500)
@given(cause=st.sampled_from(list(RiskCause)), confidence=ai_confidences(in_range=False))
def test_p54_an_ai_assisted_confidence_never_reaches_one(
    cause: RiskCause, confidence: Decimal
) -> None:
    """Feature: revora-customer-response-loop, Property 54: a Diagnosis recorded with the method
    ``AI_ASSISTED`` carries a confidence at or below 0.99, and a confidence of exactly 1.0 is
    recorded only for the method ``DETERMINISTIC``.

    The ceiling is the only thing standing between an AI-assisted row and the value R3.C10
    reserves for "the provider told us". With it, a reader of the ``diagnosis`` table can read the
    method off the confidence — which is worth more than it sounds: a row whose method column was
    mis-set becomes detectable from a *second* column rather than only from the audit trail.

    Out-of-range confidences are drawn deliberately even though the output schema refuses them,
    because the cap is applied to what the model returned rather than enforced by validation. That
    ordering is a decision R27.C4 and R27.C5 make together: R27.C5's permitted range is 0 to 1
    *inclusive*, so a model claiming certainty is a well-formed answer that gets capped and not a
    malformed one that gets hidden behind a validation error. A value above 1 cannot reach this
    function through the adapter — gate 3 rejects it — and drawing it here shows the cap is a
    ``min`` rather than a range assertion, which is what keeps the two mechanisms independent.

    The failure this catches: somebody "simplifies" the cap into the Pydantic model as an
    ``le=0.99`` bound. The row then never reaches 1.0 either, so the ceiling still looks enforced
    — but a confident answer is now reported as a schema rejection, the deterministic-fallback rate
    rises for no operational reason, and the cause the model actually named is thrown away.
    """
    recorded = resolve_ai_diagnosis(
        AiCauseProposal(
            cause=cause,
            confidence=confidence,
            method=DiagnosisMethod.AI_ASSISTED,
            invoked=True,
        ),
        confidence_floor=_FLOOR,
    )

    assert recorded.method is DiagnosisMethod.AI_ASSISTED
    assert recorded.confidence <= AI_CONFIDENCE_CEILING.value, (
        f"an AI_ASSISTED diagnosis recorded {recorded.confidence} from a returned {confidence}; "
        f"R27.C4 caps it at {AI_CONFIDENCE_CEILING.value}"
    )
    assert recorded.confidence != Decimal("1.000"), (
        "an AI_ASSISTED diagnosis reached the confidence R3.C10 reserves for the provider's own "
        "error field"
    )
    assert capped_ai_confidence(confidence) == min(confidence, AI_CONFIDENCE_CEILING.value)

    # And the reserved value is still reachable, by the one method entitled to it. A ceiling that
    # also capped the deterministic path would be a bug in the other direction, and it would be
    # invisible to every assertion above.
    deterministic = resolve_recorded_diagnosis(
        cause=cause,
        confidence=Decimal("1.000"),
        method=DiagnosisMethod.DETERMINISTIC,
        confidence_floor=_FLOOR,
    )
    assert deterministic.confidence == Decimal("1.000")
    assert deterministic.method is DiagnosisMethod.DETERMINISTIC


@pytest.mark.pure
@settings(max_examples=150)
@given(response=reasoning_responses(call_kind=ReasoningCallKind.CAUSE_HYPOTHESIS))
def test_p54_no_provider_answer_reaches_the_reserved_confidence(
    response: ReasoningResponse,
) -> None:
    """Property 54 over the reachable states, driven from the wire rather than constructed.

    The test above draws ``(method, confidence)`` pairs directly, which is the right way to
    exercise the cap and the wrong way to establish that the cap is *reached* on the real path. A
    model returning exactly ``1.0`` is a schema-valid answer, so this drives the whole gate stack
    from a provider body and asserts the reserved value never comes out the other end — for any of
    the five shapes, including the adversarial ones whose confidence is drawn at the ceiling on
    purpose.

    ``pure`` rather than ``model``: it opens no socket, touches no fake repository and reads no
    row. The adapter's transport is a ``MockTransport`` and everything after it is arithmetic.
    """
    result = _invoke(response).result
    recorded = _recorded_from(result)

    assert recorded.method is not DiagnosisMethod.DETERMINISTIC, (
        "a provider answer produced a DETERMINISTIC diagnosis. That method and its 1.0 confidence "
        "are reserved for the provider's own error field and the Delay_Reason mapping table "
        "(R3.C10), and AiCauseProposal refuses to claim it"
    )
    assert recorded.confidence < Decimal("1.000"), (
        f"a {response.shape.value} answer ({response.detail}) was recorded at "
        f"{recorded.confidence}, which is the confidence only DETERMINISTIC may hold"
    )
    if recorded.method is DiagnosisMethod.AI_ASSISTED:
        assert recorded.confidence <= AI_CONFIDENCE_CEILING.value
    else:
        assert recorded.cause is RiskCause.UNKNOWN


# ===========================================================================
# Property 55 — a refused draft is a substitution, never a non-execution
# ===========================================================================


@pytest.mark.model
@settings(max_examples=150)
@given(
    amount=st.integers(min_value=1, max_value=10_000_000_00),
    approved_link=st.sampled_from((pipeline.NO_APPROVED_LINK, _RESPONSE_PAGE_URL)),
    data=st.data(),
)
def test_p55_a_refused_draft_sends_the_template_and_retains_the_draft(
    amount: int, approved_link: str, data: st.DataObject
) -> None:
    """Feature: revora-customer-response-loop, Property 55: for a ``LINK_DESCRIPTION`` response
    failing any configured content validation rule, the description actually sent equals the
    configured deterministic template, the customer-message counter is unchanged by the
    suppression, and a ``CONTENT_REJECTED`` record names the violated rule and retains the
    rejected draft.

    **Substitution rather than non-execution, and R27.C10 says which way round.** A payment link
    carrying a template description is a complete action, so refusing to send one because a model
    wrote a bad sentence would cost the merchant a recovery over prose. The engine's line is
    ``description = template if ai_description is None else ai_description`` and the template is
    required *first* — an action with no approved wording is refused whether or not a draft
    exists, so a model cannot make an action sendable that Revora has written no copy for.

    The amount is drawn across four orders of magnitude and the mismatching draft is built by
    perturbing the case's *own* rendering, so the amount-equality rule is exercised against a real
    figure rather than a fixed one — and a change to how amounts are rendered cannot turn a
    mismatch into a match.

    The approved link is drawn from both values it can hold. Today it is ``NO_APPROVED_LINK``, the
    empty string, because nothing in ``revora`` composes a Customer_Response_Page URL yet and the
    honest reading of "zero links other than that URL" is *zero links*. Drawing the real-URL case
    as well means task 51 minting one does not silently leave this property asserting something
    narrower than it says.

    The three failures this catches:

    * **a content rule stops firing** — the draft is accepted and reaches a customer unreviewed;
    * **the draft is discarded rather than retained** — ``CONTENT_REJECTED`` names a rule with no
      text behind it, and nobody can review what broke it;
    * **the suppression moves the counter** — a case would lose one of its
      ``MAX_CUSTOMER_MESSAGES`` to a message it never sent, which is a recovery given up for a
      sentence nobody read.
    """
    formatted = pipeline._formatted_amount(amount, "INR")
    response = data.draw(
        reasoning_responses(
            call_kind=ReasoningCallKind.LINK_DESCRIPTION,
            shapes=(ResponseShape.ADVERSARIAL,),
            payment_amount_formatted=formatted,
            approved_link=approved_link,
        )
    )
    result = _invoke(
        response, payment_amount_formatted=formatted, approved_link=approved_link
    ).result

    assert not isinstance(result, Accepted), (
        f"a draft built to violate {list(response.notes)} was accepted against the amount "
        f"{formatted} and the approved link {approved_link!r}. A content rule has stopped firing, "
        "and the sentence would reach a customer without having passed R27.C9"
    )

    # ``_draft_link_description`` returns the description only on ``Accepted``, so the engine
    # receives ``None`` and composes the reviewed template.
    ai_description = result.output.description if isinstance(result, Accepted) else None
    assert ai_description is None

    template = description_for(CandidateAction.PAYMENT_LINK, merchant_name=_MERCHANT_NAME)
    assert template is not None, "PAYMENT_LINK has no approved template to substitute"
    assert template_for_action(CandidateAction.PAYMENT_LINK) is not None
    sent = template if ai_description is None else ai_description
    assert sent == template, "the suppression did not substitute the deterministic template"

    if not isinstance(result, RejectedContent):
        # A control-character or over-length draft is refused at gate 3 instead, which is a
        # RejectedSchema. Still a suppression and still the template — the retention assertions
        # below are about the record R27.C10 names, which only the content path writes.
        assert isinstance(result, RejectedSchema)
        return

    drafted = json.loads(cast("str", response.body))["description"]
    assert isinstance(result.rule, ContentRule)
    assert audit_event_type_for(result) == CONTENT_REJECTED
    columns = pipeline._invocation_columns(result, fallback_model_id=DEFAULT_MODEL)
    evidence = pipeline._reasoning_audit_evidence(result, columns)
    assert evidence["violated_rule"] == result.rule.value
    assert evidence["retained_draft"] == result.draft
    assert result.draft == drafted, (
        "the retained draft is not the text that was refused, so CONTENT_REJECTED names a rule "
        "with nothing behind it"
    )
    assert sent != result.draft, (
        "the template and the refused draft are the same string, so this example cannot "
        "distinguish substitution from sending the draft"
    )


@pytest.mark.model
def test_p55_the_message_counter_has_one_writer_and_it_cannot_see_a_description() -> None:
    """Property 55's counter half, which is structural because that is where it is decided.

    "The counter is unchanged by the suppression" is a universal over every draft, and a
    behavioural test can only show that the drafts it drew moved nothing. What makes it true is
    that ``recovery_case.customer_message_count`` has exactly **one** writer — the locked
    transition in ``revora/cases/manager.py`` — and that writer computes its delta from the
    transition rule's effects and from whether the action is customer-visible. It has no
    description, no draft and no adapter in scope, so it cannot tell a model's sentence from a
    template file's.

    Asserted three ways, each closing a different route:

    * the set of modules assigning to the counter is exactly one, so a second writer that *could*
      consult a description has to appear here first;
    * that module references no description vocabulary and imports no reasoning layer;
    * ``_draft_link_description`` — the function that obtains the draft — references no counter
      name, so the site that knows about the draft cannot move a counter either.

    From the AST rather than from a substring search, because both modules explain in prose
    exactly which value they must not read: ``_draft_link_description``'s docstring says "Nothing
    here touches the customer-message counter", and a grep would fail on the sentence that
    promises it.
    """
    writers = sorted(
        path.relative_to(_REVORA_ROOT).as_posix()
        for path in _REVORA_ROOT.rglob("*.py")
        if "customer_message_count" in _assigned_attributes(_parsed(path))
    )
    assert writers == ["cases/manager.py"], (
        f"customer_message_count is written by {writers}. One writer is what makes R27.C10's 'the "
        "counter is left unchanged by the suppression' a property of the code rather than of every "
        "call site remembering"
    )

    manager_tree = _parsed(Path(inspect.getfile(manager_module)))
    referenced = _referenced_names(manager_tree)
    for forbidden in ("ai_description", "description", "draft", "ReasoningAdapter"):
        assert forbidden not in referenced, (
            f"the counter's single writer references {forbidden!r} in code. A counter that can see "
            "where a description came from is a counter that can move differently for a model's "
            "sentence than for a template's"
        )
    assert not any(
        name.startswith("revora.reasoning") for name in _imported_modules(manager_tree)
    ), "the counter's single writer imports the reasoning layer"

    drafting = _referenced_names(_function(pipeline, "_draft_link_description"))
    for counter in ("customer_message_count", "executed_action_count", "last_outbound_at"):
        assert counter not in drafting, (
            f"_draft_link_description references {counter!r}; the site that holds the draft must "
            "not be able to move a counter"
        )
