"""The candidate prior lookup. Not a simulator, and named that way on purpose.

Requirement 6 calls this the ``Intervention_Simulator`` and the design's amendment list
overrules the name: *"Describe and implement as a prior lookup, not a simulator."* The
honest description of what this module does is that it reads four numbers per action out
of a table of configured assumptions, sets two of them by definition where a requirement
fixes them, derives one from the baseline posterior, and labels every remaining figure
``UNCALIBRATED``. There is no model, no sampling, no counterfactual, and nothing that
would justify the word simulate.

The thing it *wants* to be is uplift modelling — estimating, per action and per segment,
how much that action changes the recovery probability. Uplift modelling needs
treatment-and-control outcomes per action per segment, which is data that does not exist
until the experiment has run. Shipping a prior table and marking it ``UNCALIBRATED``
everywhere is the version that can be audited; shipping something that produced the same
numbers through a function named ``simulate`` would be the same guesses with the evidence
of their guessiness removed.

**So where does the optimism go?** Every uplift number in :data:`UPLIFT_PRIORS` makes
intervention look worthwhile, because a prior that made intervention look pointless
would be an equally unfounded guess in the opposite direction. The answer to that is not
a more careful guess. It is structural: the experiment measures whether the uplifts were
real, ``UNCALIBRATED`` propagates to every surface and export, and the value optimizer's
thresholds are applied to figures that carry that label. We do not hide the optimism; we
label it and then measure it.

**The two null actions are the reason the comparison is honest.**

``DO_NOTHING`` is *definitional*, not estimated. Its probability **equals** the baseline
exactly — the same ``Probability`` value, not a recomputation that agrees to four places
— and all three costs are exactly zero, with method ``DEFINITIONAL`` on all four
figures. That is what makes the arithmetic downstream come out at exactly zero
incremental probability, exactly zero expected incremental revenue and exactly zero net
value, which is Property 19. If ``DO_NOTHING`` were estimated like anything else, its
incremental value would be noise around zero, and roughly half the time Revora would
find a reason to act purely because the null action's estimate happened to land low.

``WAIT`` has zero action cost and zero customer cost by requirement (R6.C10), and a
probability derived from the no-intervention hazard over the time left in the window.
See :func:`wait_probability` for the assumption that derivation rests on. ``WAIT`` is not
free of consequence — it spends the recovery window — but it spends it in a way the
probability expresses, not in a cost.

**Zero provider calls, structurally.** R6.C8 requires that no request reaches the payment
provider or a communication provider from here. This module imports ``domain``,
``persistence``, ``audit`` and ``platform``, and nothing from ``revora.providers`` or
``revora.reasoning``; the import contracts enforce the layering and there is no client
object anywhere in scope to call. Every figure comes from a persisted row, a segment
aggregate, or a constant in this file.

**No ``float``.** Probabilities are ``Decimal`` at four places, costs are integer minor
units, and the one transcendental step — the hazard's fractional power — is done with
``Decimal.ln`` and ``Decimal.exp`` in an explicit context rather than with a binary
library.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, localcontext
from typing import Final

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from revora.audit.events import (
    CANDIDATE_ACTION_UNAVAILABLE,
    CANDIDATE_ESTIMATES_ALREADY_RECORDED,
    CANDIDATE_ESTIMATES_RECORDED,
    CANDIDATE_MEMORY_UNAVAILABLE,
    INVALID_ESTIMATE,
)
from revora.audit.writer import AuditEntry, AuditWriter
from revora.domain.actions import (
    ACTION_PRECEDENCE,
    NULL_ACTIONS,
    UNAVAILABLE_IN_MVP,
    CandidateAction,
    candidate_set_for,
)
from revora.domain.enums import (
    ActionAvailability,
    EstimationMethod,
    ExclusionReason,
    Provenance,
    RiskCause,
)
from revora.domain.money import ZERO, Minor
from revora.domain.probability import Probability
from revora.domain.segments import FEATURE_KEYS, FEATURE_RISK_CAUSE
from revora.persistence.models.estimates import BaselineEstimate
from revora.persistence.repositories.cases import RecoveryCaseRepository
from revora.persistence.repositories.estimates import (
    BaselineEstimateRepository,
    CandidateEstimateRepository,
    SegmentObservationRepository,
)
from revora.platform.clock import now
from revora.platform.config import Configuration
from revora.platform.logging import get_logger

__all__ = [
    "CANDIDATE_MODEL_VERSION",
    "COST_PRIORS",
    "FAILURE_NO_BASELINE",
    "METHOD_WEAKNESS_ORDER",
    "REJECTION_METHOD_NOT_RECORDED",
    "REJECTION_OUT_OF_RANGE",
    "UPLIFT_PRIORS",
    "CandidateFigures",
    "CandidateOutcome",
    "CandidateSet",
    "CostPrior",
    "RawFigures",
    "RejectedFigure",
    "build_candidate_set",
    "candidate_figures",
    "run_candidate_estimation",
    "validate_figures",
    "wait_probability",
    "weakest_method",
]

_logger = get_logger(__name__)

_CANDIDATE_ACTOR: Final = "candidate_prior_lookup"

CANDIDATE_MODEL_VERSION: Final[str] = "prior-lookup-1"
"""The lookup's version label. ``prior-lookup`` rather than ``simulator`` for the reason
in the module docstring: the name appears in audit records and on the dashboard, and a
name that overstates what produced a number is the cheapest possible way to mislead."""


# ---------------------------------------------------------------------------
# The configured priors. Every number here is an [ASSUMPTION] placeholder.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CostPrior:
    """The three costs of one action, in integer minor currency units.

    Three separate figures rather than a total, matching the schema, because they answer
    different questions and are owned by different people. ``action_cost`` is what
    performing the action costs the merchant; ``risk_cost`` prices the expected cost of
    it going wrong; ``customer_cost`` prices the intrusion on the customer. A single
    ``cost`` column would make the customer's interest invisible in the arithmetic, and
    a value model that cannot see the customer's interest will spend it.
    """

    action_cost: Minor
    risk_cost: Minor
    customer_cost: Minor


COST_PRIORS: Final[Mapping[CandidateAction, CostPrior]] = {
    CandidateAction.DO_NOTHING: CostPrior(ZERO, ZERO, ZERO),
    CandidateAction.WAIT: CostPrior(ZERO, ZERO, ZERO),
    # A payment link costs nothing to create — the provider charges on success, which is
    # a cost of the recovered revenue rather than of the attempt. The customer cost is
    # the notification it carries. [ASSUMPTION] on the customer figure.
    CandidateAction.PAYMENT_LINK: CostPrior(ZERO, ZERO, Minor(1_000)),
    # A link with provider notification enabled: the same mechanism, priced higher on
    # the customer side because it reaches out unprompted. [ASSUMPTION].
    CandidateAction.CUSTOMER_MESSAGE: CostPrior(ZERO, ZERO, Minor(2_000)),
    # Staff time. The one action whose cost is genuinely an internal one, and the one
    # most likely to be replaced by a real merchant-supplied number. [ASSUMPTION].
    CandidateAction.HUMAN_ESCALATION: CostPrior(Minor(25_000), ZERO, ZERO),
    # The three MVP-unavailable actions. Zero across the board because an action that
    # cannot be performed costs nothing; the reason they are not simply omitted is
    # R6.C9, which retains them in the recorded set so the dashboard can show they were
    # considered.
    CandidateAction.RETRY: CostPrior(ZERO, ZERO, ZERO),
    CandidateAction.DELAYED_RETRY: CostPrior(ZERO, ZERO, ZERO),
    CandidateAction.PAYMENT_METHOD_UPDATE: CostPrior(ZERO, ZERO, ZERO),
    CandidateAction.PROMISE_TO_PAY_FOLLOW_UP: CostPrior(ZERO, ZERO, ZERO),
}
"""Configured cost priors per action, in minor units.

