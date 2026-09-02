# Revora

**AI-assisted incremental revenue recovery.** A failed payment is not necessarily lost revenue. Revora
watches Razorpay payment failures, works out *why* each one failed, decides whether acting is worth
more than doing nothing, acts at most once, and then reports what it actually recovered — separating
the money that came back *because of it* from the money that would have come back anyway.

That last separation is the product. Everything else is plumbing in service of it.

---

## The problem, and why the obvious version of it is wrong

Some percentage of card payments fail for reasons that are not "this customer will never pay":
insufficient funds at 9am on payday-minus-one, an expired card, a bank timeout, a 3DS drop-off. A
recovery tool sends a payment link, some customers pay, and the tool reports the total as revenue it
recovered.

**That number is almost always wrong, and wrong in the flattering direction.** A meaningful share of
those customers would have retried on their own. Counting their payments as recovery credits the tool
for the customer's own persistence. The error is invisible, it compounds every month, and it is the
number the tool is bought on.

Revora is built around refusing to make that claim. It reports two figures and never conflates them:

| Figure | Meaning | When it appears |
| --- | --- | --- |
| `observed_recovered_revenue` | Money that arrived on cases Revora acted on. A fact. | Always |
| `incremental_recovered_revenue` | Money that arrived *because* Revora acted. A causal claim. | Only from a completed, adequately powered holdout experiment whose lift interval sits entirely above zero |

Until that experiment exists, `incremental_recovered_revenue` reads `NOT_ESTABLISHED` and every
report carries a `CAUSALITY_NOT_ESTABLISHED` label. Not zero. Not the observed figure with a
disclaimer underneath. `NOT_ESTABLISHED`, because "we have not measured this" and "we measured this
and it was nothing" are different statements and only one of them is true.

Recovery outcomes are classified into three kinds, and the classification is stored, not derived at
render time:

- **`NATURAL`** — the payment recovered and Revora did nothing. The baseline.
- **`OBSERVED`** — Revora acted and the money arrived. No causal claim.
- **`ATTRIBUTED`** — an experiment licenses the causal claim.

---

## Three rules the code enforces structurally

These are not conventions. Each is enforced by something that fails loudly — a database constraint, an
import contract, a CI gate, or a property test — because a convention about money is a convention
that gets violated during an incident.

### 1. A recovery is declared only from an authoritative provider read

A `payment.captured` webhook is a *claim*, not evidence. It triggers a `fetch_payment` against
Razorpay, and only that read may declare a recovery. The `recovery_outcome` table has a `NOT NULL`
`verified_by_read_id` pointing at the `payment_state_read` row that decided it, so a recovery
declared from a webhook is not merely discouraged — it does not fit in the schema.

When the read disagrees with the webhook, the read wins and no recovery is recorded.

### 2. Money is integer minor units, everywhere, and the server formats it

No float touches a currency value. `scripts/check_no_float.py` is a CI gate that walks every
currency-bearing module (53 of them) and fails the build on a `float`, a `/`, or a `round()` near an
amount.

Amounts cross the API as `{minor, currency, formatted}` and the client renders `formatted`. Clients
do no currency arithmetic at all — two client-side divisions in two components is how one screen shows
two different totals. INR renders with Indian digit grouping (`₹12,34,567.89`), computed as pure
string manipulation rather than via a locale, because a server-environment-dependent number format is
a number format that changes when somebody rebuilds the image.

On the client side the same rule is enforced by lint, because TypeScript cannot express it —
arithmetic operators accept every number subtype, including a branded one. `web/eslint.config.js`
rejects arithmetic on a `.minor` field, interpolating one into text, `Intl.NumberFormat`,
`toLocaleString` and `toFixed`. A test runs ESLint in-process against that config and asserts the
rule fires, because a typo in an ESLint selector does not error — it matches nothing, and the gate
would then enforce precisely nothing while staying green.

### 3. An absent value is never rendered as zero

