# Skill: Agent Contracts and Ownership

## Mission
Prevent multiple agents from corrupting each other's work by establishing explicit contracts, file ownership, and shared-contract coordination protocols.

## Agent contract

Every assigned agent MUST receive this contract before starting work:

```
### Agent ID
### Role (from Domains A–K)
### Objective (one sentence)
### Required Skills (which skills 00–16 to read)
### Inputs (what to read before coding)
### Dependencies (tasks that must be complete)
### Owned Files (files this agent creates/modifies)
### Files Allowed to Read (not modify)
### Files Forbidden to Modify
### Public Contracts Used (interfaces consumed)
### Public Contracts Allowed to Change (if any)
### Expected Outputs (deliverables)
### Tests Required
### Verification Required (how to prove correctness)
### Integration Dependencies (what must combine with this)
### Stop Conditions (when to halt and report)
```

Use the template at `.kiro/orchestration/templates/agent-contract.md`.

## File ownership rules

**ONE PRIMARY OWNER PER FILE.**

If multiple agents need the same file:

| Strategy | When to use |
|---|---|
| Freeze the shared file | Contract is stable, no agent needs to change it |
| Create an interface/adapter | Agents need different behavior from the same module |
| Split responsibility | File can be cleanly divided into separate files |
| Single owner + recommendations | One agent owns; others propose changes via review |

## Domain-to-module ownership

See `.kiro/orchestration/domain-ownership.md` for the complete mapping. Summary:

| Domain | Primary Modules | Owner Role |
|---|---|---|
| A — Governance | `.kiro/`, `ai_revenue_recovery_skills/` | Architect |
| B — Razorpay | `revora/providers/`, `revora/ingestion/signature.py` | Razorpay Agent |
| C — Recovery | `revora/cases/`, `revora/detection/`, `revora/diagnosis/` | Recovery Agent |
| D — Financial Safety | `revora/policy/`, `revora/optimizer/` | Policy Agent |
| E — AI/Reasoning | `revora/reasoning/` | AI Agent |
| F — ML/Evaluation | `revora/estimation/`, `revora/experiment/`, `revora/metrics/`, `revora/memory/` | ML Agent |
| G — Backend | `revora/api/`, `revora/jobs/`, `revora/persistence/` | Backend Agent |
| H — Frontend | `web/` | Frontend Agent |
| I — Security | `revora/platform/secrets.py`, `revora/platform/crypto.py`, `revora/api/auth.py` | Security Agent |
| J — Testing | `tests/` | Test Agent |
| K — Demo | `revora/synthetic/`, demo scripts | Demo Agent |

## Shared contracts requiring coordination

These files/interfaces require stronger coordination because multiple domains depend on them:

### Database schema (`revora/persistence/models/`)
- **Owner:** Backend Agent (Domain G)
- **Consumers:** All domain agents
- **Coordination:** Schema changes require impact analysis (Skill 15). All dependent agents stop until migration is confirmed.

### API contracts (`revora/api/routers/`)
- **Owner:** Backend Agent (Domain G)
- **Consumers:** Frontend Agent (Domain H)
- **Coordination:** DTO shapes frozen before frontend begins. Changes require frontend notification.

### Domain state transitions (`revora/domain/transitions.py`)
- **Owner:** Recovery Agent (Domain C)
- **Consumers:** Case manager, execution engine, outcome monitor, policy engine, audit, experiment
- **Coordination:** Transition table is frozen in Wave 0. Any change stops all dependent agents.

### Domain enums (`revora/domain/enums.py`)
- **Owner:** Recovery Agent (Domain C)
- **Consumers:** Nearly every module
- **Coordination:** Enum additions are additive and low-risk. Enum removals or renames require full impact analysis.

### Policy interface (`revora/policy/input.py`, `revora/policy/engine.py`)
- **Owner:** Policy Agent (Domain D)
- **Consumers:** Execution engine, case manager
- **Coordination:** PolicyInput shape frozen before execution engine begins.

### Provider interface (`revora/providers/`)
- **Owner:** Razorpay Agent (Domain B)
- **Consumers:** Execution engine, outcome monitor, backfill
- **Coordination:** Provider protocol frozen before execution engine begins.

### Platform services (`revora/platform/`)
- **Owner:** Security Agent (Domain I) for crypto/secrets; Backend Agent for clock/logging/config
- **Consumers:** All modules
- **Coordination:** Platform services are foundation-tier; frozen in Wave 0.

## Contract freezing protocol

Before parallel implementation, freeze:
1. Entity definitions (`revora/domain/`)
2. State machine (`revora/domain/transitions.py`)
3. API interfaces (DTO shapes)
4. Event schemas (`PaymentEventCanonical`)
5. Provider interfaces (`revora/providers/` protocol)
6. Policy interface (`PolicyInput`, `PolicyDecision`)
7. Metric definitions (aggregate types)
8. Audit schema (`audit_record` columns)

### If a frozen contract must change:

1. **STOP** all dependent agents.
2. Evaluate:
   - What changed and why?
   - Who is affected?
   - Which tests break?
   - Which agents must revise?
   - Is human approval required? (Yes if it hits decision-checkpoints.md)
3. Use the change impact analysis template at `.kiro/orchestration/templates/change-impact-analysis.md`.
4. Classify impact: LOW / MEDIUM / HIGH / CRITICAL.
5. For HIGH or CRITICAL: create an explicit change record. Do not silently propagate.

## Change impact analysis

Before an agent changes a shared component, calculate:

```
DIRECT IMPACT
+ DEPENDENT COMPONENTS
+ TEST IMPACT
+ DATA/MIGRATION IMPACT
+ SECURITY IMPACT
+ FINANCIAL IMPACT
+ DEMO IMPACT
= IMPACT CLASSIFICATION
```

| Level | Criteria | Action |
|---|---|---|
| LOW | Internal to one module, no shared contracts | Agent proceeds |
| MEDIUM | Touches a shared interface, additive only | Notify dependent agents |
| HIGH | Changes a shared interface shape | Stop dependent agents, impact analysis required |
| CRITICAL | Changes financial, security, or state-machine semantics | Human approval required |

## Pre-code analysis (every agent)

Before writing code:
1. Read relevant requirements.
2. Read relevant design section.
3. Read related tasks.
4. Read applicable skill(s).
5. Identify dependencies.
6. Identify owned files.
7. Identify shared contracts.
8. Identify tests.
9. Identify security/financial implications.
10. Confirm implementation is within scope.

## Related skills
- Skill 12: Multi-Agent Orchestration — assignment pipeline
- Skill 13: Parallel Workstream Design — which tasks are safe to parallelize
- Skill 15: Integration Quality Gates — verification after implementation
