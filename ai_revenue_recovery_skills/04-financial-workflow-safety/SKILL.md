# Skill: Financial Workflow Safety

## Mission
Prevent unsafe automation, duplicate actions, unbounded retries, and misleading financial measurement.

## Non-negotiable rule
AI recommendation ≠ authority.

The deterministic policy layer is the final authority for any recovery action.

## Required safety controls
For every action define:
- authorization
- preconditions
- idempotency
- cooldown
- maximum attempts
- maximum workflow duration
- stop conditions
- escalation conditions
- confirmation mechanism
- rollback/compensation where applicable

## Mandatory stopping rules
At minimum:
PAID → STOP
OPTED_OUT → STOP
HIGH_RISK/FRAUD → BLOCK AUTOMATION + ESCALATE
MAX_RETRIES → STOP/ESCALATE
MAX_COMMUNICATIONS → STOP
WINDOW_EXPIRED → STOP
HUMAN_OWNERSHIP → STOP AUTOMATION
DUPLICATE_ACTION → DO NOT RE-EXECUTE

## Event safety
Assume every external event can be:
- duplicated
- delayed
- out of order
- missing
- malformed
- replayed

Do not allow a webhook to directly bypass policy and trigger repeated actions.

## Execution protocol
1. Load current authoritative state.
2. Verify expected version/state if concurrency matters.
3. Evaluate deterministic policy.
4. Check idempotency.
5. Reserve/record action intent where needed.
6. Execute external action.
7. Persist confirmed result.
8. Write audit event.
9. Schedule/await outcome.
10. Reconcile uncertainty.

## Measurement integrity
Separate:
- revenue at risk
- attempted recovery
- observed recovery
- attributed recovery
- proven causal uplift

Never claim causal uplift without an appropriate experimental/causal design.

## Required audit fields
case_id, event_id, timestamp, actor, prior_state, new_state,
reason, evidence, policy result, action, action result,
idempotency key, correlation ID.

## Review questions
- Can this action happen twice?
- Can payment occur while this action is queued?
- What happens after timeout?
- Who has final authority?
- How does automation stop?
- Can the result be reconciled?
