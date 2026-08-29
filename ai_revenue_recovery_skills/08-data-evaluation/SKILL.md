# Skill: Data, Experiments & Evaluation

## Mission
Measure whether the system actually improves recovery decisions.

## Synthetic data rules
If real data is unavailable:
- label it synthetic
- define generation assumptions
- include ground truth
- include noise and edge cases
- make generation reproducible
- include recoverable and unrecoverable cases

Never present synthetic data as real merchant data.

## Core metrics
- revenue_at_risk
- recovered_revenue
- recovery_rate
- intervention_success_rate
- average_time_to_recovery
- escalation_rate
- blocked_rate
- unresolved_revenue
- cost_per_recovery

## Baselines
Always compare against a meaningful baseline, e.g.:
- retry everyone
- same reminder for everyone
- deterministic fixed strategy
- random allowed strategy where appropriate

## Attribution
Do not equate later payment with causal AI impact.

Use precise language:
- observed recovery after intervention
- attributed recovery under defined rules
- causal uplift only when methodology supports it

## Evaluation workflow
1. Define hypothesis.
2. Define primary metric.
3. Define baseline.
4. Freeze dataset/scenarios.
5. Run both strategies under comparable constraints.
6. Report aggregate and segment results.
7. Report failures and limitations.
8. Avoid cherry-picking.

## Segment analysis
Compare performance by:
- diagnosis
- amount band
- intervention
- event type
- confidence band
- policy outcome

## Output
### Hypothesis
### Dataset
### Ground Truth / Assumptions
### Baseline
### Method
### Metrics
### Results
### Limitations
### Conclusion
