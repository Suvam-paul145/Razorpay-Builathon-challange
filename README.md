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

A synthetic demonstration run holds that line while still producing a number: it reports a
separately named `demonstration_incremental_revenue` and leaves `incremental_recovered_revenue` at
`NOT_ESTABLISHED`. The measured figures from one such run are in **The demonstration evidence**
below.

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
currency-bearing module (72 of them) and fails the build on a `float`, a `/`, or a `round()` near an
amount.

Amounts cross the API as `{minor, currency, formatted}` and the client renders `formatted`. Clients
do no currency arithmetic at all — two client-side divisions in two components is how one screen shows
two different totals. INR renders with Indian digit grouping (`₹12,34,567.89`), computed as pure
string manipulation rather than via a locale, because a server-environment-dependent number format is
a number format that changes when somebody rebuilds the image.

On the client side lint is the *only* automated check on this, and it always would have been: the
frontend is plain JavaScript with JSDoc annotations, no TypeScript anywhere, and a type system could
not have expressed the money rule regardless — arithmetic operators accept every number subtype,
including a branded one. `web/eslint.config.js` rejects arithmetic on a `.minor` field, interpolating
one into text, `Intl.NumberFormat`, `toLocaleString` and `toFixed`. A test runs ESLint in-process
against that config and asserts the rule fires, because a typo in an ESLint selector does not error —
it matches nothing, and the gate would then enforce precisely nothing while staying green.

### 3. An absent value is never rendered as zero

A figure that has not been produced yet carries `status: NOT_YET_RECORDED` and names the case state.
One that could not be computed carries `status: DATA_UNAVAILABLE` and names the figure. Substituting
zero for either is a false financial statement, not a display shortcut.

The lint config also rejects `?? 0` and `|| 0` on a figure, which is how the honest version of this
gets undone — `?? 0` reads as defensive.

---

## Where the AI is, and where it deliberately is not

**The AI is advisory only. It cannot cause an effect.** Three import contracts, checked in CI by
`import-linter`, make that a property of the module graph rather than a promise:

- *Policy engine may not reach AI, estimation, optimizer or memory*
- *Value optimizer may not reach AI output or the provider*
- *Reasoning adapter sees only platform and domain*

The first two mean the component that decides whether an action is permitted cannot see a model's
opinion, and the component that ranks actions cannot see one either. The third points the other way
and is the newer one: with every feature package and `revora.persistence` forbidden, the only things
`revora.reasoning` can import are `revora.platform` and `revora.domain`. There is no session to open,
no repository to call and no ORM model to load — so "the adapter cannot read a case row" is a
statement about what is reachable, not about what the code happens to do today. Callers pass data in.

**There is a real reasoning layer now.** `revora/reasoning/` is `adapter.py`, `contracts.py` and
`schemas.py`: a hand-written `httpx` adapter for Gemini with three declared call kinds —
`CAUSE_HYPOTHESIS`, `DECISION_EXPLANATION`, `LINK_DESCRIPTION` — and four gates that run in order.

1. **A frozen field allow-list per prompt contract.** Every request payload is built by iterating the
   contract's field-name set, so a field outside it has no path onto the wire; there is no `if` to
   forget. A caller that supplies one gets `PROMPT_CONTRACT_VIOLATION` before a credential is even
   resolved. A contract that *declares* a forbidden name — anything matching a contact, instrument,
   token, secret or user-identifier fragment — fails at import.
2. **TLS with certificate validation**, passed explicitly, never retried on failure. The handshake
   completes before the body is written, so a certificate failure means nothing left the process.
3. **Pydantic output-schema validation**, performed regardless of the provider-side response schema,
   because a component that treats provider enforcement as a guarantee has no fallback the day it
   changes.
4. **Content validation on link descriptions** — length and control characters through the same
   validator the payment-link path uses, plus placeholder, amount-equality and single-link rules.

One `ai_invocation` row per invocation, including timeouts and rejections, so an absent answer is on
the record rather than absent from it. The per-case call bound is `len(ReasoningCallKind) *
MAX_RECOVERY_ATTEMPTS` and is counted from committed rows, which is what makes it survive a restart.

