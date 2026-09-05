# Revora — Live Demo Guide

A stepwise script for showing Revora to judges. Read it top to bottom once, then present.

Everything here uses the **deployed** app. Nothing needs to run on your laptop.

---

## Links

| What | URL | Login |
| --- | --- | --- |
| **Dashboard** (what you present) | https://revora-api-h3aj.onrender.com/app | **Evaluator credentials**: slug `razorpay-judge`, key `razorpay-pass` *(Supports 5+ concurrent judges)*<br>*(Internal admin: slug `default-merchant`, key `IM3V05ZcspAjmYyUHcK3bJplgsmagnc1`)* |
| **Customer page** (what a payer sees) | https://razorpay-builathon-challange.vercel.app/ | opens from a link in the payment message |
| API health check | https://revora-api-h3aj.onrender.com/health | — |
| API docs (optional) | https://revora-api-h3aj.onrender.com/docs | — |

> The dashboard and the API are the **same** Render service. The customer page is a **separate** Vercel deployment on purpose — an unauthenticated payer must never be shipped the admin app's source.

---

## The one sentence to open with

> "A failed payment is not lost revenue. Revora finds out **why** each payment failed, decides whether acting is worth more than doing nothing, acts **at most once**, and then reports what it **actually** recovered — separating money that came back *because of Revora* from money that would have come back anyway."

That last separation is the whole product. Everything else supports it.

---

## The idea judges must remember

Most recovery tools send a payment link, some customers pay, and the tool claims the total as "revenue recovered." **That number is inflated** — many of those customers would have paid on their own, and the tool takes credit for the customer's own persistence.

Revora refuses to make that claim. It always shows **two figures, never merged**:

- **Observed recovered revenue** — **Total payments collected:** All money that arrived after Revora reached out.
- **Incremental recovered revenue** — **True added value:** Only the *extra* revenue brought in because Revora stepped in (excluding customers who would have retried and paid anyway). Proven through A/B holdout tests; displays `NOT_ESTABLISHED` until statistically verified.

**Where this applies in the real world:** any merchant deciding whether a recovery vendor is worth paying for. Revora is the tool that tells them the honest number instead of the flattering one.

---

## Before you start (do this 5 minutes early)

The Render free tier **sleeps when idle**. The first request after a nap takes ~40–60 seconds.

1. Open https://revora-api-h3aj.onrender.com/health in a browser.
2. Wait until it returns a small JSON response (not an error page). This wakes the server.
3. Now open the dashboard link and sign in. It will be instant.

If `/health` shows an error about the schema revision, the database migration hasn't been applied — see **If something breaks** at the bottom.

---

## The demo, tab by tab

Sign in at the dashboard link with the slug and key above. You land on **Performance**. There are five tabs across the top: **Performance · Cases · Unresolved · Experiments · Consent**. Present them in this order.

### Step 1 — Performance (the headline)

**Do:** Point at the two big figures side by side.

**Say:**
> "Observed recovered revenue is real money that came back on cases we worked. Right beside it, incremental revenue reads `NOT_ESTABLISHED` with a `CAUSALITY_NOT_ESTABLISHED` label — we will not call it *ours* until an experiment proves it. Notice every amount is server-formatted; the browser never does money maths, so two parts of the screen can't disagree."

**Applies to:** the finance/ops person who signs the cheque — this is the number they actually care about.

### Step 2 — Cases (the decision, per payment)

**Do:** Open the Cases list, then click one case to open its detail.

**Say:**
> "Each failed payment becomes a case. On the detail page you can see every action Revora *considered*, priced by expected value, and all twelve policy checks it ran — in a fixed order, every one recorded. 'Do nothing' is a real, ranked option, not a fallback. If nothing is worth more than waiting, Revora waits, and says why."

**Point at:** the timeline at the top of the case — nine stages from *detected* to *outcome verified*, built entirely from the audit trail.

