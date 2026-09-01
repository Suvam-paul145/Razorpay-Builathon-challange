"""A seeded world where the true lift is known, so Revora's *measurement* can be checked.

The generator's job is not to make Revora look good. It is to build a world whose true causal
effect we wrote down, run Revora's measurement machinery over it, and see whether the machinery
recovers the effect we planted — and, more importantly, whether it correctly refuses to claim an
effect that is not there.

That second half is the point. A measurement system that reports a lift whenever you run it is
worthless, and the only way to detect one is to hand it a world with no effect and check that it
says so. Hence the null scenario, and hence its gating CI.

**The counterfactual pair is the whole trick.** For each case one uniform draw ``u`` is taken and
*both* outcomes are recorded against it: ``recovers_if_untreated = u < p_natural`` and
``recovers_if_treated[a] = u < p_treated[a]``. Using the same ``u`` for both arms is what makes the
true individual-level effect well defined — the case either would have recovered anyway, or
recovers only with treatment, or never recovers — and it makes the true average lift exactly
``mean(p_treated) - mean(p_natural)``. Two independent draws would give the right average and no
individual truth, so per-case checks would be impossible.

**No floats, and no numpy.** The design sketches ``numpy.random.default_rng``; numpy is not
installed and is not worth a dependency for this. ``random.Random(seed)`` from the standard library
is deterministic and stable across Python versions, and every draw here is an *integer* compared
against a probability scaled to :data:`SCALE`. So ``u < p`` is exact integer arithmetic with no
boundary ambiguity — the same discipline the arm assignment uses, for the same reason: a
probability comparison decided by a rounding error is a case assigned to the wrong truth.

**Amounts are drawn from a band mix, not a log-normal.** The design asks for a skewed distribution
"shaped to the band mix". Sampling the band first and then a uniform integer inside it gives exact
control over the mix, produces integer minor units with no rounding step, and needs no
transcendental functions. A log-normal would be more realistic in shape and less controllable in
the only property that matters here — that each amount band is populated.

**Payloads use only verified field names and verified ``error_reason`` values.** The generator
emits real Razorpay-shaped ``payment.failed`` bodies so the harness exercises the actual
canonicalization and failure-taxonomy code rather than a bypass. Every reason string below comes
from ``domain.failure_taxonomy.REASON_TO_CAUSE``, so a payload that fails to map is a bug in the
taxonomy rather than an invented reason.

**Contacts use reserved documentation ranges.** No real PII, ever, and nothing that could reach a
real person if a synthetic case somehow produced an outbound message.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Final

from revora.domain.actions import CandidateAction
from revora.domain.enums import RiskCause
from revora.domain.failure_taxonomy import REASON_TO_CAUSE, ErrorSource, ErrorStep
from revora.domain.money import Minor
from revora.domain.segments import AMOUNT_BAND_BOUNDARIES, AmountBand

__all__ = [
    "GENERATOR_VERSION",
    "SCALE",
    "SCENARIOS",
    "GeneratedCase",
    "GeneratedDataset",
    "GroundTruth",
    "ScenarioName",
    "ScenarioSpec",
    "generate",
    "scenario",
    "true_average_lift",
]

GENERATOR_VERSION: Final[str] = "synthetic-1"
"""Recorded on every ``synthetic_run`` row. A dataset is only reproducible from a seed if the
generator that consumed the seed is also identified — the same seed through different generator
logic is a different world."""

SCALE: Final[int] = 1_000_000
"""Probability resolution for integer comparison. One in a million.

Every probability is carried as an integer count of millionths, and every uniform draw is an
integer in ``[0, SCALE)``. So ``u < p`` is an exact integer comparison. Finer resolution would be
indistinguishable at any case count this generator produces; coarser would make a probability like
``0.8005`` unrepresentable."""

_CONTACT_PREFIX: Final[str] = "+9190000"
"""Reserved documentation range. Combined with a per-case suffix below.

