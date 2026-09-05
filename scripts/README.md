# `scripts/` — dev tools and one CI gate

Six files. Five are local development helpers; one is a build gate that fails CI.

All commands run from the **repository root**.

---

## The CI gate

### `check_no_float.py`

Walks every module that touches currency — **72 of them** — and fails the build if it finds a
`float`, a `/`, or a `round()` anywhere near an amount.

```powershell
.venv\Scripts\python.exe scripts\check_no_float.py
```

This is the text-scanning half of the money rule. `mypy` catches a wrong *type*; this catches an
integer divided by an integer, which passes the type check yet is wrong by two decimal places.
Together they cover what neither does alone.

It is a step that fails the build in [`../.github/workflows/ci.yml`](../.github/workflows/ci.yml),
not just an advisory script.

---

## Development helpers

### `dev_env.ps1` — always run this first

Sets `REVORA_DATABASE_URL` and every local secret in the current shell. Dot-source it (run it with a
leading `.`) so the variables stay set:

```powershell
. .\scripts\dev_env.ps1
```

Nothing in Revora has a default credential. A secret that cannot be resolved fails loudly where it is
used, instead of quietly degrading. So most other commands fail without this.

### `dev_check.py` — is the environment sane

```powershell
.venv\Scripts\python.exe scripts\dev_check.py
```

Checks the database connection, the migration revision, the crypto keys and the pipeline wiring.
**Run this before debugging anything else** — it turns "the app is broken" into a specific, named cause.

### `dev_seed.py` — create a merchant

```powershell
.venv\Scripts\python.exe scripts\dev_seed.py <merchant-slug>
```

Creates a tenant and prints its operator key, which is what the dashboard sign-in asks for.

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

It signs the body with the merchant's real webhook secret and POSTs it to the real endpoint. So the
delivery goes through signature verification over raw bytes, canonicalization and dedup exactly as a
real Razorpay delivery would.

**The two amounts are the demo.** Same code path, different decision, because the expected-value math
works out that way — not because of an `if` that checks the amount.

### `spikes/` — provider verification against real test-mode credentials

Four scripts that measure what Razorpay **actually does**, instead of assuming it. Several design
parameters come from their output, and `docs/provider-findings.md` records the results.

| Spike | The question it answers |
| --- | --- |
| `duplicate_reference_id.py` | Does Razorpay reject a duplicate `reference_id`? Confirms the second half of the exactly-once guarantee |
| `link_listing_freshness.py` | How stale is the payment-link listing? **This one sets the reconciliation parameters** |
| `payment_read_lag.py` | How long until a capture is visible to an authoritative read |
| `retry_capability.py` | Can a failed payment be retried at the provider, or is it terminal |

See [`spikes/README.md`](spikes/README.md) for credentials, run order and the manual steps. These are
the `spike` test tier. They never run in CI, because they make real network calls.

### `dev_tick.py` — drive the schedule by hand

```powershell
.venv\Scripts\python.exe scripts\dev_tick.py --due
```

Runs one tick of the periodic sweeps instead of waiting for the ticker process.

**Why you will need this:** nothing in Revora ticks on its own. Without the ticker running, cases
never expire, intents never reconcile, payment state is never re-read, and a case that chose
restraint is never reviewed — **and none of that logs an error.** If something seems stuck, this is
usually why.

---

## Related

- [`../RUNBOOK.md`](../RUNBOOK.md) — the four-terminal local run, and the manual test-mode recovery steps
- [`../DEMO-GUIDE.md`](../DEMO-GUIDE.md) — the presentation script
- [`../tests/README.md`](../tests/README.md) — the automated equivalents of these helpers
