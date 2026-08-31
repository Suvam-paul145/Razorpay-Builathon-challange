# Change Impact Analysis

## Change Summary
[What is changing and why]

## Requesting Agent
[Agent ID]

## Affected Contract
[File path and specific interface/type/schema]

## Current State
[What the contract looks like now]

## Proposed Change
[What the contract will look like after]

```diff
- [old definition]
+ [new definition]
```

## Reason for Change
[Why the frozen contract must change]

## Impact Assessment

### Direct Impact
- [Module/file directly affected]

### Dependent Components
| Component | Agent | Impact |
|---|---|---|
| [module] | Agent-[X] | [must update / no change / minor update] |

### Test Impact
| Test | Status After Change |
|---|---|
| `tests/[path]` | BREAKS / OK / NEEDS_UPDATE |

### Data / Migration Impact
- [ ] New migration required
- [ ] Existing data affected
- [ ] Seed data changes

### Security Impact
- [ ] Auth boundaries affected
- [ ] PII handling affected
- [ ] Tenant isolation affected

### Financial Impact
- [ ] Money calculation affected
- [ ] Recovery counting affected
- [ ] Policy evaluation affected

### Demo Impact
- [ ] Synthetic generator affected
- [ ] Demo scenario affected

## Impact Classification

| Level | Criteria |
|---|---|
| ☐ LOW | Internal to one module, no shared contracts |
| ☐ MEDIUM | Touches shared interface, additive only |
| ☐ HIGH | Changes shared interface shape |
| ☐ CRITICAL | Changes financial, security, or state-machine semantics |

## Required Actions

### Agents to Notify
- Agent-[X]: [what they need to do]

### Agents to Stop
- Agent-[Y]: [reason for stopping]

### Human Approval Required?
[Yes/No — Yes if CRITICAL or if it hits decision-checkpoints.md]

## Decision
| Decision | By | Date |
|---|---|---|
| [APPROVED / REJECTED / DEFERRED] | [human/orchestrator] | [date] |
