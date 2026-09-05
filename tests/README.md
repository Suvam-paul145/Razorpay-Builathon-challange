# `tests/` — how the guarantees are proven

Roughly **1,400 Python tests** across 78 files, split into six cost tiers. The cheap ones must pass
before every commit; the expensive ones must pass before a push or on a nightly run.

**Deep dive:** [`references/REVORA-SYSTEM-GUIDE.md`](../references/REVORA-SYSTEM-GUIDE.md) and the
Testing Strategy section of each [spec's design doc](../.kiro/specs/).

---

## The six tiers

| Marker | Cost | Runs | What it means |
| --- | --- | --- | --- |
| `pure` | microseconds | every commit | No I/O at all |
| `model` | seconds | every commit | In-memory fake repositories |
| `pg` | minutes | every push | Real PostgreSQL 18 |
| `harness` | ~17 min | nightly + pre-demo | Full synthetic pipeline, including the 1,000-case Demo_Batch |
| `smoke` | — | **never gates** | Latency and startup budgets. On a shared runner these test the hardware as much as the code, so they are tagged instead of deleted — the budgets stay written down |
| `spike` | manual | on demand | Provider verification against real test-mode credentials |

Tiers are set per test, not per file. `--strict-markers` is on, so a marker that was never registered
makes collection fail, instead of quietly running in no tier at all.

## Running them

```powershell
# every commit — no database needed
.venv\Scripts\python.exe -m pytest -m "pure or model" -q

# every push — needs Postgres
$env:REVORA_TEST_DATABASE_URL = "postgresql+psycopg://revora:revora_ci@127.0.0.1:5545/revora"
$env:REVORA_DATABASE_URL      = "postgresql+psycopg://revora_app:revora_app@127.0.0.1:5545/revora"
.venv\Scripts\python.exe -m pytest -m "pg and not harness and not smoke" -q

# nightly / before a demo — the full 1,000-case batch
.venv\Scripts\python.exe -m pytest -m harness -q
```

> Both database variables are required **together**. Two tests fail with
> `DatabaseNotConfiguredError` if only one is set.

**Current state:** `pure or model` → 988 passed, 1 failed. `pg` → 393 passed, 1 skipped.
The one failure is `properties/test_beta_interval.py::test_cdf_is_monotone`, a `Decimal` precision
issue in `revora/estimation/beta.py` that was already there. It is listed in the root README's *Known
limits*, not hidden.

---

## Directory map

| Path | Files | What it holds |
| --- | --- | --- |
| `properties/` | 22 | Hypothesis property tests — the strongest guarantees in the suite |
| `persistence/` | 24 | Constraints, RLS, migrations, concurrency, index usage |
| `integration/` | 7 | Whole-pipeline runs and the degradation ladder |
| `api/` | 9 | Routes, auth, status-code tables, the SPA mount guard |
| `platform/` | 8 | Config, clock, crypto, secrets, rate limiter |
| `strategies/` | 10 | Hypothesis generators — clocks, candidates, tokens, signals, reasoning responses |
| `fakes/` | 3 | The Razorpay fake and the customer-secret fixtures |
| `synthetic/` | 4 | Scenario harness and the demo seeding guards |
| `failure_db/` | — | Hypothesis's example database. **Do not delete** — it holds the smallest failing examples Hypothesis found |
| *(root)* | 17 | `conftest.py`, `pg_support.py`, `demo_support.py`, and the single-subject test modules |

---

## The tests worth reading first

| File | Why |
| --- | --- |
| [`integration/test_full_pipeline.py`](integration/test_full_pipeline.py) | The whole chain from a signed webhook. Nothing stubbed but the provider |
| [`integration/test_customer_loop_composition.py`](integration/test_customer_loop_composition.py) | Proves the pieces **fit together**: the token that gets created is the same one the customer's request checks, and the cause that gets recorded is the one the next cycle reads |
| [`integration/test_degradation.py`](integration/test_degradation.py) | Breaks Postgres, the worker, the clock — checks the damage is only a *delay*, never a duplicate charge |
| [`integration/test_customer_loop_degradation.py`](integration/test_customer_loop_degradation.py) | Reasoning layer broken four ways; all four produce the identical loop |
| [`properties/test_lifecycle_machine.py`](properties/test_lifecycle_machine.py) | One stateful machine, 14 history-level invariants, with Hypothesis choosing the order the steps run in |
| [`properties/test_reasoning_authority.py`](properties/test_reasoning_authority.py) | Seven properties proving a model cannot move a decision |
| [`properties/test_customer_audit_money.py`](properties/test_customer_audit_money.py) | The Demo_Batch. One batch serves three properties, because running 1,000 cases takes 17 minutes |
| [`fakes/razorpay.py`](fakes/razorpay.py) | A provider that fails in every way that matters — including the two kinds of timeout the caller cannot tell apart |

## The habit worth copying

**Every negative claim is checked against the fake's call log, not against the fact that no exception
was thrown.** "No provider request was issued" is a claim that something did not happen, and the
only proof of that is a record of everything that did.

In the same way, generators pick time steps from the **configured** bounds at just-under, exactly,
and just-over — see [`strategies/clocks.py`](strategies/clocks.py). A plain random value almost never
lands exactly on a boundary, and the boundary is where `>=` and `>` behave differently.

## Related

- [Root README](../README.md) — *Verifying it* section has the full gate
- [`../revora/README.md`](../revora/README.md) — what is being tested
- [`../.github/workflows/ci.yml`](../.github/workflows/ci.yml) — how the tiers map to CI jobs