**None of that buys the model any authority.** An AI-assisted diagnosis confidence is capped at
`0.99`; `1.0` is reserved for `DETERMINISTIC`, so a reader of the `diagnosis` table reads the method
off the confidence. A rejected or low-confidence hypothesis is substituted to `RiskCause.UNKNOWN`,
whose eligibility row permits nothing customer-visible — a bad guess makes Revora more conservative,
not less. A rejected link description is **substituted** with the reviewed deterministic template,
never sent; the draft is retained and the customer-message counter does not move for the suppression.

**The credential is optional and absent by default.** With no `REVORA_LLM_CREDENTIAL` the adapter
builds nothing: no client, no payload, no wait, no row. The layer is not "unavailable", it is not
there, and the whole system runs deterministically. `tests/properties/test_reasoning_authority.py`
asserts that the credential-absent run and a run in which every response is rejected are the same
system — both hand every pure component `None`.

That file carries seven properties, each driven from a real provider outcome over a mock transport
with all four gates running, never from a hand-built result — which matters, because a constructed
`Accepted` would let a property assert a mapping the adapter cannot produce:

- **P49** — a model answer moves no policy outcome. Every response but an accepted, above-floor cause
  leaves the twelve check outcomes byte-identical to a cycle that asked nothing; an accepted one moves
  exactly one field, the *recorded* cause after the ceiling and the floor substitution.
- **P50** — an explanation moves no figure. `influenced_recommendation` is a literal `False`.
- **P51** — the absent credential and the rejected response are the same system, and nothing waits.
- **P52** — one row per invocation, and the bound is counted from rows.
- **P53** — the transmitted key set is a subset of the declared set, driven with adversarial values
  carrying JSON separators, because the question is whether a *value* can add a *key*.
- **P54** — `1.000` confidence is reachable only through `DETERMINISTIC`.
- **P55** — a refused draft is a substitution, never a non-execution.

The rest of the "AI" is the estimation and optimization layer: a baseline recovery-probability
estimate with an honest confidence interval, per-action uplift priors, and expected-value ranking.
All of it labelled `UNCALIBRATED` until something checks it, because an uncalibrated estimate
presented as a validated one is the same category of lie as the incremental-revenue overstatement.

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
`ESCALATED`, `FAILED`), 63 legal edges, and nothing else. Every transition writes the new state, the
incremented version and the audit record in one transaction that commits all three or none.

Now that a case can be re-decided rather than left alone, the graph has cycles, and termination rests
on three facts rather than on their absence: every cycle passes through `DECISION_PENDING`, every edge
into `DECISION_PENDING` carries a decision-cycle increment, and `decision_cycle_count` never
decreases. Stating it as a property of the target state rather than as an enumeration of edges is what
makes an edge added tomorrow inherit the guarantee.

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

### Nine candidate actions, and only three can touch the outside world

`DO_NOTHING`, `WAIT`, `RETRY`, `DELAYED_RETRY`, `PAYMENT_LINK`, `CUSTOMER_MESSAGE`,
`PAYMENT_METHOD_UPDATE`, `PROMISE_TO_PAY_FOLLOW_UP`, `HUMAN_ESCALATION`.

`PROVIDER_ACTIONS` is exactly `{PAYMENT_LINK, CUSTOMER_MESSAGE, PROMISE_TO_PAY_FOLLOW_UP}`. The
promise follow-up joined it because a capability was *verified*, not because anybody decided to be
optimistic: `POST /v1/payment_links/:id/notify_by/:medium` re-notifies the link the case already
holds and creates no second link, so it is the one executable customer-visible action that mints no
new payable object. It was already customer-visible, so it already consumed the message cap and
nothing about the cap changed. Where the case holds no live link it falls back to creating one.

`HUMAN_ESCALATION` is approved and then **terminal** — the case leaves automation, because asking a
person is a real answer and routing it as though it were a provider call leaves the case authorized,
unexecuted, and waiting for its window to close with nothing on the record explaining why nothing
happened.

