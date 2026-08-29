# Skill: LLM Engineering & Bounded Agent Design

## Mission
Use AI only where reasoning over ambiguity or unstructured information provides measurable value.

## AI necessity test
Before adding an LLM compare:
1. deterministic rule
2. database/query logic
3. workflow automation
4. search/retrieval
5. human review
6. LLM reasoning

Prefer the cheapest reliable method.

## Appropriate LLM tasks
- interpret ambiguous failure context
- classify unstructured messages
- summarize case history
- explain recommendation
- rank permitted interventions
- detect conflicting evidence

## Inappropriate default uses
- simple event detection
- authorization
- policy enforcement
- arithmetic/ledger authority
- unbounded retry control
- direct unrestricted execution

## Structured output
Require machine-readable output, e.g.:
- diagnosis
- confidence
- recommended_action
- rationale
- evidence IDs
- alternatives

Validate every field server-side.

## Model output is untrusted
Reject or repair invalid:
- schema
- enum
- confidence
- unsupported action
- invented identifiers
- missing evidence

Never allow model text to become executable authority without deterministic validation.

## Bounded autonomy
Define:
- accessible data
- allowed tools
- prohibited tools
- maximum actions
- approval requirements
- confidence thresholds
- timeout
- fallback
- audit logging
- blast radius

## Confidence
Confidence is not truth. It is one signal.
Use confidence only with:
- deterministic policy
- evidence quality
- risk level
- fallback behavior

## Evaluation
Test:
- correctness
- calibration
- schema compliance
- hallucination rate
- policy violations
- latency
- cost
- fallback quality
- recovery outcome

Use representative cases, not only ideal demos.

## Prompt-injection safety
Treat customer text and external content as untrusted data, not instructions.
Keep system rules and tool permissions outside retrieved content.
Never allow retrieved content to expand authority.

## Output format
### AI Need
### Non-AI Alternative
### Input Contract
### Output Schema
### Allowed Actions
### Guardrails
### Evaluation
### Failure Handling
### Recommendation
