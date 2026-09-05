"""The evidence harness: run Revora's measurement over a known world and compare.

What this establishes, stated before anything else because it is the thing most easily overclaimed:

**It establishes that Revora's measurement machinery recovers an effect we planted, and correctly
refuses to claim one we did not plant. It establishes nothing whatsoever about real recovery rates
or real uplift**, because those live in a ground-truth table we wrote. Any claim of the form "Revora
recovers X% more revenue" derived from synthetic data would be circular. The narrative this supports
is "here is a system whose measurement you can trust", never "here is a system that recovers X%".

**What the harness actually exercises**, so nobody has to guess:

* the real canonicalizer, on real Razorpay-shaped payloads — a generated payload that will not
  canonicalize fails here rather than silently producing a case with no features;
* the real failure taxonomy, mapping ``error_reason`` to a ``RiskCause``;
* the real arm assignment, including its determinism;
* the real experiment analysis — lift, interval, four-way comparison, and the attribution gate.

**What it deliberately does not exercise**: HMAC verification, the HTTP route, the job queue and the
provider client. Those are transport and orchestration, covered by their own tests, and driving them
here would add minutes per scenario without touching the claim under test. Cases and outcomes are
written directly, and the write path is the same repository code the pipeline uses.

**The ground truth is read in exactly one place** — :func:`compare_to_truth`, at the end. Nothing
that decides anything reads it. That is enforced by the import contracts rather than by care: if a
decision component could see the true uplift, every synthetic result would be circular and the
harness would be an elaborate way of confirming what we typed in.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import text

from revora.domain.actions import CandidateAction
from revora.domain.enums import (
    CaseState,
    ExperimentGroup,
    ExperimentLabel,
    ExperimentState,
    OutcomeClass,
    Provenance,
    RiskCause,
)
from revora.experiment.analysis import ExperimentAnalysis, analyse_experiment
from revora.experiment.assignment import AllocationRatio, assign_group
from revora.ingestion.canonical import CanonicalizationError, canonicalize
from revora.persistence.models import SyntheticRun
from revora.persistence.repositories.experiments import (
    ExperimentAssignmentRepository,
    ExperimentRepository,
)
from revora.platform.clock import now
from revora.platform.logging import get_logger
from revora.synthetic.generator import (
    GeneratedCase,
    GeneratedDataset,
    generate,
    true_average_lift,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

    from sqlalchemy.orm import Session

    from revora.platform.config import Configuration

__all__ = [
    "TREATED_ACTION",
    "ComparisonReport",
    "HarnessResult",
    "compare_to_truth",
    "persist_run",
    "run_scenario",
    "seed_cases",
    "synthetic_uuid",
]

_logger = get_logger(__name__)

_ALLOCATION = AllocationRatio(control=1, treatment=1)
"""An even split. The harness is measuring measurement, and an even split minimises the interval
width for a given case count — which is the cheapest way to give the null scenario enough power for
"failed to exclude zero" to mean something other than "too few cases"."""

TREATED_ACTION = CandidateAction.PAYMENT_LINK
"""The action the treatment arm takes.

One action, not a mix, because the harness is testing measurement rather than action selection. A
mixed treatment arm would have a true lift that is an average over an action distribution the
optimizer chose, so a discrepancy between measured and true could be either a measurement error or
a selection difference — and the whole point is to be able to tell those apart."""

_ID_NAMESPACE = uuid.UUID("6f1d5b2a-0c4e-5a71-9b3d-8e2f4a6c1d70")
"""Namespace for the ids a run creates. What this fixes, and what it deliberately does not.

**The gap it closes.** The generated *dataset* was always a function of the seed, but the case ids
and the experiment id were ``uuid4`` — and the arm assignment is a digest over exactly those two
ids. So re-running a stored ``synthetic_run`` reproduced the same world and then split it into
*different* arms, which means the stored measured lift could not be re-derived. A result nobody can
re-derive is a result nobody can disagree with.

