"""The Demonstration_Loader: evidence, produced through the real paths.

A dashboard reporting ``₹0.00 recovered`` and an empty experiments page does not meet the bar,
whatever the machinery underneath is capable of. This module is what puts numbers there —
measured money recovered across a batch, with compliant escalation, stopping rules and an audit
trail — and every one of those numbers is produced by the same code a real payment failure
travels through.

**Everything goes in at the HTTP boundary. No repository is written directly.**

Each seeded case begins as a canonical ``payment.failed`` payload from the existing generator,
signed with the merchant's own webhook secret and POSTed to
``/webhooks/razorpay/{merchant_slug}``. So it traverses signature verification over raw bytes,
canonicalization, the dedup index, the ack budget, the job queue, detection, diagnosis,
estimation, the optimizer, twelve policy checks, execution and the outcome monitor. The
customer-side outcomes go in the same way, through ``/customer/{merchant_slug}/…`` with a real
bearer token.

Going through the boundary rather than calling ``run_detection`` directly is the point: **a
loader that wrote rows directly would demonstrate the schema, not the system.** It also means
the loader can produce nothing the real path cannot produce, which is what makes R28.C15's
gap-free audit sequence a *consequence* rather than an extra step — ``AuditWriter`` allocates
from ``recovery_case.audit_seq`` under the row lock, identically for a seeded case and a real
one, so there is no arrangement here that could make a sequence gap-free and no way for the
loader to create one.

``provider_event_id`` is ``demo:<seed>:<n>:<status>``, so the existing dedup index guarantees one
case per payment and **a re-run with the same seed is idempotent rather than duplicative**: the
second delivery of the same event id is deduplicated at ingestion and no second case appears.

**What is injected, and why each one has to be.**

Three collaborators arrive as protocols rather than being reached for here.

* :class:`DemoTransport` — one method, ``request``. The loader composes the paths, the bodies,
  the signature and the headers itself, because those *are* what is under test; the transport is
  a socket. That is what lets one loader drive an in-process application in the nightly harness
  and a deployed one over the network, with no branch in between.
* :class:`DemoWorker` — draining the queue and driving one sweep pass. The queue has to be
  worked by something, and the something has to be holding the substituted provider client. A
  loader that built its own worker registry would decide which provider a demonstration talks
  to, which is the caller's decision and nobody else's.
* An ``advance`` callable for the clock. A batch needs cases to expire, promises to be missed and
  reviews to fire, and every one of those is a comparison between two stored instants. Sleeping
  through seven days is not an option and neither is pretending the bounds are shorter than they
  are.

**What the loader cannot do, stated rather than worked around.**

The Customer_Access_Token's wire form exists only inside ``execute_approved_action``'s first
transaction. ``customer_access_token.secret_hash`` is an HMAC and R18.C3 leaves no reversible
representation anywhere, and nothing in the built system composes the Customer_Response_Page URL
from it — so the token is discarded the moment execution commits. R28's task text describes the
loader "reading the token from the ``customer_access_token`` row the way a customer would", and
against the system as built that is impossible: there is nothing on the row to read. The two
remaining options were to insert ``customer_signal`` rows directly — which would prove the table
exists and nothing else — or to observe the mint at the moment it happens.
:func:`capturing_customer_tokens` is the second, and it is strictly the weaker intervention: the
token is real, minted by the real execution path under all of R18's conditions, and every
submission made with it goes through the public HTTP surface and is verified in constant time
against the persisted hash exactly as a stranger's would be.

**Nothing here is reachable from the decision path.** ``synthetic-containment`` forbids eighteen
packages from importing ``revora.synthetic``, so a batch's ground truth cannot reach the code
being measured. The one open direction is ``synthetic → experiment``, which is how the loader
defines and analyses its experiment.
"""

from __future__ import annotations

import dataclasses
import hmac
import json
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum, unique
from hashlib import sha256
from typing import TYPE_CHECKING, Any, Final, Protocol

from sqlalchemy import text

