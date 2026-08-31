# Skill: Multi-Agent Development Orchestration

## Mission
Coordinate multiple independent AI coding agents working concurrently on Revora, ensuring architectural consistency, financial safety, security, and contract discipline.

**This is development-time orchestration only.** It does NOT modify the Revora runtime architecture. Development orchestration ≠ product architecture.

## Critical distinction
- **This skill enables:** Multiple AI agents working simultaneously on the codebase (Agent A → database, Agent B → domain logic, Agent C → frontend, etc.)
- **This skill does NOT enable:** Runtime product agent swarms, autonomous multi-agent orchestration, or self-directed tool use inside Revora itself.

## Architectural principles preserved
SIMPLE → CORRECT → AUDITABLE → MEASURABLE → SCALABLE

AI RECOMMENDATION ≠ AI AUTHORITY. The deterministic Policy Engine remains the final authority in the product.

Do NOT introduce microservices, Kafka, Kubernetes, event buses, vector databases, or runtime agent swarms.

## Orchestration pipeline

```
REQUEST
  ↓
UNDERSTAND — read requirements.md, design.md, tasks.md
  ↓
CLASSIFY — identify affected domains (A–K)
  ↓
LOAD PROJECT STATE — check .kiro/orchestration/ for active work
  ↓
CHECK DEPENDENCIES — consult tasks.md dependency graph (waves 0–46)
  ↓
DECOMPOSE — break into independently assignable units
  ↓
ASSIGN OWNERS — one domain, one agent, explicit contract (Skill 14)
  ↓
PARALLELIZE SAFE WORK — only when 5 conditions met
  ↓
RUN SPECIALIST AGENTS — each reads only its required skills
  ↓
VERIFY EACH RESULT — agent-level gate (Skill 15)
  ↓
INTEGRATE — combine outputs
  ↓
CROSS-AGENT REVIEW — independent review (Skill 16)
  ↓
SYSTEM TEST — run relevant test tiers
  ↓
QUALITY GATE — integration gate (Skill 15)
  ↓
MERGE / CONTINUE
```

## Agent assignment rules

Create an agent only when ALL of these are true:
1. Work is meaningfully independent.
2. Ownership can be isolated to specific files/modules.
3. The task has a clear, verifiable output.
4. Dependencies are known and either complete or frozen.
5. Integration cost is lower than sequential development cost.

Do NOT create an agent merely because another agent exists. Avoid unnecessary agent proliferation.

## Parallelization conditions

A task may run in parallel only when ALL five are met:
1. Its dependencies are complete or stable.
2. Its required contracts are frozen.
3. It has an isolated responsibility.
4. It does not modify the same critical files as another active task.
5. Its output can be verified independently.

## Agent stop conditions

An agent MUST stop and report (not guess) when:
- A public API contract needs changing.
- Database semantics need changing.
- A new dependency or service is required.
- Product scope changes.
- Money, retry, or customer-contact behavior changes.
- Authentication or permissions change.
- PII handling changes.
- Architecture direction changes.
- An approved decision is contradicted.
- Evidence is insufficient.
- Another agent owns the affected contract.
- A security or financial invariant is uncertain.

These align with `.kiro/steering/decision-checkpoints.md`.

An agent does NOT need to stop for:
- Internal naming, formatting, helper extraction.
- Test structure and test case naming.
- Equivalent implementation choices.
- Obvious bug fixes.

## Human approval boundary

**Autonomous decisions:** internal implementation details, helper functions, file organization, test structure, formatting, equivalent choices, execution ordering where dependencies are clear.

**Human approval required:** product scope, public API changes, database semantic changes, new external services, architecture direction, money/retry/customer-contact behavior, authentication/permissions, PII policy, major dependency changes.

## Conflict resolution

If two agents produce conflicting implementations:
1. Which requirement applies?
2. Which architectural decision applies?
3. Which implementation preserves invariants?
4. Which is simpler?
5. Which has lower risk?
6. Which has better test coverage?
7. Which requires fewer changes?

Then choose: KEEP A / KEEP B / COMBINE / REDESIGN / ESCALATE.

Do NOT merge both blindly.

## Orchestrator output

For every major implementation phase, generate a phase plan using `.kiro/orchestration/templates/phase-plan.md`.

## Related skills
- Skill 13: Parallel Workstream Design — wave decomposition
- Skill 14: Agent Contracts and Ownership — assignment contracts
- Skill 15: Integration Quality Gates — verification gates
- Skill 16: Cross-Agent Review — independent review

## Source of truth
- `requirements.md` — what to build
- `design.md` — how to build it
- `tasks.md` — implementation order and dependencies
- `decision-checkpoints.md` — when to ask
