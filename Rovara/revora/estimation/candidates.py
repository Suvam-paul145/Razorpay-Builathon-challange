"""The candidate prior lookup. Not a simulator, and named that way on purpose.

Requirement 6 calls this the ``Intervention_Simulator`` and the design's amendment list
overrules the name: *"Describe and implement as a prior lookup, not a simulator."* The
honest description of what this module does is that it reads five numbers per action out
of a table of configured assumptions, sets some of them by definition where a requirement
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
— and all four costs are exactly zero, with method ``DEFINITIONAL`` on all five
figures (R31.C6). That is what makes the arithmetic downstream come out at exactly zero
incremental probability, exactly zero expected incremental revenue and exactly zero net
value, which is Property 19. If ``DO_NOTHING`` were estimated like anything else, its
incremental value would be noise around zero, and roughly half the time Revora would
find a reason to act purely because the null action's estimate happened to land low.

``WAIT`` has zero financial, communication and customer cost by requirement (R6.C10,
R31.C2 — it reaches nobody), and a
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
    is_customer_visible,
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
from revora.platform.config import Configuration, money_default
from revora.platform.logging import get_logger

__all__ = [
    "CANDIDATE_MODEL_VERSION",
    "COST_PRIORS",
    "FAILURE_NO_BASELINE",
    "HUMAN_ESCALATION_FINANCIAL_COST",
    "MESSAGE_COMMUNICATION_COST",
    "METHOD_WEAKNESS_ORDER",
    "PAYMENT_LINK_FINANCIAL_COST",
    "PROMISE_FOLLOW_UP_COMMUNICATION_COST",
    "PROMISE_FOLLOW_UP_FINANCIAL_COST",
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
    "cost_prior_for",
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
    """The four costs of one action, in integer minor currency units.

    Four separate figures rather than a total, matching the schema, because they answer
    different questions and are owned by different people. ``financial_cost`` is the
    provider fee directly attributable to performing the action; ``communication_cost``
    is what reaching the customer costs per message, and is zero for an action the
    customer never perceives; ``risk_cost`` prices the expected cost of it going wrong;
    ``customer_cost`` prices the intrusion on the customer. A single ``cost`` column
    would make the customer's interest invisible in the arithmetic, and a value model
    that cannot see the customer's interest will spend it.

    The first two were one blended ``action_cost`` until R31.C1 split them, and the
    reason for the split is attribution rather than accuracy: blended, a candidate
    excluded under ``MAX_COST_TO_VALUE_RATIO`` because links are expensive and one
    excluded because messages are expensive present identically, so a merchant cannot
    tell a fee they could renegotiate from a channel they could change. Split, the two
    are only ever *consumed* as a sum (see the optimizer), which is why the split
    changes what a reader can attribute and nothing about what gets selected.
    """

    financial_cost: Minor
    communication_cost: Minor
    risk_cost: Minor
    customer_cost: Minor


PAYMENT_LINK_FINANCIAL_COST: Final[Minor] = money_default("PAYMENT_LINK_FINANCIAL_COST")
"""The provider fee attributable to creating one payment link. **[ASSUMPTION]**.

**Not a constant in this module.** R31.C11 makes it a versioned ``app_config`` row, so
the number itself lives in ``platform.config``'s catalogue as that bound's default,
migration ``0009`` seeds the row from the same declaration, and this name binds to the
default rather than restating it. Writing ``300`` here as well is the specific failure
the arrangement rules out: two places to edit, one of which silently wins.

What this name *is*, therefore, is the fallback — the value the prior table carries when
no row has been read. :func:`cost_prior_for` is what the estimator actually prices
through, and it reads the row off the ``Configuration`` rather than this name."""

MESSAGE_COMMUNICATION_COST: Final[Minor] = money_default("MESSAGE_COMMUNICATION_COST")
"""Per-message delivery cost of one customer-visible action. **[ASSUMPTION]**.

