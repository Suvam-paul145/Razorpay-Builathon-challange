# Skill: Cross-Agent Review

## Mission
Act as an independent reviewer after parallel implementation. The reviewer MUST NOT assume "Agent finished = feature correct."

This skill is invoked after agents complete their work and before integration is declared successful.

## Review dimensions

### Architecture
- Does the implementation match the approved architecture in `design.md`?
- Did an agent introduce unnecessary complexity (new patterns, layers, abstractions)?
- Were new dependencies added without approval?
- Are import contracts preserved (`lint-imports` passes)?
- Does the module structure follow the design's module map?

### Domain
- Are state transitions legal per `revora/domain/transitions.py`?
- Are domain invariants preserved?
- Do enums match the design's declared values?
- Is money handled as integer minor units with no float anywhere?
- Are probabilities bounded [0, 1] at 4 decimal places?

### Financial
- Can money be lost or misreported?
- Can an action execute twice for the same case/ordinal?
- Can a paid customer receive an unnecessary recovery action?
- Are all stopping rules enforced (PAID, OPTED_OUT, HIGH_RISK, MAX_RETRIES, WINDOW_EXPIRED)?
- Is recovered revenue counted exactly once?
- Does `DO_NOTHING` produce exactly zero net value?

### AI
- Is AI actually necessary for each place it is used?
- Can malformed model output cause damage?
- Can AI bypass the deterministic policy engine?
- Are prompt contracts enforced (allow-list, not block-list)?
- Does the system function identically with AI unavailable?

### Security
- Can one merchant access another merchant's data?
- Are secrets exposed in logs, audit records, or error messages?
- Are webhooks verified against raw bytes before any parsing?
- Can external content manipulate model behavior (prompt injection)?
- Is RLS enforced on every tenant-scoped table?
- Are PII fields masked at write time?

### Data
- Are metrics correct (integer sums, proper causality gating)?
- Is synthetic data labelled `SYNTHETIC` on every surface?
- Is observed recovery confused with causal recovery anywhere?
- Does `incremental_recovered_revenue` show `NOT_ESTABLISHED` without an adequate experiment?
- Are refund amounts captured for later restatement?

### Testing
- Were failure scenarios tested (crash, timeout, partial failure, concurrent update)?
- Were concurrency conditions tested (two workers, advisory locks)?
- Do property tests cover the declared properties?
- Is the null synthetic scenario gating CI?
- Are example tests present for known-vector verification?

### UX
- Does the dashboard clearly distinguish: recommendation vs. policy decision vs. execution vs. outcome?
- Are absent values shown as "not yet recorded" (not zero, not dash)?
- Are money values server-formatted (no client-side arithmetic)?
- Are causality labels present on every recovery figure?
- Are all rejected alternatives visible on the case detail?

## Agent communication protocol

Every agent completion MUST produce a structured report:

```markdown
### STATUS
COMPLETE / BLOCKED / NEEDS_REVIEW

### IMPLEMENTED
What changed (summary).

### FILES
Files created or modified (list).

### CONTRACTS
Contracts created, consumed, or changed.

### TESTS
Tests added and their pass/fail status.

### RISKS
Known risks or edge cases.

### ASSUMPTIONS
New assumptions made during implementation.

### DECISIONS
Important implementation decisions and rationale.

### INTEGRATION NOTES
What another agent needs to know to integrate with this work.

### BLOCKERS
What prevents continuation (if any).
```

Use the template at `.kiro/orchestration/templates/agent-completion-report.md`.

## Review process

1. Read every agent's completion report.
2. For each dimension above, check the relevant files.
3. Run the integration gate checks (Skill 15).
4. Identify conflicts between agents.
5. Produce a review verdict:

```
APPROVED — work integrates cleanly
APPROVED_WITH_NOTES — minor issues noted, agent may proceed
CHANGES_REQUIRED — specific changes needed before integration
BLOCKED — fundamental conflict requiring resolution
```

6. For `CHANGES_REQUIRED` or `BLOCKED`, specify:
   - What is wrong
   - Which requirement or property is violated
   - What the fix should be
   - Which agent owns the fix

## Conflict resolution between agents

If two agents produce conflicting implementations:

1. Identify which requirement applies.
2. Identify which architectural decision applies.
3. Determine which implementation preserves invariants.
4. Prefer the simpler implementation.
5. Prefer the one with lower risk.
6. Prefer the one with better test coverage.
7. Prefer the one requiring fewer downstream changes.

Resolution: KEEP A / KEEP B / COMBINE / REDESIGN / ESCALATE.

Never merge both blindly.

## Demo review

Before any demo milestone:
- Can the entire recovery story be reproduced from webhook to ₹ recovered?
- Does the null scenario prevent false claims?
- Is every label, classification, and caveat visible?
- Are synthetic figures clearly distinguished from real ones?

## Related skills
- Skill 06: Security Review — security dimension details
- Skill 07: Testing & Failure Analysis — testing dimension details
- Skill 04: Financial Workflow Safety — financial dimension details
- Skill 15: Integration Quality Gates — the gates this review checks