Synthetic cases are barred from reaching the provider by the import contracts, but a contact that
could ring a real phone is the kind of thing that survives a refactor. This one cannot."""

_EMAIL_DOMAIN: Final[str] = "example.invalid"
""".invalid is reserved by RFC 2606 and can never be registered, so a leaked synthetic address
cannot deliver to anyone."""


# ---------------------------------------------------------------------------
# The verified surface the generator draws from
# ---------------------------------------------------------------------------

_REASONS_BY_CAUSE: Final[dict[RiskCause, tuple[str, ...]]] = {}
for _reason, _cause in REASON_TO_CAUSE.items():
    _REASONS_BY_CAUSE.setdefault(_cause, ())
    _REASONS_BY_CAUSE[_cause] = (*_REASONS_BY_CAUSE[_cause], _reason)
"""Verified ``error_reason`` values grouped by the cause they map to.

Derived from the taxonomy rather than listed, so the generator cannot emit a reason the diagnosis
path does not recognise — which would make every generated case diagnose as ``UNKNOWN`` and
quietly destroy the segmentation the scenarios depend on."""

_METHODS_BY_CAUSE: Final[dict[RiskCause, tuple[str, ...]]] = {
    # A card-expiry failure cannot arrive from a UPI payment. Conditioning the method on the cause
    # is what the design asks for, and it is what keeps the segment features coherent — an
    # incoherent combination would land in a segment no real case could occupy.
    RiskCause.EXPIRED_PAYMENT_METHOD: ("card",),
    RiskCause.INSUFFICIENT_FUNDS: ("card", "netbanking", "upi"),
    RiskCause.BANK_OR_NETWORK_FAILURE: ("netbanking", "upi", "card"),
    RiskCause.CUSTOMER_ACTION_REQUIRED: ("card", "upi", "netbanking"),
    RiskCause.ABANDONMENT: ("upi", "netbanking", "card", "wallet"),
    RiskCause.TECHNICAL_ISSUE: ("card", "netbanking", "upi", "wallet"),
    RiskCause.FRAUD_OR_RISK_SIGNAL: ("card", "netbanking"),
    RiskCause.UNKNOWN: ("card", "netbanking", "upi", "wallet"),
}

_SOURCE_BY_CAUSE: Final[dict[RiskCause, ErrorSource]] = {
    RiskCause.EXPIRED_PAYMENT_METHOD: ErrorSource.ISSUER_BANK,
    RiskCause.INSUFFICIENT_FUNDS: ErrorSource.ISSUER_BANK,
    RiskCause.BANK_OR_NETWORK_FAILURE: ErrorSource.ISSUER_BANK,
    RiskCause.CUSTOMER_ACTION_REQUIRED: ErrorSource.CUSTOMER,
    RiskCause.ABANDONMENT: ErrorSource.CUSTOMER,
    RiskCause.TECHNICAL_ISSUE: ErrorSource.GATEWAY,
    RiskCause.FRAUD_OR_RISK_SIGNAL: ErrorSource.BUSINESS,
    RiskCause.UNKNOWN: ErrorSource.GATEWAY,
}

_STEP_BY_CAUSE: Final[dict[RiskCause, ErrorStep]] = {
    RiskCause.EXPIRED_PAYMENT_METHOD: ErrorStep.PAYMENT_AUTHORIZATION,
    RiskCause.INSUFFICIENT_FUNDS: ErrorStep.PAYMENT_AUTHORIZATION,
    RiskCause.BANK_OR_NETWORK_FAILURE: ErrorStep.PAYMENT_AUTHORIZATION,
    RiskCause.CUSTOMER_ACTION_REQUIRED: ErrorStep.PAYMENT_AUTHENTICATION,
    RiskCause.ABANDONMENT: ErrorStep.PAYMENT_AUTHENTICATION,
    RiskCause.TECHNICAL_ISSUE: ErrorStep.PAYMENT_INITIATION,
    RiskCause.FRAUD_OR_RISK_SIGNAL: ErrorStep.PAYMENT_ELIGIBILITY_CHECK,
    RiskCause.UNKNOWN: ErrorStep.PAYMENT_INITIATION,
}

_BAND_RANGES: Final[dict[AmountBand, tuple[int, int]]] = {
    # Minor units, inclusive on both ends. Derived from AMOUNT_BAND_BOUNDARIES rather than
    # hand-written, because the first draft was hand-written and every range but MICRO landed in
    # the wrong band — the boundaries are 25_000 / 500_000 / 5_000_000 and the ranges assumed a
    # tenth of that. A test asserts the derivation, but the derivation is what makes the test pass
    # for a reason rather than by coincidence.
    AmountBand.MICRO: (10_000, int(AMOUNT_BAND_BOUNDARIES[0][0]) - 1),
    AmountBand.SMALL: (int(AMOUNT_BAND_BOUNDARIES[0][0]), int(AMOUNT_BAND_BOUNDARIES[1][0]) - 1),
    AmountBand.MEDIUM: (int(AMOUNT_BAND_BOUNDARIES[1][0]), int(AMOUNT_BAND_BOUNDARIES[2][0]) - 1),
    # LARGE is unbounded above, so its top is a choice. Capped at four times the boundary
    # (₹200,000) to keep the generated amounts plausible for a single one-off payment; an
    # arbitrarily huge amount would dominate every revenue total and make a difference of minor
    # units unreadable next to it.
    AmountBand.LARGE: (
        int(AMOUNT_BAND_BOUNDARIES[2][0]),
        int(AMOUNT_BAND_BOUNDARIES[2][0]) * 4 - 1,
    ),
}
"""The amount range each band is drawn from, in minor units.