**Every figure is an [ASSUMPTION] placeholder that no measurement supports**, exactly
like the bounds in ``platform.config``, and for the same reason: they were chosen to
make the requirements testable, not because anything measured them. They are constants
here rather than configuration rows only because the configuration catalogue has no
bound for them yet; when it gains one, this mapping becomes its default.

The design's Weak Assumptions list names the customer-annoyance cost as a **RESEARCH
MORE** item specifically because every cost-ratio exclusion in the optimizer depends on
it. That is the honest status of the two customer figures above: they decide real
exclusions and nobody has measured them. ``risk_cost`` is zero throughout rather than
guessed, because a fabricated risk figure would silently suppress actions and a zero one
at least fails visibly in the direction of acting."""

UPLIFT_PRIORS: Final[Mapping[CandidateAction, Decimal]] = {
    CandidateAction.PAYMENT_LINK: Decimal("0.0800"),
    CandidateAction.CUSTOMER_MESSAGE: Decimal("0.0500"),
    CandidateAction.HUMAN_ESCALATION: Decimal("0.1000"),
}
"""How much each executable action is assumed to add to the recovery probability.

**[ASSUMPTION] on all three, and they are the numbers the experiment exists to test.**
They are uniform across risk causes, which is certainly wrong — a payment link should
help an insufficient-funds failure differently from an expired card — and it is
deliberately wrong in the direction of admitting ignorance rather than inventing a
cause-by-action matrix of guesses that would look like knowledge. Every figure derived
from these is marked ``UNCALIBRATED``, which is what carries that admission to the
dashboard.

An action absent from this mapping gets no uplift at all, which is the correct default:
the three MVP-unavailable actions cannot be performed, so claiming they would raise the
probability would be claiming an effect from an act that cannot occur."""


# ---------------------------------------------------------------------------
# Methods, and which of four to record on a row
# ---------------------------------------------------------------------------

METHOD_WEAKNESS_ORDER: Final[tuple[EstimationMethod, ...]] = (
    EstimationMethod.UNCALIBRATED,
    EstimationMethod.PRIOR_FALLBACK,
    EstimationMethod.DETERMINISTIC,
    EstimationMethod.DEFINITIONAL,
)
"""Weakest claim first.