**Applies to:** a support agent or auditor who needs to answer "why did we do that?" for any single customer.

### Step 3 — Unresolved (honest about what it didn't fix)

**Do:** Open Unresolved.

**Say:**
> "Cases that ended without recovery are grouped by reason — stopped, expired, escalated to a person, blocked. A blocked or unacted case is **not** hidden and **not** counted as a failure. If a customer disputed the charge or cancelled the order, Revora escalates to a human and permanently stops contacting them about that debt."

**Applies to:** compliance and customer-experience teams — proof the tool won't harass a customer who said no.

### Step 4 — Experiments (where the causal claim comes from)

**Do:** Open Experiments and open the one experiment.

**Say:**
> "This is how `incremental` gets established: a holdout where some cases are deliberately left untreated. The dashboard shows both arms, the measured lift, and its confidence interval. Only when that interval sits entirely above zero does Revora make a causal claim."

**Applies to:** the skeptical buyer who's been burned by inflated vendor numbers before.

### Step 5 — Consent (the guardrail)

**Do:** Open Consent briefly.

**Say:**
> "Opt-out is keyed to the customer, not the payment, so it governs cases that don't exist yet. Once someone opts out, Revora cannot contact them — enforced in the code, not by policy."

**Applies to:** anyone asking about DPDP / privacy obligations.

---

## The customer side (30 seconds)

**Do:** Open the Vercel link: https://razorpay-builathon-challange.vercel.app/

You'll see a "not found" style page — **this is correct**. The page only works when opened from a real payment link carrying a one-time token. The URL a customer receives looks like:

```
https://razorpay-builathon-challange.vercel.app/pay/default-merchant/rvc_<26 chars>.<22 chars>
```

The token sits in the **path**, never a query string, so it stays out of `Referer` headers and analytics.

**Say:**
> "This is the page a customer opens from the payment message. It shows the amount and reason, a pay button, and lets them tell us *why* it's late or promise a date. It's a completely separate deployment from the dashboard — a payer never receives the admin app. It discloses exactly eight fields and nothing else: no internal probabilities, no costs, no other customer's data."

**Applies to:** the end customer — the person actually being asked to pay.

> If you want to show it fully working, that needs a token minted by a live payment run — do that only if you've rehearsed it. For most demos, explaining it on the "not found" page is enough and safer.

---

## The proof number (say this near the end)

> "We ran 1,000 synthetic failed payments through the **real** pipeline — signed webhook, verification, diagnosis, decision, execution, outcome. It recovered **₹10,85,237** in observed revenue. And here's the discipline: because the data was synthetic, `incremental_recovered_revenue` **stayed** `NOT_ESTABLISHED` even though the internal measurement was clean and well above zero. The system refuses to overclaim even when it easily could."

**Applies to:** the closing argument — this is the behaviour that separates Revora from every "we recovered X" dashboard.

---

## If something breaks

| Symptom | Cause | Fix |
| --- | --- | --- |
| Dashboard slow / spinner on first load | Render free tier was asleep | Wait ~60s; hit `/health` first next time |
| Blank page after visiting the base URL | Dashboard lives at `/app`, not `/` | Use `https://revora-api-h3aj.onrender.com/app` |
| API error mentioning "schema revision" | Neon database not migrated to latest | Run `alembic upgrade head` against the Neon URL, then reload |
| Sign-in rejected | Wrong slug or key | slug `default-merchant`, key `IM3V05ZcspAjmYyUHcK3bJplgsmagnc1` |
| Customer page says "not found" | Expected without a token | Explain it instead of trying to load a live case |

---

## 20-second recap if you run out of time

1. Failed payments aren't automatically lost — Revora decides, per payment, whether acting beats waiting.
2. It acts **at most once**, and only declares a recovery from an authoritative provider read, never a webhook.
3. It reports **observed** money as fact and **incremental** money only when an experiment proves it — and refuses to inflate the number even when it could.

That refusal is the product.