A figure that has not been produced yet carries `status: NOT_YET_RECORDED` and names the case state.
One that could not be computed carries `status: DATA_UNAVAILABLE` and names the figure. Substituting
zero for either is a false financial statement, not a display shortcut.

The lint config also rejects `?? 0` and `|| 0` on a figure, which is how the honest version of this
gets undone — `?? 0` reads as defensive.

---

## Where the AI is, and where it deliberately is not

**The AI is advisory only. It cannot cause an effect.** Two import contracts, checked in CI by
`import-linter`, make that a property of the module graph rather than a promise:

- *Policy engine may not reach AI, estimation, optimizer or memory*
- *Value optimizer may not reach AI output or the provider*

So the component that decides whether an action is permitted cannot see a model's opinion, and the
component that ranks actions cannot see one either. A model can suggest; it cannot approve, and it
cannot reach the payment provider.

**This build ships with no LLM at all.** The reasoning layer was scoped out, `revora.reasoning` is an
empty package, and diagnosis runs entirely off a deterministic failure-reason table derived from
Razorpay's documented error taxonomy. Every diagnosis records `method = DETERMINISTIC` and there is
never an `ai_invocation` row.

This is a stronger position than a working LLM path, and the test suite asserts the strong form: the
design asked for a run with the model hard-failing to produce an identical terminal-state distribution
to a run with it available, and `test_the_pipeline_runs_with_no_reasoning_layer_at_all` asserts instead
that there is only one run possible. It also asserts that `revora.reasoning` has no public surface, so
a later phase adding an adapter has to change that test deliberately and think about what it means.

The "AI" in the description is the estimation and optimization layer: a baseline recovery-probability
estimate with an honest confidence interval, per-action uplift priors, and expected-value ranking. All
of it labelled `UNCALIBRATED` until something checks it, because an uncalibrated estimate presented as
a validated one is the same category of lie as the incremental-revenue overstatement.

---

## How a case actually flows

```
signed payment.failed webhook
  → HMAC verified over the raw bytes        (never a re-serialized body)
  → canonicalized + round-trip checked
  → deduplicated on provider_event_id
  → detection verdict
  → recovery case opened                     DETECTED
  → experiment arm assigned
  → deterministic diagnosis                  DIAGNOSED
  → baseline estimate (do-nothing probability, with interval)
  → every candidate action priced
  → candidates ranked by expected value      DECISION_PENDING
  → twelve policy checks, in a fixed order   POLICY_CHECK
  → if approved and it needs the provider    ACTION_SCHEDULED
  → exactly one payment link created         EXECUTING → WAITING_FOR_OUTCOME
  → payment.captured arrives (a claim)
  → authoritative fetch_payment (the evidence)
  → RECOVERED, with verified_by_read_id set
  → memory observation written, atomically with the terminal transition
  → metrics
```

Fourteen case states, six of them terminal (`RECOVERED`, `STOPPED`, `BLOCKED`, `EXPIRED`,
`ESCALATED`, `FAILED`), 54 legal edges, and nothing else. Every transition writes the new state, the
incremented version and the audit record in one transaction that commits all three or none.

**Each pipeline step is its own job**, and each enqueues its successor *inside the transaction that
committed its own work*. That is why the job queue is a Postgres table rather than a broker: a
broker's enqueue after commit can be lost, and before commit can fire against state that never
committed.

### The twelve policy checks

All twelve run and all twelve are recorded, always — never "the ones that got as far as running". A
partially recorded evaluation looks identical, in the record, to one that ran fewer checks and
approved.

```
1  ALREADY_PAID          5  CUSTOMER_OPTED_OUT     9  MAX_ATTEMPTS_REACHED
2  ALREADY_TERMINAL      6  CONSENT_MISSING       10  MAX_MESSAGES_REACHED
3  DUPLICATE_ACTION      7  HUMAN_OWNERSHIP       11  COOLDOWN_ACTIVE
4  FRAUD_OR_RISK         8  WINDOW_EXPIRED        12  ACTION_NOT_ELIGIBLE
```

