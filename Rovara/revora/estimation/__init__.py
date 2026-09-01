"""Estimation: the baseline, and the candidate priors measured against it.

Two components, and one sentence that governs both: **this is a calibrated prior with
an explicit interval, not a learned system.**

``baseline`` answers "what happens if Revora does nothing" as a Beta-Binomial posterior
over a hierarchical feature segment, with a 95 percent interval computed exactly. At
zero observations that interval is ``[0.025, 0.975]`` and it stays that wide all the way
to the dashboard, because a number derived from a uniform prior has to look like one.

``candidates`` prices every action, including the two null ones, against that baseline.
It is a lookup over configured priors, and it says so in its name — the design's
amendment list explicitly replaces the word "simulator" with what the thing actually is.

Three things classified BUILD LATER are absent rather than stubbed: the fitted logistic
regression, the isotonic calibration, and the bootstrap interval. Absent rather than
stubbed on purpose. A stub is an invitation to fill it in, and filling any of these in
without the experiment's control arm would train an estimator on intervened outcomes and
label the result a baseline — which is the specific mistake R5's preamble exists to
prevent.

The optional calibration report (task 15.5) is also not built. Its consequence is
visible in the code rather than hidden: :func:`~revora.estimation.baseline.estimate_baseline`
records ``CALIBRATION_UNVERIFIED`` for a segment that has control observations, because
"data exists" and "the data has been checked against the prediction" are different
statements and only one of them has happened.

What the value optimizer consumes from here: a ``baseline_estimate`` row and one
``candidate_estimate`` row per member of the set, including the members marked
``UNAVAILABLE``. Probabilities are ``NUMERIC(6,4)`` decimals, costs are ``BIGINT``
integer minor units, and no figure in either table has ever passed through a binary
approximation.
"""

from __future__ import annotations

from revora.domain.segments import (
    BACKOFF_ORDER,
    FEATURE_KEYS,
    AmountBand,
    AttemptOrdinalBand,
    ErrorSourceBand,
    PaymentMethodBand,
    SegmentFeatures,
    SegmentLevel,
    segment_id_for,
)
from revora.estimation.baseline import (
    BASELINE_MODEL_VERSION,
    UNCERTAINTY_UNAVAILABLE,
    BaselineComputation,
    BaselineFailure,
    BaselineFigures,
    BaselineOutcome,
    MemoryUnavailableError,
    SelectedSegment,
    estimate_baseline,
    run_baseline_estimation,
    select_segment,
)
from revora.estimation.beta import (
    UNIFORM_PRIOR,
    BetaPosterior,
    BetaPrior,
    beta_cdf,
    central_interval,
    posterior_mean,
)
from revora.estimation.candidates import (
    COST_PRIORS,
    UPLIFT_PRIORS,
    CandidateFigures,
    CandidateOutcome,
    CandidateSet,
    build_candidate_set,
    candidate_figures,
    run_candidate_estimation,
    wait_probability,
)

__all__ = [
    "BACKOFF_ORDER",
    "BASELINE_MODEL_VERSION",
    "COST_PRIORS",
    "FEATURE_KEYS",
    "UNCERTAINTY_UNAVAILABLE",
    "UNIFORM_PRIOR",
    "UPLIFT_PRIORS",
    "AmountBand",
    "AttemptOrdinalBand",
    "BaselineComputation",
    "BaselineFailure",
    "BaselineFigures",
    "BaselineOutcome",
    "BetaPosterior",
    "BetaPrior",
    "CandidateFigures",
    "CandidateOutcome",
    "CandidateSet",
    "ErrorSourceBand",
    "MemoryUnavailableError",
    "PaymentMethodBand",
    "SegmentFeatures",
    "SegmentLevel",
    "SelectedSegment",
    "beta_cdf",
    "build_candidate_set",
    "candidate_figures",
    "central_interval",
    "estimate_baseline",
    "posterior_mean",
    "run_baseline_estimation",
    "run_candidate_estimation",
    "segment_id_for",
    "select_segment",
    "wait_probability",
]
