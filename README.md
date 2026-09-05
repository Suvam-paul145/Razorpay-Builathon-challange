# Revora

**AI-assisted incremental revenue recovery.** A failed payment is not always lost revenue. Revora
watches Razorpay payment failures and works out *why* each one failed. It then decides whether acting
is worth more than doing nothing, acts at most once, and reports what it actually recovered. It keeps
two things apart: the money that came back *because of Revora* and the money that would have come back
anyway.

That last separation is the product. Everything else is plumbing in service of it.

**Live:** [dashboard](https://revora-api-h3aj.onrender.com/app) ·
[customer page](https://razorpay-builathon-challange.vercel.app/) ·
[API health](https://revora-api-h3aj.onrender.com/health)

---

## Start here

Pick the door that matches why you came:

| You want to… | Read |
| --- | --- |
| **See it working in 5 minutes** | [`DEMO-GUIDE.md`](DEMO-GUIDE.md) — a click-through script with the live links |
| **Run it locally** | [`RUNBOOK.md`](RUNBOOK.md) — four terminals, copy-pasteable |
| **Understand the argument** | This file. Start with *The problem* below, then *Three rules the code enforces structurally* |
| **Understand it at a low level** | [`references/REVORA-SYSTEM-GUIDE.md`](references/REVORA-SYSTEM-GUIDE.md) — a 220 KB walkthrough: layer map, feature-by-feature, one payment traced end to end, glossary |
| **Read the backend** | [`revora/README.md`](revora/README.md) — package map and the six import contracts |
| **Check how it's proven** | [`tests/README.md`](tests/README.md) — six test tiers and the tests worth reading |
| **Read the frontends** | [`web/README.md`](web/README.md) — two bundles, and why lint guards money instead of types |
| **Read the schema** | [`alembic/README.md`](alembic/README.md) — 18 migrations and why downgrades refuse |
| **Use the dev tools** | [`scripts/README.md`](scripts/README.md) — send a webhook, drive the schedule, seed a merchant |
| **See what was verified about Razorpay** | [`docs/provider-findings.md`](docs/provider-findings.md) — measured provider behaviour, not assumed |
| **Read the requirements and design** | [`.kiro/specs/`](.kiro/specs/) — two specs, with `[ASSUMPTION]` / `[EVIDENCE INSUFFICIENT]` tags intact |
| **See the decisions and the mistakes** | [`EXECUTION-LEDGER.md`](EXECUTION-LEDGER.md) — every ruling, including the ones that were wrong first |
| **See the original brief** | [`references/problem statement.png`](references/problem%20statement.png) |

### Repository map

```mermaid
flowchart LR
    subgraph code["Code"]
        R["revora/<br/><i>backend, 23 packages</i>"]
        W["web/<br/><i>dashboard + customer page</i>"]
        A["alembic/<br/><i>18 migrations</i>"]
        T["tests/<br/><i>~1400 tests, 6 tiers</i>"]
        S["scripts/<br/><i>dev tools + 1 CI gate</i>"]
    end
    subgraph docs["Docs"]
        RM["README.md<br/><i>the argument</i>"]
        DG["DEMO-GUIDE.md<br/><i>how to present</i>"]
        RB["RUNBOOK.md<br/><i>how to run</i>"]
        SG["references/<br/>REVORA-SYSTEM-GUIDE.md<br/><i>the deep dive</i>"]
        SP[".kiro/specs/<br/><i>requirements + design</i>"]
        EL["EXECUTION-LEDGER.md<br/><i>decisions</i>"]
    end
    RM --> DG & RB & SG & SP & EL
    R --> A
    T --> R
    W --> R
```

---

## The problem, and why the obvious version of it is wrong

Some card payments fail for reasons that do not mean "this customer will never pay": not enough money
in the account at 9am the day before payday, an expired card, a bank timeout, or a 3DS drop-off (the
customer gives up during the card's extra bank security step). A recovery tool sends a payment link,
some customers pay, and the tool reports the whole amount as revenue it recovered.

**That number is almost always wrong, and wrong in the direction that flatters the tool.** A good
share of those customers would have tried again on their own. Counting their payments as recovery
gives the tool credit for the customer's own effort. The error cannot be seen, it grows month after
month, and it is the very number people buy the tool for.

Revora is built to refuse that claim. It reports two figures and never mixes them up:

| Figure | Meaning | When it appears |
| --- | --- | --- |
| `observed_recovered_revenue` | Money that arrived on cases Revora acted on. A fact. | Always |
| `incremental_recovered_revenue` | Money that arrived *because* Revora acted. A cause-and-effect claim. | Only after a finished holdout experiment (some cases left untouched for comparison) that had enough data and whose measured lift stayed entirely above zero |

Until that experiment exists, `incremental_recovered_revenue` reads `NOT_ESTABLISHED`, and every
report carries a `CAUSALITY_NOT_ESTABLISHED` label. It is not shown as zero. It is not the observed
figure with a disclaimer underneath. It is `NOT_ESTABLISHED`, because "we have not measured this" and
"we measured this and it came to nothing" are different statements, and only one of them is true.

A synthetic demonstration run (one driven by made-up test data, not real payments) holds that line
while still producing a number. It reports a separately named `demonstration_incremental_revenue` and
leaves `incremental_recovered_revenue` at `NOT_ESTABLISHED`. The measured figures from one such run
are in **The demonstration evidence** below.

Recovery outcomes are sorted into three kinds. The kind is stored, not worked out each time the page
is drawn:

- **`NATURAL`** — the payment recovered and Revora did nothing. The baseline.
- **`OBSERVED`** — Revora acted and the money arrived. No cause-and-effect claim.
- **`ATTRIBUTED`** — an experiment backs the cause-and-effect claim.

---

## Three rules the code enforces structurally

These are not just conventions. Each one is enforced by something that fails loudly — a database
constraint, an import contract, a CI gate (an automatic check that blocks a bad build), or a property
test. A convention about money is a convention that gets broken during an incident.

### 1. A recovery is declared only from an authoritative provider read

A `payment.captured` webhook is a *claim*, not proof. It triggers a `fetch_payment` call to Razorpay,
and only that read is allowed to declare a recovery. The `recovery_outcome` table has a `NOT NULL`
`verified_by_read_id` that points at the `payment_state_read` row that decided it. So a recovery
declared straight from a webhook is not just frowned upon — there is no place for it in the schema.

When the read disagrees with the webhook, the read wins and no recovery is recorded.

### 2. Money is integer minor units, everywhere, and the server formats it

No floating-point number (a value with a decimal point, which can round in surprising ways) ever
touches a currency value. `scripts/check_no_float.py` is a CI gate that walks every module that
handles money (72 of them) and fails the build if it finds a `float`, a `/`, or a `round()` near an
amount.

Amounts cross the API as `{minor, currency, formatted}`, and the client just shows `formatted`. The
client does no money math at all. Two divisions on the client side, in two components, is all it takes
for one screen to show two different totals. INR is shown with Indian digit grouping
(`₹12,34,567.89`), built by plain string handling rather than a locale setting. A number format that
depends on the server's environment is a number format that changes when someone rebuilds the image.

On the client side, lint (an automatic code-style check) is the *only* automated check on this, and
it always would have been. The frontend is plain JavaScript with JSDoc comments and no TypeScript
anywhere. A type system could not have expressed the money rule anyway, since math operators accept
every kind of number, including a specially tagged one. `web/eslint.config.js` rejects math on a
`.minor` field, dropping one into text, `Intl.NumberFormat`, `toLocaleString`, and `toFixed`. A test
runs ESLint against that config and checks that the rule actually fires. A typo in an ESLint selector
does not raise an error — it simply matches nothing, and the gate would then enforce nothing at all
while still looking green.

### 3. An absent value is never rendered as zero

A figure that has not been produced yet carries `status: NOT_YET_RECORDED` and names the case state.
One that could not be computed carries `status: DATA_UNAVAILABLE` and names the figure. Putting zero
in place of either is a false financial statement, not a display shortcut.

The lint config also rejects `?? 0` and `|| 0` on a figure, because that is how the honest version of
this quietly gets undone — `?? 0` looks like harmless defensive code.

---

## Where the AI is, and where it deliberately is not

**The AI only advises. It cannot make anything happen.** Three import contracts, checked in CI by
`import-linter`, make that a fact about how the code is wired rather than a promise:

- *Policy engine may not reach AI, estimation, optimizer or memory*
- *Value optimizer may not reach AI output or the provider*
- *Reasoning adapter sees only platform and domain*

The first two mean the part that decides whether an action is allowed cannot see the model's opinion,
and the part that ranks actions cannot see it either. The third one points the other way and is
newer. With every feature package and `revora.persistence` blocked, the only things
`revora.reasoning` can import are `revora.platform` and `revora.domain`. There is no session to open,
no repository to call, and no ORM model to load. So "the adapter cannot read a case row" is a
statement about what the code can even reach, not about what it happens to do today. Callers pass the
data in.

**There is a real reasoning layer now.** `revora/reasoning/` is `adapter.py`, `contracts.py` and
`schemas.py`: a hand-written `httpx` adapter for Gemini with three declared call kinds —
`CAUSE_HYPOTHESIS`, `DECISION_EXPLANATION`, `LINK_DESCRIPTION` — and four gates that run in order.

1. **A fixed list of allowed fields per prompt contract.** Every request is built by going through
   the contract's set of field names, so a field outside that list has no way onto the wire; there is
   no `if` check to forget. A caller that supplies one gets `PROMPT_CONTRACT_VIOLATION` before a
   credential is even looked up. A contract that *lists* a forbidden name — anything that looks like a
   contact, instrument, token, secret, or user-identifier — fails at import.
2. **TLS with certificate validation** (verifying the server's identity), passed in explicitly and
   never retried if it fails. The handshake finishes before the body is sent, so a certificate
   failure means nothing left the process.
3. **Pydantic output-schema validation**, done no matter what the provider's own response schema
   says. A component that trusts the provider to enforce this has no backup the day the provider
   changes it.
4. **Content checks on link descriptions** — length and control characters, through the same
   validator the payment-link path uses, plus rules for placeholders, matching the amount, and
   allowing only one link.

There is one `ai_invocation` row per call, including timeouts and rejections, so a missing answer is
still on the record rather than left off it. The limit on calls per case is `len(ReasoningCallKind) *
MAX_RECOVERY_ATTEMPTS`, counted from committed rows, which is what lets it survive a restart.

**None of that gives the model any authority.** The confidence of an AI-assisted diagnosis is capped
at `0.99`; `1.0` is reserved for `DETERMINISTIC`, so a reader of the `diagnosis` table can tell the
method just from the confidence. A rejected or low-confidence guess is replaced with
`RiskCause.UNKNOWN`, whose eligibility row allows nothing the customer can see — a bad guess makes
Revora more careful, not less. A rejected link description is **replaced** with the reviewed
fixed template and never sent; the draft is kept, and the customer-message counter does not move for
the suppression.

**The credential is optional and absent by default.** With no `REVORA_LLM_CREDENTIAL`, the adapter
builds nothing: no client, no payload, no wait, no row. The layer is not "unavailable", it is simply
not there, and the whole system runs the same way every time. `tests/properties/test_reasoning_authority.py`
proves that a run with no credential and a run where every response is rejected are the same
system — both hand every pure component `None`.

That file carries seven properties. Each one is driven from a real provider outcome sent over a mock
transport with all four gates running, never from a result built by hand. That matters: a
hand-built `Accepted` result could let a property assert a mapping the adapter cannot actually
produce.

- **P49** — a model answer changes no policy outcome. Every response except an accepted,
  above-floor cause leaves the twelve check outcomes byte-for-byte the same as a cycle that asked
  nothing. An accepted one changes exactly one field: the *recorded* cause, after the ceiling and the
  floor substitution.
- **P50** — an explanation changes no figure. `influenced_recommendation` is a literal `False`.
- **P51** — the missing credential and the rejected response are the same system, and nothing waits.
- **P52** — one row per call, and the limit is counted from rows.
- **P53** — the set of keys sent is a subset of the set that was declared, tested with hostile
  values that carry JSON separators, because the question is whether a *value* can sneak in a *key*.
- **P54** — a `1.000` confidence can only be reached through `DETERMINISTIC`.
- **P55** — a refused draft is a replacement, never a skipped step.

The rest of the "AI" is the estimation and optimization layer: a baseline estimate of the chance of
recovery with an honest confidence interval (a range showing how sure the estimate is), per-action
uplift priors (starting assumptions about how much each action helps), and expected-value ranking.
All of it is labelled `UNCALIBRATED` until something checks it, because an unchecked estimate
presented as a validated one is the same kind of lie as overstating incremental revenue.

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

The same thing as a state machine, with the parts that make it terminate:

```mermaid
stateDiagram-v2
    [*] --> DETECTED
    DETECTED --> DIAGNOSED
    DIAGNOSED --> DECISION_PENDING
    DECISION_PENDING --> POLICY_CHECK
    POLICY_CHECK --> ACTION_SCHEDULED : approved, needs provider
    POLICY_CHECK --> BLOCKED : a check said no
    ACTION_SCHEDULED --> EXECUTING
    EXECUTING --> WAITING_FOR_OUTCOME : effect confirmed
    WAITING_FOR_OUTCOME --> RECOVERED : authoritative read says captured
    WAITING_FOR_OUTCOME --> DECISION_PENDING : re-decide, +1 cycle
    POLICY_CHECK --> DECISION_PENDING : review, restraint revisited, +1 cycle
    POLICY_CHECK --> STOPPED : cycle budget spent
    DECISION_PENDING --> ESCALATED : hand to a person
    DETECTED --> EXPIRED : window elapsed
    RECOVERED --> [*]
    STOPPED --> [*]
    BLOCKED --> [*]
    EXPIRED --> [*]
    ESCALATED --> [*]
```

*Simplified — the real table has 63 edges. `FAILED` and the verified-capture shortcuts from every
non-terminal state are left out here to keep it readable.*

There are fourteen case states, six of them terminal (`RECOVERED`, `STOPPED`, `BLOCKED`, `EXPIRED`,
`ESCALATED`, `FAILED`), 63 legal edges, and nothing else. Every transition writes the new state, the
bumped version, and the audit record in one transaction that commits all three or none.

Now that a case can be re-decided instead of just left alone, the graph has loops, so ending well
rests on three facts rather than on the absence of loops: every loop passes through
`DECISION_PENDING`, every edge into `DECISION_PENDING` bumps the decision-cycle count, and
`decision_cycle_count` never goes down. Stating this as a rule about the target state, rather than as
a list of edges, is what lets an edge added tomorrow inherit the same guarantee.

**Each pipeline step is its own job**, and each one queues the next step *inside the same transaction
that saved its own work*. That is why the job queue is a Postgres table, not a message broker. A
broker that queues after commit can lose the message, and one that queues before commit can fire
against state that never committed.

### The twelve policy checks

All twelve run and all twelve are recorded, every time — never just "the ones that got as far as
running". In the record, a partly recorded evaluation would look exactly like one that ran fewer
checks and then approved.

```
1  ALREADY_PAID          5  CUSTOMER_OPTED_OUT     9  MAX_ATTEMPTS_REACHED
2  ALREADY_TERMINAL      6  CONSENT_MISSING       10  MAX_MESSAGES_REACHED
3  DUPLICATE_ACTION      7  HUMAN_OWNERSHIP       11  COOLDOWN_ACTIVE
4  FRAUD_OR_RISK         8  WINDOW_EXPIRED        12  ACTION_NOT_ELIGIBLE
```

The policy engine is a pure function (its output depends only on its inputs, with no side effects).
It cannot read the database and cannot reach the network. The row-loading around it lives in
`revora/jobs/pipeline.py`, the one layer allowed to see both. That purity is what makes the engine
easy to test exhaustively with property tests.

### Nine candidate actions, and only three can touch the outside world

`DO_NOTHING`, `WAIT`, `RETRY`, `DELAYED_RETRY`, `PAYMENT_LINK`, `CUSTOMER_MESSAGE`,
`PAYMENT_METHOD_UPDATE`, `PROMISE_TO_PAY_FOLLOW_UP`, `HUMAN_ESCALATION`.

`PROVIDER_ACTIONS` is exactly `{PAYMENT_LINK, CUSTOMER_MESSAGE, PROMISE_TO_PAY_FOLLOW_UP}`. The
promise follow-up joined the list because the capability was *verified*, not because anyone chose to
be optimistic. `POST /v1/payment_links/:id/notify_by/:medium` re-sends the link the case already
has and creates no second link. So it is the one customer-visible action Revora can run that creates
no new payable object. It was already customer-visible, so it had already used up the message cap, and
nothing about the cap changed. Where the case holds no live link, it falls back to creating one.

`HUMAN_ESCALATION` is approved and then **terminal** — the case leaves automation. Asking a person is
a real answer. Treating it like a provider call would leave the case approved but not carried out,
waiting for its window to close, with nothing on the record to explain why nothing happened.

Three actions stay unavailable — `RETRY`, `DELAYED_RETRY` and `PAYMENT_METHOD_UPDATE` — because a
failed one-off payment is final at the provider, and a fresh attempt is a new payment that the
customer starts. They stay in the vocabulary and stay visible in the recorded candidate set with the
reason they were excluded, because showing an action as unavailable is more honest than quietly
leaving it out.

Doing nothing is a full, ranked, priced option. `DO_NOTHING` is by definition net-zero. When nothing
clears the value floors, the recommendation says `NO_POSITIVE_VALUE` and the case stops with a
reason. A blocked or unacted case is not a failed case, and no screen presents it as one.

### Exactly one external effect per approval

The idempotency key (a key that makes doing the same request twice have the same effect as doing it
once) is built from `(case_id, action, attempt_ordinal)` and travels to Razorpay as `reference_id`,
so their side rejects a duplicate too — two independent mechanisms for one guarantee.

The hard case is a timeout after the request has already left the socket: the link may or may not
exist, and the caller cannot tell. Revora records the intent as `UNCERTAIN` rather than guessing, and
reconciliation settles it by *reading* — reusing the same idempotency key, never making a new one. A
property test checks that, across any mix of crashes, timeouts, and reconciliation passes, the number
of creates sent per key is at most one.

---

## Failing safely

Infrastructure faults produce **delayed decisions**, never duplicate charges, duplicate messages, or
made-up numbers. Each of these has an integration test that breaks the thing and checks that the
damage is only a delay:

- **Postgres unreachable at ingest** → HTTP 503, nothing saved, and the event lands cleanly when the
  provider redelivers it. It returns 503 rather than 500 on purpose, because Revora's only recovery
  here is that redelivery, and a retry only helps against a temporary fault. The catch is narrow —
  connection-level errors only — so a repeatable SQL bug stays a 500 instead of being retried until
  the provider gives up and quietly drops the event.
- **Worker killed mid-execution** → the intent stays unresolved, reconciliation confirms the existing
  link against the provider's state, and the provider ever sees only one create for that key.
- **A recovery window elapsed during downtime** → the expiry is worked out again from saved rows on
  restart, and the case is expired *before* anything schedules an action against it.
- **A withheld action whose limits have since moved** → thrown away on the way back up, the discard
  audited, no external call, and both limit counters left untouched.

---

## The customer response loop

A failed payment has a person on the other end, and that person knows things Revora cannot work out:
that payday is Friday, that the card was replaced, that the order was cancelled, that the charge is
disputed. The second spec adds a way for them to say so. Its whole design is about letting that
information in without letting it decide anything.

### The page, and the token that reaches it

**A public customer response page** is the only route you can reach without a session, apart from the
inbound webhook and `GET /health`. You reach it with a `Customer_Access_Token` shaped like
`rvc_<token_id>.<secret>`: a separately random 26-character handle, plus a 128-bit secret. The handle
is the only part allowed in a log line or an audit field, and it is randomised on its own *because* it
is the part that gets copied around — deriving it from the secret would turn every legitimate mention
into a partial leak.

What is stored is `HMAC-SHA256(signing_key, token_id ‖ secret)` — keyed, not a plain hash — so a
database dump on its own does not let someone verify tokens offline. Verification does one indexed
lookup and then compares against **every** active signing secret, combining the results with `|=` and
never stopping early. A missing row uses a per-process decoy hash and does the same work, so "no such
handle", "wrong secret", and "signed by a retired key" all fold into one branch with an identical
response.

The token is created **inside the execution's first transaction**, before the provider call, because
the token URL is what the message carries. A token created at confirmation would arrive after the only
message that could have delivered it. So a failed token creation rolls back the intent, the counter
movement, and the used-up decision all together. There is exactly one live token per case, enforced by
a partial unique index over `revoked_at IS NULL`. Its expiry is the earlier of the token lifetime and
the case's `window_end_at`, and is never extended. It is revoked when the case goes terminal or
contact is suppressed.

**One result worth stating plainly.** If the token signing secret cannot be resolved, `PAYMENT_LINK`
and `CUSTOMER_MESSAGE` stop too. The execution is abandoned in its first transaction with zero
external calls, rather than sending a customer a message whose only link is dead.

### What the page discloses

**Exactly eight fields and no ninth**: merchant display name, amount, currency, plain-language
reason, pay URL, window end, signals remaining, and the promise if there is one. They come from a
purpose-built frozen dataclass rather than a trimmed-down dashboard model. That is the mechanism, not
just the intention: there is no inherited shape to trim, so **adding a field to the dashboard cannot
leak it here**. No probability, no cost term, no net value, no rejected candidate, no policy
decision, no configuration value, and no contact identifier in any form. The reason is built from the
recorded `Risk_Cause`, never the provider's error string. That string is internal wording written for
an operator debugging a payment rail, not for a person deciding whether they can pay today.

The amount is a whole-number count of minor units, formatted by the server's renderer and passed
downward, so the customer page and the dashboard cannot disagree about what a rupee looks like.

### Three write shapes, and none of them decides anything

A delay reason, a partial-arrangement request, a promise to pay. Each accepted write is **one
transaction holding four things**: the accepted-submission-count increment under the token's row
lock, the `customer_signal` insert, the queued consequence, and the audit record — last, so any
rollback it causes rolls back work that was already staged. All four or none. No state transition, no
action scheduling, no policy evaluation, and no provider request happens inside the accepting request.
The worker applies every consequence through the single writer of `recovery_case.state`.

**Customer input is evidence, never authority.** Property 36 replaces every `Customer_Signal` field
with any schema-valid content and checks that the twelve policy check outcomes stay byte-for-byte the
same, with the suppression state pinned so the claim is about signal *content*. Its structural half is
stronger: `PolicyInput`'s set of fields shares none with any signal field, so there is nowhere for
signal content to sit.

**A partial-arrangement request cannot be stored as a partial amount.** The request model declares no
`amount`, no `instalment_count`, and no `schedule`, and sets `extra="forbid"`. One layer down,
`customer_signal` has no column for any of the three. The refusal is built into the structure: there
is nowhere to put the number. The request is recorded, it fetches a person, and it changes no money
field.

**Promises never buy time.** `follow_up_at = min(promise_date + follow_up_offset, window_end_at −
safety_margin)`. The recovery window is set when the case is created and never extended. A promise
past the window sends the case to a person instead of stretching the window, and `promise_to_pay`
carries a `CHECK (follow_up_at IS NULL OR follow_up_at < window_end_at_snapshot)`, so a follow-up
computed at or past the window end is a failed insert rather than a bad decision.

**A hard stop is permanent.** A disputed charge or a cancelled order writes a contact suppression
keyed on `sha256(customer_key ‖ order_id)`, in the same transaction as the signal that caused it.
There is no `expires_at` column, so it cannot expire — that is enforced by the missing column rather
than by a check — and releasing it requires a named person. It feeds into the input of the existing
check 5, `CUSTOMER_OPTED_OUT`, rather than becoming a thirteenth check. Every spot a thirteenth check
could take is *after* a limit, and a ban must not sit where a counter bug could overrule it. It
suppresses that one debt **without** setting the customer-wide opt-out, because "I object to this
charge" and "stop contacting me at all" are different statements, and merging them destroys the record
of which one was said.

### Restraint is now revisited rather than abandoned

This is the flaw the second spec exists to fix. A `Null_Action` choice — `DO_NOTHING` or `WAIT` —
now saves a `next_review_at`, a sweeper re-decides the case, and a `CASE_REVIEWED` record is written
*whether or not the choice changed*, because a review that chose to wait again is the proof that
restraint was looked at again. Such a case shows up in no "ended" grouping on any screen. The
decision-cycle counter keeps a limit on the whole thing.

### The case timeline

Nine stages, built purely from the gap-free audit sequence. The projection owns no table and cannot
write, has no session and no queue in scope, and takes `now` as an argument — so the same records
always produce the same timeline. It always shows all nine stages, not just "the ones that happened",
because a reader used to nine rows who sees six does not notice which three are missing. A gap in the
audit sequence shows a banner naming the missing numbers rather than claiming a stage is done.

### A second frontend, not a second route

The customer page is its own Vite entry, built to `web/dist-customer/`. A `/pay/:token` route inside
the dashboard bundle would hand a stranger with no login the whole admin surface as readable source —
nothing in it is secret, but it is a map. The page also has to open from an SMS on a slow, cold
mobile connection, so it carries neither the router nor the query client. It uses no router at all,
and it shares exactly one module with the dashboard, the `<Money>` component, so it cannot compute a
figure that disagrees with the server.

---

## The demonstration evidence

**This is a synthetic demonstration, not a real-money claim.** Every figure below is measured, from a
single run on a freshly migrated database, and the most important one is the figure that stayed
refused.

1000 cases were seeded **through the signed webhook endpoint**, not written straight in as rows. Each
one goes through signature verification over the raw bytes, canonicalization, dedup, detection,
diagnosis, estimation, the optimizer, twelve policy checks, execution, and the outcome monitor. The
customer-side outcomes are driven over real HTTP with real bearer tokens against the mounted customer
surface.

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
that refusal held even through a run whose lift was clean, backed by enough data, and clearly above
zero. So the demonstration figure is a separate field with its own name and its own `SYNTHETIC` and
`DEMONSTRATION_ONLY` labels, shown right next to it rather than in a footnote.

The interval leaves out zero **and** contains the planted truth. That is the whole reason for quoting
it: it says the measurement setup works, and says nothing at all about real recovery rates.

Outcome coverage was checked *after* the run rather than assumed, audit sequences were gap-free on
every case, and there were zero `REAL`-provenance rows anywhere in the database.

Reproduce it with the `harness` tier, from the repository root, against a migrated Postgres:

```powershell
$env:REVORA_TEST_DATABASE_URL = "postgresql+psycopg://…"
.venv\Scripts\python.exe -m pytest -m harness -q
```

It takes about seventeen minutes, which is why this tier runs nightly and before a demo build rather
than on every commit.

**One honest zero.** The authoritative reads in a harness run are genuine reads of a **fake**
provider, so `verified_test_mode_recoveries` reports `0`, and that is correct. Razorpay's Payment
Links API can create, fetch, update, cancel, and re-notify a link; it has no documented endpoint that
*pays* one, and in test mode payment happens on a page that asks a person to pick success or failure.
So the three real test-mode recoveries are a documented manual step in `RUNBOOK.md` rather than a
script. Automating them would mean faking a capture, which is the one thing this figure exists not to
do.

---

## The audit trail

R11.C5 lists eight questions the record must answer, and one ordered read of a case's audit records
answers all eight: what happened, why, on what evidence, which alternatives were considered, which
policy rules allowed or blocked the action, which action ran, whether payment recovered, and how the
recovery is classified.

The per-case sequence is gap-free and starts at 1. Without that, "the full history" is a claim nobody
can check, because a missing record and a record that never existed look the same. Audit records are
append-only, enforced by a revoked grant plus a trigger, and an attempt to change one is itself
logged.

One correlation id ties the whole delivery chain together, so "something odd happened at 14:32" is
one query rather than a search.

---

## Security and customer data

- **Session auth is a DB-backed `merchant_session` row**, not a stateless signed token, because
  sessions must be revocable. The token is `<merchant-slug>.<32 urlsafe random bytes>`; only a keyed
  HMAC digest is stored. The slug finds the merchant, and the digest is then looked up *inside that
  tenant*, so a swapped slug fails safely.
- **Every auth failure answers 401 in a byte-for-byte identical way.** The audit record keeps the
  difference; the response does not, so the endpoint gives an attacker nothing to probe. Those audit
  records are written in their own transaction, because the caller raises 401 right afterwards and a
  shared transaction would roll back the evidence.
- **Cross-tenant reads answer 404**, never 403 — a 403 would confirm the resource exists.
- **Row-level security on 36 of the 37 tables**, as an extra layer behind the application's own
  tenant scoping. The exception is `merchant`, which has no `merchant_id` because it *is* the tenant.
  Every session sets its merchant binding.
- **The inbound webhook is one of exactly two unauthenticated routes** (the other is `GET /health`,
  which reveals only whether the service is alive — the schema revision it checks is logged, not
  returned). An unknown merchant slug answers exactly like a bad signature. The customer response page
  is the only other surface reachable without a session, and it is not unauthenticated: it needs a
  `Customer_Access_Token`, and it is a mounted sub-application, so its CORS and cache middleware apply
  to exactly its routes and do not loosen the dashboard's settings.
- **Raw webhook payloads are encrypted at rest** with a keyed nonce and carry a `retain_until`.
  Retention redaction clears the contact and the ciphertext but **keeps `customer_key`** — destroying
  it would wipe out every recorded opt-out — and keeps every amount and timestamp.
- **Opt-out is keyed on the customer, not the payment**, so it covers cases that do not exist yet.
- Contact details are masked in every audit field, and are never written to the test fake's call log
  either, because a fixture is still a place where a PII habit can form.

---

## Layout

A modular monolith (one codebase, cleanly split into modules). One image, three process roles, chosen by `REVORA_ROLE`: `api`, `worker`, and `ticker` — the scheduler, which creates every periodic sweep job and reclaims the leases of jobs a dead worker left `RUNNING`. Without the ticker running, nothing expires a case, reconciles an intent, re-reads payment state, backfills a detection gap, redacts customer data, or reviews a case that chose restraint — and nothing looks broken.

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

Each of those directories has its own README with a map and the reasons behind its layout:
[`revora/`](revora/README.md) · [`web/`](web/README.md) · [`tests/`](tests/README.md) ·
[`alembic/`](alembic/README.md) · [`scripts/`](scripts/README.md).

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
process under `/app`, because the API already owns `/cases` and `/metrics`. Interactive API docs are
at `/docs`.

The one image builds both frontends in its own stage, so `web/dist` and `web/dist-customer` are never
committed, and what ships is always built from the committed source. Serving the dashboard and the API
from one origin is what lets the deployment run with **no CORS middleware at all**, which is safer
than an allowlist because there is nothing to misconfigure later. The customer page really is
cross-origin, so it carries its own CORS middleware scoped to its own mount rather than widening the
dashboard's.

To work on the frontends with hot reload, run the API on `:8000` and then, from `web/`:

```bash
npm ci
npm run dev        # :5173, proxying the API paths to :8000 so dev is same-origin too
```

The dev server serves the dashboard at `/app/` and the customer page at `/pay/<token>`, which is the
URL a customer actually receives. A developer opening `/index-customer.html` would get a page with no
token and see the not-found panel instead of the page they are working on.

The dev proxy exists so local development uses the same origin setup as production. Developing
cross-origin and deploying same-origin would mean the CORS configuration is only ever tested in dev,
never in the form that ships.

### Secrets

Supplied through the environment. Nothing has a default — a credential that cannot be resolved fails
loudly at the point of use rather than quietly degrading.

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

The `pure or model` selection is 988 passing plus one known failure that was already there, described
under **Known limits**. `mypy` covers sixteen packages; the full list is in
`.github/workflows/ci.yml`.

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
JavaScript. `npm run build` runs two passes, one for the dashboard and one for the customer page.

**Roughly 1400 Python tests collected, plus 81 frontend tests.** Six cost tiers, marked so the cheap
ones gate every commit and the expensive ones gate a push:

| Marker | What it means |
| --- | --- |
| `pure` | no I/O at all, microseconds |
| `model` | in-memory fake repositories, seconds |
| `pg` | real Postgres via testcontainers |
| `harness` | full synthetic pipeline plus the Demo_Batch, nightly and pre-demo |
| `spike` | manual provider verification against test-mode credentials |
| `smoke` | latency and startup-cost budgets, left out of every gating selection on purpose — on a shared runner a latency limit measures the hardware as much as the code, and a gate nobody can act on just teaches everyone to re-run the build. Tagged rather than deleted so the budgets stay written down |

The customer response loop added no marker and no CI job. The existing tiers absorbed it, which is one
fewer list to keep in sync.

`mypy` runs `strict` with `disallow_any_explicit` on `domain`, `policy` and `optimizer` — the three
packages where a type error turns into a money bug — and standard elsewhere. Using strict everywhere
would mean fighting SQLAlchemy's descriptor typing for no extra safety on the figures that matter.

### The tests worth reading

- `tests/integration/test_full_pipeline.py` — the whole chain from a signed webhook, with nothing
  faked but the provider. Checks one external effect and a recovery backed by a read.
- `tests/integration/test_degradation.py` — the four infrastructure faults above.
- `tests/properties/test_lifecycle_machine.py` — one stateful machine carrying fourteen
  history-level invariants (rules that must always hold), checked after every step. Hypothesis picks
  the order of deliveries, clock steps, sweeps, opt-outs, ownership changes, and process restarts;
  the teardown then moves past every limit and requires that no case is still running, which is the
  only way to catch a lifecycle that leaks cases.
- `tests/properties/test_reasoning_authority.py` — the seven properties above.
- `tests/properties/test_customer_signal_authority.py` — signal content changes no policy outcome,
  first by behaviour and then by structure, which is the stronger of the two.
- `tests/properties/test_customer_audit_money.py` — the Demo_Batch. One batch, three properties,
  because a thousand cases through the webhook endpoint takes about seventeen minutes, and a batch
  per test would cost three times as much and prove nothing more.
- `tests/properties/` — Hypothesis properties over the state machine, the policy engine, the
  idempotency guarantee, and the money type.
- `tests/strategies/clocks.py` — time steps taken from the *configured* bounds at just-under,
  exactly, and just-over, because a plain random delta almost never lands right on a boundary, and a
  boundary is exactly where `>=` and `>` differ.
- `tests/fakes/razorpay.py` — a provider that can fail in every way that matters, including the two
  timeout cases the caller cannot tell apart, which differ only in whether the link exists.
- `web/src/routes/Metrics.test.jsx` — checks that the incremental slot shows `NOT ESTABLISHED` and
  that the observed figure appears exactly once on the page. That count is the test: it would catch
  the most tempting possible regression, a well-meaning "fall back to the observed number when
  incremental is unavailable".
- `web/src/test/lint-rule.test.js` — runs ESLint against the shipped config and proves the money rule
  fires, and that it leaves the legitimate use (sorting by `minor`) alone.
- `web/src/customer/build.test.js` — checks that the customer page still ships with no router and
  shares exactly one module with the dashboard, which is the boundary the second entry exists to hold.

Every negative claim in the suite is checked against the fake's call log, not against the mere
absence of exceptions. "No provider request was issued" is a claim about something that did not
happen, and the only evidence for that is a log of everything that did.

---

## Known limits, stated plainly

The design marks its own weak points, and so does this README. None of these is hidden behind a
confident number.

- **The baseline is unvalidated.** Nothing has yet checked the do-nothing probability estimates
  against reality, so they report `UNVALIDATED_BASELINE` or `CALIBRATION_UNVERIFIED` and never
  `VALIDATED`.
- **Uplift priors are just priors.** Per-action uplift figures are assumptions, labelled
  `UNCALIBRATED`, and the math they drive is out in the open: a payment link and a human escalation
  cross over around ₹12,000, above which Revora asks a person. That is a real product behaviour that
  falls out of the numbers, not a hand-tuned constant.
- **`NO_INTERVENTION_CONFIRMED` is narrower than it sounds.** It means no Revora action *and* no
  recorded merchant action. Revora cannot see a merchant phoning a customer. The weakness is labelled
  in the data rather than solved, and only control-arm cases with zero confirmed actions qualify. A
  case Revora chose not to treat is a *selected* group, and counting it as a baseline label is the
  classic selection bias that would flatter every later incremental claim.
- **Provider redelivery behaviour is assumed.** Revora relies on Razorpay redelivering after a 5xx.
  The retry schedule and attempt count are unverified.
- **Read-after-write lag on the payment-link listing is unmeasured**, which is why reconciliation may
  only treat an empty listing as a failure on its final attempt.
- **No regulatory compliance claim is made.** The security section describes engineering controls.
- **`ruff format` is not a gate.** The tree is hand-wrapped to 100 columns, enforced by `E501`.
  Adopting the formatter would be a mechanical change across the whole tree; lint runs, formatting
  does not.
- **One test fails, and it is not hidden.** `tests/properties/test_beta_interval.py::test_cdf_is_monotone`
  fails on a `Decimal` precision issue in `revora/estimation/beta.py`. It has nothing to do with
  either spec's features, it was failing before both, and the honest thing is to name it rather than
  quote a clean suite. The `pure or model` selection is 988 passed, 1 failed.
- **The demonstration run is synthetic and says so.** Its lift is measured against a lift the
  generator planted. It supports a claim about the measurement setup and no claim at all about what
  Revora would recover from real traffic — which is why `incremental_recovered_revenue` stayed
  `NOT_ESTABLISHED` through it.
- **`verified_test_mode_recoveries` is `0` in any automated run**, correctly: the reads are real but
  they read a fake. The three real test-mode recoveries are a manual `RUNBOOK.md` step.
- **Two reasoning-authority claims are checked at the `model` tier, not `pg`.** In their fullest
  form, P51 and P52 are claims about rows; what runs here is the mechanism that decides them — the
  value handed to every pure component is identical, and exactly one complete row can be derived from
  every result variant. Row-level confirmation lives in `tests/persistence/test_reasoning_wiring.py`.
- **Suppression scope is the narrower reading.** A dispute is keyed on the order the event carried
  one for, so it suppresses that one debt and not every debt the customer has. It is an
  `[ASSUMPTION]`, and it is the direction that can be widened later without losing data.

---

## Design record

Two specs, each with its full requirements, design and task breakdown:

- `.kiro/specs/revora-incremental-revenue-recovery/` — the pipeline, the two-figure separation, the
  three structural rules, exactly-once execution, the audit trail.
- `.kiro/specs/revora-customer-response-loop/` — the customer response page and its access token, the
  three write shapes, promises and hard stops, revisited restraint, the case timeline, the reasoning
  layer, and the demonstration batch.

Claims in those documents are tagged `[ASSUMPTION]`, `[INFERENCE]` or `[EVIDENCE INSUFFICIENT]` where
they are not established, and those tags do real work: several of the limits above exist because a tag
was not allowed to quietly turn into a fact.