Also an R31.C11 configuration row, bound from the catalogue's default for the same
reason, and the fallback on the same terms."""

PROMISE_FOLLOW_UP_FINANCIAL_COST: Final[Minor] = ZERO
"""Provider fee of following up on a promise: **zero, and verified rather than assumed**.

The endpoint is ``POST /v1/payment_links/:id/notify_by/:medium``, which re-notifies
against the payment link already recorded for the case and **creates no second link**.
No new link means no new link fee, so this zero is a fact about the provider's API
rather than a placeholder — the only figure in this table that is not an
**[ASSUMPTION]**. If that endpoint ever stopped existing, a follow-up would have to
mint a fresh link and this figure would become ``PAYMENT_LINK_FINANCIAL_COST``."""

PROMISE_FOLLOW_UP_COMMUNICATION_COST: Final[Minor] = Minor(50)
"""Per-message delivery cost of a promise follow-up. **[ASSUMPTION]**.

Higher than ``MESSAGE_COMMUNICATION_COST`` because a follow-up is priced against the
provider's re-notification rather than against the notification bundled with a new
link. Nothing has measured either, so the ordering between them is a guess too."""

HUMAN_ESCALATION_FINANCIAL_COST: Final[Minor] = Minor(25_000)
"""Staff time for one escalation. **[ASSUMPTION]**.

Not a provider fee, and the one figure here whose cost is genuinely internal — which
also makes it the one most likely to be replaced by a real merchant-supplied number."""


COST_PRIORS: Final[Mapping[CandidateAction, CostPrior]] = {
    CandidateAction.DO_NOTHING: CostPrior(ZERO, ZERO, ZERO, ZERO),
    CandidateAction.WAIT: CostPrior(ZERO, ZERO, ZERO, ZERO),
    # A payment link carries a creation fee and the notification it sends. The customer
    # cost prices the intrusion of that notification. [ASSUMPTION] on all three
    # non-zero figures.
    CandidateAction.PAYMENT_LINK: CostPrior(
        PAYMENT_LINK_FINANCIAL_COST, MESSAGE_COMMUNICATION_COST, ZERO, Minor(1_000)
    ),
    # A link with provider notification enabled: the same mechanism and therefore the
    # same two cost figures, priced higher on the customer side because it reaches out
    # unprompted. [ASSUMPTION].
    CandidateAction.CUSTOMER_MESSAGE: CostPrior(
        PAYMENT_LINK_FINANCIAL_COST, MESSAGE_COMMUNICATION_COST, ZERO, Minor(2_000)
    ),
    # The one action with a verified-zero financial term. See
    # PROMISE_FOLLOW_UP_FINANCIAL_COST: the resend creates no second link.
    CandidateAction.PROMISE_TO_PAY_FOLLOW_UP: CostPrior(
        PROMISE_FOLLOW_UP_FINANCIAL_COST,
        PROMISE_FOLLOW_UP_COMMUNICATION_COST,
        ZERO,
        Minor(2_000),
    ),
    # Staff time, and zero communication cost because an escalation is not
    # customer-visible — R31.C2 fixes that term at zero rather than leaving it guessed.
    CandidateAction.HUMAN_ESCALATION: CostPrior(
        HUMAN_ESCALATION_FINANCIAL_COST, ZERO, ZERO, ZERO
    ),
    # The three MVP-unavailable actions. Zero across the board because an action that
    # cannot be performed costs nothing; the reason they are not simply omitted is
    # R6.C9, which retains them in the recorded set so the dashboard can show they were
    # considered.
    CandidateAction.RETRY: CostPrior(ZERO, ZERO, ZERO, ZERO),
    CandidateAction.DELAYED_RETRY: CostPrior(ZERO, ZERO, ZERO, ZERO),
    CandidateAction.PAYMENT_METHOD_UPDATE: CostPrior(ZERO, ZERO, ZERO, ZERO),
}
"""Configured cost priors per action, in minor units.