MICRO starts at 10 000 rather than at zero so every generated amount clears
``MIN_DETECTION_AMOUNT`` (100 minor units by default). A case below the detection floor would be
one the real pipeline would never have opened, so including it would mean measuring a population
that cannot exist."""


# ---------------------------------------------------------------------------
# Ground truth
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GroundTruth:
    """The true world. Hidden from every Revora component except the comparison reporter.

    ``natural_by_cause`` is the probability a case recovers with no intervention.
    ``uplift_by_action`` is what each action adds to it.

    Both are integer millionths, not ``Decimal`` fractions, so the comparison against a uniform
    draw is exact. Stored on the ``synthetic_run`` row and read by nothing in the decision path —
    the import contracts are what keep that true, and a ground truth that leaked into the decision
    path would make every synthetic result circular.
    """

    natural_by_cause: dict[RiskCause, int]
    uplift_by_action: dict[CandidateAction, int]
    default_natural: int = 200_000

    def natural(self, cause: RiskCause) -> int:
        """The no-intervention probability for a cause, in millionths."""
        return self.natural_by_cause.get(cause, self.default_natural)

    def treated(self, cause: RiskCause, action: CandidateAction) -> int:
        """The treated probability, clipped to ``[0, SCALE]``.

        Clipping rather than raising: an uplift that would push a high baseline past certainty is a
        legitimate scenario (the high-baseline case is exactly that), and the honest representation
        is a probability of one rather than an error.
        """
        return max(0, min(SCALE, self.natural(cause) + self.uplift_by_action.get(action, 0)))

    def as_document(self) -> dict[str, object]:
        """For ``synthetic_run.ground_truth``. Read only by the comparison reporter."""
        return {
            "scale": SCALE,
            "natural_by_cause": {
                cause.value: value for cause, value in sorted(
                    self.natural_by_cause.items(), key=lambda item: item[0].value
                )
            },
            "uplift_by_action": {
                action.value: value for action, value in sorted(
                    self.uplift_by_action.items(), key=lambda item: item[0].value
                )
            },
            "default_natural": self.default_natural,
        }


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------


class ScenarioName:
    """The four mandatory scenarios.

    Constants rather than an enum because they are also used as ``synthetic_run.scenario`` values
    and as CI job names, and a bare string is what those want.
    """

    NULL = "null"
    NEGATIVE = "negative"
    HIGH_BASELINE = "high_baseline"
    POSITIVE = "positive"


@dataclass(frozen=True, slots=True)
class ScenarioSpec:
    """One scenario: its ground truth, its case mix, and what Revora must conclude."""

    name: str
    ground_truth: GroundTruth
    causes: tuple[RiskCause, ...]
    bands: tuple[AmountBand, ...]
    expectation: str

    def as_assumptions(self) -> dict[str, object]:
        """For ``synthetic_run.assumptions``. Everything a reader needs to judge the result."""
        return {
            "scenario": self.name,
            "causes": [cause.value for cause in self.causes],
            "amount_bands": [band.value for band in self.bands],
            "expectation": self.expectation,
            "generator_version": GENERATOR_VERSION,
            "scale": SCALE,
            "note": (
                "Synthetic data establishes that Revora's measurement machinery recovers a known "
                "effect and refuses to claim an absent one. It establishes nothing about real "
                "recovery rates, because the ground truth is ours."
            ),
        }


_ALL_CAUSES: Final[tuple[RiskCause, ...]] = (
    RiskCause.INSUFFICIENT_FUNDS,
    RiskCause.EXPIRED_PAYMENT_METHOD,
    RiskCause.BANK_OR_NETWORK_FAILURE,
    RiskCause.CUSTOMER_ACTION_REQUIRED,
    RiskCause.ABANDONMENT,
    RiskCause.TECHNICAL_ISSUE,
)

_ALL_BANDS: Final[tuple[AmountBand, ...]] = (
    AmountBand.MICRO,
    AmountBand.SMALL,
    AmountBand.MEDIUM,
    AmountBand.LARGE,
)

_MODERATE_NATURAL: Final[dict[RiskCause, int]] = {
    RiskCause.INSUFFICIENT_FUNDS: 250_000,
    RiskCause.EXPIRED_PAYMENT_METHOD: 150_000,
    RiskCause.BANK_OR_NETWORK_FAILURE: 300_000,
    RiskCause.CUSTOMER_ACTION_REQUIRED: 220_000,
    RiskCause.ABANDONMENT: 180_000,
    RiskCause.TECHNICAL_ISSUE: 280_000,
}

SCENARIOS: Final[dict[str, ScenarioSpec]] = {
    ScenarioName.NULL: ScenarioSpec(
        name=ScenarioName.NULL,
        ground_truth=GroundTruth(
            natural_by_cause=dict(_MODERATE_NATURAL),
            # Zero for every action. This is the scenario that catches a broken measurement.
            uplift_by_action=dict.fromkeys(CandidateAction, 0),
        ),
        causes=_ALL_CAUSES,
        bands=_ALL_BANDS,
        expectation=(
            "true lift is exactly zero for every action; Revora must report an interval "
            "containing zero and label the result CAUSALITY_NOT_ESTABLISHED. Reporting a lift "
            "here means the measurement is broken and every other result is untrustworthy."
        ),
    ),
    ScenarioName.NEGATIVE: ScenarioSpec(
        name=ScenarioName.NEGATIVE,
        ground_truth=GroundTruth(
            natural_by_cause=dict(_MODERATE_NATURAL),
            # Acting makes things worse. A real possibility — a badly timed message can push a
            # customer away — and the optimizer must prefer doing nothing.
            uplift_by_action={
                CandidateAction.PAYMENT_LINK: -120_000,
                CandidateAction.CUSTOMER_MESSAGE: -100_000,
                CandidateAction.DO_NOTHING: 0,
                CandidateAction.WAIT: 0,
            },
        ),
        causes=_ALL_CAUSES,
        bands=_ALL_BANDS,
        expectation=(
            "true lift is negative; the reported interval must not lie entirely above zero, so "
            "no attributed claim is permitted, and the sign of the measured lift must be negative."
        ),
    ),
    ScenarioName.HIGH_BASELINE: ScenarioSpec(
        name=ScenarioName.HIGH_BASELINE,
        ground_truth=GroundTruth(
            # Above HIGH_BASELINE_THRESHOLD (0.80). These customers were going to pay anyway.
            natural_by_cause=dict.fromkeys(_ALL_CAUSES, 850000),
            uplift_by_action={
                CandidateAction.PAYMENT_LINK: 20_000,
                CandidateAction.CUSTOMER_MESSAGE: 15_000,
                CandidateAction.DO_NOTHING: 0,
                CandidateAction.WAIT: 0,
            },
            default_natural=850_000,
        ),
        causes=_ALL_CAUSES,
        bands=_ALL_BANDS,
        expectation=(
            "the baseline is above HIGH_BASELINE_THRESHOLD and the uplift is small but positive; "
            "acting costs money for almost no gain, so the correct behaviour is to do nothing. "
            "The customer was going to pay anyway."
        ),
    ),
    ScenarioName.POSITIVE: ScenarioSpec(
        name=ScenarioName.POSITIVE,
        ground_truth=GroundTruth(
            natural_by_cause=dict(_MODERATE_NATURAL),
            uplift_by_action={
                CandidateAction.PAYMENT_LINK: 150_000,
                CandidateAction.CUSTOMER_MESSAGE: 90_000,
                CandidateAction.DO_NOTHING: 0,
                CandidateAction.WAIT: 0,
            },
        ),
        causes=_ALL_CAUSES,
        bands=_ALL_BANDS,
        expectation=(
            "true lift is a known positive 0.15 for PAYMENT_LINK; with enough cases the measured "
            "interval should exclude zero and the measured lift should land close to the true one."
        ),
    ),
}


def scenario(name: str) -> ScenarioSpec:
    """One scenario by name, refusing an unknown one.

    Refusing rather than defaulting: a typo that silently ran the positive scenario in place of the
    null one would turn the CI gate into a test that always passes.
    """
    try:
        return SCENARIOS[name]
    except KeyError:
        raise ValueError(
            f"unknown scenario {name!r}; expected one of {sorted(SCENARIOS)}"
        ) from None


# ---------------------------------------------------------------------------
# Generated cases
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GeneratedCase:
    """One synthetic payment failure, with both of its counterfactual outcomes.

    ``recovers_if_untreated`` and ``recovers_if_treated`` come from one shared uniform draw, so the
    individual causal effect is well defined: a case is a *responder* only when treatment flips it
    from not recovering to recovering.
    """

    provider_event_id: str
    provider_payment_id: str
    amount: Minor
    amount_band: AmountBand
    cause: RiskCause
    error_reason: str
    payment_method: str
    uniform_draw: int
    recovers_if_untreated: bool
    recovers_if_treated: dict[CandidateAction, bool]
    p_natural: int
    p_treated: dict[CandidateAction, int]

    def responds_to(self, action: CandidateAction) -> bool:
        """Whether treatment changes this case's outcome. The individual causal effect.

        Only definable because both arms share one draw. With independent draws this would be a
        comparison of two unrelated coin flips.
        """
        return self.recovers_if_treated.get(action, False) and not self.recovers_if_untreated

    def webhook_payload(self) -> dict[str, object]:
        """A Razorpay-shaped ``payment.failed`` body, using only verified field names.

        Fed through the real canonicalizer by the harness, so a shape error here surfaces as a
        canonicalization failure rather than as silently wrong data. The contact fields use reserved
        ranges that cannot reach a real person.
        """
        suffix = self.provider_payment_id[-5:]
        return {
            "event": "payment.failed",
            "created_at": 1_700_000_000,
            "payload": {
                "payment": {
                    "entity": {
                        "id": self.provider_payment_id,
                        "entity": "payment",
                        "status": "failed",
                        "amount": int(self.amount),
                        "amount_refunded": 0,
                        "captured": False,
                        "currency": "INR",
                        "method": self.payment_method,
                        "created_at": 1_700_000_000,
                        "contact": f"{_CONTACT_PREFIX}{suffix}",
                        "email": f"case-{suffix}@{_EMAIL_DOMAIN}",
                        "error_code": "BAD_REQUEST_ERROR",
                        "error_description": "synthetic failure for evidence harness",
                        "error_reason": self.error_reason,
                        "error_source": _SOURCE_BY_CAUSE[self.cause].value,
                        "error_step": _STEP_BY_CAUSE[self.cause].value,
                    }
                }
            },
        }


@dataclass(frozen=True, slots=True)
class GeneratedDataset:
    """A whole generated world: its cases, its ground truth, and how to reproduce it."""

    scenario: ScenarioSpec
    seed: int
    cases: tuple[GeneratedCase, ...]
    generator_version: str = GENERATOR_VERSION
    ground_truth: GroundTruth = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "ground_truth", self.scenario.ground_truth)

    @property
    def case_count(self) -> int:
        return len(self.cases)

    def true_lift(self, action: CandidateAction) -> Decimal:
        """The true average lift for an action, as a four-place ``Decimal``.

        ``mean(p_treated) - mean(p_natural)`` over the generated cases — exact, because both are
        integers and the division is the only inexact step. This is the number the measured lift is
        compared against, and it is computed from the cases actually generated rather than from the
        ground-truth table, so a scenario whose case mix skews the cause distribution still reports
        its own true lift.
        """
        return true_average_lift(self.cases, action)


def true_average_lift(
    cases: tuple[GeneratedCase, ...] | list[GeneratedCase], action: CandidateAction
) -> Decimal:
    """``mean(p_treated) - mean(p_natural)`` over a set of cases, to four places.

    Zero for an empty set rather than undefined: a lift over no cases is what the null scenario's
    ground truth is, and raising here would make the degenerate case harder to test than the
    interesting one.
    """
    if not cases:
        return Decimal("0.0000")
    total_natural = sum(case.p_natural for case in cases)
    total_treated = sum(case.p_treated.get(action, case.p_natural) for case in cases)
    difference = Decimal(total_treated - total_natural) / Decimal(len(cases)) / Decimal(SCALE)
    return difference.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


def generate(scenario_name: str, *, seed: int, case_count: int) -> GeneratedDataset:
    """Build a reproducible dataset. The same seed and scenario always give the same cases.

    Reproducibility is not a convenience here. A ``synthetic_run`` row stores only the seed, the
    scenario and the generator version, and the claim is that those three reproduce the dataset
    exactly — so a result can be re-derived months later rather than taken on trust.

    Args:
        seed: any integer. Drives ``random.Random``, whose Mersenne Twister is stable across
            Python versions, so a seed recorded today reproduces tomorrow.
        case_count: how many cases to generate. The scenarios need a few hundred per arm before an
            interval is narrow enough to exclude a real effect, and the null scenario needs enough
            that failing to exclude zero is meaningful rather than merely underpowered.
    """
    if case_count < 0:
        raise ValueError(f"case count cannot be negative, got {case_count}")

    spec = scenario(scenario_name)
    rng = random.Random(seed)
    truth = spec.ground_truth
    actions = tuple(CandidateAction)

    cases: list[GeneratedCase] = []
    for index in range(case_count):
        cause = spec.causes[rng.randrange(len(spec.causes))]
        band = spec.bands[rng.randrange(len(spec.bands))]
        low, high = _BAND_RANGES[band]
        amount = Minor(rng.randint(low, high))

        reasons = _REASONS_BY_CAUSE.get(cause, ())
        error_reason = reasons[rng.randrange(len(reasons))] if reasons else "payment_failed"

        methods = _METHODS_BY_CAUSE.get(cause, ("card",))
        method = methods[rng.randrange(len(methods))]

        # One draw per case, shared by both arms. See the module docstring — this is what makes
        # the individual causal effect well defined and the true average lift exact.
        draw = rng.randrange(SCALE)

        p_natural = truth.natural(cause)
        p_treated = {action: truth.treated(cause, action) for action in actions}

        identifier = f"{seed:d}{index:06d}"
        cases.append(
            GeneratedCase(
                provider_event_id=f"synthetic:{seed}:{index}",
                provider_payment_id=f"pay_SYN{identifier[-11:]}",
                amount=amount,
                amount_band=band,
                cause=cause,
                error_reason=error_reason,
                payment_method=method,
                uniform_draw=draw,
                recovers_if_untreated=draw < p_natural,
                recovers_if_treated={
                    action: draw < value for action, value in p_treated.items()
                },
                p_natural=p_natural,
                p_treated=p_treated,
            )
        )

    return GeneratedDataset(scenario=spec, seed=seed, cases=tuple(cases))