The policy engine is a pure function. It cannot read the database and cannot reach the network — the
row-loading around it lives in `revora/jobs/pipeline.py`, the one layer permitted to see both — and
that purity is what makes the engine exhaustively property-testable.

### Nine candidate actions, and only two can touch the outside world

`DO_NOTHING`, `WAIT`, `RETRY`, `DELAYED_RETRY`, `PAYMENT_LINK`, `CUSTOMER_MESSAGE`,
`PAYMENT_METHOD_UPDATE`, `PROMISE_TO_PAY_FOLLOW_UP`, `HUMAN_ESCALATION`.

`PROVIDER_ACTIONS` is exactly `{PAYMENT_LINK, CUSTOMER_MESSAGE}`. `HUMAN_ESCALATION` is approved and
then **terminal** — the case leaves automation, because asking a person is a real answer and routing
it as though it were a provider call leaves the case authorized, unexecuted, and waiting for its
window to close with nothing on the record explaining why nothing happened.

Doing nothing is a first-class, ranked, priced option. `DO_NOTHING` is definitionally net-zero, and
when nothing clears the value floors the recommendation says `NO_POSITIVE_VALUE` and the case stops
with a reason. A blocked or unacted case is not a failed case and no surface presents it as one.

### Exactly one external effect per approval

The idempotency key is derived from `(case_id, action, attempt_ordinal)` and travels to Razorpay as
`reference_id`, so their side rejects a duplicate too — two independent mechanisms for one guarantee.

A timeout after the request left the socket is the hard case: the link may or may not exist, and the
caller cannot tell. Revora records the intent `UNCERTAIN` rather than guessing, and reconciliation
resolves it by *reading* — reusing the same idempotency key, never deriving a new one. A property test
asserts that across any interleaving of crashes, timeouts and reconciliation passes, the count of
creates issued per key is at most one.

---

## Failing safely

Infrastructure faults produce **delayed decisions**, never duplicate charges, duplicate messages or
invented numbers. Each of these has an integration test that breaks the thing and asserts the damage
is a delay:

- **Postgres unreachable at ingest** → HTTP 503, nothing persisted, and the event lands cleanly on
  the provider's redelivery. 503 rather than 500 specifically, because Revora's only recovery here is
  the redelivery and a retry only helps against a transient fault. The catch is narrow —
  connection-level errors only — so a deterministic SQL defect stays a 500 instead of being retried
  until the provider gives up and silently drops the event.
- **Worker killed mid-execution** → the intent stays unresolved, reconciliation confirms the existing
  link against provider state, and the provider sees one create for that key, ever.
- **A recovery window elapsed during downtime** → expiry is re-derived from persisted rows on restart
  and the case is expired *before* anything schedules an action against it.
- **A withheld action whose bounds have since moved** → discarded on the way back up, the discard
  audited, no external call, and both bounds counters left untouched.

---

## The audit trail

R11.C5 enumerates eight questions the record must answer, and one ordered read of a case's audit
records answers all eight: what happened, why, on what evidence, which alternatives were considered,
which policy rules allowed or blocked the action, which action executed, whether payment recovered,
and how the recovery is classified.

The per-case sequence is gap-free and starts at 1 — without that, "the full history" is a claim nobody
can check, because a missing record and a record that never existed look identical. Audit records are
append-only, enforced by a revoked grant plus a trigger, and an attempted mutation is itself logged.

One correlation id joins the whole delivery chain, so "something odd happened at 14:32" is one query
rather than a search.

---

## Security and customer data

- **Session auth is a DB-backed `merchant_session` row**, not a stateless signed token, because
  sessions must be revocable. The token is `<merchant-slug>.<32 urlsafe random bytes>`; only a keyed
  HMAC digest is stored. The slug resolves the merchant and the digest is then looked up *inside that
  tenant*, so a swapped slug fails closed.