**Every figure here is an [ASSUMPTION] placeholder that no measurement supports** —
with the single exception of ``PROMISE_TO_PAY_FOLLOW_UP``'s financial term, which is a
verified zero and says so at its own declaration. Otherwise these are exactly like the
bounds in ``platform.config``, and two of them now literally are: under R31.C11
``PAYMENT_LINK_FINANCIAL_COST`` and ``MESSAGE_COMMUNICATION_COST`` are versioned
``app_config`` rows, declared in that catalogue, seeded from it by migration ``0009``,
and read back here through ``money_default``. So this mapping is what those rows *default
to* — the fallback — and not a second source of truth: the two numbers appear once, in
the catalogue.

**Nothing prices an action from this mapping directly.** :func:`cost_prior_for` is the
only reader, and for the two R31.C11 actions it takes the financial and communication
terms off the ``Configuration`` — so changing the row changes what gets priced, which is
what makes the recorded configuration change naming an approving user have a consequence.
The entries below are what those two terms fall back to when no row has been read, and
the sole source of the other seven actions' figures.

The design's Weak Assumptions list names the customer-annoyance cost as a **RESEARCH
MORE** item specifically because every cost-ratio exclusion in the optimizer depends on
it. That is the honest status of the three customer figures above: they decide real
exclusions and nobody has measured them. ``risk_cost`` is zero throughout rather than
guessed, because a fabricated risk figure would silently suppress actions and a zero one
at least fails visibly in the direction of acting."""

_CONFIGURED_COST_ACTIONS: Final[frozenset[CandidateAction]] = frozenset(
    {CandidateAction.PAYMENT_LINK, CandidateAction.CUSTOMER_MESSAGE}
)
"""The actions whose financial and communication terms come from a configuration row.

Exactly the two R31.C11 names, and no more. ``PROMISE_TO_PAY_FOLLOW_UP``'s financial term
is a *verified* zero rather than a tunable figure — see
:data:`PROMISE_FOLLOW_UP_FINANCIAL_COST` — so exposing it as a row would invite somebody
to tune away a fact about the provider's API. The other three non-zero figures
(``PROMISE_FOLLOW_UP_COMMUNICATION_COST``, ``HUMAN_ESCALATION_FINANCIAL_COST``, and the
customer costs) stay in the table because no requirement has asked for them to be
changeable, and a bound nobody needs is still a bound somebody can get wrong."""


def cost_prior_for(action: CandidateAction, config: Configuration) -> CostPrior:
    """The four cost priors of one action, with the configured rows taking precedence.

    R31.C11 requires ``PAYMENT_LINK_FINANCIAL_COST`` and ``MESSAGE_COMMUNICATION_COST`` to
    be versioned configuration rows changeable only through a recorded change naming an
    approving user. Seeding the rows is not enough on its own: an estimator that read the
    module constant would price identically whatever the row said, and the recorded change
    would have no consequence. So the two terms are read from ``config`` here, and this is
    the only function that reads :data:`COST_PRIORS`.

    Only the two named terms are overridden, and only for the two actions R31.C11 names.
    ``risk_cost`` and ``customer_cost`` come from the table unchanged even for those
    actions, because those figures are not configured and substituting a configured one
    would make a cost-ratio exclusion attributable to a row nobody set.

    The value is not re-validated here. A negative or otherwise impossible configured cost
    reaches :func:`validate_figures` like any other figure and is rejected there, naming
    the term — which is where R6.C12's "outside its declared valid range" check belongs,
    and means a misconfigured row produces an ``INVALID_ESTIMATE`` record rather than a
    silently clamped price.
    """
    prior = COST_PRIORS.get(action, CostPrior(ZERO, ZERO, ZERO, ZERO))
    if action not in _CONFIGURED_COST_ACTIONS:
        return prior
    return CostPrior(
        financial_cost=config.PAYMENT_LINK_FINANCIAL_COST,
        communication_cost=config.MESSAGE_COMMUNICATION_COST,
        risk_cost=prior.risk_cost,
        customer_cost=prior.customer_cost,
    )


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
    EstimationMethod.COST_SPLIT_NOT_MEASURED,
    EstimationMethod.UNCALIBRATED,
    EstimationMethod.PRIOR_FALLBACK,
    EstimationMethod.DETERMINISTIC,
    EstimationMethod.DEFINITIONAL,
)
"""Weakest claim first.

