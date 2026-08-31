# Skill: Integration and Quality Gates

## Mission
Parallel work is not complete when agents finish. It is complete only after integration verification. This skill defines the verification gates at agent level and integration level.

## Agent-level completion gate

Before an agent declares completion, ALL of these must be true:

1. ☐ Implementation matches the assigned contract (Skill 14).
2. ☐ Tests added for new functionality.
3. ☐ All existing tests still pass.
4. ☐ No unrelated files modified.
5. ☐ Shared contracts preserved (no undocumented changes).
6. ☐ Security reviewed where relevant (Skill 06).
7. ☐ Audit behavior preserved (append-only, masked, gap-free).
8. ☐ Error handling implemented (no swallowed exceptions).
9. ☐ Documentation updated where required.
10. ☐ Agent completion report filed (`.kiro/orchestration/templates/agent-completion-report.md`).

## Integration gate

After combining multiple agents' work, run in this order:

1. **Formatting** — `ruff format --check`
2. **Linting** — `ruff check`
3. **Type/schema validation** — `mypy --strict` on scoped modules
4. **Import contracts** — `lint-imports`
5. **No-float check** — `python scripts/check_no_float.py`
6. **Unit tests** — `pytest tests/properties -m pure`
7. **Database tests** — `pytest tests/persistence` (requires testcontainers)
8. **Integration tests** — `pytest tests/integration`
9. **Security checks** — secret exposure scan, RLS verification
10. **End-to-end critical workflow** — the Revora critical path (below)

## Revora critical path verification

The following flow MUST work end-to-end after integration:

```
Razorpay webhook (signed, raw body)
  ↓
Signature verification (HMAC-SHA256, multi-secret window)
  ↓
Canonicalization (round-trip self-check)
  ↓
Deduplication (provider_event_id unique constraint)
  ↓
Detection (deterministic rules → AT_RISK verdict)
  ↓
Case creation (partial unique index guard)
  ↓
Experiment assignment (HMAC-SHA256 deterministic)
  ↓
Diagnosis (deterministic mapping table)
  ↓
Baseline estimation (Beta-Binomial posterior)
  ↓
Candidate estimation (eligibility table lookup)
  ↓
Value optimization (integer arithmetic, argmax)
  ↓
Recommendation (all candidates recorded)
  ↓
Policy evaluation (12 ordered checks, pure function)
  ↓
Policy decision persisted (before authorization released)
  ↓
Execution intent committed (before provider call)
  ↓
Razorpay Payment Link created (exactly once per key)
  ↓
Result classified (5-way: Success/ClientError/ServerError/Timeout/Unclassifiable)
  ↓
Outcome wait (WAITING_FOR_OUTCOME state)
  ↓
payment.captured webhook or reconciliation
  ↓
Authoritative payment read (fetch_payment → verified captured)
  ↓
RECOVERED / STOPPED / BLOCKED / ESCALATED / EXPIRED / FAILED
  ↓
Memory observation (same transaction as terminal transition)
  ↓
Metrics aggregation (BIGINT sums, causality gating)
  ↓
Audit trail (gap-free, append-only, one-query readable)
```

## Financial quality gate

Prove each of these before declaring a milestone complete:

| # | Invariant | Test approach |
|---|---|---|
| 1 | Duplicate events do not duplicate cases | Property test 10.3 |
| 2 | Duplicate requests do not duplicate external actions | Property test 20.7 (crash plan) |
| 3 | Paid cases cannot execute recovery actions | Property test 21.5 (P7) |
| 4 | Opt-out blocks all customer communication | Property test 17.6 (P8) |
| 5 | High-risk cases block automation | Policy scenario test (check 10) |
| 6 | Maximum retries are enforced | Property test 11.5 (P9) |
| 7 | Workflow expires correctly | Property test 11.5 (P6) with clock advancement |
| 8 | Customer payment while action queued cancels execution | Outcome monitor test 21.3 |
| 9 | AI cannot bypass policy | Property test 17.5 (P2) — replace AI fields, re-evaluate |
| 10 | Recovered revenue is not double-counted | Property test 25.5 (P20) — partition exactness |

## Wave completion gates

### After Wave 0 (Foundation)
- Import contracts fail on deliberate violation.
- `mypy --strict` clean on `domain`, `policy`, `optimizer`.
- No-float check fires on deliberate float.
- All domain property tests pass.

### After Wave 1 (Persistence)
- Every constraint test passes against real Postgres.
- RLS returns nothing for cross-tenant query.
- Audit UPDATE raises.
- Append-only trigger rejects mutation.

### After Wave 2 (Ingest to Lifecycle)
- Signed `payment.failed` delivered twice → one event, one case, one verdict.
- Gap-free audit trail.
- Sweeper terminates a case with worker idle.

### After Wave 3 (Decision Pipeline)
- Failed payment → recommendation + policy decision + 12 check outcomes.
- Zero external calls at this stage.
- AI field replacement produces identical policy verdict (P2).

### After Wave 4 (Execution & Outcome)
- Property 3 passes against crash plan with real Postgres.
- Timeout-with-effect-created reconciles to CONFIRMED without duplicate link.
- Exactly one provider create-call per idempotency key.

### After Wave 5 (Measurement)
- Null synthetic scenario reports `CAUSALITY_NOT_ESTABLISHED`.
- No surface presents observed recovery as incremental.
- Interval coverage ~95% across 200 seeds.

### After Wave 6 (Surfaces & System)
- Full pipeline integration test passes.
- Same test with LLM hard-failing produces identical terminal states.
- Degradation and restart tests pass.

## Final orchestration quality gate

Before declaring a major milestone complete, answer ALL:

| Dimension | Question |
|---|---|
| **Product** | Did we build the intended product? |
| **Architecture** | Does implementation match approved architecture? |
| **Financial Safety** | Can unsafe financial actions occur? |
| **AI Safety** | Can AI bypass deterministic authority? |
| **Security** | Are trust boundaries protected? |
| **Data** | Are metrics and recovery classifications correct? |
| **Reliability** | Does the system survive failure? |
| **Testing** | Were critical failure cases tested? |
| **Integration** | Do all parallel workstreams work together? |
| **Auditability** | Can we explain what happened and why? |
| **Measurement** | Can we demonstrate ₹ recovered? |
| **Simplicity** | Did parallel agents introduce unnecessary complexity? |
| **Demo** | Can the complete story be demonstrated? |

## Forbidden shortcuts

Never allow, regardless of time pressure:
- Disabling tests
- Bypassing policy
- Mocking critical behavior without labeling
- Silently changing schemas
- Copying logic between agents
- Hardcoding secrets
- Ignoring provider errors
- Swallowing exceptions
- Disabling validation
- Weakening idempotency
- Bypassing audit
- Creating duplicate implementations
- Merging incompatible contracts

## Related skills
- Skill 04: Financial Workflow Safety — the invariants being tested
- Skill 07: Testing & Failure Analysis — test methodology
- Skill 12: Multi-Agent Orchestration — the pipeline that reaches these gates
