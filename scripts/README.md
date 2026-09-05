# `scripts/` — dev tools and one CI gate

Six files. Five are local development helpers; one is a build gate that fails CI.

All commands run from the **repository root**.

---

## The CI gate

### `check_no_float.py`

Walks every currency-bearing module — **72 of them** — and fails the build on a `float`, a `/`, or a
`round()` anywhere near an amount.

```powershell
.venv\Scripts\python.exe scripts\check_no_float.py
```

This is the lexical half of the money rule. `mypy` catches a wrong *type*; this catches an integer
divided by an integer, which type-checks perfectly and is wrong by two decimal places. Together they
cover what neither does alone.

It is a build-failing step in [`../.github/workflows/ci.yml`](../.github/workflows/ci.yml), not an
advisory script.

---

## Development helpers

### `dev_env.ps1` — always run this first

Sets `REVORA_DATABASE_URL` and every local secret in the current shell. Dot-source it so the
variables persist:

```powershell
. .\scripts\dev_env.ps1
```

Nothing in Revora has a credential default — an unresolvable secret fails loudly at the point of use
rather than silently degrading — so most other commands fail without this.

### `dev_check.py` — is the environment sane

```powershell
.venv\Scripts\python.exe scripts\dev_check.py
```

Verifies the database connection, the migration revision, the crypto keys and the pipeline wiring.
**Run this before debugging anything else** — it turns "the app is broken" into a named cause.

### `dev_seed.py` — create a merchant

```powershell
.venv\Scripts\python.exe scripts\dev_seed.py <merchant-slug>
```

Creates a tenant and prints its operator key, which is what the dashboard sign-in wants.

### `dev_webhook.py` — send a payment event

The fastest way to watch the whole pipeline run.

```powershell
# a ₹1,000 failure — takes the payment-link path
.venv\Scripts\python.exe scripts\dev_webhook.py failed --slug default-merchant

# a ₹20,000 failure — crosses the crossover, so Revora escalates to a person instead
.venv\Scripts\python.exe scripts\dev_webhook.py failed --slug default-merchant --amount 2000000

# the customer paid — triggers a real authoritative read
.venv\Scripts\python.exe scripts\dev_webhook.py captured pay_XXXXXXXX
```

It signs the body with the merchant's real webhook secret and POSTs it to the real endpoint, so the
delivery traverses signature verification over raw bytes, canonicalization and dedup exactly as
Razorpay's would.

**The two amounts are the demo.** Same code path, different decision, because the expected-value
arithmetic says so — not because of a branch on amount.

### `spikes/` — provider verification against real test-mode credentials

Four scripts that measure what Razorpay **actually does**, rather than assuming it. Several design
parameters are set from their output, and `docs/provider-findings.md` records the results.

| Spike | The question it answers |
| --- | --- |
| `duplicate_reference_id.py` | Does Razorpay reject a duplicate `reference_id`? Confirms the second half of the exactly-once guarantee |
| `link_listing_freshness.py` | How stale is the payment-link listing? **This one sets the reconciliation parameters** |
| `payment_read_lag.py` | How long until a capture is visible to an authoritative read |
| `retry_capability.py` | Can a failed payment be retried at the provider, or is it terminal |

See [`spikes/README.md`](spikes/README.md) for credentials, run order and the manual steps. These are
the `spike` test tier — never run in CI, because they make real network calls.

### `dev_tick.py` — drive the schedule by hand

```powershell
.venv\Scripts\python.exe scripts\dev_tick.py --due
```

Runs one tick of the periodic sweeps instead of waiting for the ticker process.

**Why you will need this:** nothing in Revora ticks itself. Without the ticker running, cases never
expire, intents never reconcile, payment state is never re-read, and a case that chose restraint is
never reviewed — **and none of that logs an error.** If something seems stuck, this is usually why.

---

## Related

- [`../RUNBOOK.md`](../RUNBOOK.md) — the four-terminal local run, and the manual test-mode recovery steps
- [`../DEMO-GUIDE.md`](../DEMO-GUIDE.md) — the presentation script
- [`../tests/README.md`](../tests/README.md) — the automated equivalents of these helpers