``UNCALIBRATED`` is the weakest: nothing has checked it. ``PRIOR_FALLBACK`` is next: it
is a stated prior applied deliberately. ``DETERMINISTIC`` means fitted from data.
``DEFINITIONAL`` is strongest because it cannot be wrong — a figure fixed at zero by a
requirement is not an estimate at all."""


def weakest_method(*methods: EstimationMethod) -> EstimationMethod:
    """The weakest of several methods.

    R6.C5 wants a method recorded per figure, and ``candidate_estimate`` has one
    ``method`` column. Rather than change a frozen schema, the row records the weakest
    of its four figures and the audit record carries all four individually. The weakest
    is the right summary because a candidate is consumed as a unit: the optimizer
    multiplies the probability by an amount and subtracts all three costs, so the
    resulting net value is only as trustworthy as its least trustworthy input. Recording
    the strongest, or the modal one, would let a ``DEFINITIONAL`` zero cost make an
    ``UNCALIBRATED`` probability look checked.
    """
    for method in METHOD_WEAKNESS_ORDER:
        if method in methods:
            return method
    raise ValueError("no methods supplied")


# ---------------------------------------------------------------------------
# The hazard behind WAIT
# ---------------------------------------------------------------------------

_WORKING_PRECISION: Final[int] = 34
_PROBABILITY_PLACES: Final[Decimal] = Decimal("0.0001")
_ZERO: Final[Decimal] = Decimal(0)
_ONE: Final[Decimal] = Decimal(1)


def wait_probability(
    baseline: Probability, *, remaining: timedelta, window: timedelta
) -> Probability:
    """Recovery probability with no intervention over the time left in the window.

    R6.C10 defines ``WAIT``'s probability as exactly this, and the baseline is the same
    quantity over the *whole* window. Converting between them needs an assumption about
    how recovery is distributed within the window, and this uses a **constant hazard**:
    the chance of recovering in the next hour is the same whichever hour it is.
    Formally, with survival ``S(t) = (1 - p)^(t/T)``, the probability of recovering in
    the remaining fraction ``f`` of the window is ``1 - (1 - p)^f``.

    **[ASSUMPTION]**, and a load-bearing one. Real recovery is almost certainly
    front-loaded — a customer who is going to retry mostly does it soon — which would
    make this *overstate* ``WAIT`` late in a window and therefore understate the value
    of acting late. That direction is the conservative one: it errs toward doing nothing,
    which is the error this system should prefer to make. A front-loaded hazard is what
    the control arm's timing data will let us fit, and until it does, a constant hazard
    is the only shape that does not require inventing a parameter.

    Computed with ``Decimal.ln`` and ``Decimal.exp`` at 34 digits of working precision,
    then rounded once. The whole point is that this stays exact enough to be
    reproducible: two runs of the same case must produce the same ``WAIT`` probability,
    and a binary power would make that depend on the platform's libm.

    Args:
        baseline: recovery probability with no intervention over the whole window.
        remaining: time left in the recovery window. May be negative or zero.
        window: ``Configuration.RECOVERY_WINDOW_DURATION``.

    Returns:
        The probability over the remaining time, at four places. Three boundary cases
        are answered directly rather than through the formula: a fully-elapsed window
        gives zero, because there is no time left in which to recover; a baseline of 1
        gives 1, because the logarithm of zero survival is undefined and the honest
        answer is not in doubt; and a remaining time at or beyond the whole window
        gives the baseline unchanged.
    """
    if remaining <= timedelta(0):
        return Probability(_ZERO)
    if baseline.value >= _ONE:
        return Probability(_ONE)
    if window <= timedelta(0) or remaining >= window:
        return baseline
    if baseline.value <= _ZERO:
        return Probability(_ZERO)

    # Integer microseconds rather than a seconds quotient: timedelta division yields an
    # exact integer ratio here, and nothing on the path from the stored window to the
    # stored probability passes through a binary approximation.
    remaining_us = remaining // timedelta(microseconds=1)
    window_us = window // timedelta(microseconds=1)

    with localcontext() as context:
        context.prec = _WORKING_PRECISION
        fraction = Decimal(remaining_us) / Decimal(window_us)
        survival = ((_ONE - baseline.value).ln() * fraction).exp()
        value = _ONE - survival
    return Probability(value.quantize(_PROBABILITY_PLACES))


# ---------------------------------------------------------------------------
# Figures, validation, and the set
# ---------------------------------------------------------------------------

REJECTION_OUT_OF_RANGE: Final[str] = "OUT_OF_DECLARED_RANGE"
REJECTION_METHOD_NOT_RECORDED: Final[str] = "METHOD_NOT_RECORDED"
"""The two conditions R6.C12 names, as recorded tokens."""


@dataclass(frozen=True, slots=True)
class RawFigures:
    """One action's four figures before they are validated into typed values.

    This intermediate exists so R6.C12 is a real check rather than a formality.
    ``Probability`` refuses an out-of-range value at construction and ``Minor`` is an
    integer, so a validated figure cannot be out of range — which means the validation
    has to happen on something that *can* be. That something is this: the untyped
    product of a configured prior table plus arithmetic, which is exactly where a
    misconfigured cost or a probability pushed past one would come from.
    """

    action: CandidateAction
    intervention_probability: Decimal
    action_cost: int
    risk_cost: int
    customer_cost: int
    probability_method: EstimationMethod | None
    action_cost_method: EstimationMethod | None
    risk_cost_method: EstimationMethod | None
    customer_cost_method: EstimationMethod | None
    availability: ActionAvailability = ActionAvailability.AVAILABLE
    unavailable_reason: str | None = None


@dataclass(frozen=True, slots=True)
class RejectedFigure:
    """A figure that failed validation, named precisely enough to fix.

    ``figure`` is the field name and ``value`` its rendered value, because the question
    asked after an ``INVALID_ESTIMATE`` record appears is always "which one, and what
    was it" — and a record that says only "an estimate was invalid" sends somebody to
    read this file.
    """

    action: CandidateAction
    figure: str
    value: str
    reason: str


@dataclass(frozen=True, slots=True)
class CandidateFigures:
    """One action's validated estimate, with a method per figure.

    Immutable, and every field lands either in a ``candidate_estimate`` column or in the
    audit record. The four per-figure methods are kept separately here even though the
    row stores one summary, so the audit trail can carry what R6.C5 asks for — see
    :func:`weakest_method`.
    """

    action: CandidateAction
    intervention_probability: Probability
    action_cost: Minor
    risk_cost: Minor
    customer_cost: Minor
    probability_method: EstimationMethod
    action_cost_method: EstimationMethod
    risk_cost_method: EstimationMethod
    customer_cost_method: EstimationMethod
    availability: ActionAvailability
    unavailable_reason: str | None

    @property
    def recorded_method(self) -> EstimationMethod:
        """The single method the row stores: the weakest of the four."""
        return weakest_method(
            self.probability_method,
            self.action_cost_method,
            self.risk_cost_method,
            self.customer_cost_method,
        )

    @property
    def total_cost(self) -> Minor:
        """The three costs summed. Integer addition, so exact."""
        return Minor(int(self.action_cost) + int(self.risk_cost) + int(self.customer_cost))

    def method_document(self) -> dict[str, str]:
        """The per-figure methods, for the audit record."""
        return {
            "intervention_probability": self.probability_method.value,
            "action_cost": self.action_cost_method.value,
            "risk_cost": self.risk_cost_method.value,
            "customer_cost": self.customer_cost_method.value,
        }


def validate_figures(raw: RawFigures) -> CandidateFigures | RejectedFigure:
    """Check one action's four figures against their declared ranges and methods.

    R6.C12: a figure outside its declared valid range, or carrying no recorded
    estimation method, marks the candidate ``UNAVAILABLE``, excludes it from selection,
    and produces an ``INVALID_ESTIMATE`` record naming the action and the figure.

    Returns the first failure rather than all of them. One is enough to reject the
    candidate, and a caller that fixes the first will re-run and find the second — while
    a list would invite somebody to render four rejection records for one broken row.

    The probability range is ``[0, 1]`` (R6.C3) and each cost must be a non-negative
    integer in the same minor units as ``payment_amount`` (R6.C7). Booleans are refused
    where an integer is required, because ``True`` is an ``int`` in Python and a cost of
    ``True`` would pass a naive check and store as 1.
    """
    if raw.probability_method is None:
        return RejectedFigure(
            raw.action, "intervention_probability", str(raw.intervention_probability),
            REJECTION_METHOD_NOT_RECORDED,
        )
    if raw.intervention_probability < _ZERO or raw.intervention_probability > _ONE:
        return RejectedFigure(
            raw.action, "intervention_probability", str(raw.intervention_probability),
            REJECTION_OUT_OF_RANGE,
        )
    costs = (
        ("action_cost", raw.action_cost, raw.action_cost_method),
        ("risk_cost", raw.risk_cost, raw.risk_cost_method),
        ("customer_cost", raw.customer_cost, raw.customer_cost_method),
    )
    # The methods are collected as they are checked rather than read off ``raw`` again below.
    # Reading them again would mean the type checker had to trust that this loop returned on every
    # ``None``, which it cannot see; carrying the checked value forward makes the narrowing real.
    checked: dict[str, EstimationMethod] = {}
    for name, value, method in costs:
        if method is None:
            return RejectedFigure(
                raw.action, name, str(value), REJECTION_METHOD_NOT_RECORDED
            )
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return RejectedFigure(raw.action, name, str(value), REJECTION_OUT_OF_RANGE)
        checked[name] = method
    return CandidateFigures(
        action=raw.action,
        intervention_probability=Probability(raw.intervention_probability),
        action_cost=Minor(raw.action_cost),
        risk_cost=Minor(raw.risk_cost),
        customer_cost=Minor(raw.customer_cost),
        probability_method=raw.probability_method,
        action_cost_method=checked["action_cost"],
        risk_cost_method=checked["risk_cost"],
        customer_cost_method=checked["customer_cost"],
        availability=raw.availability,
        unavailable_reason=raw.unavailable_reason,
    )


def candidate_figures(
    action: CandidateAction,
    *,
    baseline: Probability,
    remaining: timedelta,
    window: timedelta,
    memory_available: bool = True,
) -> RawFigures:
    """Produce one action's four raw figures.

    Three shapes, and the differences between them are the substance of Requirement 6.

    ``DO_NOTHING`` is definitional: the probability is the baseline **object**, all three
    costs are zero, and all four methods are ``DEFINITIONAL``. Passing the baseline value
    through unchanged rather than recomputing it is what makes the incremental
    subtraction downstream come out at exactly zero rather than at a rounding artefact.

    ``WAIT`` gets zero action and customer cost by requirement — those two are
    ``DEFINITIONAL`` — and a probability from :func:`wait_probability`, which is derived
    from the baseline posterior and so is ``PRIOR_FALLBACK``, or ``UNCALIBRATED`` when
    the segment could not be read at all.

    Everything else gets its uplift and its costs from the tables above. The probability
    is ``UNCALIBRATED`` because nothing has ever checked the uplift. The costs split:
    ``action_cost`` is ``PRIOR_FALLBACK`` because it is a stated figure a merchant can
    own, while ``risk_cost`` and ``customer_cost`` are ``UNCALIBRATED`` because nothing
    has measured what going wrong costs or what an unsolicited message costs a customer.
    A configured zero is still ``PRIOR_FALLBACK`` and not ``DEFINITIONAL``: zero because
    a table says so is a very different claim from zero because a requirement fixes it.

    Actions in ``UNAVAILABLE_IN_MVP`` are marked ``UNAVAILABLE`` with
    ``PROVIDER_CAPABILITY_UNVERIFIED`` and **retained**. Their probability is set to the
    baseline and their costs to zero, because the truthful statement about an act that
    cannot be performed is that it changes nothing and costs nothing — but the method
    stays ``UNCALIBRATED`` so nothing reads those figures as measured.
    """
    if action is CandidateAction.DO_NOTHING:
        return RawFigures(
            action=action,
            intervention_probability=baseline.value,
            action_cost=int(ZERO),
            risk_cost=int(ZERO),
            customer_cost=int(ZERO),
            probability_method=EstimationMethod.DEFINITIONAL,
            action_cost_method=EstimationMethod.DEFINITIONAL,
            risk_cost_method=EstimationMethod.DEFINITIONAL,
            customer_cost_method=EstimationMethod.DEFINITIONAL,
        )

    costs = COST_PRIORS.get(action, CostPrior(ZERO, ZERO, ZERO))

    if action is CandidateAction.WAIT:
        derived = wait_probability(baseline, remaining=remaining, window=window)
        return RawFigures(
            action=action,
            intervention_probability=derived.value,
            action_cost=int(ZERO),
            risk_cost=int(costs.risk_cost),
            customer_cost=int(ZERO),
            probability_method=(
                EstimationMethod.PRIOR_FALLBACK
                if memory_available
                else EstimationMethod.UNCALIBRATED
            ),
            action_cost_method=EstimationMethod.DEFINITIONAL,
            risk_cost_method=EstimationMethod.PRIOR_FALLBACK,
            customer_cost_method=EstimationMethod.DEFINITIONAL,
        )

    unavailable = action in UNAVAILABLE_IN_MVP
    if unavailable:
        probability = baseline.value
    else:
        uplift = UPLIFT_PRIORS.get(action, _ZERO)
        probability = min(_ONE, baseline.value + uplift)

    cost_method = (
        EstimationMethod.PRIOR_FALLBACK if memory_available else EstimationMethod.UNCALIBRATED
    )
    return RawFigures(
        action=action,
        intervention_probability=probability,
        action_cost=int(costs.action_cost),
        risk_cost=int(costs.risk_cost),
        customer_cost=int(costs.customer_cost),
        probability_method=EstimationMethod.UNCALIBRATED,
        action_cost_method=cost_method,
        risk_cost_method=EstimationMethod.UNCALIBRATED,
        customer_cost_method=EstimationMethod.UNCALIBRATED,
        availability=(
            ActionAvailability.UNAVAILABLE if unavailable else ActionAvailability.AVAILABLE
        ),
        unavailable_reason=(
            ExclusionReason.PROVIDER_CAPABILITY_UNVERIFIED.value if unavailable else None
        ),
    )


@dataclass(frozen=True, slots=True)
class CandidateSet:
    """The full candidate set for one case, plus what was left out and why."""

    figures: tuple[CandidateFigures, ...]
    rejected: tuple[RejectedFigure, ...]
    excluded_by_cause: tuple[CandidateAction, ...]
    memory_available: bool

    @property
    def unavailable(self) -> tuple[CandidateFigures, ...]:
        """Members marked unavailable but retained in the set (R6.C9)."""
        return tuple(
            figure
            for figure in self.figures
            if figure.availability is ActionAvailability.UNAVAILABLE
        )

    def figure_for(self, action: CandidateAction) -> CandidateFigures | None:
        """One member by action, or ``None`` if it is not in the set."""
        for figure in self.figures:
            if figure.action is action:
                return figure
        return None


def build_candidate_set(
    cause: RiskCause,
    *,
    baseline: Probability,
    remaining: timedelta,
    window: timedelta,
    memory_available: bool = True,
) -> CandidateSet:
    """Build the whole candidate set for one case. Pure.

    Membership comes from ``domain.actions.candidate_set_for``, which always yields
    ``DO_NOTHING`` and ``WAIT`` and adds whatever the cause's eligibility row permits
    (R6.C1, R6.C2). A cause with no row — which includes every cause substituted to
    ``UNKNOWN`` — permits nothing beyond the two null actions, so a low-confidence
    diagnosis makes Revora more conservative rather than less.

    Ordered by ``ACTION_PRECEDENCE`` rather than by any figure. Precedence is the
    optimizer's last-resort tie-break, so ordering the set by it means a rendered
    comparison and a tie resolution agree on what "first" means. Ordering by net value
    here would be the repository sorting by a decision it is not making.

    Rejected figures are replaced in the set by a neutral ``UNAVAILABLE`` member rather
    than dropped: R6.C12 excludes the candidate from *selection*, and dropping it from
    the record too would erase the evidence that a broken estimate existed. The
    substitute claims nothing — probability equal to the baseline, all costs zero — so
    it cannot be selected on its merits either.
    """
    members = candidate_set_for(cause)
    ordered = [action for action in ACTION_PRECEDENCE if action in members]
    figures: list[CandidateFigures] = []
    rejected: list[RejectedFigure] = []

    for action in ordered:
        raw = candidate_figures(
            action,
            baseline=baseline,
            remaining=remaining,
            window=window,
            memory_available=memory_available,
        )
        validated = validate_figures(raw)
        if isinstance(validated, RejectedFigure):
            rejected.append(validated)
            figures.append(_neutral_unavailable(action, baseline))
            continue
        figures.append(validated)

    excluded = tuple(
        action
        for action in ACTION_PRECEDENCE
        if action not in members and action not in NULL_ACTIONS
    )
    return CandidateSet(
        figures=tuple(figures),
        rejected=tuple(rejected),
        excluded_by_cause=excluded,
        memory_available=memory_available,
    )


def _neutral_unavailable(
    action: CandidateAction, baseline: Probability
) -> CandidateFigures:
    """The stand-in for a candidate whose figures were rejected.

    Every figure is the neutral one — the baseline probability and zero costs — and the
    availability is ``UNAVAILABLE`` with ``INVALID_ESTIMATE_INPUT``. Constructed rather
    than validated, because it is built from a value that has already passed validation
    (the baseline) and three literal zeros, so re-checking it would only add a path on
    which the stand-in itself could be rejected and leave the set incomplete.
    """
    return CandidateFigures(
        action=action,
        intervention_probability=baseline,
        action_cost=ZERO,
        risk_cost=ZERO,
        customer_cost=ZERO,
        probability_method=EstimationMethod.UNCALIBRATED,
        action_cost_method=EstimationMethod.UNCALIBRATED,
        risk_cost_method=EstimationMethod.UNCALIBRATED,
        customer_cost_method=EstimationMethod.UNCALIBRATED,
        availability=ActionAvailability.UNAVAILABLE,
        unavailable_reason=ExclusionReason.INVALID_ESTIMATE_INPUT.value,
    )


# ---------------------------------------------------------------------------
# The service
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CandidateOutcome:
    """What one candidate-estimation run did.

    ``candidate_estimate_ids`` is ordered to match ``candidates.figures``, so a caller
    that needs the row id for an action zips the two rather than issuing a second read.
    """

    baseline_estimate_id: uuid.UUID | None
    candidates: CandidateSet | None
    candidate_estimate_ids: tuple[uuid.UUID, ...]
    failure_reason: str | None
    already_recorded: bool = False

    @property
    def member_count(self) -> int:
        """How many members the recorded set has. R6.C1 bounds this at 2 to 9."""
        return 0 if self.candidates is None else len(self.candidates.figures)


FAILURE_NO_BASELINE: Final[str] = "NO_BASELINE_FOR_CYCLE"
"""No baseline exists for the cycle, so there is nothing to estimate against.