``COST_SPLIT_NOT_MEASURED`` is the weakest, ahead of ``UNCALIBRATED``, and the position
is the whole point of adding it. ``UNCALIBRATED`` means somebody stated a prior that
nothing has checked; ``COST_SPLIT_NOT_MEASURED`` means **nothing measured the figure and
nothing guessed it either** — migration ``0008`` put a blended total into
``financial_cost`` and left ``communication_cost`` at zero. That is strictly less of a
claim than an unchecked guess, so it has to sort first: put it anywhere above
``UNCALIBRATED`` and a migrated row's summary method would make a genuinely estimated
probability look more checked than it is.

``UNCALIBRATED`` is next: nothing has checked it. ``PRIOR_FALLBACK`` follows: it is a
stated prior applied deliberately. ``DETERMINISTIC`` means fitted from data.
``DEFINITIONAL`` is strongest because it cannot be wrong — a figure fixed at zero by a
requirement is not an estimate at all.

No estimator produces ``COST_SPLIT_NOT_MEASURED``, so nothing this module writes will
ever select it. It is here because :func:`weakest_method` is also the summary applied to
rows read back out of the database, and an ordering that omitted a persisted label would
raise on a historical row rather than rank it."""


def weakest_method(*methods: EstimationMethod) -> EstimationMethod:
    """The weakest of several methods.

    R6.C5 wants a method recorded per figure, and ``candidate_estimate`` has one
    ``method`` column. Rather than change a frozen schema, the row records the weakest
    of its five figures and the audit record carries all five individually. The weakest
    is the right summary because a candidate is consumed as a unit: the optimizer
    multiplies the probability by an amount and subtracts all four costs, so the
    resulting net value is only as trustworthy as its least trustworthy input. Recording
    the strongest, or the modal one, would let a ``DEFINITIONAL`` zero cost make an
    ``UNCALIBRATED`` probability look checked.

    Splitting the action term into two (R31.C1) adds a fifth figure and changes nothing
    here in principle: a fifth input to a minimum is still a minimum.
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
    """One action's five figures before they are validated into typed values.

    This intermediate exists so R6.C12 is a real check rather than a formality.
    ``Probability`` refuses an out-of-range value at construction and ``Minor`` is an
    integer, so a validated figure cannot be out of range — which means the validation
    has to happen on something that *can* be. That something is this: the untyped
    product of a configured prior table plus arithmetic, which is exactly where a
    misconfigured cost or a probability pushed past one would come from.
    """

    action: CandidateAction
    intervention_probability: Decimal
    financial_cost: int
    communication_cost: int
    risk_cost: int
    customer_cost: int
    probability_method: EstimationMethod | None
    financial_cost_method: EstimationMethod | None
    communication_cost_method: EstimationMethod | None
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
    audit record. The five per-figure methods are kept separately here even though the
    row stores one summary, so the audit trail can carry what R6.C5 asks for — see
    :func:`weakest_method`. Two of the five now have columns of their own
    (``financial_cost_method``, ``communication_cost_method``) because R31.C10 needs the
    split's provenance readable beside the split's figures, which a row-level summary
    cannot express.
    """

    action: CandidateAction
    intervention_probability: Probability
    financial_cost: Minor
    communication_cost: Minor
    risk_cost: Minor
    customer_cost: Minor
    probability_method: EstimationMethod
    financial_cost_method: EstimationMethod
    communication_cost_method: EstimationMethod
    risk_cost_method: EstimationMethod
    customer_cost_method: EstimationMethod
    availability: ActionAvailability
    unavailable_reason: str | None

    @property
    def recorded_method(self) -> EstimationMethod:
        """The single method the row stores: the weakest of the five.

        Still the weakest over *all* of them rather than over the four that are not the
        split, because a candidate is consumed as a unit — the optimizer subtracts every
        cost term from one net value, so a weak term weakens the whole row.
        """
        return weakest_method(
            self.probability_method,
            self.financial_cost_method,
            self.communication_cost_method,
            self.risk_cost_method,
            self.customer_cost_method,
        )

    @property
    def total_cost(self) -> Minor:
        """The four costs summed. Integer addition, so exact."""
        return Minor(
            int(self.financial_cost)
            + int(self.communication_cost)
            + int(self.risk_cost)
            + int(self.customer_cost)
        )

    def method_document(self) -> dict[str, str]:
        """The per-figure methods, for the audit record."""
        return {
            "intervention_probability": self.probability_method.value,
            "financial_cost": self.financial_cost_method.value,
            "communication_cost": self.communication_cost_method.value,
            "risk_cost": self.risk_cost_method.value,
            "customer_cost": self.customer_cost_method.value,
        }


def validate_figures(raw: RawFigures) -> CandidateFigures | RejectedFigure:
    """Check one action's five figures against their declared ranges and methods.

    R6.C12: a figure outside its declared valid range, or carrying no recorded
    estimation method, marks the candidate ``UNAVAILABLE``, excludes it from selection,
    and produces an ``INVALID_ESTIMATE`` record naming the action and the figure.

    Returns the first failure rather than all of them. One is enough to reject the
    candidate, and a caller that fixes the first will re-run and find the second — while
    a list would invite somebody to render five rejection records for one broken row.

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
        ("financial_cost", raw.financial_cost, raw.financial_cost_method),
        ("communication_cost", raw.communication_cost, raw.communication_cost_method),
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
        financial_cost=Minor(raw.financial_cost),
        communication_cost=Minor(raw.communication_cost),
        risk_cost=Minor(raw.risk_cost),
        customer_cost=Minor(raw.customer_cost),
        probability_method=raw.probability_method,
        financial_cost_method=checked["financial_cost"],
        communication_cost_method=checked["communication_cost"],
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
    config: Configuration,
    memory_available: bool = True,
) -> RawFigures:
    """Produce one action's five raw figures.

    Three shapes, and the differences between them are the substance of Requirement 6.

    ``DO_NOTHING`` is definitional: the probability is the baseline **object**, all four
    costs are zero, and all five methods are ``DEFINITIONAL`` (R31.C6). Passing the
    baseline value through unchanged rather than recomputing it is what makes the
    incremental subtraction downstream come out at exactly zero rather than at a
    rounding artefact.

    ``WAIT`` gets zero financial, communication and customer cost by requirement — those
    three are ``DEFINITIONAL`` — and a probability from :func:`wait_probability`, which
    is derived from the baseline posterior and so is ``PRIOR_FALLBACK``, or
    ``UNCALIBRATED`` when the segment could not be read at all.

    Everything else gets its uplift and its costs from the tables above. The probability
    is ``UNCALIBRATED`` because nothing has ever checked the uplift. The costs split:
    ``financial_cost`` is ``PRIOR_FALLBACK`` because it is a stated provider fee a
    merchant can own, and ``communication_cost`` is ``PRIOR_FALLBACK`` for the same
    reason **when the action reaches the customer at all** — while ``risk_cost`` and
    ``customer_cost`` are ``UNCALIBRATED`` because nothing has measured what going wrong
    costs or what an unsolicited message costs a customer. A configured zero is still
    ``PRIOR_FALLBACK`` and not ``DEFINITIONAL``: zero because a table says so is a very
    different claim from zero because a requirement fixes it.

    The one exception to that last rule is the communication term of an action the
    customer never perceives. R31.C2 *fixes* it at zero rather than configuring it, so
    it is ``DEFINITIONAL`` — an escalation's zero messaging cost cannot be wrong, and
    labelling it ``PRIOR_FALLBACK`` would invite somebody to tune it.

    Actions in ``UNAVAILABLE_IN_MVP`` are marked ``UNAVAILABLE`` with
    ``PROVIDER_CAPABILITY_UNVERIFIED`` and **retained**. Their probability is set to the
    baseline, because the truthful statement about an act that cannot be performed is
    that it changes nothing — but the method stays ``UNCALIBRATED`` so nothing reads
    those figures as measured.

    ``config`` is required rather than defaulted, and that is R31.C11 rather than style:
    the two configured cost rows are resolved through :func:`cost_prior_for`, and a
    default here would be a second silent source of the two figures that a caller could
    reach by forgetting an argument.
    """
    if action is CandidateAction.DO_NOTHING:
        return RawFigures(
            action=action,
            intervention_probability=baseline.value,
            financial_cost=int(ZERO),
            communication_cost=int(ZERO),
            risk_cost=int(ZERO),
            customer_cost=int(ZERO),
            probability_method=EstimationMethod.DEFINITIONAL,
            financial_cost_method=EstimationMethod.DEFINITIONAL,
            communication_cost_method=EstimationMethod.DEFINITIONAL,
            risk_cost_method=EstimationMethod.DEFINITIONAL,
            customer_cost_method=EstimationMethod.DEFINITIONAL,
        )

    costs = cost_prior_for(action, config)

    if action is CandidateAction.WAIT:
        derived = wait_probability(baseline, remaining=remaining, window=window)
        return RawFigures(
            action=action,
            intervention_probability=derived.value,
            financial_cost=int(ZERO),
            communication_cost=int(ZERO),
            risk_cost=int(costs.risk_cost),
            customer_cost=int(ZERO),
            probability_method=(
                EstimationMethod.PRIOR_FALLBACK
                if memory_available
                else EstimationMethod.UNCALIBRATED
            ),
            financial_cost_method=EstimationMethod.DEFINITIONAL,
            communication_cost_method=EstimationMethod.DEFINITIONAL,
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
        financial_cost=int(costs.financial_cost),
        communication_cost=int(costs.communication_cost),
        risk_cost=int(costs.risk_cost),
        customer_cost=int(costs.customer_cost),
        probability_method=EstimationMethod.UNCALIBRATED,
        financial_cost_method=cost_method,
        communication_cost_method=(
            cost_method
            if is_customer_visible(action)
            else EstimationMethod.DEFINITIONAL
        ),
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
    config: Configuration,
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

    Still pure, and ``config`` does not change that: it is a frozen value the caller
    already holds, read for the two R31.C11 cost rows and for nothing else. Two calls
    with equal arguments still return equal sets, which is what the neutrality
    properties rest on.
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
            config=config,
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
    (the baseline) and four literal zeros, so re-checking it would only add a path on
    which the stand-in itself could be rejected and leave the set incomplete.

    Every method is ``UNCALIBRATED`` and none is ``COST_SPLIT_NOT_MEASURED``, even though
    the two split terms here were genuinely never measured. That label means one specific
    thing — a row migration ``0008`` rewrote — and reusing it for a live rejection would
    make a configuration bug indistinguishable from a historical row.
    """
    return CandidateFigures(
        action=action,
        intervention_probability=baseline,
        financial_cost=ZERO,
        communication_cost=ZERO,
        risk_cost=ZERO,
        customer_cost=ZERO,
        probability_method=EstimationMethod.UNCALIBRATED,
        financial_cost_method=EstimationMethod.UNCALIBRATED,
        communication_cost_method=EstimationMethod.UNCALIBRATED,
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
        config=config,
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
                "financial_cost": int(figure.financial_cost),
                "communication_cost": int(figure.communication_cost),
                "risk_cost": int(figure.risk_cost),
                "customer_cost": int(figure.customer_cost),
                # R31.C5 leaves neither label unset. The columns are nullable because
                # migration 0008 had no correct value to guess for historical rows, not
                # because a new estimate may omit them.
                "financial_cost_method": figure.financial_cost_method.value,
                "communication_cost_method": figure.communication_cost_method.value,
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
                        "financial_cost": int(figure.financial_cost),
                        "communication_cost": int(figure.communication_cost),
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
