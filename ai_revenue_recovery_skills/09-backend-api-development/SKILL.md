# Skill: Backend & API Development

## Mission
Implement explicit, maintainable, testable domain workflows.

## Development rules
- Understand existing code before modifying it.
- Do not silently rewrite unrelated systems.
- Preserve contracts unless change is deliberate and documented.
- Prefer small modules with explicit responsibilities.
- Validate inputs at boundaries.
- Keep business rules out of UI code.
- Make side effects explicit.

## API design
For each endpoint define:
- actor
- authorization
- input schema
- output schema
- state preconditions
- idempotency requirements
- errors
- audit effects

## Domain-first workflow
Route/controller
→ application service
→ domain validation/state transition
→ persistence
→ external adapter
→ audit/observability

Avoid giant controllers and hidden side effects.

## Concurrency
For state-changing commands consider:
- duplicate requests
- stale reads
- optimistic locking/versioning where useful
- idempotency keys
- race between queued action and payment success

## Error handling
Do not swallow errors.
Classify:
- validation error
- authorization error
- transient external failure
- permanent external failure
- unknown failure

Return safe errors to clients and record diagnostic context without leaking secrets.

## Before coding
State:
- intended behavior
- affected files
- domain invariants
- tests to add/change
- migration/compatibility risks

## After coding
Verify:
- tests pass
- types/schemas validate
- critical paths are covered
- failure modes considered
- no secrets exposed
- audit behavior correct