Three actions remain unavailable — `RETRY`, `DELAYED_RETRY` and `PAYMENT_METHOD_UPDATE` — because a
failed one-off payment is terminal at the provider and a new attempt is a new payment the customer
starts. They stay in the vocabulary and stay visible in the recorded candidate set with their
exclusion reason, because an action shown as unavailable is more honest than one silently omitted.

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

## The customer response loop

A failed payment has a person on the other end of it, and that person knows things Revora cannot
derive: that payday is Friday, that the card was replaced, that the order was cancelled, that the
charge is disputed. The second spec adds a way for them to say so — and the whole of its design is
about letting that information in without letting it decide anything.

### The page, and the token that reaches it

**A public customer response page** is the only route reachable without a session besides the inbound
webhook and `GET /health`. It is reached with a `Customer_Access_Token` of the form
`rvc_<token_id>.<secret>`: a separately random 26-character handle, and a 128-bit secret. The handle
is the only form permitted in a log line or an audit field, and it is separately random *because* it
is the form that gets copied around — deriving it from the secret would make every legitimate mention
a partial disclosure.

What is stored is `HMAC-SHA256(signing_key, token_id ‖ secret)`, keyed rather than a plain digest, so
a database dump alone does not permit offline verification. Verification does one indexed lookup and
then compares against **every** active signing secret, accumulating with `|=` and never breaking
early; a missing row substitutes a per-process decoy hash and does the same work, so "no such handle",
"wrong secret" and "signed by a retired key" fold into one branch with an identical response.

The token is minted **inside the execution's first transaction**, before the provider call, because
the token URL is what the message carries — one minted at confirmation would arrive after the only
message that could have delivered it. A failed mint therefore rolls back the intent, the counter
movement and the consumed decision together. Exactly one live token per case, enforced by a partial
unique index over `revoked_at IS NULL`; expiry is the earlier of the token lifetime and the case's
`window_end_at` and is never extended; revoked when the case goes terminal or contact is suppressed.

**A consequence worth stating plainly.** If the token signing secret is unresolvable, `PAYMENT_LINK`
and `CUSTOMER_MESSAGE` stop too. The execution is abandoned in its first transaction with zero
external calls rather than sending a customer a message whose only link is dead.

### What the page discloses

**Exactly eight fields and no ninth**: merchant display name, amount, currency, plain-language
reason, pay URL, window end, signals remaining, and the promise if there is one. They come from a
purpose-built frozen dataclass rather than a filtered dashboard model, which is the mechanism rather
than the intent: there is no inherited shape to filter, so **adding a field to the dashboard cannot
leak it here**. No probability, no cost term, no net value, no rejected candidate, no policy
decision, no configuration value, no contact identifier in any form. The reason is a rendering of the
recorded `Risk_Cause`, never the provider's error string — that string is internal vocabulary written
for an operator debugging a payment rail, not for a person deciding whether they can pay today.

The amount is an integer count of minor units formatted by the server's renderer, injected downward,
so the customer page and the dashboard cannot disagree about what a rupee looks like.

### Three write shapes, and none of them decides anything

A delay reason, a partial-arrangement request, a promise to pay. Each accepted write is **one
transaction containing four things**: the accepted-submission-count increment under the token's row
lock, the `customer_signal` insert, the enqueued consequence, and the audit record — last, so the
rollback it can cause is a rollback of work already staged. All four or none. No state transition, no
action scheduling, no policy evaluation and no provider request happens inside the accepting request;
the worker applies every consequence through the one writer of `recovery_case.state`.

**Customer input is evidence, never authority.** Property 36 replaces every `Customer_Signal` field
with arbitrary schema-valid content and asserts the twelve policy check outcomes stay byte-identical,
with the suppression state pinned so the claim is about signal *content*. Its structural half is
stronger: `PolicyInput`'s declared field set is disjoint from every signal field, so there is nowhere
for signal content to sit.