Ids are now ``uuid5`` over merchant, scenario, seed and index, so a run reproduces exactly **for the
merchant it was run against**. The merchant is in the key on purpose: ``recovery_case.id`` and
``experiment.id`` are globally unique, so dropping it would make two merchants on the same seed
collide on a primary key, and would make re-running the whole test suite against a persisted
database fail on the first insert.

**What it does not fix, and must not be mistaken for fixing.** A fresh merchant is a fresh
randomization. That is not a defect — it is what a randomized experiment *is* — but it means a
scenario test cannot be made deterministic by pinning a seed, and a database-level fix is barred
because the audit trail is append-only by design, so a run's rows cannot be cleaned and replayed.
The consequence for the tests is structural: every statistical assertion has to hold for *any* valid
randomization, not for a lucky one. That is why the positive scenario runs at a case count where
detecting the effect is a five-sigma event rather than a two-sigma one, and why the null gate reads
many independent randomizations instead of trusting a single interval to land the 95 percent of the
time it is designed to."""


def synthetic_uuid(
    merchant_id: uuid.UUID, scenario_name: str, seed: int, kind: str, index: int = 0
) -> uuid.UUID:
    """A stable id for one object in one run, so the whole run reproduces from the seed."""
    return uuid.uuid5(_ID_NAMESPACE, f"{merchant_id}:{scenario_name}:{seed}:{kind}:{index}")


@dataclass(frozen=True, slots=True)
class ComparisonReport:
    """Measured against true, with everything a reader needs to judge it (R13.C12).

    The design requires assumptions, the embedded true lift, the seed and the difference between
    measured and true to be reported alongside every synthetic result. All five are here, and
    ``as_document`` renders them together — a measured lift shown without its true counterpart is a
    number that looks like a finding.
    """

    scenario: str
    seed: int
    generator_version: str
    case_count: int
    true_lift: Decimal
    measured_lift: Decimal | None
    measured_ci_low: Decimal | None
    measured_ci_high: Decimal | None
    attribution_permitted: bool
    refusal_codes: tuple[str, ...]
    assumptions: dict[str, object]

    @property
    def difference(self) -> Decimal | None:
        """Measured minus true. ``None`` when nothing was measured.

        ``None`` rather than treating an absent measurement as zero — an unmeasurable arm and a
        measurement of exactly no difference are not the same thing, and this is the field a reader
        would quote as "how wrong was it".
        """
        if self.measured_lift is None:
            return None
        return self.measured_lift - self.true_lift

    @property
    def interval_contains_true_lift(self) -> bool:
        """Whether the reported interval covers the true lift. The coverage check's unit.

        Over many seeds this should be true about as often as the confidence level says. Materially
        less often means the interval is too narrow and every causal claim built on it is
        overconfident.
        """
        if self.measured_ci_low is None or self.measured_ci_high is None:
            return False
        return self.measured_ci_low <= self.true_lift <= self.measured_ci_high

    @property
    def interval_contains_zero(self) -> bool:
        if self.measured_ci_low is None or self.measured_ci_high is None:
            return False
        return self.measured_ci_low <= 0 <= self.measured_ci_high

    def as_document(self) -> dict[str, object]:
        """Everything together, because a measured lift alone reads as a finding."""
        return {
            "scenario": self.scenario,
            "seed": self.seed,
            "generator_version": self.generator_version,
            "case_count": self.case_count,
            "true_lift": str(self.true_lift),
            "measured_lift": None if self.measured_lift is None else str(self.measured_lift),
            "measured_interval": (
                None
                if self.measured_ci_low is None
                else f"[{self.measured_ci_low}, {self.measured_ci_high}]"
            ),
            "difference_measured_minus_true": (
                None if self.difference is None else str(self.difference)
            ),
            "interval_contains_true_lift": self.interval_contains_true_lift,
            "interval_contains_zero": self.interval_contains_zero,
            "attribution_permitted": self.attribution_permitted,
            "refusal_codes": list(self.refusal_codes),
            "assumptions": self.assumptions,
            "provenance": Provenance.SYNTHETIC.value,
            "warning": (
                "SYNTHETIC. This establishes that the measurement recovers a known effect and "
                "refuses an absent one. It says nothing about real recovery rates."
            ),
        }


@dataclass(frozen=True, slots=True)
class HarnessResult:
    """One scenario run end to end."""

    synthetic_run_id: uuid.UUID
    experiment_id: uuid.UUID
    dataset: GeneratedDataset
    analysis: ExperimentAnalysis | None
    report: ComparisonReport
    control_case_ids: tuple[uuid.UUID, ...]
    treatment_case_ids: tuple[uuid.UUID, ...]


def persist_run(
    session: Session, merchant_id: uuid.UUID, dataset: GeneratedDataset
) -> uuid.UUID:
    """Record the run, so the dataset is reproducible from three stored values.

    Seed, scenario and generator version are what reproduce the world; assumptions and ground truth
    are what let a reader judge the result. Stored together because a result whose assumptions are
    not written down is a result nobody can disagree with.
    """
    row = SyntheticRun(
        seed=dataset.seed,
        scenario=dataset.scenario.name,
        assumptions=dataset.scenario.as_assumptions(),
        ground_truth=dataset.ground_truth.as_document(),
        generator_version=dataset.generator_version,
        case_count=dataset.case_count,
    )
    row.merchant_id = merchant_id
    session.add(row)
    session.flush()
    return row.id


def seed_cases(
    session: Session,
    merchant_id: uuid.UUID,
    dataset: GeneratedDataset,
    synthetic_run_id: uuid.UUID,
    *,
    moment: datetime | None = None,
) -> tuple[uuid.UUID, ...]:
    """Canonicalize each generated payload and write the case it implies.

    The canonicalization is the load-bearing part. Every payload goes through the *real*
    canonicalizer, so a generated body with a wrong field name or an unverified ``error_reason``
    fails here loudly rather than producing a case whose features are empty — which would collapse
    every segment to the global prior and make the whole run meaningless while looking fine.

    Cases carry ``provenance = SYNTHETIC`` and ``synthetic_run_id``. That is what propagates the
    label into every downstream figure: one synthetic case is enough to mark a metrics report
    ``SYNTHETIC``, and the propagation starts here.

    Case ids are derived from the seed rather than drawn, because the arm assignment is a digest
    over the case id — see :data:`_ID_NAMESPACE`. Random ids would leave the *split* random even
    though the world was fixed.
    """
    import json

    detected_at = (moment or now()) - timedelta(days=1)
    case_ids: list[uuid.UUID] = []

    for index, case in enumerate(dataset.cases):
        body = json.dumps(case.webhook_payload()).encode()
        try:
            canonical_result = canonicalize(body)
        except CanonicalizationError as exc:  # pragma: no cover - a generator bug, not a data one
            raise AssertionError(
                f"the generator produced a payload the real canonicalizer rejected "
                f"({exc.rule}); the generated field names or error_reason values have drifted "
                "from the verified surface"
            ) from exc

        canonical = canonical_result.canonical.to_dict()
        case_id = synthetic_uuid(
            merchant_id, dataset.scenario.name, dataset.seed, "case", index
        )
        session.execute(
            text(
                """
                INSERT INTO recovery_case (
                    id, merchant_id, state, provider_payment_id, payment_amount, currency,
                    customer_key, detected_at, window_end_at, provenance, synthetic_run_id,
                    decision_cycle_count, created_at
                ) VALUES (
                    :id, :m, :state, :pid, :amount, 'INR', :ck, :detected, :we,
                    :prov, :run, 1, now()
                )
                """
            ),
            {
                "id": str(case_id),
                "m": str(merchant_id),
                "state": CaseState.DETECTED.value,
                "pid": case.provider_payment_id,
                "amount": int(case.amount),
                "ck": str(canonical.get("customer_key") or f"ck-{case_id}"),
                "detected": detected_at,
                "we": detected_at + timedelta(days=7),
                "prov": Provenance.SYNTHETIC.value,
                "run": str(synthetic_run_id),
            },
        )
        # The diagnosis the real taxonomy implies, so segmentation and the cause-conditioned
        # ground truth line up. Written rather than run through the diagnosis job because the job
        # is orchestration; the mapping it applies is what matters and it was applied above.
        session.execute(
            text(
                """
                INSERT INTO diagnosis (
                    id, merchant_id, case_id, cause, confidence, method, decision_cycle,
                    is_active, substituted_to_unknown, created_at
                ) VALUES (
                    gen_random_uuid(), :m, :c, :cause, 0.90, 'DETERMINISTIC', 1, true,
                    false, now()
                )
                """
            ),
            {
                "m": str(merchant_id),
                "c": str(case_id),
                "cause": _cause_of(canonical, case).value,
            },
        )
        case_ids.append(case_id)

    session.flush()
    return tuple(case_ids)


def _cause_of(canonical: dict[str, object], case: GeneratedCase) -> RiskCause:
    """The cause the canonical event implies, falling back to what the generator intended.

    The canonical row may or may not carry a mapped cause depending on how much of the taxonomy the
    canonicalizer applies. Where it does, that value wins — it is what the real pipeline would
    record. Where it does not, the generator's own cause is used, and the two are asserted equal by
    a test so this fallback cannot mask a mapping regression.
    """
    recorded = canonical.get("risk_cause") or canonical.get("cause")
    if isinstance(recorded, str):
        try:
            return RiskCause(recorded)
        except ValueError:  # pragma: no cover - taxonomy drift, caught by a test
            return case.cause
    return case.cause


def realize_outcomes(
    session: Session,
    merchant_id: uuid.UUID,
    dataset: GeneratedDataset,
    *,
    control: Sequence[tuple[uuid.UUID, GeneratedCase]],
    treatment: Sequence[tuple[uuid.UUID, GeneratedCase]],
    moment: datetime | None = None,
) -> None:
    """Write the outcome each arm's counterfactual says happened.

    Control cases realize ``recovers_if_untreated``; treatment cases realize
    ``recovers_if_treated[TREATED_ACTION]``. Both come from the same per-case draw, which is what
    makes the arms comparable at the individual level rather than merely on average.

    Recoveries are written as ``recovery_outcome`` rows with a backing ``payment_state_read``,
    because that is the only shape the metrics engine will count — ``verified_by_read_id`` is ``NOT
    NULL``. Writing a recovery without a read would be a shortcut that made the harness pass while
    the production path could not.
    """
    when = moment or now()

    for case_ids_and_cases, recovered_flag in (
        (control, "untreated"),
        (treatment, "treated"),
    ):
        for case_id, case in case_ids_and_cases:
            recovered = (
                case.recovers_if_untreated
                if recovered_flag == "untreated"
                else case.recovers_if_treated.get(TREATED_ACTION, False)
            )
            if not recovered:
                # No recovery: the window elapses. Terminal, so the case counts in the denominator.
                session.execute(
                    text(
                        "UPDATE recovery_case SET state = :s, terminal_reason = :tr WHERE id = :c"
                    ),
                    {
                        "s": CaseState.EXPIRED.value,
                        "tr": "RECOVERY_WINDOW_ELAPSED",
                        "c": str(case_id),
                    },
                )
                continue

            read_id = uuid.uuid4()
            session.execute(
                text(
                    """
                    INSERT INTO payment_state_read (
                        id, merchant_id, case_id, provider_payment_id, status, amount,
                        amount_refunded, captured, read_at, attempt_no, created_at
                    ) VALUES (
                        :id, :m, :c, :pid, 'captured', :amount, 0, true, :ra, 1, now()
                    )
                    """
                ),
                {
                    "id": str(read_id),
                    "m": str(merchant_id),
                    "c": str(case_id),
                    "pid": case.provider_payment_id,
                    "amount": int(case.amount),
                    "ra": when,
                },
            )
            session.execute(
                text(
                    """
                    INSERT INTO recovery_outcome (
                        id, merchant_id, case_id, classification, recovered_amount,
                        recovery_timestamp, seconds_to_recovery, verified_by_read_id, created_at
                    ) VALUES (
                        gen_random_uuid(), :m, :c, :cls, :amount, :ts, 3600, :rid, now()
                    )
                    """
                ),
                {
                    "m": str(merchant_id),
                    "c": str(case_id),
                    # OBSERVED in the treatment arm (we acted and money arrived), NATURAL in
                    # control (no action was taken). Never ATTRIBUTED — only the experiment's own
                    # gate may license that, and it is what this harness is testing.
                    "cls": (
                        OutcomeClass.OBSERVED.value
                        if recovered_flag == "treated"
                        else OutcomeClass.NATURAL.value
                    ),
                    "amount": int(case.amount),
                    "ts": when,
                    "rid": str(read_id),
                },
            )
            session.execute(
                text("UPDATE recovery_case SET state = :s WHERE id = :c"),
                {"s": CaseState.RECOVERED.value, "c": str(case_id)},
            )
    session.flush()


def compare_to_truth(
    dataset: GeneratedDataset,
    analysis: ExperimentAnalysis | None,
    *,
    control: Sequence[GeneratedCase],
    treatment: Sequence[GeneratedCase],
) -> ComparisonReport:
    """The only function in the codebase that reads ground truth. R13.C12.

    Everything above this line is Revora measuring a world it cannot see into. This function is the
    marker's answer sheet, and it exists at the very end so the boundary is obvious: nothing that
    decides anything calls it, and the import contracts stop anything that decides anything from
    even reaching this module.

    The true lift is computed over the *generated* cases rather than read from the ground-truth
    table, so a scenario whose sampling skewed the cause mix still reports the lift its own cases
    imply. That is the number the measurement should have found.
    """
    # The true lift for the arms as they were actually split, which is what the measurement saw.
    combined = [*control, *treatment]
    true_lift = true_average_lift(combined, TREATED_ACTION)

    measured_lift = None if analysis is None else analysis.lift
    ci_low = None if analysis is None else analysis.lift_ci_low
    ci_high = None if analysis is None else analysis.lift_ci_high
    permitted = bool(analysis is not None and analysis.attribution_permitted)
    codes = (
        ()
        if analysis is None
        else tuple(refusal.code for refusal in analysis.refusals)
    )

    return ComparisonReport(
        scenario=dataset.scenario.name,
        seed=dataset.seed,
        generator_version=dataset.generator_version,
        case_count=dataset.case_count,
        true_lift=true_lift,
        measured_lift=measured_lift,
        measured_ci_low=ci_low,
        measured_ci_high=ci_high,
        attribution_permitted=permitted,
        refusal_codes=codes,
        assumptions=dataset.scenario.as_assumptions(),
    )


def run_scenario(
    session: Session,
    merchant_id: uuid.UUID,
    *,
    scenario_name: str,
    seed: int,
    case_count: int,
    config: Configuration,
    required_sample_size: int | None = None,
    complete: bool = True,
    moment: datetime | None = None,
) -> HarnessResult:
    """Generate a world, run Revora's measurement over it, and report measured against true.

    Must be called inside a transaction; commits nothing.

    Args:
        required_sample_size: the experiment's per-arm threshold. Defaults to half the case count,
            so a run of the size the caller asked for is adequately powered by construction. Passed
            explicitly by the test that wants to prove an *underpowered* run is refused.
        complete: whether to mark the experiment ``COMPLETED``. The attribution gate requires it, so
            leaving it ``False`` is how a test proves an interim look establishes nothing.
    """
    dataset = generate(scenario_name, seed=seed, case_count=case_count)
    run_id = persist_run(session, merchant_id, dataset)

    experiment_id = _create_experiment(
        session,
        merchant_id,
        dataset=dataset,
        required_sample_size=required_sample_size
        if required_sample_size is not None
        else max(case_count // 2, 1),
        complete=complete,
        moment=moment,
    )

    case_ids = seed_cases(session, merchant_id, dataset, run_id, moment=moment)

    # Real assignment code, on the real experiment id. Deterministic, so the split is reproducible
    # from the ids alone — which is what makes a coverage run over many seeds meaningful.
    ratio = _ALLOCATION
    assignments = ExperimentAssignmentRepository(session)
    control: list[tuple[uuid.UUID, GeneratedCase]] = []
    treatment: list[tuple[uuid.UUID, GeneratedCase]] = []

    for case_id, case in zip(case_ids, dataset.cases, strict=True):
        group = assign_group(experiment_id, case_id, ratio)
        assignments.assign_if_absent(
            merchant_id,
            experiment_id=experiment_id,
            case_id=case_id,
            group=group,
            assigned_at=now(),
        )
        (control if group is ExperimentGroup.CONTROL else treatment).append((case_id, case))

    realize_outcomes(
        session,
        merchant_id,
        dataset,
        control=control,
        treatment=treatment,
        moment=moment,
    )

    analysis = analyse_experiment(session, merchant_id, experiment_id, config=config)

    report = compare_to_truth(
        dataset,
        analysis,
        control=[case for _, case in control],
        treatment=[case for _, case in treatment],
    )

    _logger.warning(
        "synthetic scenario measured",
        scenario=dataset.scenario.name,
        seed=seed,
        true_lift=str(report.true_lift),
        measured_lift=None if report.measured_lift is None else str(report.measured_lift),
        attribution_permitted=report.attribution_permitted,
        provenance=Provenance.SYNTHETIC.value,
    )

    return HarnessResult(
        synthetic_run_id=run_id,
        experiment_id=experiment_id,
        dataset=dataset,
        analysis=analysis,
        report=report,
        control_case_ids=tuple(case_id for case_id, _ in control),
        treatment_case_ids=tuple(case_id for case_id, _ in treatment),
    )


def _create_experiment(
    session: Session,
    merchant_id: uuid.UUID,
    *,
    dataset: GeneratedDataset,
    required_sample_size: int,
    complete: bool,
    moment: datetime | None,
) -> uuid.UUID:
    """An experiment for the run, labelled ``SYNTHETIC``.

    The label is not optional and it is not cosmetic: it is one of the three blocking labels in the
    attribution gate, so a synthetic experiment can *never* license an attributed revenue claim no
    matter how clean its lift is. That is the mechanism that stops a demo becoming a circular
    argument — the harness can prove the measurement works while remaining structurally incapable
    of reporting synthetic money as recovered revenue.
    """
    when = moment or now()
    experiment = ExperimentRepository(session).insert(
        merchant_id,
        values={
            # Derived, not drawn: the arm assignment digests this id together with each case id,
            # so a random experiment id would randomize the split of a fixed world.
            "id": synthetic_uuid(
                merchant_id, dataset.scenario.name, dataset.seed, "experiment"
            ),
            "name": f"synthetic-{dataset.scenario.name}-{dataset.seed}",
            "state": (
                ExperimentState.COMPLETED.value if complete else ExperimentState.ACTIVE.value
            ),
            "primary_metric": "recovery_rate",
            "allocation_ratio": "1:1",
            "assumed_baseline_rate": Decimal("0.2000"),
            "minimum_detectable_effect": Decimal("0.1000"),
            "significance_level": Decimal("0.05"),
            "power": Decimal("0.80"),
            "analysis_method": "two_proportion_normal_approximation",
            "required_sample_size_per_group": required_sample_size,
            "activated_at": when - timedelta(days=30),
            "completed_at": when if complete else None,
            "labels": [ExperimentLabel.SYNTHETIC.value],
        },
    )
    return experiment.id
