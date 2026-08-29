# Skill: Testing, Reliability & Failure Analysis

## Mission
Prove behavior under failure, not just the happy path.

## Testing pyramid
1. Unit tests: domain rules and state transitions.
2. Integration tests: database, APIs, webhooks, external adapters.
3. End-to-end tests: critical recovery workflows.
4. Evaluation tests: AI decisions and business metrics.

## Mandatory scenarios
- duplicate event
- out-of-order event
- late payment success
- action executed twice
- API timeout
- external success with lost response
- AI timeout
- malformed AI output
- concurrent updates
- customer opt-out
- fraud block
- maximum retry reached
- recovery window expiration
- human takeover
- metric calculation error

## Financial invariant tests
Examples:
- recovered amount cannot be negative
- case cannot be recovered twice for same outcome
- terminal cases cannot execute new actions
- opt-out blocks communication
- duplicate event does not duplicate action
- policy cannot be bypassed by AI output

## Failure analysis
For each critical path ask:
What fails?
How is failure detected?
What state remains?
Can it be retried safely?
Can it be reconciled?
Can an operator recover it?
Is there an audit trail?

## Idempotency test
Run the same command/event multiple times and prove the resulting authoritative state and external action count are safe.

## Output
### Scenario
### Expected Behavior
### Failure Injection
### Invariant
### Test Level
### Recovery Behavior
### Result