**A partial-arrangement request cannot be stored as a partial amount.** The request model declares no
`amount`, no `instalment_count` and no `schedule` and sets `extra="forbid"`, and one layer down
`customer_signal` has no column for any of the three. The refusal is structural: there is nowhere to
put the number. The request is recorded, it fetches a person, and it changes no money field.

**Promises never buy time.** `follow_up_at = min(promise_date + follow_up_offset, window_end_at −
safety_margin)`. The recovery window is set at case creation and never extended, a promise past the
window escalates to a person instead of stretching it, and `promise_to_pay` carries a
`CHECK (follow_up_at IS NULL OR follow_up_at < window_end_at_snapshot)` so a follow-up computed at or
past the window end is a failed insert rather than a bad decision.

**A hard stop is permanent.** A disputed charge or a cancelled order writes a contact suppression
keyed on `sha256(customer_key ‖ order_id)`, in the same transaction as the signal that caused it.
There is no `expires_at` column, so non-expiry is enforced by absence rather than by a check, and a
release requires a named person. It enters the input of the existing check 5, `CUSTOMER_OPTED_OUT`,
rather than becoming a thirteenth check — every position a thirteenth could occupy is *after* a
bound, and a prohibition must not sit where a counter bug can overrule it. It suppresses that debt
**without** setting the customer-wide opt-out, because "I object to this charge" and "stop contacting
me at all" are different statements and collapsing them destroys the record of which was said.

### Restraint is now revisited rather than abandoned

This is the defect the second spec exists to fix. A `Null_Action` selection — `DO_NOTHING` or `WAIT` —
now persists a `next_review_at`, a sweeper re-decides the case, and a `CASE_REVIEWED` record is
written *whether or not the selection changed*, because a review that chose to wait again is the
evidence that restraint was re-examined. Such a case appears in no ended grouping on any surface. The
decision-cycle counter bounds the whole thing.

### The case timeline

Nine stages, projected purely from the gap-free audit sequence. The projection owns no table and
cannot write, has no session and no queue in scope, and takes `now` as an argument — so the same
records always produce the same timeline. It always renders all nine stages, not "the ones that
happened", because a reader used to nine rows seeing six does not notice which three went. A gap in
the audit sequence renders a banner naming the missing numbers rather than asserting a stage is done.

### A second frontend, not a second route

The customer page is its own Vite entry, built to `web/dist-customer/`. A `/pay/:token` route inside
the dashboard bundle would ship an unauthenticated stranger the entire administrative surface as
readable source — nothing in it is secret, but it is a map. It also has to open from an SMS on a cold
mobile connection, so it carries neither the router nor the query client. It uses no router at all and
shares exactly one module with the dashboard, the `<Money>` component, so it cannot compute a figure
that disagrees with the server.

---

## The demonstration evidence

**This is a synthetic demonstration, not a real-money claim.** Every figure below is measured, from a
single run on a freshly migrated database, and the most important one is the one that stayed refused.

1000 cases were seeded **through the signed webhook endpoint**, not written as rows. Each one
traverses signature verification over the raw bytes, canonicalization, dedup, detection, diagnosis,
estimation, the optimizer, twelve policy checks, execution and the outcome monitor. The customer-side
outcomes are driven over real HTTP with real bearer tokens against the mounted customer surface.

| Figure | Value |
| --- | --- |
| Seeded | 1000 / 1000, zero refusals, 748 seconds |
| `observed_recovered_revenue` | 108,523,787 minor units (₹10,85,237.87) |
| `incremental_recovered_revenue` | `NOT_ESTABLISHED`, refusal code `DISQUALIFYING_LABEL` |
| `demonstration_incremental_revenue` | +33,763,071 minor units |
| Measured lift | 0.1148, 95% interval [0.0576, 0.1721] |
| Planted ground-truth lift | 0.1500 |
| Terminal states | RECOVERED 312 / EXPIRED 658 / ESCALATED 22 / STOPPED 8 |
| Promises | KEPT 4 / MISSED 4 / BEYOND_WINDOW_ESCALATED 4 |