- **Every auth failure answers 401 byte-identically.** The audit record keeps the distinction; the
  response does not, so the endpoint is not an oracle. Those audit records are written in their own
  transaction, because the caller raises 401 immediately afterwards and a shared transaction would
  roll back the evidence.
- **Cross-tenant reads answer 404**, never 403 — a 403 confirms the resource exists.
- **Row-level security on 32 of 34 tables**, as defence in depth behind the application's own tenant
  scoping. Every session sets its merchant binding.
- **The inbound webhook is one of exactly two unauthenticated routes** (the other is `GET /health`,
  which reveals only liveness — the schema revision it verifies is logged, not returned). An unknown
  merchant slug answers exactly like a bad signature.
- **Raw webhook payloads are encrypted at rest** with a keyed nonce and carry a `retain_until`.
  Retention redaction clears the contact and the ciphertext but **keeps `customer_key`** — destroying
  it would revoke every recorded opt-out — and keeps every amount and timestamp.
- **Opt-out is keyed on the customer, not the payment**, so it governs cases that do not exist yet.
- Contact details are masked in every audit field and are never recorded in the test fake's call log
  either, because a fixture is still somewhere a PII habit can form.

---

## Layout

A modular monolith. One image, two process roles, selected by `REVORA_ROLE`.

```
revora/
  domain/        pure types: money, states, transitions, actions, taxonomy   (stdlib only)
  policy/        the twelve checks, as a pure function
  optimizer/     expected-value ranking
  estimation/    baseline probability + per-candidate pricing
  diagnosis/     deterministic failure-reason mapping
  detection/     is this payment at risk
  ingestion/     verify → canonicalize → dedup → enqueue, + the backfill
  execution/     the exactly-once engine and its reconciliation sweep
  outcome/       authoritative reads; the only place a recovery may be declared
  experiment/    holdout assignment and analysis
  memory/        training observations and model versions
  metrics/       the reported figures, with their provenance labels
  cases/         the state machine, the lifecycle sweeper, retention
  jobs/          the queue, the worker loop, the pipeline steps
  providers/     the Razorpay client and result classification
  persistence/   models and repositories
  audit/         the append-only writer
  api/           routes, auth, views, server-side rendering
  platform/      config, clock, crypto, secrets, logging, role
  synthetic/     scenario generation (unreachable from the decision path, by contract)
  reasoning/     deliberately empty

web/             the dashboard: React 18 + TypeScript strict + Vite + TanStack Query
  src/api/       hand-written DTOs, the fetch client, the query hooks
  src/components/ Money, Rate, Enum, AbsentValue, Label — every figure goes through one
  src/routes/    performance, cases, case detail, unresolved, experiments, consent
```

139 source files, 21 packages, 7 migrations. Five import contracts, all kept:

1. Policy engine may not reach AI, estimation, optimizer or memory
2. Domain imports only the standard library
3. Value optimizer may not reach AI output or the provider
4. Synthetic data is unreachable from the decision and action path
5. Feature modules depend downward only

---

## Running it

Requires Python 3.12+ and Docker.

```bash
# 1. Dependencies  (Windows: .venv\Scripts\python.exe)
python -m venv .venv
.venv/bin/python -m pip install -e ".[ml,dev]"

# 2. Local Postgres 18 + API + worker + the built dashboard
docker compose up --build

# 3. Migrate
export REVORA_DATABASE_URL="postgresql+psycopg://revora:revora_local_dev@localhost:5432/revora"
alembic upgrade head
```

Everything is on `:8000` — the API, and the dashboard served from the same origin by the same
process. Interactive API docs at `/docs`.

The one image builds the SPA in its own stage, so `web/dist` is never committed and what ships is
always built from the committed source. Serving both from one origin is what lets the deployment run
with **no CORS middleware at all**, which is a stronger position than an allowlist because there is
nothing to misconfigure later.

To work on the frontend with hot reload, run the API on `:8000` and then:

```bash
cd web
npm ci
npm run dev        # :5173, proxying the API paths to :8000 so dev is same-origin too
```

The dev proxy exists so local development has the same origin arrangement as production. Developing
cross-origin and deploying same-origin would mean the CORS configuration is exercised only in dev and
never in the shape that ships.

### Secrets

Supplied via environment. Nothing has a default — an unresolvable credential fails loudly at the
point of use rather than silently degrading.

| Variable | Purpose |
| --- | --- |
| `REVORA_DATABASE_URL` | Postgres DSN (`postgresql+psycopg://…`) |
| `REVORA_PAYLOAD_ENCRYPTION_KEYS` | `<version>:<base64 32 bytes>`, comma-separated for rotation |
| `REVORA_CUSTOMER_KEY_SECRET` | base64 32 bytes; derives `customer_key` from a contact |
| `REVORA_SESSION_TOKEN_SECRET` | base64 32 bytes; HMAC key for stored session digests |
| `REVORA_DASHBOARD_KEYS_<SLUG>` | per-merchant operator key that mints a session |
| `REVORA_WEBHOOK_SECRETS_<SLUG>` | per-merchant Razorpay webhook signing secret |
| `REVORA_RAZORPAY_KEY_ID` / `_KEY_SECRET` | Razorpay API credentials |
| `REVORA_ROLE` | `api` or `worker` |
| `REVORA_API_CORS_ORIGINS` | exact origins, comma-separated. A `*` is refused at startup. Omit entirely when the SPA is same-origin, which is the intended deployment |
| `REVORA_WEB_ROOT` | where the built dashboard lives. Defaults to `web/dist` beside the package, which the image produces. Absent build → nothing mounted, API-only |

### Endpoints

Unauthenticated:

```
POST   /webhooks/razorpay/{merchant_slug}    HMAC over the raw body
GET    /health                                liveness + schema revision check
```

Session-authenticated (`Authorization: Bearer <token>`):

```
POST   /auth/sessions            mint (X-Revora-Dashboard-Key)
DELETE /auth/session             revoke
GET    /cases                    list
GET    /cases/{id}               detail: every candidate, all twelve checks
GET    /cases/{id}/audit         the ordered trail
POST   /cases/{id}/owner         claim human ownership
DELETE /cases/{id}/owner         release it
GET    /metrics/summary          observed vs incremental, with provenance labels
GET    /metrics/unresolved       grouped by reason, all five groups always
GET    /experiments              list
GET    /experiments/{id}         detail with the lift interval
POST   /consent                  record an opt-out or a grant
GET    /health/webhook           delivery health
```

---

## Verifying it

Five gates, all of which must pass:

```bash
ruff check .                                    # lint
python scripts/check_no_float.py                # no float near money — expects 54 files
lint-imports                                    # 5 contracts, 0 broken
mypy -p revora.domain -p revora.policy ...      # strict where a type error is a money bug
pytest -m "pure or model"                       # 634 tests, no I/O
```

Then the Postgres tier:

```bash
export REVORA_TEST_DATABASE_URL="postgresql+psycopg://…"
pytest -m pg                                    # 225 tests, real Postgres 18
```

And the dashboard:

```bash
cd web
npm run typecheck && npm run lint && npm run test && npm run build   # 20 tests
```

**859 Python tests plus 20 frontend tests.** Four cost tiers, marked so the cheap ones gate every
commit and the expensive ones gate a push:

| Marker | What it means |
| --- | --- |
| `pure` | no I/O at all, microseconds |
| `model` | in-memory fake repositories, seconds |
| `pg` | real Postgres via testcontainers |
| `harness` | full synthetic pipeline, nightly |
| `spike` | manual provider verification against test-mode credentials |

`mypy` runs `strict` with `disallow_any_explicit` on `domain`, `policy` and `optimizer` — the three
packages where a type error becomes a money bug — and standard elsewhere. Applying strict everywhere
would mean fighting SQLAlchemy's descriptor typing in exchange for no additional safety on the
figures that matter.

