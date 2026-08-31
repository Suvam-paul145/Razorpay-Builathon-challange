# Domain-to-Module Ownership Mapping

Derived from the `design.md` module map and `tasks.md` task assignments.

## Domain A — Governance / Architecture

**Owner Role:** Architect Agent

| Module / Path | Responsibility |
|---|---|
| `.kiro/specs/` | Requirements, design, tasks |
| `.kiro/steering/` | Decision checkpoints |
| `.kiro/orchestration/` | Orchestration state |
| `ai_revenue_recovery_skills/` | Skill definitions |
| `Rovara/.importlinter` | Import contracts |
| `Rovara/pyproject.toml` | Dependencies, build config |

---

## Domain B — Razorpay Integration

**Owner Role:** Razorpay Agent

| Module / Path | Responsibility |
|---|---|
| `revora/providers/razorpay.py` | Razorpay HTTP client (3 operations) |
| `revora/providers/classification.py` | 5-way response classification |
| `revora/providers/payment_link.py` | Payment link request construction |
| `revora/ingestion/signature.py` | HMAC webhook verification |
| `revora/ingestion/canonical.py` | Event canonicalization |
| `revora/ingestion/backfill.py` | Detection-gap backfill |
| `scripts/spikes/` | Provider verification spikes |
| `docs/provider-findings.md` | Spike results |
| `tests/fakes/razorpay.py` | Fake provider with behavior catalogue |

---

## Domain C — Recovery Domain

**Owner Role:** Recovery Agent

| Module / Path | Responsibility |
|---|---|
| `revora/domain/enums.py` | All enumerations |
| `revora/domain/transitions.py` | Legal transition table |
| `revora/domain/actions.py` | Candidate actions, eligibility |
| `revora/domain/money.py` | Integer money type |
| `revora/domain/probability.py` | Probability types |
| `revora/cases/manager.py` | `apply_transition`, single state writer |
| `revora/cases/sweeper.py` | Lifecycle sweeper |
| `revora/cases/startup.py` | Restart re-evaluation |
| `revora/detection/rules.py` | Detection rule set |
| `revora/detection/service.py` | Case creation |
| `revora/diagnosis/service.py` | Diagnosis service |
| `revora/domain/failure_taxonomy.py` | Failure reason mapping |

---

## Domain D — Financial Safety

**Owner Role:** Policy Agent

| Module / Path | Responsibility |
|---|---|
| `revora/policy/input.py` | PolicyInput (frozen dataclass) |
| `revora/policy/rules.py` | Versioned rule set |
| `revora/policy/engine.py` | `evaluate()` — pure function, 12 checks |
| `revora/policy/service.py` | Decision persistence |
| `revora/optimizer/arithmetic.py` | Arithmetic chain (integer) |
| `revora/optimizer/selection.py` | Exclusion rules, selection |
| `revora/optimizer/service.py` | Recommendation persistence |

---

## Domain E — AI / Reasoning

**Owner Role:** AI Agent

| Module / Path | Responsibility |
|---|---|
| `revora/reasoning/contracts.py` | Prompt contracts, allow-lists |
| `revora/reasoning/adapter.py` | 4 gates, provider protocol |
| `tests/fakes/llm.py` | Deterministic LLM fake |

---

## Domain F — ML / Evaluation

**Owner Role:** ML Agent

| Module / Path | Responsibility |
|---|---|
| `revora/estimation/segments.py` | Hierarchical segments |
| `revora/estimation/baseline.py` | Beta-Binomial baseline |
| `revora/estimation/candidates.py` | Candidate prior lookup |
| `revora/estimation/calibration.py` | Calibration report |
| `revora/experiment/assignment.py` | Deterministic assignment |
| `revora/experiment/control.py` | Control arm, Baseline_Workflow |
| `revora/experiment/design.py` | Sample size, version freezing |
| `revora/experiment/analysis.py` | Analysis, labels, comparison |
| `revora/metrics/engine.py` | Cohort aggregation |
| `revora/memory/store.py` | Observation writer |
| `revora/memory/versions.py` | Model versions, promotion |

---

## Domain G — Backend

**Owner Role:** Backend Agent

| Module / Path | Responsibility |
|---|---|
| `revora/api/main.py` | FastAPI application |
| `revora/api/routers/` | All API endpoints |
| `revora/api/auth.py` | Session auth (shared with Security) |
| `revora/persistence/models/` | SQLAlchemy models |
| `revora/persistence/repositories/` | Repository layer |
| `revora/jobs/queue.py` | Postgres job queue |
| `revora/jobs/worker.py` | Worker loop |
| `revora/jobs/scheduler.py` | Periodic sweep scheduler |
| `revora/execution/engine.py` | Execution engine |
| `revora/execution/intents.py` | Intent management |
| `revora/execution/reconcile.py` | Reconciliation loop |
| `revora/outcome/monitor.py` | Outcome monitor |
| `revora/audit/writer.py` | Audit log writer |
| `revora/audit/queries.py` | Audit read queries |
| `revora/ingestion/ordering.py` | Out-of-order handling |
| `revora/ingestion/quarantine.py` | Quarantine, rate limiting |
| `alembic/` | Database migrations |

---

## Domain H — Frontend

**Owner Role:** Frontend Agent

| Module / Path | Responsibility |
|---|---|
| `web/` | React + TypeScript SPA (entire directory) |

---

## Domain I — Security

**Owner Role:** Security Agent

| Module / Path | Responsibility |
|---|---|
| `revora/platform/secrets.py` | Secret resolution, rotation window |
| `revora/platform/crypto.py` | Envelope encryption, customer key |
| `revora/platform/masking.py` | PII masking serializer |
| `revora/api/auth.py` | Session authentication (shared with Backend) |

---

## Domain J — Testing / Reliability

**Owner Role:** Test Agent

| Module / Path | Responsibility |
|---|---|
| `tests/strategies/` | Hypothesis strategy library |
| `tests/properties/` | Property tests |
| `tests/integration/` | Integration tests |
| `tests/persistence/` | Database constraint tests |
| `tests/platform/` | Platform unit tests |
| `tests/synthetic/` | Synthetic scenario tests |
| `tests/test_contracts.py` | Import contract tests |

---

## Domain K — Demo / Product Proof

**Owner Role:** Demo Agent

| Module / Path | Responsibility |
|---|---|
| `revora/synthetic/generator.py` | Seeded event generator |
| `revora/synthetic/harness.py` | Evidence harness |
| Demo scripts and datasets | Reproducible scenarios |

---

## Shared files (multiple owners)

| File | Primary Owner | Secondary |
|---|---|---|
| `revora/api/auth.py` | Backend (G) | Security (I) |
| `revora/platform/config.py` | Backend (G) | All (read-only) |
| `revora/platform/clock.py` | Backend (G) | All (read-only) |
| `revora/platform/logging.py` | Backend (G) | All (read-only) |
| `revora/domain/enums.py` | Recovery (C) | All (read-only) |
| `revora/domain/transitions.py` | Recovery (C) | All (read-only) |