`incremental_recovered_revenue` stayed `NOT_ESTABLISHED` **because the inputs were synthetic**, and
that refusal survived a run whose lift was clean, well powered and interval-separated from zero. So
the demonstration figure is a separate field with its own name and its own `SYNTHETIC` and
`DEMONSTRATION_ONLY` labels, presented adjacent to it rather than in a footnote.

The interval excludes zero **and** contains the planted truth. That is the whole point of quoting it:
it says the measurement apparatus works, and nothing whatsoever about real recovery rates.

Outcome coverage was asserted *after* the run rather than assumed, audit sequences were gap-free on
every case, and there were zero `REAL`-provenance rows anywhere in the database.

Reproduce it with the `harness` tier, from the repository root, against a migrated Postgres:

```powershell
$env:REVORA_TEST_DATABASE_URL = "postgresql+psycopg://…"
.venv\Scripts\python.exe -m pytest -m harness -q
```

It takes about seventeen minutes, which is why this tier runs nightly and before a demo build rather
than on every commit.

**One honest zero.** The authoritative reads in a harness run are genuine reads of a **fake**
provider, so `verified_test_mode_recoveries` reports `0` and that is correct. Razorpay's Payment
Links API can create, fetch, update, cancel and re-notify a link; it has no documented endpoint that
*pays* one, and in test mode payment happens on a page that asks a person to choose success or
failure. So the three real test-mode recoveries are a documented manual step in `RUNBOOK.md` rather
than a script. Automating them would mean faking a capture, which is the one thing this figure exists
not to do.

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
- **Row-level security on 36 of the 37 tables**, as defence in depth behind the application's own
  tenant scoping. The exception is `merchant`, which has no `merchant_id` because it *is* the tenant.
  Every session sets its merchant binding.
- **The inbound webhook is one of exactly two unauthenticated routes** (the other is `GET /health`,
  which reveals only liveness — the schema revision it verifies is logged, not returned). An unknown
  merchant slug answers exactly like a bad signature. The customer response page is the only other
  surface reachable without a session, and it is not unauthenticated: it requires a
  `Customer_Access_Token`, and it is a mounted sub-application so its CORS and cache middleware are
  scoped to exactly its routes rather than relaxing the dashboard's posture.
- **Raw webhook payloads are encrypted at rest** with a keyed nonce and carry a `retain_until`.
  Retention redaction clears the contact and the ciphertext but **keeps `customer_key`** — destroying
  it would revoke every recorded opt-out — and keeps every amount and timestamp.
- **Opt-out is keyed on the customer, not the payment**, so it governs cases that do not exist yet.
- Contact details are masked in every audit field and are never recorded in the test fake's call log
  either, because a fixture is still somewhere a PII habit can form.

---

## Layout

A modular monolith. One image, three process roles, selected by `REVORA_ROLE`: `api`, `worker`, and `ticker` — the schedule, which produces every periodic sweep job and reclaims the leases of jobs a dead worker left `RUNNING`. Without the ticker running, nothing expires a case, reconciles an intent, re-reads payment state, backfills a detection gap, redacts customer data, or reviews a case that chose restraint, and nothing looks broken.

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
  jobs/          the queue, the worker loop, the ticker, the pipeline steps
  providers/     the Razorpay client and result classification
  persistence/   models and repositories
  audit/         the append-only writer
  api/           routes, auth, views, server-side rendering
  platform/      config, clock, crypto, secrets, logging, role
  synthetic/     scenario generation (unreachable from the decision path, by contract)
  customer/      access tokens, the eight-field projection, signals, promises, suppression
  timeline/      the nine-stage projection over the audit sequence — owns no table
  reasoning/     the Gemini adapter, its three prompt contracts and its output schemas

web/             two frontends: React 18 + Vite + TanStack Query, plain JavaScript with JSDoc
  src/api/       hand-written DTOs (types.js), the fetch client, the query hooks
  src/components/ Money, Rate, Enum, AbsentValue, Label, Timeline — every figure goes through one
  src/routes/    performance, cases, case detail, unresolved, experiments, consent, sign-in
  src/customer/  the customer response page — no router, one shared module
  dist/          the dashboard bundle          (built, never committed)
  dist-customer/ the customer page bundle      (built, never committed)