R6.C1 is triggered *by* the baseline being recorded, so this is an ordering fault in the
job pipeline rather than a data condition. It produces no rows and no candidate set: a
candidate probability is only meaningful as a difference against a baseline, and
building one against an assumed baseline would manufacture the exact false denominator
R5.C11 exists to prevent."""


def run_candidate_estimation(
    session: Session,
    merchant_id: uuid.UUID,
    case_id: uuid.UUID,
    config: Configuration,
    *,
    correlation_id: uuid.UUID | None = None,
) -> CandidateOutcome:
    """Build and persist the candidate set for one case's current decision cycle.

    Must be called inside a transaction; it commits nothing itself. Takes the case row
    under ``FOR UPDATE`` because the audit writer allocates its per-case sequence from a
    counter on that row, and because the lock serializes a concurrent second job onto
    the existing-candidates check rather than onto the unique index underneath it.

    Idempotent under retry: a baseline that already has candidates is left alone and
    reported. The check is keyed on the baseline id because
    ``uq_candidate_estimate_baseline_estimate_id_action`` is what actually prevents a
    second set, and a check keyed on anything else could disagree with it.
    """
    cases = RecoveryCaseRepository(session)
    case = cases.lock_for_update(merchant_id, case_id)
    if case is None:
        _logger.warning("candidate estimation for missing case", case_id=str(case_id))
        return CandidateOutcome(None, None, (), FAILURE_NO_BASELINE)

    moment = now()
    decision_cycle = case.decision_cycle_count
    baseline = BaselineEstimateRepository(session).for_cycle(
        merchant_id, case_id, decision_cycle
    )
    if baseline is None:
        _logger.warning(
            "candidate estimation with no baseline",
            case_id=str(case_id),
            decision_cycle=decision_cycle,
        )
        return CandidateOutcome(None, None, (), FAILURE_NO_BASELINE)

    writer = AuditWriter(
        session,
        disclosure_length=config.MASK_DISCLOSURE_LENGTH,
        max_field_length=config.MAX_AUDIT_FIELD_LENGTH,
    )
    estimates = CandidateEstimateRepository(session)
    if estimates.exists_for_baseline(merchant_id, baseline.id):
        writer.write_for_case(
            merchant_id,
            case_id,
            AuditEntry(
                event_type=CANDIDATE_ESTIMATES_ALREADY_RECORDED,
                actor=_CANDIDATE_ACTOR,
                evidence={
                    "decision_cycle": decision_cycle,
                    "baseline_estimate_id": str(baseline.id),
                },
            ),
            correlation_id=correlation_id,
            occurred_at=moment,
        )
        return CandidateOutcome(baseline.id, None, (), None, already_recorded=True)

    cause = _cause_from_baseline(baseline)
    memory_available = _segment_readable(session, merchant_id, baseline)
    if not memory_available:
        writer.write_for_case(
            merchant_id,
            case_id,
            AuditEntry(
                event_type=CANDIDATE_MEMORY_UNAVAILABLE,
                actor=_CANDIDATE_ACTOR,
                evidence={
                    "decision_cycle": decision_cycle,
                    "baseline_estimate_id": str(baseline.id),
                    "all_estimates_uncalibrated": True,
                    "provider_requests_issued": 0,
                },
            ),
            correlation_id=correlation_id,
            occurred_at=moment,
        )

    candidates = build_candidate_set(
        cause,
        baseline=Probability(baseline.probability),
        remaining=case.window_end_at - moment,
        window=config.RECOVERY_WINDOW_DURATION,
        memory_available=memory_available,
    )

    provenance = Provenance(baseline.provenance)
    created = estimates.insert_all(
        merchant_id,
        rows=[
            {
                "case_id": case_id,
                "baseline_estimate_id": baseline.id,
                "action": figure.action.value,
                "intervention_probability": figure.intervention_probability.value,
                "action_cost": int(figure.action_cost),
                "risk_cost": int(figure.risk_cost),
                "customer_cost": int(figure.customer_cost),
                "method": figure.recorded_method.value,
                "provenance": provenance.value,
                "availability": figure.availability.value,
                "unavailable_reason": figure.unavailable_reason,
                # Null for the same reason the baseline's is: a configured prior table
                # is not a trained artefact and must not be recorded as one.
                "model_version_id": None,
            }
            for figure in candidates.figures
        ],
    )

    _write_candidate_audit(
        writer,
        merchant_id,
        case_id,
        baseline=baseline,
        candidates=candidates,
        cause=cause,
        decision_cycle=decision_cycle,
        provenance=provenance,
        correlation_id=correlation_id,
        moment=moment,
    )

    return CandidateOutcome(
        baseline_estimate_id=baseline.id,
        candidates=candidates,
        candidate_estimate_ids=tuple(row.id for row in created),
        failure_reason=None,
    )


# ---------------------------------------------------------------------------
# internals
# ---------------------------------------------------------------------------


def _cause_from_baseline(baseline: BaselineEstimate) -> RiskCause:
    """The risk cause the candidate set is built from, read off the baseline's features.

    Read from the stored feature document rather than by re-querying the diagnosis, and
    that is a correctness point rather than a saved round trip: the eligibility row and
    the feature segment must be the same cause, or the recorded set would be justified
    by one cause and priced against a baseline computed from another. A re-query could
    return a newer diagnosis if a cycle advanced between the two jobs.

    An unreadable or unrecognized value falls back to ``UNKNOWN``, which permits nothing
    beyond the two null actions. That is the conservative direction: a set we cannot
    justify is a set with nothing customer-visible in it.
    """
    features: Mapping[str, object] = baseline.features or {}
    raw = features.get(FEATURE_RISK_CAUSE)
    if not isinstance(raw, str):
        return RiskCause.UNKNOWN
    try:
        return RiskCause(raw)
    except ValueError:
        return RiskCause.UNKNOWN


def _segment_readable(
    session: Session, merchant_id: uuid.UUID, baseline: BaselineEstimate
) -> bool:
    """Whether the segment's per-action observation counts could be read.

    The counts do not currently change a label — in the MVP every non-definitional
    figure is ``UNCALIBRATED`` whatever they say, because nothing is ever fitted — so
    this call exists for two other reasons. It is the number that tells us when
    calibration becomes possible (R6.C6), and its *failure* is the condition R6.C11
    describes: a memory error degrades every figure to ``UNCALIBRATED``, including
    ``WAIT``'s, and completes without issuing a provider request.

    Wrapped in a savepoint so a database error rolls back to it and leaves the session
    usable for the audit record that has to explain what happened. Without that, the
    failure would poison the transaction and take its own explanation down with it.
    """
    features: Mapping[str, object] = baseline.features or {}
    # Only the five declared feature keys. The stored document also holds the segment
    # level, the counts and the estimator version, and including any of those in a JSONB
    # containment predicate would match nothing at all — which would look exactly like a
    # segment with no observations rather than like the query bug it is.
    subset = {
        key: value
        for key, value in features.items()
        if key in FEATURE_KEYS and isinstance(value, str)
    }
    try:
        with session.begin_nested():
            SegmentObservationRepository(session).action_observation_counts(
                merchant_id, features=subset
            )
    except SQLAlchemyError:
        _logger.warning(
            "candidate segment aggregate unavailable",
            baseline_estimate_id=str(baseline.id),
        )
        return False
    return True


def _write_candidate_audit(
    writer: AuditWriter,
    merchant_id: uuid.UUID,
    case_id: uuid.UUID,
    *,
    baseline: BaselineEstimate,
    candidates: CandidateSet,
    cause: RiskCause,
    decision_cycle: int,
    provenance: Provenance,
    correlation_id: uuid.UUID | None,
    moment: datetime,
) -> None:
    """Record the set, then one record per unavailable action and per rejected figure.

    Separate records rather than flags on one, for the same reason the diagnosis service
    splits its own: each is something a different person goes looking for. The set is
    the explanation a merchant reads; an unavailable action is a product question about
    provider capability; a rejected figure is a configuration bug. One record with three
    lists in it is a record nobody queries.
    """
    writer.write_for_case(
        merchant_id,
        case_id,
        AuditEntry(
            event_type=CANDIDATE_ESTIMATES_RECORDED,
            actor=_CANDIDATE_ACTOR,
            evidence={
                "decision_cycle": decision_cycle,
                "baseline_estimate_id": str(baseline.id),
                "baseline_probability": str(baseline.probability),
                "risk_cause": cause.value,
                "member_count": len(candidates.figures),
                "model_version": CANDIDATE_MODEL_VERSION,
                "provenance": provenance.value,
                "memory_available": candidates.memory_available,
                "provider_requests_issued": 0,
                # R6.C2's recording duty. These are not set members — the requirement
                # caps membership at what the eligibility table permits — so the record
                # is where "considered and ruled out by cause" lives.
                "excluded_by_cause": {
                    action.value: ExclusionReason.CAUSE_NOT_ELIGIBLE.value
                    for action in candidates.excluded_by_cause
                },
                "members": [
                    {
                        "action": figure.action.value,
                        "intervention_probability": str(figure.intervention_probability),
                        "action_cost": int(figure.action_cost),
                        "risk_cost": int(figure.risk_cost),
                        "customer_cost": int(figure.customer_cost),
                        "availability": figure.availability.value,
                        "unavailable_reason": figure.unavailable_reason,
                        "recorded_method": figure.recorded_method.value,
                        "methods": figure.method_document(),
                    }
                    for figure in candidates.figures
                ],
            },
        ),
        correlation_id=correlation_id,
        occurred_at=moment,
    )

    for figure in candidates.unavailable:
        writer.write_for_case(
            merchant_id,
            case_id,
            AuditEntry(
                event_type=CANDIDATE_ACTION_UNAVAILABLE,
                actor=_CANDIDATE_ACTOR,
                action=figure.action.value,
                evidence={
                    "baseline_estimate_id": str(baseline.id),
                    "unavailable_reason": figure.unavailable_reason or "",
                    "retained_in_set": True,
                },
            ),
            correlation_id=correlation_id,
            occurred_at=moment,
        )

    for rejection in candidates.rejected:
        writer.write_for_case(
            merchant_id,
            case_id,
            AuditEntry(
                event_type=INVALID_ESTIMATE,
                actor=_CANDIDATE_ACTOR,
                action=rejection.action.value,
                evidence={
                    "baseline_estimate_id": str(baseline.id),
                    "rejected_figure": rejection.figure,
                    "rejected_value": rejection.value,
                    "rejection_reason": rejection.reason,
                    "excluded_from_selection": True,
                },
            ),
            correlation_id=correlation_id,
            occurred_at=moment,
        )