### The tests worth reading

- `tests/integration/test_full_pipeline.py` — the whole chain from a signed webhook, nothing stubbed
  but the provider. Asserts one external effect and a recovery backed by a read.
- `tests/integration/test_degradation.py` — the four infrastructure faults above.
- `tests/properties/test_lifecycle_machine.py` — one stateful machine carrying eleven history-level
  invariants, checked after every step. Hypothesis chooses the interleaving of deliveries, clock
  steps, sweeps, opt-outs, ownership changes and process restarts; the teardown then advances past
  every bound and requires that no case is still running, which is the only way to catch a lifecycle
  that leaks cases.
- `tests/properties/` — Hypothesis properties over the state machine, the policy engine, the
  idempotency guarantee and the money type.
- `tests/strategies/clocks.py` — time steps drawn from the *configured* bounds at just-under,
  exactly and just-over, because a uniform random delta essentially never lands on a boundary and a
  boundary is where `>=` and `>` differ.
- `tests/fakes/razorpay.py` — a provider that can fail in every way that matters, including the two
  timeout cases that are indistinguishable to the caller and differ only in whether the link exists.
- `web/src/routes/Metrics.test.tsx` — asserts the incremental slot renders `NOT ESTABLISHED` and that
  the observed figure appears exactly once on the page. That count is the test: it would catch the
  most tempting possible regression, a well-meaning "fall back to the observed number when
  incremental is unavailable".
- `web/src/test/lint-rule.test.ts` — runs ESLint against the shipped config and proves the money rule
  fires, including that it leaves the legitimate use (sorting by `minor`) alone.

Every negative claim in the suite is asserted against the fake's call log rather than against an
absence of exceptions. "No provider request was issued" is a claim about something that did not
happen, and the only evidence for that is a log of everything that did.

---

## Known limits, stated plainly

The design marks its own weak points and so does this README. None of these is hidden behind a
confident number.

- **The baseline is unvalidated.** Nothing has checked the do-nothing probability estimates against
  reality yet, so they report `UNVALIDATED_BASELINE` or `CALIBRATION_UNVERIFIED` and never
  `VALIDATED`.
- **Uplift priors are priors.** Per-action uplift figures are assumptions, labelled `UNCALIBRATED`,
  and the arithmetic they drive is visible: a payment link and a human escalation cross over around
  ₹12,000, above which Revora asks a person. That is a real product behaviour derived from the
  numbers, not a tuned constant.
- **`NO_INTERVENTION_CONFIRMED` is narrower than it sounds.** It means no Revora action *and* no
  recorded merchant action. Revora cannot see a merchant phoning a customer. The weakness is labelled
  in the data rather than solved, and only control-arm cases with zero confirmed actions qualify —
  a case Revora declined to treat is a *selected* population and counting it as a baseline label is
  the classic selection bias that would flatter every subsequent incremental claim.
- **Provider redelivery behaviour is assumed.** Revora depends on Razorpay redelivering on a 5xx.
  The retry schedule and attempt count are unverified.
- **Read-after-write lag on the payment-link listing is unquantified**, which is why reconciliation
  may only treat an empty listing as a failure on its final attempt.
- **No regulatory compliance claim is made.** The security section describes engineering controls.
- **`ruff format` is not a gate.** The tree is hand-wrapped to 100 columns, enforced by `E501`.
  Adopting the formatter would be a tree-wide mechanical diff; lint runs, formatting does not.

---

## Design record

The full requirements, design and task breakdown live in
`.kiro/specs/revora-incremental-revenue-recovery/`. Claims in those documents are tagged
`[ASSUMPTION]`, `[INFERENCE]` or `[EVIDENCE INSUFFICIENT]` where they are not established, and the
tags are load-bearing: several of the limits above exist because a tag was not allowed to quietly
become a fact.