```

166 source files, 23 top-level packages, 18 migrations. Six import contracts, all kept:

1. Policy engine may not reach AI, estimation, optimizer or memory
2. Domain imports only the standard library
3. Value optimizer may not reach AI output or the provider
4. Synthetic data is unreachable from the decision and action path
5. Reasoning adapter sees only platform and domain
6. Feature modules depend downward only

---

## Running it

Requires Python 3.12+ and Docker.

Every command below runs from the repository root.

```bash
# 1. Dependencies
python -m venv .venv
.venv/bin/python -m pip install -e ".[ml,dev]"

# 2. Local Postgres 18 + API + worker + the built dashboard
docker compose up --build

# 3. Migrate
export REVORA_DATABASE_URL="postgresql+psycopg://revora:revora_local_dev@localhost:5432/revora"
.venv/bin/python -m alembic upgrade head
```

On Windows, the same three steps:

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[ml,dev]"

docker compose up --build

. .\scripts\dev_env.ps1                     # sets the DSN and the local secrets
.venv\Scripts\python.exe -m alembic upgrade head
.venv\Scripts\python.exe scripts\dev_check.py   # migrated? seeded? signable?
```

The `.venv\Scripts\python.exe -m <tool>` form is used throughout rather than a bare `alembic` or
`pytest`, because it works whether or not the environment is activated and wherever the checkout sits.

Everything is on `:8000` — the API, and the dashboard served from the same origin by the same
process under `/app`, because the API already owns `/cases` and `/metrics`. Interactive API docs at
`/docs`.

The one image builds both frontends in its own stage, so `web/dist` and `web/dist-customer` are never
committed and what ships is always built from the committed source. Serving the dashboard and the API
from one origin is what lets the deployment run with **no CORS middleware at all**, which is a
stronger position than an allowlist because there is nothing to misconfigure later. The customer page
is genuinely cross-origin, and it carries its own CORS middleware scoped to its own mount rather than
widening the dashboard's.

To work on the frontends with hot reload, run the API on `:8000` and then, from `web/`:

```bash
npm ci
npm run dev        # :5173, proxying the API paths to :8000 so dev is same-origin too
```

