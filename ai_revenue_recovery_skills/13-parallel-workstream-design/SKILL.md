# Skill: Parallel Workstream Design

## Mission
Convert the Revora implementation plan into dependency-aware parallel workstreams, using the existing `tasks.md` as the authoritative task graph.

Do NOT invent a second project plan that contradicts `tasks.md`.

## Task classification

Every task must be classified as one of:

| Classification | Description | Example |
|---|---|---|
| FOUNDATION | Infrastructure, tooling, type system | Task 1 (skeleton), Task 4 (platform) |
| CONTRACT | Frozen interfaces between modules | Domain enums, API DTOs, state transitions |
| DOMAIN | Core business logic within a bounded module | Task 11 (case manager), Task 17 (policy engine) |
| INTEGRATION | Combining outputs from multiple domains | Task 32 (end-to-end) |
| UI | Frontend components and views | Task 30 (frontend) |
| TEST | Test infrastructure and property tests | Task 28 (stateful model), Task 31 (CI tiers) |
| SECURITY | Auth, tenant isolation, secret handling | Task 29.1 (auth), Task 5.4 (RLS) |
| EVALUATION | Metrics, experiments, synthetic data | Task 24–27 |
| DOCUMENTATION | Provider findings, spike results | Task 2.5 |
| REVIEW | Cross-agent review, checkpoints | Tasks 6, 12, 18, 23, 33 |

## Dependency types

### HARD DEPENDENCY
Task B cannot start until Task A is complete.
```
database migration (5.2) → repository implementation (5.5)
```

### SOFT DEPENDENCY
Task B can begin against a frozen interface while A is being implemented.
```
frontend (30) → backend API contract (29 DTOs frozen)
```

### PARALLEL
Tasks can safely run independently.
```
frontend UI (30.2) || unit tests (31.1) || security review (29.1)
```

### INTEGRATION DEPENDENCY
Tasks can work independently but require a shared integration gate.
```
Razorpay adapter (19) || recovery engine (20) → integration test (32)
```

## Revora wave mapping

The following maps the master prompt's conceptual waves to the actual `tasks.md` dependency graph.

### WAVE 0 — Architecture & Contract Verification
**tasks.md waves 0–4**

Tasks: 1.1–1.5, 3.1–3.6, 4.1–4.5

Freeze before proceeding:
- Domain entities (`revora/domain/`)
- State transitions (`revora/domain/transitions.py`)
- Candidate actions (`revora/domain/actions.py`)
- Enumerations (`revora/domain/enums.py`)
- Platform contracts (`revora/platform/`)

Agents: 1 (architecture + domain + platform)

### WAVE 1 — Persistence & Foundation
**tasks.md waves 5–8**

Tasks: 5.1–5.7, 7.1, 8.1

Parallelizable after schema freeze:
- **Agent A:** Database models + migrations (5.1–5.4)
- **Agent B:** Repository layer + config (5.5–5.6)
- **Agent C (after 5.2):** Job queue (7.1) ∥ Audit writer (8.1)

Checkpoint: Task 6

### WAVE 2 — Ingest to Lifecycle
**tasks.md waves 9–16**

Tasks: 7.2–7.4, 8.2–8.5, 9.1–9.6, 10.1–10.3, 11.1–11.5

Sequential critical path:
```
Job queue worker (7.2) → Ingestion (9.1–9.5) → Detection (10.1–10.2) → Case manager (11.1–11.4)
```

Parallelizable off critical path:
- **Agent D:** Audit masking + queries (8.2–8.4) ∥ critical path
- **Agent E:** Provider spikes (2.1–2.5) — runs independently

Checkpoint: Task 12

### WAVE 3 — Decision Pipeline
**tasks.md waves 17–24**

Tasks: 13.1–13.3, 14.1–14.5 (optional), 15.1–15.5, 16.1–16.4, 17.1–17.6

Parallelizable after diagnosis contracts frozen:
- **Agent F:** Diagnosis deterministic (13.1–13.2)
- **Agent G:** Estimation baseline + candidates (15.1–15.3)
- **Agent H:** Value optimizer (16.1–16.4) — can start from domain (task 3) + estimation contracts
- **Agent I (optional):** Reasoning adapter (14.1–14.5)

Sequential: Policy engine (17.1–17.4) must follow optimizer + estimation

Checkpoint: Task 18

### WAVE 4 — Execution & Outcome
**tasks.md waves 25–33**

Tasks: 19.1–19.5, 20.1–20.7, 21.1–21.5, 22.1–22.2

Parallelizable:
- **Agent J:** Razorpay client + fake (19.1–19.4)
- **Agent K:** Detection-gap backfill (22.1–22.2)

Sequential: Execution engine (20.1–20.7) → Outcome monitor (21.1–21.4)

Checkpoint: Task 23

### WAVE 5 — Measurement & Evidence
**tasks.md waves 32–38**

Tasks: 24.1–24.6, 25.1–25.5, 26.1–26.4, 27.1–27.5

Parallelizable after case manager + outcome monitor exist:
- **Agent L:** Experiment engine (24.1–24.5)
- **Agent M:** Metrics engine (25.1–25.4)
- **Agent N:** Recovery memory (26.1–26.3)
- **Agent O:** Synthetic generator (27.1–27.5) — depends on L, M, N

### WAVE 6 — Surfaces & System Verification
**tasks.md waves 39–46**

Tasks: 28.1–28.2, 29.1–29.5, 30.1–30.8, 31.1–31.4, 32.1–32.3

Parallelizable:
- **Agent P:** API layer (29.1–29.5)
- **Agent Q:** Frontend (30.1–30.8) — starts once API DTOs frozen
- **Agent R:** Stateful property model (28.1–28.2)
- **Agent S:** CI tiers + wiring (31.1–31.4)

Sequential: End-to-end integration (32.1–32.3) after all above

Checkpoint: Task 33

## DAG generation rules

When decomposing tasks:
1. Generate a DAG from `tasks.md` dependency edges.
2. Tasks with no path between them may be parallelized.
3. Tasks sharing mutable contracts must be coordinated.
4. The critical path (dark nodes in `tasks.md` Mermaid) is always sequential.
5. Off-critical-path tasks are candidates for parallelization.

## The rule of smallest agent count
Use the smallest number of specialized independent agents that can safely reduce development time while preserving correctness.

Optimize: VALIDATED BUSINESS VALUE PER UNIT OF DEVELOPMENT TIME.

Do NOT optimize: number of agents, number of parallel branches, number of AI calls.

## Related skills
- Skill 12: Multi-Agent Orchestration — the pipeline
- Skill 14: Agent Contracts and Ownership — assignment
- Skill 15: Integration Quality Gates — wave completion gates