from revora.cases.review import CASE_REVIEW_KIND
from revora.customer.promises import PROMISE_SWEEP_KIND
from revora.customer.tokens import MintOutcome, TokenService
from revora.domain.actions import CandidateAction
from revora.domain.enums import (
    NOT_ESTABLISHED,
    CaseState,
    DelayReason,
    ExperimentGroup,
    ExperimentLabel,
    ExperimentState,
    Provenance,
    SelectionReason,
    TerminalReason,
)
from revora.domain.money import Minor
from revora.domain.segments import amount_band_for
from revora.domain.transitions import TERMINAL_STATES
from revora.experiment.analysis import ExperimentAnalysis, analyse_experiment
from revora.experiment.design import activate_experiment, define_experiment
from revora.ingestion.signature import EVENT_ID_HEADER, SIGNATURE_HEADER
from revora.jobs.scheduler import (
    EXECUTION_RECONCILIATION_KIND,
    LIFECYCLE_EVALUATION_KIND,
    PAYMENT_STATE_RECONCILIATION_KIND,
)
from revora.memory.versions import FROZEN_COMPONENTS
from revora.metrics.engine import (
    GROUND_TRUTH_LIFT_KEY,
    ReportingPeriod,
    compute_metrics,
)
from revora.persistence.models import SyntheticRun
from revora.persistence.repositories.session import tenant_transaction
from revora.platform.clock import now
from revora.platform.logging import get_logger
from revora.platform.ratelimit import WINDOW as INGEST_RATE_WINDOW
from revora.synthetic.generator import (
    GENERATOR_VERSION,
    GeneratedCase,
    ScenarioName,
    generate,
    scenario,
    true_average_lift,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sqlalchemy.orm import Session

    from revora.platform.config import Configuration

__all__ = [
    "CUSTOMER_DRIVEN_OUTCOMES",
    "DEMONSTRATION_BASELINE_RATE",
    "DEMO_BATCH_CASE_COUNT",
    "DEMO_ESCALATION_AMOUNT_RANGE",
    "DEMO_LINK_AMOUNT_RANGE",
    "DEMO_MINIMUM_DETECTABLE_EFFECT",
    "DEMO_PRIOR_COHORT_SIZE",
    "DEMO_PROVENANCE",
    "DEMO_SCENARIO",
    "DEMO_SEED",
    "DEMO_VERIFIED_RECOVERY_MIN_COUNT",
    "PRIOR_OUTCOMES",
    "PROVENANCE_BEARING_TABLES",
    "REQUIRED_TERMINAL_REASONS",
    "REQUIRED_TERMINAL_STATES",
    "TREATED_ACTION",
    "BatchCoverage",
    "CasePlan",
    "DemoBatchReport",
    "DemoOutcome",
    "DemoTenant",
    "DemoTransport",
    "DemoWorker",
    "HttpResult",
    "SeedDeliveryShortfallError",
    "UnderpoweredDemoBatchError",
    "authoritative_test_mode_recoveries",
    "capturing_customer_tokens",
    "define_demonstration_experiment",
    "demo_provider_event_id",
    "ground_truth_document",
    "plan_batch",
    "prior_cohort_split",
    "read_coverage",
    "run_demo_batch",
    "sign_payload",
    "verified_test_mode_capability",
]

_logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# The constants the requirement names
# ---------------------------------------------------------------------------

DEMO_PROVENANCE: Final[Provenance] = Provenance.SYNTHETIC
"""The provenance every row this loader causes carries (R28.C1, R28.C16).

A module constant rather than a parameter, and that is the whole of R28.C16's structural half:
there is no argument through which a caller could seed a batch as ``REAL``. It reaches the
``recovery_case`` row through ``build_registry(provenance=…)`` — the worker's seam, not a
request's — and propagates from there into ``customer_signal``, ``memory_observation``,
``baseline_estimate`` and ``candidate_estimate``, each of which copies the case's value.

Two of the four tables R28.C16 names — ``webhook_event`` and ``audit_record`` — have no
provenance column at all. That is a gap in the schema rather than in this loader, and it is
recorded here rather than papered over: a demonstration cannot label rows a table has no room to
label, and the assertion that backs C16 checks the tables that can carry the value."""

DEMO_BATCH_CASE_COUNT: Final[int] = 1_000
"""**[ASSUMPTION]** How many Recovery_Cases one Demo_Batch seeds (R28.C1).

Chosen by the power calculation and not by convenience — see
:func:`define_demonstration_experiment`. At ``EXPERIMENT_SIGNIFICANCE_LEVEL`` 0.05,
``EXPERIMENT_POWER`` 0.80, an assumed control rate of 0.20 and a minimum detectable effect of
0.08 absolute, the two-proportion normal approximation asks for about 444 cases per arm, so 888
in total; a thousand clears that with 112 cases of margin (R28.C7). The margin is deliberate:
an experiment sized to exactly its requirement fails the requirement the moment one case is
excluded for contamination."""

DEMO_VERIFIED_RECOVERY_MIN_COUNT: Final[int] = 3
"""**[ASSUMPTION]** How many Verified_Demo_Recoveries R28.C2 asks for.

A Verified_Demo_Recovery is a case that reached ``RECOVERED`` from an authoritative read
**against the provider test environment** reporting ``captured = true`` with an amount equal to
the case's ``payment_amount``.

**A minimum, not the reported figure.** What the report carries is
:func:`authoritative_test_mode_recoveries` — a count of the rows that satisfy that definition —
and this constant is what that count has to reach. The two were briefly the same thing, which was
the defect: reporting the constant whenever a capability check passed would have made the field a
restatement of the requirement rather than a measurement against it.

Paying a test-mode link programmatically is not possible — design open question 15, answered in
:func:`verified_test_mode_capability` — so the payment step is a documented manual action in
``RUNBOOK.md`` (§ Verified test-mode recoveries)."""

DEMO_SEED: Final[int] = 20_260_903
"""The recorded generation seed (R28.C1).

Recorded rather than drawn, because R28.C1 asks for a seed that reproduces the identical
Synthetic_Dataset on re-generation and a seed nobody wrote down reproduces nothing. It is also
half of the idempotence claim: ``provider_event_id`` is built from it, so re-running this seed
against the same merchant is deduplicated at ingestion rather than doubling the batch."""

DEMO_SCENARIO: Final[str] = ScenarioName.POSITIVE
"""The scenario the main cohort's counterfactuals come from.

``positive`` because a demonstration has to have something to demonstrate: its ground truth
plants a known lift of 0.15 for ``PAYMENT_LINK``, so the measured lift can be compared against a
number somebody wrote down. The other three scenarios are the evidence harness's job — the null
one especially, which checks that the measurement refuses to claim an effect that is not there,
and which a demonstration batch cannot substitute for."""

DEMONSTRATION_BASELINE_RATE: Final[Decimal] = Decimal("0.2000")
"""**[ASSUMPTION]** ``p₁``, the assumed Control_Group rate, recorded before assignment begins."""

DEMO_MINIMUM_DETECTABLE_EFFECT: Final[Decimal] = Decimal("0.0800")
"""**[ASSUMPTION]** ``δ``, the smallest lift worth detecting, absolute.

The single number that decides whether the experiment needs 444 cases per arm or 6,500. Recorded
with the definition (R28.C6) rather than defaulted, because a defaulted minimum detectable effect
is an experiment whose cost was chosen silently."""

TREATED_ACTION: Final[CandidateAction] = CandidateAction.PAYMENT_LINK
"""The action the treatment arm's ground truth is stated for.

The same choice the evidence harness makes and for the same reason: the true lift has to be the
lift of *one* action, or a discrepancy between measured and true could be either a measurement
error or a difference in which actions the optimizer happened to choose."""

DEMO_PRIOR_COHORT_SIZE: Final[int] = 180
"""How many cases build the segment history that makes a high baseline reachable.

R28.C4 wants a Null_Action selection with the reason ``HIGH_BASELINE_NO_INTERVENTION``, and
that reason is produced when the *estimated* baseline clears ``HIGH_BASELINE_THRESHOLD`` (0.80).
A fresh merchant's baseline is the uniform prior's 0.500, so no case can reach it — the history
has to exist first, and it has to be history the real estimator will read.

**The cohort is two halves, and the second half is not padding.** See
:func:`prior_cohort_split`. The first half is seeded inside the designated feature segment and
driven to recover, which is what puts the *specific* cell above ``MIN_SEGMENT_SAMPLE_SIZE``. The
second half is seeded across the *other* segments and driven not to recover, which is what keeps
the **global** level — the one every other case's backoff lands on — from being made of nothing
but this batch's successes.

**180, and the number is set by a floor with a margin rather than by taste.** Only *control-arm*
members become usable baseline labels — ``NO_INTERVENTION_CONFIRMED`` requires zero confirmed
actions **and** a control assignment — and the arm is a digest over the case id, so each seeded
case contributes a label on what is effectively a fair coin. A measured batch produced 63 labels
from 120 prior cases and 96 control assignments from 200, both within a percent of half.

So a designated half of ``n`` yields about ``n/2`` labels with a standard deviation near
``sqrt(n)/2``, and it has to clear ``MIN_SEGMENT_SAMPLE_SIZE`` of 30 *every* time rather than on
average. At 60 the expectation is 30 — a coin flip on the outcome R28.C4 requires, and a measured
run cleared it by two. At 90 the floor is three standard deviations below the expectation, which
is the margin this is sized for. The contrast half matches it at 90, which puts the global
posterior mean near 0.46 rather than near 0.97.

At the recovery rate :data:`_PRIOR_COHORT_FAILURE_STRIDE` produces, the designated cell's own
posterior mean sits near 0.85 — comfortably above ``HIGH_BASELINE_THRESHOLD`` rather than beside
it, and visibly below the 0.97 a segment that never failed would report.

**These cases are part of the batch and part of the experiment**, and they are counted in every
figure. Seeding them before activating the experiment would have kept them out of the arms, and
would also have made them useless: an unassigned case is
``MERCHANT_INTERVENTION_UNKNOWN``, which is not a baseline label."""

_PRIOR_COHORT_FAILURE_STRIDE: Final[int] = 12
"""One case in twelve of the prior cohort does not recover, so the segment rate is 11/12.

Deterministic rather than drawn from the ``high_baseline`` scenario's 0.85, and the difference
matters: 0.85 over thirty-odd observations puts the posterior mean within a percentage point or
two of the 0.80 threshold, so whether the demonstration produces a
``HIGH_BASELINE_NO_INTERVENTION`` selection at all would come down to the randomization. Shaping
the population by construction is what R28.C4 asks for; leaving a required outcome to a coin
flip is what it asks against.

Not 12/12 either. A segment that never fails is a segment whose posterior mean is
``(n+1)/(n+2)``, which reaches 0.80 at four observations — so the demonstration would no longer
be showing that a *history* produced the high baseline."""


def prior_cohort_split(prior_cohort_size: int) -> tuple[int, int]:
    """Split the prior cohort into its designated half and its contrast half.

    **This split is the fix for a defect that made the whole batch meaningless while every
    component looked healthy.** The segment estimator backs off: it asks for the most specific
    cell first and drops one feature at a time, and where no level holds
    ``MIN_SEGMENT_SAMPLE_SIZE`` confirmed no-intervention observations it uses the *global* level
    and labels the estimate a fallback. An observation counted in a specific cell is counted at
    every more general level too, global included — that is what makes backoff a narrowing of one
    query rather than six.

    So a prior cohort that is seeded in one segment and driven only to *recover* does not raise
    one segment's baseline. It raises the global prior to its own recovery rate, and the global
    prior is what every case outside the designated segment reads, because no other cell in a
    batch this size ever reaches thirty observations. Measured: 31 global observations, 31 of
    them recoveries, a 0.9700 baseline on 272 of 312 estimates, ``Null_Action`` correctly
    selected almost everywhere, and therefore no payment links, no Customer_Access_Tokens, no
    customer signals, no escalations. The pipeline was right; the population was a lie. It is
    the same failure ``revora.synthetic.harness`` warns about from the other direction — there,
    empty features collapse every segment *into* the global prior; here, one segment's successes
    **become** it.

    The contrast half fixes it by giving the global level what a real merchant's history has and
    this one did not: outcomes that did not recover. Its members are seeded across the
    non-designated causes, so no cell of their own approaches ``MIN_SEGMENT_SAMPLE_SIZE`` and
    every one of them backs off to a global prior they themselves populate.

    **Halves, and the ratio is not a free parameter in either direction.** Shifting cases to the
    designated half raises the global mean it feeds — every designated recovery is counted at the
    global level too — and shifting them the other way walks the designated cell back toward
    ``MIN_SEGMENT_SAMPLE_SIZE``. An even split at :data:`DEMO_PRIOR_COHORT_SIZE` satisfies both
    with room: about 45 labels in the cell against a floor of 30, and about 45 zero-recovery
    labels beside 41 recoveries at the global level, which is a mean near 0.46.

    Returns:
        ``(designated, contrast)``, summing to ``prior_cohort_size``. The designated half is the
        larger one on an odd size, because it is the half with a floor to clear.
    """
    contrast = prior_cohort_size // 2
    return prior_cohort_size - contrast, contrast


DEMO_LINK_AMOUNT_RANGE: Final[tuple[int, int]] = (150_000, 1_100_000)
"""Minor units, inclusive. The band where the real optimizer selects ``PAYMENT_LINK``.

Both ends are decided by the priors rather than picked. Below the lower end the link's expected
gain — ``UPLIFT_PRIORS``' 0.08 of the amount — no longer clears ``MIN_NET_VALUE_THRESHOLD`` once
its four cost terms are subtracted, so the optimizer correctly prefers a null action; above the
upper end ``HUMAN_ESCALATION``'s flat staff cost is outweighed by its larger uplift and Revora
correctly asks a person. Both of those are real and defensible product behaviours, and both
would silently empty the treatment arm of the one action the ground truth is stated for.

The range spans two amount bands (``SMALL`` below 500 000 and ``MEDIUM`` above it), so the
segmented figures on the dashboard have more than one populated cell."""

DEMO_ESCALATION_AMOUNT_RANGE: Final[tuple[int, int]] = (1_600_000, 3_000_000)
"""Minor units, inclusive. Above the crossover, so a human is asked and no provider call is made.

R28.C4's ``ESCALATED`` terminal state, reached by the decision path rather than by a customer
objection — which is the version that shows the *stopping* rule working, since a case Revora
declines to automate is a case it hands over rather than one it abandons."""

_DESIGNATED_SEGMENT_AMOUNT: Final[int] = 400_000
"""₹4,000. The one amount the prior cohort and the high-baseline cases share.

Fixed so both land in the same ``SMALL`` band and therefore in the same ``FULL`` feature
segment. A drawn amount would scatter them across bands and the accumulated history would be
split between cells, none of which would reach ``MIN_SEGMENT_SAMPLE_SIZE``."""

_DESIGNATED_CAUSE_REASON: Final[str] = "insufficient_funds"
"""The ``error_reason`` the designated segment is built from.

Maps deterministically to ``INSUFFICIENT_FUNDS``, whose eligibility row permits
``PAYMENT_LINK`` — which is what makes the high-baseline demonstration mean something. If the
cause admitted no customer-visible action, a null selection would prove nothing: there would
have been nothing else to select. Here there is, and the high baseline is why it was not chosen.

Every other cohort's cases are filtered to *exclude* this cause, so the designated segment
accumulates only the history this loader put there. Without the filter the main cohort's
``INSUFFICIENT_FUNDS`` cases in the ``SMALL`` band would land in the same cell at their own much
lower recovery rate and drag the segment mean back under the threshold."""

_CUSTOMER_ROLE_COUNT: Final[int] = 4
"""How many cases are driven to each customer-side outcome (R28.C5).

Four rather than one. Each of these requires a live Customer_Access_Token, which exists only for
a case whose approved action was customer-visible and actually executed — and a case can miss
that for reasons the loader does not control: it may have landed in the control arm, where the
action is withheld with ``SUPPRESSED_BY_CONTROL_ARM``, or its execution may have been refused by
one of twelve policy checks reading state the loader did not set. Roles are therefore assigned
*after* the arms are known, from the cases that actually hold a token, and four is the margin
that keeps "at least one of each" a fact rather than a hope."""

_EXPIRE_ROLE_COUNT: Final[int] = 8
"""How many cases are left entirely alone, so the window closes on them (R28.C4, ``EXPIRED``)."""

_HIGH_BASELINE_ROLE_COUNT: Final[int] = 6
"""How many cases are seeded into the designated segment after its history exists."""

_REPEAT_FAILURE_ROLE_COUNT: Final[int] = 4
"""How many designated-segment cases are failed a **second** time once their cycles are spent.

R28.C4's ``STOPPED``, and it is the one required outcome that no amount of waiting produces. A
case whose selection was a null action rests at ``POLICY_CHECK`` and the review sweep re-decides
it until ``MAX_RECOVERY_ATTEMPTS`` cycles are spent — at which point
``RecoveryCaseRepository.list_due_for_review`` deliberately stops returning it, because a sweep
that queues work whose only outcome is a transition to ``STOPPED`` is a sweep filling the queue
with pointless jobs. So the capped case sits there and the lifecycle sweep eventually expires it:
measured, 81 cases at the cap and not one ``STOPPED``.

R30.C10's transition is reached by the *handler*, under the row lock, on a review that arrives
from one of the other three triggers. The honest one to arrange is R30.C7's: a second
``payment.failed`` on the same payment attaches to the open case and enqueues a review with
``EVENT_ATTACHED``, and the review finds the cycle budget spent and stops the case with
``DECISION_CYCLE_LIMIT_REACHED``. A customer whose retry fails again is not a contrivance — it is
the commonest thing that happens to a failed payment, and it is delivered through the same signed
webhook endpoint as everything else here.

Four rather than one, because the delivery only enqueues a review from ``POLICY_CHECK`` and a
case is only there if its own selection was a null action — which is the designated segment's
behaviour rather than something this loader can assert."""

_ESCALATION_ROLE_COUNT: Final[int] = 6
"""How many cases are seeded above the escalation crossover."""

CUSTOMER_DRIVEN_OUTCOMES: Final[tuple[str, ...]] = (
    "DISPUTE",
    "CANCEL",
    "PARTIAL_ARRANGEMENT",
    "PROMISE_KEPT",
    "PROMISE_MISSED",
    "PROMISE_BEYOND_WINDOW",
)
"""The six outcomes that require a customer to say something, in the order they are assigned."""


@unique
class DemoOutcome(StrEnum):
    """What each seeded case is shaped to demonstrate.

    A closed enumeration rather than a set of booleans, because the coverage assertion of R28.C4
    and R28.C5 is a statement about *which* outcomes the batch reached, and the honest way to
    check that a generator change has not quietly dropped one is to name them all in one place
    and read the batch back against the list.
    """

    PRIOR_HISTORY = "PRIOR_HISTORY"
    """Builds the designated segment's no-intervention history. Recovers by construction."""

    PRIOR_CONTRAST = "PRIOR_CONTRAST"
    """Builds the global level's no-intervention history. Does **not** recover, by construction.

    Seeded across the non-designated causes and resolved before the main cohort exists, so the
    global prior every other case backs off to holds outcomes that did not recover as well as
    outcomes that did. Without it the designated segment's history *is* the global prior — see
    :func:`prior_cohort_split`.

    Not "recovers at the scenario's natural rate": the half exists to put zero-recovery labels at
    the global level, and a drawn rate would make how many there are a property of the seed, so
    whether the rest of the batch saw a usable baseline would come down to the randomization."""

    COUNTERFACTUAL = "COUNTERFACTUAL"
    """Realizes the generator's counterfactual for whichever arm it was assigned to.

    The bulk of the batch, and the only outcome whose recovery is decided by the ground truth
    rather than by the loader. ``recovers_if_untreated`` in control, ``recovers_if_treated`` in
    treatment, both read from the *same* per-case uniform draw — which is what makes the true
    average lift exactly ``mean(p_treated) - mean(p_natural)`` and the arms comparable at the
    individual level rather than merely on average."""

    HIGH_BASELINE = "HIGH_BASELINE"
    """Seeded into the designated segment once its history exists (R28.C4)."""

    REPEAT_FAILURE = "REPEAT_FAILURE"
    """Designated segment, then failed a second time once its decision cycles are spent.

    The second delivery attaches to the open case (R30.C7) and the review it enqueues finds the
    cycle budget spent, which is R30.C10's ``STOPPED`` — see
    :data:`_REPEAT_FAILURE_ROLE_COUNT`."""

    ESCALATE_TO_HUMAN = "ESCALATE_TO_HUMAN"
    """Above the crossover, so ``HUMAN_ESCALATION`` wins and the case ends ``ESCALATED``."""

    EXPIRE = "EXPIRE"
    """Left alone until ``window_end_at`` passes (R28.C4, ``EXPIRED``)."""

    DISPUTE = "DISPUTE"
    """The customer disputes the charge — hard stop, suppression, ``CUSTOMER_DISPUTED_CHARGE``."""

    CANCEL = "CANCEL"
    """The customer no longer wants the order — ``CUSTOMER_CANCELLED_ORDER``."""

    PARTIAL_ARRANGEMENT = "PARTIAL_ARRANGEMENT"
    """The customer asks to pay differently — ``CUSTOMER_REQUESTED_PARTIAL_ARRANGEMENT``."""

    PROMISE_KEPT = "PROMISE_KEPT"
    """A promise inside the window, then the money arrives — ``Promise_Status`` ``KEPT``."""

    PROMISE_MISSED = "PROMISE_MISSED"
    """A promise inside the window that the money does not follow — ``MISSED``.

    **This role does not currently reach ``MISSED``, and the reason is in the pipeline rather
    than in this loader.** ``resolve_missed`` requires a ``PROMISE_TO_PAY_FOLLOW_UP`` intent to
    have reached ``CONFIRMED`` before it will blame a customer for a message Revora may not have
    sent, and that intent needs a decision cycle after the promise sweep moves the promise to
    ``FOLLOW_UP_SCHEDULED``. All four review triggers — the sweep, the promise sweep, a customer
    signal and an attached event — enqueue a cycle only from ``POLICY_CHECK``. But a promise can
    only be submitted with a live Customer_Access_Token, which is minted inside the transition to
    ``EXECUTING``, so every case that can make a promise is at ``WAITING_FOR_OUTCOME`` — and the
    ``WAITING_FOR_OUTCOME -> DECISION_PENDING`` re-entry edge, though present in
    ``domain.transitions``, has no caller in the built system.

    So the promise is recorded, the sweep schedules its follow-up, nothing selects it, and the
    case expires with the promise at ``FOLLOW_UP_SCHEDULED``. Observed exactly that, twice. No
    population this loader can generate closes that gap: a case reaches ``POLICY_CHECK`` only by
    selecting a null action, and a case that selected a null action executed nothing and therefore
    holds no token to promise with.

    The role is kept, and the coverage assertion is left failing on it, because that is the honest
    state: a required outcome the system cannot currently produce is a finding, and deleting
    either half of the check would convert it into silence."""

    PROMISE_BEYOND_WINDOW = "PROMISE_BEYOND_WINDOW"
    """A promise past ``window_end_at`` — ``PROMISE_BEYOND_RECOVERY_WINDOW``."""


PRIOR_OUTCOMES: Final[frozenset[DemoOutcome]] = frozenset(
    {DemoOutcome.PRIOR_HISTORY, DemoOutcome.PRIOR_CONTRAST}
)
"""The two roles that are *history* rather than batch: seeded, resolved and closed before the
main cohort's first decision, because the estimator reads what is already there.

A set rather than a comparison against one member, which is what it was while the prior cohort
had one half — and the comparison was the sort that keeps working while meaning something
narrower than the name it is written under."""


# ---------------------------------------------------------------------------
# The transport, the worker, and the tenant
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HttpResult:
    """One HTTP response, reduced to what the loader asserts on.

    Status and a decoded body, and nothing else. Headers are deliberately absent: the loader
    checks that a write was *accepted*, and every header on these surfaces has its own test
    which is a better place for that claim than a batch run.
    """

    status_code: int
    body: Mapping[str, Any] = field(default_factory=dict)

    @property
    def accepted(self) -> bool:
        return 200 <= self.status_code < 300


class DemoTransport(Protocol):
    """How the loader speaks HTTP. One method, because one method is all it needs.

    The loader composes the path, the raw body, the signature and the headers, so the transport
    carries bytes and returns a status. Everything a demonstration is trying to prove lives on
    the loader's side of that line — a transport with a ``post_webhook`` method would be a
    transport that knew the route, and the route is part of what is under test.
    """

    def request(
        self,
        method: str,
        path: str,
        *,
        content: bytes | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> HttpResult: ...


class DemoWorker(Protocol):
    """How the loader gets the queue worked and the sweeps driven.

    Injected rather than built here, because a worker registry decides which provider client the
    run talks to — and a loader that chose that would be choosing whether a demonstration
    creates payment links against the real Razorpay. That is the caller's decision.
    """

    def drain(self) -> int:
        """Work the queue to empty. Returns how many passes it took."""
        ...

    def tick(self, kinds: Sequence[str]) -> int:
        """Enqueue the named periodic sweeps once. Returns how many were enqueued."""
        ...


@dataclass(frozen=True, slots=True)
class DemoTenant:
    """The merchant a batch is seeded into, and the two credentials it needs.

    ``webhook_secret`` is the merchant's *own* secret, which is the point: the loader signs with
    it and the endpoint verifies with it, so a batch that reached a case proves the signature
    path ran. A loader handed a way to skip verification would be a loader that could seed a
    case the provider could not.
    """

    merchant_id: uuid.UUID
    slug: str
    webhook_secret: str
    dashboard_headers: Mapping[str, str]


# ---------------------------------------------------------------------------
# Identifiers and signing
# ---------------------------------------------------------------------------


def demo_provider_event_id(seed: int, index: int, status: str) -> str:
    """``demo:<seed>:<n>:<status>`` — the dedup key that makes a re-run idempotent.

    The ``demo:`` prefix is not decoration. ``webhook_event`` has no provenance column, so the
    event id is the only place on that row where "this delivery came from a demonstration" is
    written down at all, and it is what a reader querying the ingestion log has to go on.

    ``<status>`` distinguishes the failure that opens a case from the capture that resolves it,
    so both can be delivered for one payment without colliding on
    ``uq_webhook_event_merchant_provider_event_id``.
    """
    return f"demo:{seed}:{index}:{status}"


def sign_payload(secret: str, body: bytes) -> str:
    """``HMAC-SHA256(secret, raw_body)``, hex — over the bytes that will be sent.

    Over the exact bytes rather than over a re-serialization of the parsed body, because that is
    what the provider signs and what :func:`revora.ingestion.signature.verify_webhook_signature`
    verifies. A loader that signed a re-serialization would pass its own signature check and
    fail the real one on the first payload whose key order differed.
    """
    return hmac.new(secret.encode("utf-8"), body, sha256).hexdigest()


def _encode(payload: Mapping[str, object]) -> bytes:
    """Compact JSON, the way a provider sends it. The bytes signed and the bytes sent."""
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


# ---------------------------------------------------------------------------
# The plan
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CasePlan:
    """One seeded case: the generated truth, the amount it is posted with, and its role.

    ``case`` is the generator's own :class:`~revora.synthetic.generator.GeneratedCase`, carrying
    both counterfactual outcomes against one shared uniform draw. ``outcome`` is what this batch
    shapes it to demonstrate, and ``index`` is its position in the seed's sequence — which is
    what makes ``provider_event_id`` reproducible.
    """

    index: int
    case: GeneratedCase
    outcome: DemoOutcome
    seed: int = DEMO_SEED

    @property
    def amount(self) -> Minor:
        return self.case.amount

    @property
    def provider_payment_id(self) -> str:
        return self.case.provider_payment_id

    @property
    def failed_event_id(self) -> str:
        return demo_provider_event_id(self.seed, self.index, "failed")

    @property
    def captured_event_id(self) -> str:
        return demo_provider_event_id(self.seed, self.index, "captured")

    @property
    def repeat_failed_event_id(self) -> str:
        """The second failure on the same payment: a different event, the same payment.

        A distinct ``provider_event_id`` because it is a distinct delivery and the dedup index is
        on the event, not the payment — and the *same* ``provider_payment_id``, because that is
        what makes detection attach it to the open case rather than open a second one (R1.C10).
        """
        return demo_provider_event_id(self.seed, self.index, "refailed")

    @property
    def contact(self) -> str:
        """The reserved-range contact the generated payload carries.

        Read back out of the payload rather than recomputed, so the consent this loader records
        is keyed on exactly the contact the webhook will deliver. Deriving it twice is how the
        integration helpers once recorded consent for a different person than the case was
        about, and the opt-out test passed for the wrong reason.
        """
        entity = _payment_entity_of(self.case.webhook_payload())
        return str(entity["contact"])

    def recovers_in(self, group: ExperimentGroup | None) -> bool:
        """Whether this case's money arrives, given the arm it was assigned to.

        The only place the batch decides a recovery, and for :attr:`DemoOutcome.COUNTERFACTUAL`
        it does not decide it at all — it reads the arm's counterfactual off the generated case.
        Shaped roles answer from their own definition, because an outcome the batch is *required*
        to contain (R28.C4, R28.C5) cannot be left to a draw.
        """
        match self.outcome:
            case DemoOutcome.PRIOR_HISTORY:
                return self.index % _PRIOR_COHORT_FAILURE_STRIDE != 0
            case DemoOutcome.PROMISE_KEPT:
                return True
            case DemoOutcome.COUNTERFACTUAL:
                if group is ExperimentGroup.TREATMENT:
                    return self.case.recovers_if_treated.get(TREATED_ACTION, False)
                return self.case.recovers_if_untreated
            case _:
                return False


def _payment_entity_of(payload: Mapping[str, object]) -> Mapping[str, object]:
    """The ``payload.payment.entity`` object of a Razorpay-shaped envelope."""
    payload_block = payload["payload"]
    assert isinstance(payload_block, dict)
    payment = payload_block["payment"]
    assert isinstance(payment, dict)
    entity = payment["entity"]
    assert isinstance(entity, dict)
    return entity


def _rebanded(case: GeneratedCase, amount: int) -> GeneratedCase:
    """The same generated case at a different amount, with its band kept consistent.

    **Why the loader chooses amounts at all.** The scenarios' band mix exists to populate every
    amount band, which is what the evidence harness needs. A demonstration needs something the
    scenario does not own: for the batch to exercise a *particular decision branch* of the real
    optimizer — the payment link, the human escalation, the null action — the amount has to sit
    in the range where that branch wins. See :data:`DEMO_LINK_AMOUNT_RANGE`.

    **Why this does not touch the ground truth.** ``recovers_if_untreated`` and
    ``recovers_if_treated`` come from one uniform draw compared against cause-conditioned
    probabilities; no amount appears in either. So re-banding changes which action the pipeline
    selects and changes nothing about what the generated world says will happen — which is
    exactly the separation that lets the measured lift be compared against a planted one.

    ``amount_band`` is recomputed rather than passed, so a re-banded case cannot end up claiming
    a band its amount does not fall in — which would put it in the wrong feature segment and
    make the whole high-baseline construction silently miss.
    """
    minor = Minor(amount)
    return dataclasses.replace(case, amount=minor, amount_band=amount_band_for(minor))


def _amount_in(band: tuple[int, int], index: int, span: int) -> int:
    """A deterministic amount inside ``band``, spread evenly across ``span`` positions.

    Deterministic and evenly spread rather than drawn, on two grounds. The seed already fixes
    the world, and adding a second random source would mean a batch that reproduced its
    counterfactuals and not its amounts — so a re-run would price the same cases differently and
    the measured cost figures could not be re-derived. Even spreading also guarantees both
    amount bands the range covers are populated, which a draw only does in expectation.
    """
    low, high = band
    if span <= 1:
        return low
    return low + ((high - low) * (index % span)) // (span - 1)


def plan_batch(
    *,
    seed: int = DEMO_SEED,
    case_count: int = DEMO_BATCH_CASE_COUNT,
    prior_cohort_size: int = DEMO_PRIOR_COHORT_SIZE,
) -> tuple[CasePlan, ...]:
    """Build the whole batch's plan before anything is posted.

    Two cohorts, in the order they have to be seeded.

    The **prior cohort** comes first and is two halves. Its designated half sits entirely inside
    the designated feature segment and recovers, which is what makes that *cell* clear
    ``MIN_SEGMENT_SAMPLE_SIZE``; its contrast half sits across the other segments and does not
    recover, which is what keeps the *global* level — where every case outside the designated
    segment lands — from inheriting the designated cell's recovery rate. Both are needed and
    :func:`prior_cohort_split` explains why at length.

    The **main cohort** is everything else, and its causes exclude the designated one so the
    designated segment accumulates only what this loader put there. Its amounts are placed in the
    range where the optimizer selects a payment link, except for the escalation role, which is
    placed above the crossover on purpose, and the two designated-segment roles, which are placed
    on :data:`_DESIGNATED_SEGMENT_AMOUNT` so they land in the cell the history was built in.

    Roles that need a customer to say something are **not** assigned here. They are assigned
    after the arms are known, because a customer submission needs a live token and a token
    exists only for a case whose customer-visible action was executed — see
    :data:`_CUSTOMER_ROLE_COUNT`.

    Raises:
        ValueError: if ``case_count`` cannot hold the prior cohort and the shaped roles. Refused
            rather than truncated: a batch too small to contain a required outcome is a batch
            whose coverage assertion would fail at the end, and failing at the start says why.
    """
    designated_prior, contrast_prior = prior_cohort_split(prior_cohort_size)
    shaped = (
        _HIGH_BASELINE_ROLE_COUNT
        + _REPEAT_FAILURE_ROLE_COUNT
        + _ESCALATION_ROLE_COUNT
        + _EXPIRE_ROLE_COUNT
    )
    reserved = prior_cohort_size + shaped + _CUSTOMER_ROLE_COUNT * len(CUSTOMER_DRIVEN_OUTCOMES)
    if case_count < reserved:
        raise ValueError(
            f"case_count={case_count} cannot hold the demonstration's required outcomes; "
            f"the prior cohort and the shaped roles need {reserved} cases before a single "
            "counterfactual case is seeded"
        )

    spec = scenario(DEMO_SCENARIO)
    designated_cause = _designated_cause(spec.causes)

    # Generated with headroom, because both cohorts filter on cause: the designated roles keep
    # only the designated cause and everything else keeps only the others, so neither can be
    # filled from exactly ``case_count`` draws. Three times is comfortably enough at any cause mix
    # the scenarios declare, and the slice below is what fixes the count.
    dataset = generate(DEMO_SCENARIO, seed=seed, case_count=case_count * 3)

    designated = [case for case in dataset.cases if case.cause is designated_cause]
    others = [case for case in dataset.cases if case.cause is not designated_cause]
    designated_needed = (
        designated_prior + _HIGH_BASELINE_ROLE_COUNT + _REPEAT_FAILURE_ROLE_COUNT
    )
    if len(designated) < designated_needed:
        raise ValueError(  # pragma: no cover - only on a scenario cause-mix change
            "the generated dataset holds too few cases of the designated cause to build the "
            "segment history; raise the generation headroom in plan_batch"
        )

    plans: list[CasePlan] = []
    index = 0

    for offset in range(designated_prior):
        case = _rebanded(designated[offset], _DESIGNATED_SEGMENT_AMOUNT)
        plans.append(
            CasePlan(
                index=index, case=case, outcome=DemoOutcome.PRIOR_HISTORY, seed=seed
            )
        )
        index += 1

    # The contrast half takes the *front* of ``others`` and the main cohort takes what is left of
    # it, disjointly. Two plans sharing a generated case would share its ``provider_payment_id``,
    # and the second delivery would be deduplicated onto the first one's case — so the batch would
    # be quietly smaller than it says it is.
    for offset in range(contrast_prior):
        case = _rebanded(
            others[offset], _amount_in(DEMO_LINK_AMOUNT_RANGE, offset, max(contrast_prior, 2))
        )
        plans.append(
            CasePlan(
                index=index, case=case, outcome=DemoOutcome.PRIOR_CONTRAST, seed=seed
            )
        )
        index += 1

    for offset in range(_HIGH_BASELINE_ROLE_COUNT + _REPEAT_FAILURE_ROLE_COUNT):
        case = _rebanded(
            designated[designated_prior + offset], _DESIGNATED_SEGMENT_AMOUNT
        )
        outcome = (
            DemoOutcome.HIGH_BASELINE
            if offset < _HIGH_BASELINE_ROLE_COUNT
            else DemoOutcome.REPEAT_FAILURE
        )
        plans.append(CasePlan(index=index, case=case, outcome=outcome, seed=seed))
        index += 1

    remaining = case_count - len(plans)
    available = others[contrast_prior:]
    for offset in range(remaining):
        source = available[offset % len(available)]
        if offset < _ESCALATION_ROLE_COUNT:
            outcome = DemoOutcome.ESCALATE_TO_HUMAN
            amount = _amount_in(
                DEMO_ESCALATION_AMOUNT_RANGE, offset, _ESCALATION_ROLE_COUNT
            )
        elif offset < _ESCALATION_ROLE_COUNT + _EXPIRE_ROLE_COUNT:
            outcome = DemoOutcome.EXPIRE
            amount = _amount_in(
                DEMO_LINK_AMOUNT_RANGE, offset - _ESCALATION_ROLE_COUNT, _EXPIRE_ROLE_COUNT
            )
        else:
            outcome = DemoOutcome.COUNTERFACTUAL
            amount = _amount_in(DEMO_LINK_AMOUNT_RANGE, offset, max(remaining, 2))
        plans.append(
            CasePlan(
                index=index, case=_rebanded(source, amount), outcome=outcome, seed=seed
            )
        )
        index += 1

    return tuple(plans)


def _designated_cause(causes: Sequence[Any]) -> Any:
    """The cause the designated segment is built from, refusing a scenario that lacks it.

    Refused rather than substituted, because the substitute would be a cause whose eligibility
    row may permit no customer-visible action — and a null-action selection in *that* segment
    would demonstrate nothing, since there would have been nothing else to select.
    """
    from revora.domain.failure_taxonomy import REASON_TO_CAUSE

    wanted = REASON_TO_CAUSE[_DESIGNATED_CAUSE_REASON]
    if wanted not in causes:
        raise ValueError(  # pragma: no cover - only on a scenario change
            f"scenario {DEMO_SCENARIO!r} does not generate {wanted.value} cases, so the "
            "designated high-baseline segment cannot be built from it"
        )
    return wanted


# ---------------------------------------------------------------------------
# Capturing the minted token
# ---------------------------------------------------------------------------


@contextmanager
def capturing_customer_tokens() -> Iterator[dict[uuid.UUID, str]]:
    """Record every Customer_Access_Token wire form minted while the block runs, by case id.

    **Read the module docstring before this one.** The wire form exists only inside
    ``execute_approved_action``'s first transaction; ``secret_hash`` is an HMAC and R18.C3 leaves
    no reversible representation anywhere; nothing composes the Customer_Response_Page URL from
    it. So the token is unreachable after execution commits, and R28's "read the token from the
    row the way a customer would" cannot be done against the system as built.

    The alternative was inserting ``customer_signal`` rows directly, which would prove the table
    exists and nothing about the surface. This observes the *real* mint — R18's one-live-token
    index, its expiry clamp, its ``CUSTOMER_TOKEN_ISSUED`` audit record, all of it — and every
    submission made with what it captures is verified in constant time against the persisted
    hash by the public endpoint, exactly as a stranger's would be.

    **Why it wraps rather than being a hook in production code.** A production observer would be
    a supported way for a credential to leave the transaction that created it, which is the one
    thing R18.C3 is built to prevent. A wrapper installed by a module ``synthetic-containment``
    keeps unreachable from every decision component, for the duration of one ``with`` block, is
    not that. It restores the original on the way out even on an exception, so a failing batch
    cannot leave the mint observed for whatever runs next.

    Nothing captured here is logged, and the returned mapping holds wire tokens — treat it as
    the credential store it is.
    """
    captured: dict[uuid.UUID, str] = {}
    original = TokenService.mint

    def observed(
        self: TokenService, merchant_id: uuid.UUID, **kwargs: Any
    ) -> MintOutcome:
        outcome = original(self, merchant_id, **kwargs)
        token = outcome.token
        if token is not None and token.wire_token is not None:
            captured[token.case_id] = token.wire_token
        return outcome

    TokenService.mint = observed  # type: ignore[method-assign]
    try:
        yield captured
    finally:
        TokenService.mint = original  # type: ignore[method-assign]


# ---------------------------------------------------------------------------
# The experiment
# ---------------------------------------------------------------------------


class UnderpoweredDemoBatchError(RuntimeError):
    """The computed per-arm sample size exceeds half the batch, so nothing is activated.

    R28.C7, and the refusal is the requirement rather than a guard around it. The alternative —
    raising the batch size until the requirement fits — is how an underpowered experiment gets
    reported as a powered one: the sample size stops being a threshold the data has to clear and
    becomes a description of whatever data arrived.

    Raised at *definition* time, before a single case is assigned, so there is no partially
    populated experiment to clean up and no half-run batch to explain.
    """

    def __init__(self, required_per_arm: int, case_count: int) -> None:
        self.required_per_arm = required_per_arm
        self.case_count = case_count
        super().__init__(
            f"the demonstration experiment needs {required_per_arm} cases per arm and the "
            f"batch holds {case_count}, which allows {case_count // 2}. Refusing to activate: "
            "a batch sized to fit a conclusion is how an underpowered experiment gets reported "
            "as a powered one"
        )


def define_demonstration_experiment(
    session: Session,
    merchant_id: uuid.UUID,
    *,
    config: Configuration,
    seed: int,
    case_count: int,
    correlation_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """Define and activate the Demonstration_Experiment, or refuse to (R28.C6 through C9).

    Must be called inside a transaction; commits nothing.

    The sample size is computed by ``Experiment_Engine.required_sample_size`` **at definition
    time**, from the recorded assumptions, and stored on the row before any case is assigned.
    Everything R28.C6 enumerates — primary metric, allocation ratio, assumed baseline rate,
    minimum detectable effect, significance level, power, analysis method, required sample size —
    lands in that row through the ordinary ``define_experiment`` path. No column is written here
    that a real experiment would not have.

    The ``SYNTHETIC`` label goes on at definition and is not optional. It is one of the three
    blocking labels in the attribution gate, so this experiment can **never** license an
    attributed revenue claim no matter how clean its lift turns out (R28.C10, R13.C8). That is
    the mechanism that stops a demonstration becoming a circular argument, and it is why no new
    code path is added for it: the label already does the work.

    Raises:
        UnderpoweredDemoBatchError: if the computed per-arm requirement exceeds
            ``case_count // 2``.
    """
    definition = define_experiment(
        session,
        merchant_id,
        name=f"demo-{DEMO_SCENARIO}-{seed}",
        config=config,
        assumed_baseline_rate=DEMONSTRATION_BASELINE_RATE,
        minimum_detectable_effect=DEMO_MINIMUM_DETECTABLE_EFFECT,
        secondary_metrics=("recovered_revenue", "average_hours_to_recovery"),
        eligibility={
            "detected_via": "signed webhook delivery",
            "population": f"Demo_Batch seed {seed}",
            "note": (
                "Every input originates from a Synthetic_Dataset, so the result is labelled "
                "SYNTHETIC and is disqualified from Attributed_Recovery by R13.C8."
            ),
        },
        labels=(ExperimentLabel.SYNTHETIC.value,),
        correlation_id=correlation_id,
    )

    if definition.required_sample_size_per_group > case_count // 2:
        raise UnderpoweredDemoBatchError(
            definition.required_sample_size_per_group, case_count
        )

    activate_experiment(
        session,
        merchant_id,
        definition.experiment_id,
        # Every frozen component pinned as ``unversioned``, which is the honest value on a
        # deployment that versions none of them: an unversioned component cannot be detected as
        # having changed, and recording that is better than implying nothing was pinned.
        live_versions=dict.fromkeys(FROZEN_COMPONENTS, "unversioned"),
        correlation_id=correlation_id,
    )
    _logger.warning(
        "demonstration experiment activated",
        experiment_id=str(definition.experiment_id),
        required_sample_size_per_group=definition.required_sample_size_per_group,
        case_count=case_count,
        labels=list(definition.labels),
        provenance=DEMO_PROVENANCE.value,
    )
    return definition.experiment_id


def ground_truth_document(
    plans: Sequence[CasePlan], *, seed: int
) -> dict[str, object]:
    """The ``synthetic_run.ground_truth`` document, carrying the planted average lift.

    The generator's own ground-truth table plus one derived figure the table cannot hold: the
    true average lift **over the cases this batch actually seeded**, computed by
    :func:`~revora.synthetic.generator.true_average_lift`. The table gives probabilities per
    cause; the lift depends on the cause mix, and the mix is a property of the batch.

    Recorded under :data:`~revora.metrics.engine.GROUND_TRUTH_LIFT_KEY`, whose spelling is owned
    by the reader rather than by this writer — see that constant. Recording it here is what makes
    R28.C9's measured-versus-planted difference computable by a component that is forbidden from
    importing the generator, which is the containment working rather than being worked around.
    """
    spec = scenario(DEMO_SCENARIO)
    cases = [plan.case for plan in plans]
    document = dict(spec.ground_truth.as_document())
    document[GROUND_TRUTH_LIFT_KEY] = str(true_average_lift(cases, TREATED_ACTION))
    document["treated_action"] = TREATED_ACTION.value
    document["seed"] = seed
    document["generator_version"] = GENERATOR_VERSION
    document["note"] = (
        "SYNTHETIC. The lift here is the one the generator planted, so a measured lift close "
        "to it establishes that the measurement works. It establishes nothing about real "
        "recovery rates, because the ground truth is ours."
    )
    return document


# ---------------------------------------------------------------------------
# Coverage
# ---------------------------------------------------------------------------


REQUIRED_TERMINAL_STATES: Final[tuple[str, ...]] = (
    CaseState.RECOVERED.value,
    CaseState.STOPPED.value,
    CaseState.EXPIRED.value,
    CaseState.ESCALATED.value,
)
"""The four Terminal_States R28.C4 requires a Demo_Batch to contain."""

REQUIRED_TERMINAL_REASONS: Final[tuple[str, ...]] = (
    TerminalReason.CUSTOMER_DISPUTED_CHARGE.value,
    TerminalReason.CUSTOMER_CANCELLED_ORDER.value,
    TerminalReason.CUSTOMER_REQUESTED_PARTIAL_ARRANGEMENT.value,
    TerminalReason.PROMISE_BEYOND_RECOVERY_WINDOW.value,
)
"""The four customer-caused terminal reasons R28.C5 requires."""

PROVENANCE_BEARING_TABLES: Final[tuple[str, ...]] = (
    "recovery_case",
    "customer_signal",
    "memory_observation",
    "baseline_estimate",
    "candidate_estimate",
)
"""The tables R28.C16 can actually be checked against.

R28.C16 names ``recovery_case``, ``webhook_event``, ``customer_signal`` and ``audit_record``.
Two of those four have no ``provenance`` column — see :data:`DEMO_PROVENANCE` — so the check
covers the two that do, plus the three further tables that copy the case's value and would be
the first place a propagation gap showed up."""


@dataclass(frozen=True, slots=True)
class BatchCoverage:
    """What the batch actually reached, read back from persisted rows (R28.C4, R28.C5).

    **Read back rather than accumulated as the run goes.** A counter incremented by the loader
    records what the loader believed it did; this records what the database holds. The
    difference is the whole value of the assertion — a generator change, a policy change or a
    bound change that quietly stopped producing one of these outcomes has to fail here rather
    than at demo time.
    """

    terminal_states: Mapping[str, int]
    terminal_reasons: Mapping[str, int]
    selection_reasons: Mapping[str, int]
    promise_statuses: Mapping[str, int]
    suppressions: Mapping[str, int]
    audit_sequence_gaps: tuple[str, ...]
    real_provenance_rows: Mapping[str, int]

    @property
    def missing(self) -> tuple[str, ...]:
        """Every required outcome the batch did not reach, as readable tokens.

        Returned as a tuple rather than raised, so a caller can report *all* the gaps at once.
        A coverage check that failed on the first missing outcome would make a batch with three
        gaps take three runs to diagnose, and a run is measured in minutes.
        """
        gaps: list[str] = []
        gaps.extend(
            f"terminal_state:{state}"
            for state in REQUIRED_TERMINAL_STATES
            if self.terminal_states.get(state, 0) < 1
        )
        gaps.extend(
            f"terminal_reason:{reason}"
            for reason in REQUIRED_TERMINAL_REASONS
            if self.terminal_reasons.get(reason, 0) < 1
        )
        if self.selection_reasons.get(
            SelectionReason.HIGH_BASELINE_NO_INTERVENTION.value, 0
        ) < 1:
            gaps.append("selection_reason:HIGH_BASELINE_NO_INTERVENTION")
        for status in ("KEPT", "MISSED"):
            if self.promise_statuses.get(status, 0) < 1:
                gaps.append(f"promise_status:{status}")
        if not self.suppressions:
            gaps.append("contact_suppression:none")
        gaps.extend(f"audit_gap:{detail}" for detail in self.audit_sequence_gaps)
        gaps.extend(
            f"real_provenance:{table}={count}"
            for table, count in sorted(self.real_provenance_rows.items())
            if count > 0
        )
        return tuple(gaps)

    @property
    def complete(self) -> bool:
        return not self.missing

    def as_document(self) -> dict[str, object]:
        return {
            "terminal_states": dict(sorted(self.terminal_states.items())),
            "terminal_reasons": dict(sorted(self.terminal_reasons.items())),
            "selection_reasons": dict(sorted(self.selection_reasons.items())),
            "promise_statuses": dict(sorted(self.promise_statuses.items())),
            "contact_suppressions": dict(sorted(self.suppressions.items())),
            "audit_sequence_gaps": list(self.audit_sequence_gaps),
            "real_provenance_rows": dict(sorted(self.real_provenance_rows.items())),
            "missing": list(self.missing),
            "complete": self.complete,
        }


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DemoBatchReport:
    """Everything one Demo_Batch produced, in one object a caller can assert on or print.

    The figures and the labels travel together on purpose. ``observed_recovered_revenue`` beside
    ``incremental_recovered_revenue``'s refusal beside ``demonstration_incremental_revenue``'s
    number is the *point* of the demonstration: the first is real money a provider read backs,
    the second is a claim nothing licenses, and the third is a sound comparison over a world we
    wrote. Printed apart, any one of them reads as the others.
    """

    seed: int
    synthetic_run_id: uuid.UUID
    experiment_id: uuid.UUID
    required_sample_size_per_group: int
    case_count: int
    seeded_case_count: int
    control_case_count: int
    treatment_case_count: int
    recovered_case_count: int
    observed_recovered_revenue: int
    natural_recovered_revenue: int
    unresolved_revenue: int
    revenue_at_risk: int
    incremental_status: str
    incremental_refusal_codes: tuple[str, ...]
    demonstration_value: int | None
    demonstration_labels: tuple[str, ...]
    measured_lift: Decimal | None
    lift_ci_low: Decimal | None
    lift_ci_high: Decimal | None
    ground_truth_lift: Decimal
    metrics_labels: tuple[str, ...]
    coverage: BatchCoverage
    verified_test_mode_recoveries: int
    customer_submissions: Mapping[str, int]

    @property
    def measured_minus_ground_truth(self) -> Decimal | None:
        if self.measured_lift is None:
            return None
        return self.measured_lift - self.ground_truth_lift

    def as_document(self) -> dict[str, object]:
        difference = self.measured_minus_ground_truth
        return {
            "seed": self.seed,
            "synthetic_run_id": str(self.synthetic_run_id),
            "experiment_id": str(self.experiment_id),
            "required_sample_size_per_group": self.required_sample_size_per_group,
            "case_count": self.case_count,
            "seeded_case_count": self.seeded_case_count,
            "control_case_count": self.control_case_count,
            "treatment_case_count": self.treatment_case_count,
            "recovered_case_count": self.recovered_case_count,
            "observed_recovered_revenue": self.observed_recovered_revenue,
            "natural_recovered_revenue": self.natural_recovered_revenue,
            "unresolved_revenue": self.unresolved_revenue,
            "revenue_at_risk": self.revenue_at_risk,
            "incremental_recovered_revenue": {
                "status": self.incremental_status,
                "refusal_codes": list(self.incremental_refusal_codes),
            },
            "demonstration_incremental_revenue": {
                "value": self.demonstration_value,
                "labels": list(self.demonstration_labels),
                "lift": None if self.measured_lift is None else str(self.measured_lift),
                "lift_interval": (
                    None
                    if self.lift_ci_low is None
                    else f"[{self.lift_ci_low}, {self.lift_ci_high}]"
                ),
                "ground_truth_lift": str(self.ground_truth_lift),
                "measured_minus_ground_truth": (
                    None if difference is None else str(difference)
                ),
            },
            "metrics_labels": list(self.metrics_labels),
            "verified_test_mode_recoveries": self.verified_test_mode_recoveries,
            "customer_submissions": dict(sorted(self.customer_submissions.items())),
            "coverage": self.coverage.as_document(),
            "provenance": DEMO_PROVENANCE.value,
            "warning": (
                "SYNTHETIC. observed_recovered_revenue is money a provider read confirmed, and "
                "nothing here says Revora caused it. demonstration_incremental_revenue is a "
                "sound comparison over a generated world and is never presented as "
                "incremental_recovered_revenue."
            ),
        }


# ---------------------------------------------------------------------------
# Test-mode capability (R28.C2, design open question 15)
# ---------------------------------------------------------------------------


def verified_test_mode_capability(transport: object) -> bool:
    """Whether this run can **pay** a Razorpay test-mode payment link programmatically.

    ``False``, and design open question 15 is now answered rather than open: **the API does not
    exist.** The Payment Links surface Razorpay documents is create (standard and UPI), fetch,
    fetch-all, update, cancel, offers, and ``POST /v1/payment_links/:id/notify_by/:medium`` —
    seven operations, none of which pays a link. Payment happens on the customer-facing payment
    page, and in test mode that page is a mock that asks a *person* to choose success or failure.
    ``revora.providers.razorpay.PaymentProviderClient`` mirrors that surface with five methods
    (``create_payment_link``, ``notify_by``, ``find_payment_links_by_reference_id``,
    ``fetch_payment``, ``list_payments``), so there is nothing for this function to call even if
    it wanted to.

    So no automation is written. Step 2 of R28.C2 — *pay the link* — is a documented manual
    action in ``RUNBOOK.md`` (§ Verified test-mode recoveries), which is what task 53.7 asks for
    where the API is absent: a capability check plus a real manual step, not a fabricated
    automation that would have to pretend a capture happened.

    **This gates the automation, not the counting.** A test-mode run whose links an operator paid
    by hand produces genuine Verified_Demo_Recoveries, and they are counted from the persisted
    reads by :func:`authoritative_test_mode_recoveries` rather than being asserted from this
    boolean. What used to happen here was the dishonest shape: this function returning ``True``
    would have made the report claim exactly ``DEMO_VERIFIED_RECOVERY_MIN_COUNT`` recoveries —
    the constant, not a count of anything.

    **What would change this answer.** A documented endpoint that completes a payment against a
    test-mode link, or a documented test-payment route for Payment Links of the kind that exists
    for BharatQR codes. Then the automation goes behind this check, and the ``transport``
    parameter is what it would reach the provider through — which is why it stays in the
    signature rather than being added later as a change to every caller.

    Args:
        transport: accepted and deliberately unused. See above.
    """
    _ = transport
    return False


def authoritative_test_mode_recoveries(session: Session, merchant_id: uuid.UUID) -> int:
    """Count the Verified_Demo_Recoveries this merchant's rows actually evidence (R28.C2).

    A Verified_Demo_Recovery is defined by R28.C2 as a case that reached ``RECOVERED`` from an
    authoritative provider state read reporting ``captured = true`` with an amount equal to the
    case's ``payment_amount``. Every one of those clauses is a column, so this is a count of rows
    that satisfy the definition rather than a number the loader decided:

    * ``recovery_outcome.verified_by_read_id`` is ``NOT NULL`` by design, so a recorded recovery
      always names the read that verified it. The join cannot silently miss one.
    * ``payment_state_read.captured`` and ``.amount`` are the read's own values, compared against
      the case's ``payment_amount`` here rather than trusted to have been compared upstream. The
      Outcome_Monitor does apply that rule — this asks the rows whether it held.
    * ``recovery_case.state`` must be ``RECOVERED``. A case that recovered and was later
      superseded is not evidence of a recovery that stands.

    **What this cannot see, and who supplies it.** No column records whether the client behind a
    read was Razorpay test mode or ``tests.fakes.razorpay``, and inventing one would be a
    provenance claim written by the thing making it. The caller knows: ``run_demo_batch``'s
    ``script_payment`` is ``None`` exactly when the run takes the provider's own answers, so
    :func:`_conclude` reports this count then and reports zero otherwise. That keeps a harness
    run's scripted captures — which are real reads of a fake — out of a field whose whole meaning
    is *money that moved at the provider*.
    """
    return int(
        session.execute(
            text(
                "SELECT count(*) FROM recovery_outcome o "
                "JOIN payment_state_read r ON r.id = o.verified_by_read_id "
                "AND r.merchant_id = o.merchant_id "
                "JOIN recovery_case c ON c.id = o.case_id AND c.merchant_id = o.merchant_id "
                "WHERE o.merchant_id = :m AND c.state = :recovered "
                "AND r.captured AND r.amount = c.payment_amount"
            ),
            {"m": str(merchant_id), "recovered": CaseState.RECOVERED.value},
        ).scalar_one()
    )


# ---------------------------------------------------------------------------
# Driving the batch
# ---------------------------------------------------------------------------


def _record_consent(
    transport: DemoTransport, tenant: DemoTenant, plans: Sequence[CasePlan]
) -> int:
    """Record consent for every planned contact through ``POST /consent``. Returns how many.

    **Before the webhooks, and the ordering is not cosmetic.** ``CONSENT_MISSING`` is one of the
    twelve policy checks and absence of a consent row is a *fact* rather than a gap in reading
    one, so it fails the check for every customer-visible action. A worker pass drains a
    merchant's whole queue and each pipeline step enqueues its successor inside its own
    transaction, so the first drain runs detection through policy — which means consent recorded
    after "the case exists" is consent recorded after the decision it was meant to govern.

    Through the endpoint rather than by insert, for the same reason as everything else here, and
    with one useful side effect: the endpoint derives the ``customer_key`` from the contact with
    the one HMAC that holds the secret, so the key consent is recorded against is definitionally
    the key the case will carry. Deriving it twice is how a consent record ends up describing a
    different person than the case it governs.
    """
    recorded = 0
    for plan in plans:
        result = transport.request(
            "POST",
            "/consent",
            content=_encode(
                {
                    "contact": plan.contact,
                    "opted_out": False,
                    "source": f"demo-batch-seed-{plan.seed}",
                }
            ),
            headers={**dict(tenant.dashboard_headers), "content-type": "application/json"},
        )
        if result.accepted:
            recorded += 1
    return recorded


_INGEST_WINDOW_MARGIN: Final[timedelta] = timedelta(seconds=1)
"""How far past the ingest rate window the clock is moved to open a fresh one.

One second, because :meth:`revora.platform.ratelimit.RateLimiter.allow` compares
``moment - start >= window`` — so one second past the window *is* the next window, and any
larger margin only ages the batch further in exchange for nothing."""


class _IngestPacer:
    """Keeps the loader's webhook deliveries inside ``INGEST_RATE_LIMIT`` instead of exceeding it.

    **The defect this exists to fix.** ``INGEST_RATE_LIMIT`` defaults to 600 accepted ingestion
    requests per minute per source identifier, and every case this loader seeds arrives at
    ``/webhooks/razorpay/{slug}`` — one source key, one clock. Delivering a 1 000-case batch in
    one tight loop against a frozen clock therefore put 820 main-cohort deliveries into a single
    window: 600 were accepted, 220 came back ``RATE_LIMITED`` → HTTP 429, and the batch reported
    ``seeded_case_count`` 780 against ``case_count`` 1 000 with both arms below the 447-per-arm
    requirement it had computed for itself. The arithmetic is exact and the limiter was right
    every time. **The loader was what was wrong**, and it is the loader that is fixed here: the
    rate limit is a real protection on the only unauthenticated endpoint in Revora and raising,
    overriding or special-casing it for a demonstration would be removing the guard in order to
    demonstrate the system that has it.

    **The window semantics this relies on**, read off ``revora.platform.ratelimit`` rather than
    assumed: the counter is a *fixed* window that resets rather than sliding, keyed per source,
    and it reads **no clock of its own** — every caller passes the instant it already has, and
    ``ingest_webhook`` passes ``revora.platform.clock.now()``. So the same substituted clock the
    harness drives through ``advance`` decides which window a delivery falls in, and pacing the
    deliveries against a moved clock is not a trick played on the limiter: it is a batch arriving
    at the rate a provider would send it at.

    The arithmetic here mirrors ``allow`` exactly — same window start, same ``>=`` comparison —
    so the pacer stays in step with the limiter across the clock advances the *rest* of the run
    makes (``_close_prior_window`` moves a week, ``_run_sweeps`` moves several). A pacer that
    counted deliveries without tracking when its window opened would keep advancing the clock
    after those, for allowance the limiter had already released.

    **One pacer per run, covering every delivery.** The limit is per source identifier and the
    loader is one source, so a per-cohort counter would let the main cohort's tail and the
    captures that resolve it pool their allowances in the same window and refuse the second half
    — which is the original defect displaced rather than fixed.

    **What the clock advances cost, stated rather than left unexamined** — the concern
    :func:`_close_prior_window` is careful about, and the same reasoning applies:

    * Each advance is ``WINDOW + 1s`` = 61 seconds. A 1 000-case batch delivers roughly 1 400
      webhooks in total (1 000 failures, the captures for the cases whose counterfactual
      recovers, and the repeat failures), so it spends **two or three** advances: about three
      minutes across a run whose ``RECOVERY_WINDOW_DURATION`` is seven days. Three minutes is
      0.03% of a 10 080-minute window.
    * It cannot *shorten* a window. ``window_end_at`` is written once, at detection, as
      ``detected_at + RECOVERY_WINDOW_DURATION`` (R2.C5 makes it immutable), so a case seeded
      after an advance gets a full window measured from its own later detection instant. The only
      effect is that cases in a later chunk are detected up to a minute after those in an earlier
      one, which is what a provider delivering a real backlog would also produce.
    * Nothing else in the batch is measured in minutes. ``CUSTOMER_TOKEN_LIFETIME`` is 72 hours,
      ``WAIT_REVIEW_INTERVAL`` 12 hours, ``PROMISE_MIN_LEAD_TIME`` and the follow-up offset are
      driven by explicit advances of their own in :func:`_run_sweeps`. A minute moves none of
      them across a boundary, and every advance moves time *forward*, so no bound that was
      satisfied becomes unsatisfied.
    """

    __slots__ = ("_advance", "_limit", "_spent", "_started", "_window", "advances")

    def __init__(
        self,
        advance: Callable[[timedelta], None],
        limit: int,
        window: timedelta = INGEST_RATE_WINDOW,
    ) -> None:
        self._advance = advance
        self._limit = int(limit)
        self._window = window
        self._started: datetime | None = None
        self._spent = 0
        self.advances = 0
        """How many rate windows the run opened. Reported so a caller can price the ageing."""

    def reserve(self) -> None:
        """Make room for one delivery, moving the clock past the window if the allowance is spent.

        Called immediately before the request rather than after it, because the allowance has to
        exist at the moment the request is made; a check afterwards would be a report of a
        refusal that already happened.

        A limit below one is not paced. There is no allowance to renew in that case, so advancing
        would move the clock forward for ever and never admit anything — the delivery goes out,
        the endpoint refuses it, and :class:`SeedDeliveryShortfallError` says so with the status.
        """
        moment = now()
        if self._started is None or moment - self._started >= self._window:
            self._started, self._spent = moment, 0
        if self._spent >= self._limit and self._limit >= 1:
            self._advance(self._window + _INGEST_WINDOW_MARGIN)
            self.advances += 1
            self._started, self._spent = now(), 0
        self._spent += 1


def _deliver(
    transport: DemoTransport,
    tenant: DemoTenant,
    payload: Mapping[str, object],
    event_id: str,
    *,
    pacer: _IngestPacer,
) -> int:
    """POST one signed webhook the way Razorpay would. Returns the status code.

    The signature is computed over the same bytes that are sent, and the event id goes in the
    header the provider uses — so a batch that produced a case proves signature verification
    over raw bytes, canonicalization and the dedup index all ran.

    Every webhook the loader sends goes through here, which is what lets one :class:`_IngestPacer`
    account for the whole run's ingest allowance rather than each loop guessing at its own share.
    """
    pacer.reserve()
    body = _encode(payload)
    return transport.request(
        "POST",
        f"/webhooks/razorpay/{tenant.slug}",
        content=body,
        headers={
            SIGNATURE_HEADER: sign_payload(tenant.webhook_secret, body),
            EVENT_ID_HEADER: event_id,
            "content-type": "application/json",
        },
    ).status_code


def _failed_payload(plan: CasePlan) -> Mapping[str, object]:
    """The generated ``payment.failed`` body, with an order id the canonicalizer can read.

    The envelope comes from :meth:`~revora.synthetic.generator.GeneratedCase.webhook_payload` —
    the generator authors every payload, so a field-name drift surfaces as a canonicalization
    failure here rather than as a case with empty features. The ``order_id`` is added because the
    generator does not emit one and a case with no order reference is harder to follow on the
    dashboard than it needs to be; it names the demo event, so it is traceable back to the seed.
    """
    payload = dict(plan.case.webhook_payload())
    entity = dict(_payment_entity_of(payload))
    entity["order_id"] = f"order_{plan.seed}_{plan.index}"
    payload["payload"] = {"payment": {"entity": entity}}
    return payload


def _captured_payload(plan: CasePlan) -> Mapping[str, object]:
    """A ``payment.captured`` envelope for this case's payment.

    **The success signal, not the proof of success.** Revora declares no recovery from this: it
    triggers an authoritative read, and the read is what decides (R10.C1). The amount here is the
    case's own amount so the read and the case agree — a read reporting less would be a *partial*
    capture to the Outcome_Monitor, which correctly refuses to call it a recovery.
    """
    return {
        "entity": "event",
        "event": "payment.captured",
        "contains": ["payment"],
        "created_at": 1_700_003_000,
        "payload": {
            "payment": {
                "entity": {
                    "id": plan.provider_payment_id,
                    "amount": int(plan.amount),
                    "amount_refunded": 0,
                    "captured": True,
                    "currency": "INR",
                    "status": "captured",
                    "method": plan.case.payment_method,
                    "created_at": 1_700_003_000,
                }
            }
        },
    }


def _customer_headers(token: str) -> dict[str, str]:
    """The two headers every accepted customer write needs.

    ``Content-Type: application/json`` is mandatory rather than conventional: the surface's guard
    answers 415 **before the body is parsed** if it is absent, so a loader that omitted it would
    get a refusal that says nothing about the submission.
    """
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _submit(
    transport: DemoTransport,
    tenant: DemoTenant,
    token: str,
    route: str,
    payload: Mapping[str, object],
) -> HttpResult:
    """One customer submission through the public surface. 201 means recorded."""
    return transport.request(
        "POST",
        f"/customer/{tenant.slug}/{route}",
        content=_encode(payload),
        headers=_customer_headers(token),
    )


def _assign_customer_roles(
    plans: Sequence[CasePlan],
    tokens: Mapping[uuid.UUID, str],
    case_ids: Mapping[int, uuid.UUID],
) -> dict[int, DemoOutcome]:
    """Decide which seeded cases are driven to which customer-side outcome.

    **After the arms are known, not before**, and that ordering is forced rather than chosen. A
    customer submission needs a live Customer_Access_Token, and a token exists only where a
    customer-visible action was actually executed — which excludes every control-arm case, whose
    action is withheld with ``SUPPRESSED_BY_CONTROL_ARM``, and any case one of the twelve policy
    checks refused. Planning these roles up front would have meant hoping the right cases landed
    in the right arm.

    Only ``COUNTERFACTUAL`` cases are eligible, taken in index order so the assignment is
    reproducible from the seed. Taking them from the shaped roles instead would have overwritten
    an outcome the batch is separately required to contain.
    """
    eligible = [
        plan
        for plan in plans
        if plan.outcome is DemoOutcome.COUNTERFACTUAL
        and case_ids.get(plan.index) is not None
        and case_ids[plan.index] in tokens
    ]
    roles: dict[int, DemoOutcome] = {}
    cursor = 0
    for name in CUSTOMER_DRIVEN_OUTCOMES:
        outcome = DemoOutcome(name)
        for _ in range(_CUSTOMER_ROLE_COUNT):
            if cursor >= len(eligible):
                break
            roles[eligible[cursor].index] = outcome
            cursor += 1
    return roles


def _drive_customer_outcomes(
    transport: DemoTransport,
    tenant: DemoTenant,
    plans: Sequence[CasePlan],
    *,
    roles: Mapping[int, DemoOutcome],
    tokens: Mapping[uuid.UUID, str],
    case_ids: Mapping[int, uuid.UUID],
    window_ends: Mapping[uuid.UUID, datetime],
    moment: datetime,
    config: Configuration,
) -> dict[str, int]:
    """Submit each customer-side outcome through ``/customer/{slug}/…`` (R28.C5, R19.C4).

    Six outcomes, three write shapes, one credential each. Every submission is a real HTTP
    request carrying a real bearer token that the endpoint verifies in constant time against the
    persisted hash — so a batch that produced ``CUSTOMER_DISPUTED_CHARGE`` proves the token path,
    the rate limits, the submission caps, the four-writes-in-one-transaction and the enqueued
    consequence all ran.

    **The two promise dates are computed from the case's own window and the configured bound**,
    not hard-coded. ``PROMISE_MIN_LEAD_TIME`` is a merchant-configurable interval and a date
    inside it is refused with 422; ``window_end_at`` is what separates a promise the system can
    hold from one that escalates. A literal date would make this function pass or fail on
    whichever way a bound had last been configured.

    Returns a count per submitted outcome, so a caller can see what the surface accepted rather
    than what was attempted.
    """
    accepted: dict[str, int] = {}
    lead = config.PROMISE_MIN_LEAD_TIME + timedelta(minutes=5)

    for plan in plans:
        outcome = roles.get(plan.index)
        if outcome is None:
            continue
        case_id = case_ids[plan.index]
        token = tokens[case_id]
        window_end = window_ends.get(case_id, moment + timedelta(days=7))

        match outcome:
            case DemoOutcome.DISPUTE:
                result = _submit(
                    transport,
                    tenant,
                    token,
                    "delay-reason",
                    {
                        "delay_reason": DelayReason.DISPUTES_THE_CHARGE.value,
                        "note": "This charge is not mine.",
                    },
                )
            case DemoOutcome.CANCEL:
                result = _submit(
                    transport,
                    tenant,
                    token,
                    "delay-reason",
                    {
                        "delay_reason": DelayReason.NO_LONGER_WANTS_THE_ORDER.value,
                        "note": "I do not want the order any more.",
                    },
                )
            case DemoOutcome.PARTIAL_ARRANGEMENT:
                result = _submit(
                    transport,
                    tenant,
                    token,
                    "partial-arrangement",
                    {"note": "Could I pay this in parts?"},
                )
            case DemoOutcome.PROMISE_KEPT | DemoOutcome.PROMISE_MISSED:
                # Comfortably inside the window and clear of the lead-time bound, so the promise
                # is one the system can hold. The two differ only in whether the money follows.
                result = _submit(
                    transport,
                    tenant,
                    token,
                    "promise",
                    {"promise_date": (moment + lead).isoformat()},
                )
            case DemoOutcome.PROMISE_BEYOND_WINDOW:
                # Past ``window_end_at``, which is what R23.C5 escalates on. Deliberately not
                # clamped to the window end: a customer who says "not until next month" has said
                # something the recovery window cannot accommodate, and pretending otherwise
                # would schedule a follow-up for a date they did not name.
                result = _submit(
                    transport,
                    tenant,
                    token,
                    "promise",
                    {"promise_date": (window_end + timedelta(days=3)).isoformat()},
                )
            case _:  # pragma: no cover - roles only ever hold the six above
                continue

        if result.accepted:
            accepted[outcome.value] = accepted.get(outcome.value, 0) + 1
        else:
            _logger.warning(
                "demo customer submission refused",
                outcome=outcome.value,
                status_code=result.status_code,
                body=dict(result.body),
            )
    return accepted


# ---------------------------------------------------------------------------
# Reading the batch back
# ---------------------------------------------------------------------------


def _case_ids_by_index(
    session: Session, merchant_id: uuid.UUID, plans: Sequence[CasePlan]
) -> dict[int, uuid.UUID]:
    """Which seeded plan became which Recovery_Case, matched on ``provider_payment_id``.

    Matched on the payment id rather than tracked as the webhooks were posted, because the case
    is created *by the pipeline* and the loader never sees its id. That is the whole shape of
    this module: the identifier a demonstration reads back is one the real path allocated.
    """
    by_payment = {plan.provider_payment_id: plan.index for plan in plans}
    rows = session.execute(
        text(
            "SELECT provider_payment_id, id FROM recovery_case WHERE merchant_id = :m "
            "AND provider_payment_id = ANY(:ids)"
        ),
        {"m": str(merchant_id), "ids": list(by_payment)},
    ).all()
    return {by_payment[str(row[0])]: uuid.UUID(str(row[1])) for row in rows}


def _arms(
    session: Session, merchant_id: uuid.UUID, experiment_id: uuid.UUID
) -> dict[uuid.UUID, ExperimentGroup]:
    """The arm each case was assigned to, read from ``experiment_assignment``.

    Read rather than recomputed from ``assign_group``. The digest is deterministic so the two
    would agree, and that is exactly why reading is better: a batch whose arms were recomputed
    would still balance if the pipeline had failed to persist an assignment at all.
    """
    rows = session.execute(
        text(
            'SELECT case_id, "group" FROM experiment_assignment '
            "WHERE merchant_id = :m AND experiment_id = :e"
        ),
        {"m": str(merchant_id), "e": str(experiment_id)},
    ).all()
    return {uuid.UUID(str(row[0])): ExperimentGroup(str(row[1])) for row in rows}


def _window_ends(
    session: Session, merchant_id: uuid.UUID, case_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, datetime]:
    """Each case's ``window_end_at``, so a promise date can be placed relative to it."""
    if not case_ids:
        return {}
    rows = session.execute(
        text(
            "SELECT id, window_end_at FROM recovery_case "
            "WHERE merchant_id = :m AND id = ANY(:ids)"
        ),
        {"m": str(merchant_id), "ids": [str(case_id) for case_id in case_ids]},
    ).all()
    return {uuid.UUID(str(row[0])): row[1] for row in rows}


def _counts(session: Session, sql: str, params: Mapping[str, object]) -> dict[str, int]:
    """A ``GROUP BY`` reduced to a mapping, with ``NULL`` rendered as the empty string.

    ``NULL`` becomes ``""`` rather than being dropped, because a terminal case with no recorded
    reason is a finding — it is a case that ended and did not say why — and a dropped row would
    make it look like it never happened.
    """
    rows = session.execute(text(sql), dict(params)).all()
    return {("" if row[0] is None else str(row[0])): int(row[1]) for row in rows}


def _audit_sequence_gaps(
    session: Session, merchant_id: uuid.UUID, *, limit: int = 25
) -> tuple[str, ...]:
    """Per-case audit sequences that do not start at 1 and step by exactly 1 (R28.C15).

    Checked in SQL over every attached record of the merchant, because the claim is about *all*
    of them and a sample would be a claim about a sample. Three conditions, and each is a
    different failure: the minimum must be 1 (a sequence that starts at 2 lost its first
    record), the maximum must equal the count (a gap), and the count must equal the number of
    distinct sequence numbers (a duplicate, which the unique index should already forbid).

    This is deliberately a check on a *consequence*. ``AuditWriter`` allocates from
    ``recovery_case.audit_seq`` under the row lock, identically for a seeded case and a real one,
    so a gap-free sequence is not something the loader arranges — it is something the loader
    cannot prevent and therefore something worth verifying rather than asserting.

    Truncated at ``limit`` findings, because a systematic failure would report a thousand
    identical lines and the twenty-fifth adds nothing to the diagnosis.
    """
    rows = session.execute(
        text(
            """
            SELECT case_id, min(seq), max(seq), count(*), count(DISTINCT seq)
            FROM audit_record
            WHERE merchant_id = :m AND case_id IS NOT NULL
            GROUP BY case_id
            HAVING min(seq) <> 1
                OR max(seq) <> count(*)
                OR count(*) <> count(DISTINCT seq)
            LIMIT :limit
            """
        ),
        {"m": str(merchant_id), "limit": limit},
    ).all()
    return tuple(
        f"case {row[0]}: min={row[1]} max={row[2]} count={row[3]} distinct={row[4]}"
        for row in rows
    )


def _real_provenance_rows(
    session: Session, merchant_id: uuid.UUID
) -> dict[str, int]:
    """How many ``REAL``-provenance rows this merchant holds, per table (R28.C16).

    Zero is the only acceptable answer for a demonstration tenant, and the count is taken per
    table rather than summed so a propagation gap names the table it is in. See
    :data:`PROVENANCE_BEARING_TABLES` for the two tables R28.C16 names that cannot be checked
    because they have no column to check.
    """
    counts: dict[str, int] = {}
    for table in PROVENANCE_BEARING_TABLES:
        counts[table] = int(
            session.execute(
                text(
                    f"SELECT count(*) FROM {table} "
                    "WHERE merchant_id = :m AND provenance = :p"
                ),
                {"m": str(merchant_id), "p": Provenance.REAL.value},
            ).scalar_one()
        )
    return counts


def read_coverage(session: Session, merchant_id: uuid.UUID) -> BatchCoverage:
    """Everything R28.C4, C5, C15 and C16 require, read back from persisted rows.

    One function, six queries, no arguments about which cases to look at: a demonstration tenant
    holds nothing but the batch, so "this merchant's rows" and "the batch's rows" are the same
    set — which is also what makes the ``REAL``-provenance count meaningful rather than a filter
    somebody could get wrong.
    """
    return BatchCoverage(
        terminal_states=_counts(
            session,
            "SELECT state, count(*) FROM recovery_case WHERE merchant_id = :m GROUP BY state",
            {"m": str(merchant_id)},
        ),
        terminal_reasons=_counts(
            session,
            "SELECT terminal_reason, count(*) FROM recovery_case WHERE merchant_id = :m "
            "AND terminal_reason IS NOT NULL GROUP BY terminal_reason",
            {"m": str(merchant_id)},
        ),
        selection_reasons=_counts(
            session,
            "SELECT selection_reason, count(*) FROM recommendation WHERE merchant_id = :m "
            "GROUP BY selection_reason",
            {"m": str(merchant_id)},
        ),
        promise_statuses=_counts(
            session,
            "SELECT status, count(*) FROM promise_to_pay WHERE merchant_id = :m GROUP BY status",
            {"m": str(merchant_id)},
        ),
        suppressions=_counts(
            session,
            "SELECT hard_stop_reason, count(*) FROM contact_suppression WHERE merchant_id = :m "
            "GROUP BY hard_stop_reason",
            {"m": str(merchant_id)},
        ),
        audit_sequence_gaps=_audit_sequence_gaps(session, merchant_id),
        real_provenance_rows=_real_provenance_rows(session, merchant_id),
    )


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------


def run_demo_batch(
    tenant: DemoTenant,
    *,
    transport: DemoTransport,
    worker: DemoWorker,
    advance: Callable[[timedelta], None],
    config: Configuration,
    synthetic_run_id: uuid.UUID,
    seed: int = DEMO_SEED,
    case_count: int = DEMO_BATCH_CASE_COUNT,
    prior_cohort_size: int = DEMO_PRIOR_COHORT_SIZE,
    script_payment: Callable[[str, int, bool], None] | None = None,
) -> DemoBatchReport:
    """Seed a Demo_Batch, drive it to terminal states, and report what it produced.

    The whole of R28, in the order the system's own ordering constraints force. Each phase is
    named where it happens; the ones whose *position* is load-bearing say so.

    Args:
        synthetic_run_id: the id the ``synthetic_run`` row will be written under. Supplied by the
            caller rather than minted here, and the reason is an ordering the system imposes: the
            worker registry carries the run id, the registry exists before the first webhook, and
            the row is written by this function. One of the three has to know the id first, and
            the caller is the only one that can tell the other two.
        script_payment: told, per payment, the amount an authoritative read should report and
            whether it should report a capture. Injected because what the provider says is the
            caller's business — in the nightly harness it is the scriptable fake, and against
            Razorpay test mode it is a real link being paid. ``None`` runs the batch with
            whatever the provider client already answers, which is what a test-mode run does.

    Returns:
        A :class:`DemoBatchReport`. Every figure in it is read back out of the database after the
        run rather than accumulated during it, because a counter the loader incremented records
        what the loader believed.

    Raises:
        UnderpoweredDemoBatchError: from :func:`define_demonstration_experiment`, before any case
            is seeded.
        SeedDeliveryShortfallError: if either cohort's deliveries were not all accepted. A batch
            that seeded fewer cases than it was asked for fails here rather than reporting a
            figure computed over a population it does not describe.
    """
    plans = plan_batch(
        seed=seed, case_count=case_count, prior_cohort_size=prior_cohort_size
    )
    prior = [plan for plan in plans if plan.outcome in PRIOR_OUTCOMES]
    main = [plan for plan in plans if plan.outcome not in PRIOR_OUTCOMES]

    # One pacer for the whole run, because ``INGEST_RATE_LIMIT`` is per source identifier and
    # every webhook below is delivered under the same one. See :class:`_IngestPacer`.
    pacer = _IngestPacer(advance, config.INGEST_RATE_LIMIT)

    # -- phase 0: the run row and the experiment, before a single case exists ---------------
    #
    # The experiment has to be ACTIVE before the first webhook, because arm assignment happens
    # inside the transaction that creates the case (R13.C1). A case seeded before activation is
    # unassigned for ever, and an unassigned case is not a control-arm observation — it is
    # ``MERCHANT_INTERVENTION_UNKNOWN``, which is no use as a baseline label.
    with tenant_transaction(tenant.merchant_id) as session:
        run_row = SyntheticRun(
            seed=seed,
            scenario=DEMO_SCENARIO,
            assumptions=_assumptions(plans, seed=seed, case_count=case_count),
            ground_truth=ground_truth_document(plans, seed=seed),
            generator_version=GENERATOR_VERSION,
            case_count=len(plans),
        )
        run_row.id = synthetic_run_id
        run_row.merchant_id = tenant.merchant_id
        session.add(run_row)
        session.flush()
        experiment_id = define_demonstration_experiment(
            session,
            tenant.merchant_id,
            config=config,
            seed=seed,
            case_count=len(plans),
        )
        required_per_arm = int(
            session.execute(
                text(
                    "SELECT required_sample_size_per_group FROM experiment "
                    "WHERE merchant_id = :m AND id = :e"
                ),
                {"m": str(tenant.merchant_id), "e": str(experiment_id)},
            ).scalar_one()
        )

    # -- phase 1: consent, then a minute of clock so it is effective before any evaluation ---
    _record_consent(transport, tenant, plans)
    advance(timedelta(minutes=1))

    # Every payment starts as a read that says "still failed". Without this the provider's
    # default answer would be a capture at the *wrong amount*, which the Outcome_Monitor
    # correctly reads as a partial payment and holds — so the whole batch would stall on a
    # conflicting-signal branch that nothing in the demonstration is about.
    if script_payment is not None:
        for plan in plans:
            script_payment(plan.provider_payment_id, int(plan.amount), False)

    # -- phase 2: the prior cohort, so a high baseline becomes reachable ---------------------
    #
    # Seeded and resolved *before* the main cohort, because the estimator reads history and this
    # is the history. See :data:`DEMO_PRIOR_COHORT_SIZE` and :func:`prior_cohort_split`.
    with capturing_customer_tokens() as prior_tokens:
        _seed_cohort(transport, tenant, prior, worker, pacer=pacer)
    _resolve_cohort(
        transport,
        tenant,
        prior,
        worker=worker,
        experiment_id=experiment_id,
        script_payment=script_payment,
        pacer=pacer,
    )
    _close_prior_window(worker, advance, config)
    _ = prior_tokens

    # -- phase 3: the main cohort ------------------------------------------------------------
    with capturing_customer_tokens() as tokens:
        _seed_cohort(transport, tenant, main, worker, pacer=pacer)

        with tenant_transaction(tenant.merchant_id) as session:
            case_ids = _case_ids_by_index(session, tenant.merchant_id, plans)
            ends = _window_ends(session, tenant.merchant_id, list(case_ids.values()))

        # -- phase 4: the customer surface, while the tokens are live ------------------------
        roles = _assign_customer_roles(main, tokens, case_ids)
        submissions = _drive_customer_outcomes(
            transport,
            tenant,
            main,
            roles=roles,
            tokens=tokens,
            case_ids=case_ids,
            window_ends=ends,
            moment=now(),
            config=config,
        )
    worker.drain()

    # -- phase 5: the money arrives where the counterfactual says it does --------------------
    _resolve_cohort(
        transport,
        tenant,
        [plan for plan in main if roles.get(plan.index) is None],
        worker=worker,
        experiment_id=experiment_id,
        script_payment=script_payment,
        pacer=pacer,
    )
    # The kept promise is the one customer-driven role whose money does arrive.
    _resolve_cohort(
        transport,
        tenant,
        [plan for plan in main if roles.get(plan.index) is DemoOutcome.PROMISE_KEPT],
        worker=worker,
        experiment_id=experiment_id,
        script_payment=script_payment,
        pacer=pacer,
        force_recovery=True,
    )

    # -- phase 6: the sweeps, each after the clock has reached what it sweeps for ------------
    #
    # The second failure is delivered between the review passes and the window close, which is
    # the only interval in which it does anything: before them the cycle budget is unspent, and
    # after them the case is already ``EXPIRED``.
    _run_sweeps(
        worker,
        advance,
        config,
        after_reviews=lambda: _deliver_repeat_failures(
            transport, tenant, main, worker, pacer
        ),
    )

    # -- phase 7: analyse, complete, and read everything back --------------------------------
    return _conclude(
        tenant,
        plans=plans,
        config=config,
        seed=seed,
        synthetic_run_id=synthetic_run_id,
        experiment_id=experiment_id,
        required_per_arm=required_per_arm,
        submissions=submissions,
        transport=transport,
        # ``script_payment is None`` is the loader's only structural evidence that the
        # authoritative reads behind this run's recoveries went to a real provider rather than a
        # scriptable fake — see :func:`authoritative_test_mode_recoveries`.
        provider_reads_live=script_payment is None,
        rate_windows_opened=pacer.advances,
    )


def _assumptions(
    plans: Sequence[CasePlan], *, seed: int, case_count: int
) -> dict[str, object]:
    """The ``synthetic_run.assumptions`` document: everything needed to judge the result.

    The scenario's own assumptions plus the batch's shape, because a reader asking "why did this
    batch produce these outcomes" needs the role mix as much as the ground truth. A result whose
    assumptions are not written down is a result nobody can disagree with.
    """
    spec = scenario(DEMO_SCENARIO)
    roles: dict[str, int] = {}
    for plan in plans:
        roles[plan.outcome.value] = roles.get(plan.outcome.value, 0) + 1
    document = dict(spec.as_assumptions())
    document["demo_seed"] = seed
    document["demo_case_count"] = case_count
    document["demo_roles"] = dict(sorted(roles.items()))
    document["demo_link_amount_range"] = list(DEMO_LINK_AMOUNT_RANGE)
    document["demo_escalation_amount_range"] = list(DEMO_ESCALATION_AMOUNT_RANGE)
    document["demo_provenance"] = DEMO_PROVENANCE.value
    return document


class SeedDeliveryShortfallError(RuntimeError):
    """A cohort was not fully accepted, so the batch is smaller than the design it reports.

    **This is a raise rather than a warning because the alternative is the one dishonesty the
    rest of this module is built to prevent.** ``_seed_cohort`` used to log one line per refusal
    and return a short count; a run that seeded 780 of 1 000 therefore *finished*, printed
    ``observed_recovered_revenue``, and reported both arms below the per-arm sample size it had
    computed for itself — a figure produced from a population that was not the one described
    beside it. It is the same argument task 53.3 makes for asserting outcome coverage after the
    run rather than assuming it: a demonstration whose shortfalls are warnings is a demonstration
    whose numbers nobody can check.

    ``statuses`` carries the refusal status codes and how many of each, because the status is the
    diagnosis. 429 is the ingest rate limit — which, now that :class:`_IngestPacer` paces every
    delivery, means the limit was lowered below what the pacer was told or a second source shared
    the key. 401 is the signature, 413 the payload size, 503 the ack budget. A message that said
    only "short by 220" would send the next reader back to the logs to find out which.
    """

    def __init__(self, requested: int, accepted: int, statuses: Mapping[int, int]) -> None:
        self.requested = requested
        self.accepted = accepted
        self.statuses = dict(statuses)
        observed = ", ".join(
            f"HTTP {status}: {count}" for status, count in sorted(self.statuses.items())
        )
        super().__init__(
            f"requested {requested} seeded cases and {accepted} were accepted; refusals "
            f"observed: {observed or 'none'}. Refusing to run a batch smaller than its design: "
            "a short cohort drops both experiment arms below the sample size the loader computed "
            "and every figure the batch then reports describes a population it does not name"
        )


def _seed_cohort(
    transport: DemoTransport,
    tenant: DemoTenant,
    plans: Sequence[CasePlan],
    worker: DemoWorker,
    *,
    pacer: _IngestPacer,
) -> int:
    """Deliver every ``payment.failed`` in the cohort, then let the pipeline run. Returns 2xx count.

    Delivered first and drained afterwards rather than one at a time, because that is how a
    provider behaves: the ack budget is per delivery and the work is asynchronous, so a batch
    that drained between deliveries would be testing a system nobody deployed. ``pacer`` is what
    keeps "all of them, then drain" inside ``INGEST_RATE_LIMIT`` — see :class:`_IngestPacer`.

    Raises:
        SeedDeliveryShortfallError: if any delivery was refused. Raised before the drain, because
            there is nothing worth draining about a cohort that is already the wrong size.
    """
    accepted = 0
    refusals: dict[int, int] = {}
    for plan in plans:
        status = _deliver(
            transport, tenant, _failed_payload(plan), plan.failed_event_id, pacer=pacer
        )
        if status == 200:
            accepted += 1
        else:
            refusals[status] = refusals.get(status, 0) + 1
            _logger.warning(
                "demo webhook refused",
                index=plan.index,
                status_code=status,
                event_id=plan.failed_event_id,
            )
    if accepted != len(plans):
        raise SeedDeliveryShortfallError(len(plans), accepted, refusals)
    worker.drain()
    return accepted


def _resolve_cohort(
    transport: DemoTransport,
    tenant: DemoTenant,
    plans: Sequence[CasePlan],
    *,
    worker: DemoWorker,
    experiment_id: uuid.UUID,
    script_payment: Callable[[str, int, bool], None] | None,
    pacer: _IngestPacer,
    force_recovery: bool = False,
) -> int:
    """Let the money arrive for the cases whose outcome says it does. Returns how many captures.

    Two steps, in this order and not the other. The provider's answer is scripted **first**, so
    the authoritative read the capture triggers finds a capture at the case's own amount; then
    the ``payment.captured`` webhook is delivered. Reversed, the read would fire against a
    provider still saying "failed" and the case would correctly refuse to recover.

    The arm is read here rather than passed in, because whether a ``COUNTERFACTUAL`` case
    recovers *depends on which arm it landed in* — that is what makes the two arms comparable at
    the individual level, and the arm is a fact the pipeline decided.
    """
    if not plans:
        return 0
    with tenant_transaction(tenant.merchant_id) as session:
        case_ids = _case_ids_by_index(session, tenant.merchant_id, plans)
        arms = _arms(session, tenant.merchant_id, experiment_id)

    delivered = 0
    for plan in plans:
        case_id = case_ids.get(plan.index)
        group = None if case_id is None else arms.get(case_id)
        if not (force_recovery or plan.recovers_in(group)):
            continue
        if script_payment is not None:
            script_payment(plan.provider_payment_id, int(plan.amount), True)
        status = _deliver(
            transport, tenant, _captured_payload(plan), plan.captured_event_id, pacer=pacer
        )
        if status == 200:
            delivered += 1
    worker.drain()
    return delivered


def _close_prior_window(
    worker: DemoWorker, advance: Callable[[timedelta], None], config: Configuration
) -> None:
    """Run the prior cohort's recovery windows out, so its *non*-recoveries are history too.

    **Without this the prior cohort teaches the estimator only that payments recover.** A
    Recovery_Memory observation is written inside the terminal transition, so a case that has not
    ended has not been observed — and the only prior cases that end early are the ones the money
    arrived for. Every case that did *not* recover stays open until its window closes, which the
    lifecycle sweep does at the end of the run, long after the main cohort's baselines were
    estimated. Measured: 31 global observations and 31 recoveries, a recovery rate of exactly
    1.000 for a population whose real rate was 0.200.

    So the clock is moved past ``RECOVERY_WINDOW_DURATION`` and the lifecycle sweep is run *here*,
    while the batch is nothing but its history. The main cohort is seeded afterwards and its own
    windows start then, so nothing else in the batch is aged by this.

    Two passes, for the reason :func:`_run_sweeps` gives: the first pass's terminal transitions
    enqueue an observation write and a token revocation of their own, and a case that expired on
    the first pass has not finished expiring until that work has run.

    This is population shaping, not rule bending. The cases end ``EXPIRED`` because a genuine
    ``RECOVERY_WINDOW_ELAPSED`` was applied to them by the sweep that owns that rule; all this
    decides is *when* they were failing, and history predating the batch is what history is.
    """
    advance(config.RECOVERY_WINDOW_DURATION + timedelta(hours=1))
    worker.tick((LIFECYCLE_EVALUATION_KIND, EXECUTION_RECONCILIATION_KIND))
    worker.drain()
    worker.tick((LIFECYCLE_EVALUATION_KIND,))
    worker.drain()


def _deliver_repeat_failures(
    transport: DemoTransport,
    tenant: DemoTenant,
    plans: Sequence[CasePlan],
    worker: DemoWorker,
    pacer: _IngestPacer,
) -> int:
    """Deliver a second ``payment.failed`` for each ``REPEAT_FAILURE`` case. Returns the 2xx count.

    The same payment, a different event id, the same signed endpoint. Detection finds the open case
    and attaches the event rather than opening a second one, and — because the case is resting at
    ``POLICY_CHECK`` on a null selection — enqueues the decision cycle R30.C7 requires. That cycle
    is the one that finds the cycle budget spent and stops the case (R30.C10).

    Nothing here asserts the case is at the cap or even that it is at ``POLICY_CHECK``: both are
    the pipeline's to decide, and a loader that checked would be a loader arranging the outcome it
    then reports. See :data:`_REPEAT_FAILURE_ROLE_COUNT` for why four are sent.
    """
    delivered = 0
    for plan in plans:
        if plan.outcome is not DemoOutcome.REPEAT_FAILURE:
            continue
        status = _deliver(
            transport, tenant, _failed_payload(plan), plan.repeat_failed_event_id, pacer=pacer
        )
        if status == 200:
            delivered += 1
        else:
            _logger.warning(
                "demo repeat failure refused",
                index=plan.index,
                status_code=status,
                event_id=plan.repeat_failed_event_id,
            )
    worker.drain()
    return delivered


def _run_sweeps(
    worker: DemoWorker,
    advance: Callable[[timedelta], None],
    config: Configuration,
    *,
    after_reviews: Callable[[], object] | None = None,
) -> None:
    """Drive every sweep the batch needs, each one after the clock has reached what it sweeps for.

    Nothing in Revora ticks itself — the ticker role is a separate process — so a batch that
    needs promises missed, reviews fired and windows closed has to drive the sweeps. The order is
    forced by what each one reads:

    1. **The promise sweep**, after the follow-up offset has elapsed. A promise whose date passed
       with no capture becomes ``MISSED``, and that has to happen while the case is still inside
       its window — a case whose window closed first is ``EXPIRED``, and the lifecycle sweep is
       deliberately the only writer of that.
    2. **The review sweep**, once per ``WAIT_REVIEW_INTERVAL``, ``MAX_RECOVERY_ATTEMPTS`` times.
       A case whose selection was a null action rests at ``POLICY_CHECK`` with a
       ``next_review_at``; each pass re-decides it until the decision-cycle cap is spent.

       **The passes alone do not produce ``STOPPED``, and it took a batch to notice.** Once a
       case is at the cap, ``list_due_for_review`` stops returning it on purpose — a sweep that
       queued work whose only outcome is a transition to ``STOPPED`` would be filling the queue
       with pointless jobs — so the capped case rests where it is and the window close below ends
       it ``EXPIRED`` instead. R30.C10's transition belongs to the review *handler*, on a review
       that arrived from one of the other three triggers. ``after_reviews`` is where the batch
       supplies one; see :func:`_deliver_repeat_failures`.
    3. **The lifecycle sweep**, past ``RECOVERY_WINDOW_DURATION``. Everything still open expires,
       which is both R28.C4's ``EXPIRED`` and what makes R28.C8's "every assigned case has
       reached a Terminal_State" true — an experiment cannot complete while a case is in flight.

    Execution reconciliation and payment-state reconciliation are driven alongside, because an
    intent left ``UNCERTAIN`` blocks its case's recovery assessment and a batch is exactly where
    one would appear.

    Args:
        after_reviews: run once between the last review pass and the window close. A callback
            rather than a fourth step in the list, because what it does is deliver webhooks and
            this function is deliberately given no transport.
    """
    advance(config.PROMISE_MIN_LEAD_TIME + config.PROMISE_FOLLOW_UP_OFFSET + timedelta(hours=2))
    worker.tick((PROMISE_SWEEP_KIND, EXECUTION_RECONCILIATION_KIND))
    worker.drain()

    for _ in range(int(config.MAX_RECOVERY_ATTEMPTS) + 1):
        advance(config.WAIT_REVIEW_INTERVAL + timedelta(minutes=5))
        worker.tick((CASE_REVIEW_KIND, PAYMENT_STATE_RECONCILIATION_KIND))
        worker.drain()

    # Between the review passes and the window close, because that is the only interval where a
    # second failure changes anything: the cycle budget is now spent and the case is still open.
    if after_reviews is not None:
        after_reviews()

    advance(config.RECOVERY_WINDOW_DURATION + timedelta(hours=1))
    worker.tick((LIFECYCLE_EVALUATION_KIND, EXECUTION_RECONCILIATION_KIND))
    worker.drain()
    # A second lifecycle pass, because the first one's terminal transitions enqueue work of
    # their own — an observation write, a token revocation — and a case that expired on the
    # first pass has not finished expiring until that work has run.
    worker.tick((LIFECYCLE_EVALUATION_KIND,))
    worker.drain()


def _conclude(
    tenant: DemoTenant,
    *,
    plans: Sequence[CasePlan],
    config: Configuration,
    seed: int,
    synthetic_run_id: uuid.UUID,
    experiment_id: uuid.UUID,
    required_per_arm: int,
    submissions: Mapping[str, int],
    transport: DemoTransport,
    provider_reads_live: bool,
    rate_windows_opened: int,
) -> DemoBatchReport:
    """Complete the experiment, compute the figures, and read the coverage back.

    The experiment moves to ``COMPLETED`` only once every assigned case is terminal (R28.C8), and
    the state is set here rather than by a sweep because nothing in the built system completes an
    experiment on its own — which is honest: deciding a comparison is over is a judgement, and
    the judgement is "the population is resolved".

    ``analyse_experiment`` runs **after** the state change, because the attribution gate reads
    the experiment's state off the row and an analysis computed while it was ``ACTIVE`` would be
    an interim look. The gate refuses this experiment anyway — it carries ``SYNTHETIC`` — and
    that refusal is what R28.C10 is: zero ``Attributed_Recovery``, with no new code path.
    """
    with tenant_transaction(tenant.merchant_id) as session:
        open_cases = int(
            session.execute(
                text(
                    "SELECT count(*) FROM recovery_case c "
                    "JOIN experiment_assignment a ON a.case_id = c.id "
                    "AND a.merchant_id = c.merchant_id "
                    "WHERE c.merchant_id = :m AND a.experiment_id = :e "
                    "AND c.state <> ALL(:terminal)"
                ),
                {
                    "m": str(tenant.merchant_id),
                    "e": str(experiment_id),
                    "terminal": [state.value for state in TERMINAL_STATES],
                },
            ).scalar_one()
        )
        if open_cases == 0:
            session.execute(
                text(
                    "UPDATE experiment SET state = :s, completed_at = now() "
                    "WHERE merchant_id = :m AND id = :e"
                ),
                {
                    "s": ExperimentState.COMPLETED.value,
                    "m": str(tenant.merchant_id),
                    "e": str(experiment_id),
                },
            )
        else:
            _logger.error(
                "demonstration experiment left ACTIVE: assigned cases are still in flight",
                open_cases=open_cases,
                experiment_id=str(experiment_id),
            )

    with tenant_transaction(tenant.merchant_id) as session:
        analysis: ExperimentAnalysis | None = analyse_experiment(
            session, tenant.merchant_id, experiment_id, config=config
        )

    moment = now()
    period = ReportingPeriod(start=moment - timedelta(days=365), end=moment + timedelta(days=1))
    with tenant_transaction(tenant.merchant_id) as session:
        metrics = compute_metrics(session, tenant.merchant_id, period)
        coverage = read_coverage(session, tenant.merchant_id)
        arms = _arms(session, tenant.merchant_id, experiment_id)
        seeded = len(_case_ids_by_index(session, tenant.merchant_id, plans))
        # Counted from the rows that satisfy R28.C2's definition, and reported only where the
        # authoritative reads behind them went to a real provider. A scripted fake produces
        # genuine reads of something that is not the provider, which is not what this field says.
        evidenced = authoritative_test_mode_recoveries(session, tenant.merchant_id)

    verified = evidenced if provider_reads_live else 0

    report = DemoBatchReport(
        seed=seed,
        synthetic_run_id=synthetic_run_id,
        experiment_id=experiment_id,
        required_sample_size_per_group=required_per_arm,
        case_count=len(plans),
        seeded_case_count=seeded,
        control_case_count=sum(
            1 for group in arms.values() if group is ExperimentGroup.CONTROL
        ),
        treatment_case_count=sum(
            1 for group in arms.values() if group is ExperimentGroup.TREATMENT
        ),
        recovered_case_count=metrics.recovered_case_count,
        observed_recovered_revenue=metrics.observed_recovered_revenue,
        natural_recovered_revenue=metrics.natural_recovered_revenue,
        unresolved_revenue=metrics.unresolved_revenue,
        revenue_at_risk=metrics.revenue_at_risk,
        incremental_status=(
            "ESTABLISHED" if metrics.incremental.established else NOT_ESTABLISHED
        ),
        incremental_refusal_codes=metrics.incremental.refusal_codes,
        demonstration_value=metrics.demonstration.value,
        demonstration_labels=metrics.demonstration.labels,
        measured_lift=None if analysis is None else analysis.lift,
        lift_ci_low=None if analysis is None else analysis.lift_ci_low,
        lift_ci_high=None if analysis is None else analysis.lift_ci_high,
        ground_truth_lift=true_average_lift(
            [plan.case for plan in plans], TREATED_ACTION
        ),
        metrics_labels=metrics.labels,
        coverage=coverage,
        verified_test_mode_recoveries=verified,
        customer_submissions=dict(submissions),
    )
    _logger.warning(
        "demo verified test-mode recoveries",
        required=DEMO_VERIFIED_RECOVERY_MIN_COUNT,
        reported=verified,
        recoveries_evidenced_by_authoritative_read=evidenced,
        provider_reads_live=provider_reads_live,
        # Design open question 15, answered: no documented endpoint pays a payment link, so the
        # payment step is a manual RUNBOOK.md action rather than an automation.
        programmatic_test_mode_payment=verified_test_mode_capability(transport),
    )
    _logger.warning(
        "demo batch complete",
        seed=seed,
        case_count=report.case_count,
        rate_windows_opened=rate_windows_opened,
        recovered=report.recovered_case_count,
        observed_recovered_revenue=report.observed_recovered_revenue,
        incremental=report.incremental_status,
        demonstration_value=report.demonstration_value,
        coverage_complete=report.coverage.complete,
        missing=list(report.coverage.missing),
        provenance=DEMO_PROVENANCE.value,
    )
    return report