The dev server serves the dashboard at `/app/` and the customer page at `/pay/<token>`, which is the
URL a customer actually receives — a developer opening `/index-customer.html` would get a page with
no token and see the not-found panel instead of the page they are working on.

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
| `REVORA_CUSTOMER_TOKEN_SIGNING_SECRETS` | `<version>:<base64 32 bytes>`, comma-separated for rotation; keys the customer access token hash. Highest version mints, every version verifies |
| `REVORA_SESSION_TOKEN_SECRET` | base64 32 bytes; HMAC key for stored session digests |
| `REVORA_DASHBOARD_KEYS_<SLUG>` | per-merchant operator key that mints a session |
| `REVORA_WEBHOOK_SECRETS_<SLUG>` | per-merchant Razorpay webhook signing secret |
| `REVORA_RAZORPAY_KEY_ID` / `_KEY_SECRET` | Razorpay API credentials |
| `REVORA_ROLE` | `api`, `worker` or `ticker`. An unknown value is refused at startup |
| `REVORA_TICKER_INTERVAL_SECONDS` | ticker role only. Seconds between ticks, default 30. Deliberately shorter than the shortest sweep interval; over-ticking is free because every enqueue is dedupe-keyed by interval bucket |
| `REVORA_JOB_LEASE_SECONDS` | ticker role only. How long a `RUNNING` job may hold its lease before the tick presumes its worker died, default 900 |
| `REVORA_API_CORS_ORIGINS` | exact origins, comma-separated. A `*` is refused at startup. Omit entirely when the SPA is same-origin, which is the intended deployment |
| `REVORA_CUSTOMER_ORIGINS` | exact origins permitted to call the customer surface from a browser. Separate from the dashboard's list so widening one cannot widen the other |
| `REVORA_CUSTOMER_REQUIRE_TLS` | `1` where TLS terminates at a proxy in front of this process |
| `REVORA_LLM_CREDENTIAL` | **optional.** The reasoning credential. Absent means no client, no payload, no wait and no `ai_invocation` row — the deterministic path, and the deployed default |
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
GET    /cases/{id}/timeline      the nine-stage projection, with any sequence gap named
POST   /cases/{id}/owner         claim human ownership
DELETE /cases/{id}/owner         release it
GET    /metrics/summary          observed vs incremental, with provenance labels
GET    /metrics/unresolved       grouped by reason, all five groups always
GET    /experiments              list
GET    /experiments/{id}         detail with the lift interval
POST   /consent                  record an opt-out or a grant
GET    /health/webhook           delivery health
```

Token-authenticated (`Authorization: Bearer rvc_<token_id>.<secret>`), a mounted sub-application at
`/customer` with its own CORS and cache middleware:

```
GET    /customer/{merchant_slug}/case                  the eight disclosed fields, and no ninth
POST   /customer/{merchant_slug}/delay-reason          why the payment is late
POST   /customer/{merchant_slug}/promise               when they will pay
POST   /customer/{merchant_slug}/partial-arrangement   a request, carrying no amount
```

Every rejection on that surface answers from one table — 404 for a forgery, an unknown handle, a
retired signing key or a malformed presentation; 410 for expired and revoked; 503 for an unreadable
signing secret.

---

## Verifying it

Five gates, all from the repository root, all of which must pass:

```powershell
.venv\Scripts\python.exe -m ruff check revora tests scripts    # lint
.venv\Scripts\python.exe scripts\check_no_float.py             # no float near money — 72 files
.venv\Scripts\python.exe -m importlinter.cli lint              # 6 contracts, 0 broken
.venv\Scripts\python.exe -m mypy -p revora.domain -p ...       # strict where a type error is money
.venv\Scripts\python.exe -m pytest -m "pure or model" -q       # 989 tests, no I/O
```

The `pure or model` selection is 988 passing and one known pre-existing failure, described under
**Known limits**. `mypy` covers sixteen packages; the full list is in `.github/workflows/ci.yml`.

Then the Postgres tier:

```powershell
$env:REVORA_TEST_DATABASE_URL = "postgresql+psycopg://…"
.venv\Scripts\python.exe -m pytest -m pg -q                    # 393 passed, 1 skipped, Postgres 18
```

And the frontends, from `web/`:

```bash
npm run lint && npm run test && npm run build   # 81 tests across 10 files, then both bundles
```

There is no `typecheck` script, because there is nothing to typecheck — the frontend is plain
JavaScript. `npm run build` runs two passes, the dashboard's and the customer page's.

**Roughly 1400 Python tests collected, plus 81 frontend tests.** Six cost tiers, marked so the cheap
ones gate every commit and the expensive ones gate a push:

| Marker | What it means |
| --- | --- |
| `pure` | no I/O at all, microseconds |
| `model` | in-memory fake repositories, seconds |
| `pg` | real Postgres via testcontainers |
| `harness` | full synthetic pipeline plus the Demo_Batch, nightly and pre-demo |
| `spike` | manual provider verification against test-mode credentials |
| `smoke` | latency and startup-cost budgets, deliberately excluded from every gating selection — on a shared runner a latency bound measures the hardware as much as the code, and a gate nobody can act on teaches everyone to re-run the build. Tagged rather than deleted so the budgets stay written down |

The customer response loop added no marker and no CI job. The existing tiers absorbed it, which is one
fewer list to keep in step.

`mypy` runs `strict` with `disallow_any_explicit` on `domain`, `policy` and `optimizer` — the three
packages where a type error becomes a money bug — and standard elsewhere. Applying strict everywhere
would mean fighting SQLAlchemy's descriptor typing in exchange for no additional safety on the
figures that matter.

### The tests worth reading

- `tests/integration/test_full_pipeline.py` — the whole chain from a signed webhook, nothing stubbed
  but the provider. Asserts one external effect and a recovery backed by a read.
- `tests/integration/test_degradation.py` — the four infrastructure faults above.
- `tests/properties/test_lifecycle_machine.py` — one stateful machine carrying fourteen history-level
  invariants, checked after every step. Hypothesis chooses the interleaving of deliveries, clock
  steps, sweeps, opt-outs, ownership changes and process restarts; the teardown then advances past
  every bound and requires that no case is still running, which is the only way to catch a lifecycle
  that leaks cases.
- `tests/properties/test_reasoning_authority.py` — the seven properties above.
- `tests/properties/test_customer_signal_authority.py` — signal content moves no policy outcome,
  behaviourally and then structurally, which is the stronger of the two.
- `tests/properties/test_customer_audit_money.py` — the Demo_Batch. One batch, three properties,
  because a thousand cases through the webhook endpoint is about seventeen minutes and a batch per
  test would cost three times as much and assert nothing more.
- `tests/properties/` — Hypothesis properties over the state machine, the policy engine, the
  idempotency guarantee and the money type.
- `tests/strategies/clocks.py` — time steps drawn from the *configured* bounds at just-under,
  exactly and just-over, because a uniform random delta essentially never lands on a boundary and a
  boundary is where `>=` and `>` differ.
- `tests/fakes/razorpay.py` — a provider that can fail in every way that matters, including the two
  timeout cases that are indistinguishable to the caller and differ only in whether the link exists.
- `web/src/routes/Metrics.test.jsx` — asserts the incremental slot renders `NOT ESTABLISHED` and that
  the observed figure appears exactly once on the page. That count is the test: it would catch the
  most tempting possible regression, a well-meaning "fall back to the observed number when
  incremental is unavailable".
- `web/src/test/lint-rule.test.js` — runs ESLint against the shipped config and proves the money rule
  fires, including that it leaves the legitimate use (sorting by `minor`) alone.
- `web/src/customer/build.test.js` — asserts the customer page still ships with no router and shares
  exactly one module with the dashboard, which is the boundary the second entry exists to hold.

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
- **One test fails, and it is not hidden.** `tests/properties/test_beta_interval.py::test_cdf_is_monotone`
  fails on a `Decimal` precision issue in `revora/estimation/beta.py`. It is unrelated to either
  spec's features, it predates both, and the honest thing is to name it rather than quote a clean
  suite. The `pure or model` selection is 988 passed, 1 failed.
- **The demonstration run is synthetic and says so.** Its lift is measured against a lift the
  generator planted. It licenses a claim about the measurement apparatus and no claim at all about
  what Revora would recover from real traffic — which is why
  `incremental_recovered_revenue` stayed `NOT_ESTABLISHED` through it.
- **`verified_test_mode_recoveries` is `0` in any automated run**, correctly: the reads are genuine
  but they read a fake. The three real test-mode recoveries are a manual `RUNBOOK.md` step.
- **Two reasoning-authority claims are checked at the `model` tier, not `pg`.** P51 and P52 are, in
  their fullest form, claims about rows; what runs here is the mechanism that determines them — the
  value handed to every pure component is identical, and exactly one complete row is derivable from
  every result variant. Row-level confirmation lives in `tests/persistence/test_reasoning_wiring.py`.
- **Suppression scope is the narrower reading.** A dispute is keyed on the order where the event
  carried one, so it suppresses that debt and not every debt the customer has. An `[ASSUMPTION]`, and
  the direction that can be widened later without losing data.

---

## Design record

Two specs, each with its full requirements, design and task breakdown:

- `.kiro/specs/revora-incremental-revenue-recovery/` — the pipeline, the two-figure separation, the
  three structural rules, exactly-once execution, the audit trail.
- `.kiro/specs/revora-customer-response-loop/` — the customer response page and its access token, the
  three write shapes, promises and hard stops, revisited restraint, the case timeline, the reasoning
  layer, and the demonstration batch.

Claims in those documents are tagged `[ASSUMPTION]`, `[INFERENCE]` or `[EVIDENCE INSUFFICIENT]` where
they are not established, and the tags are load-bearing: several of the limits above exist because a
tag was not allowed to quietly become a fact.
