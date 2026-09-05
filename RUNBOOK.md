# 🚀 Revora — Local Development & Execution Runbook

This guide has everything you need to run, test, and work on the **Revora Autonomous Revenue Recovery System** (Backend API, Background Worker, Ticker, Dashboard SPA, and Webhook Simulation).

---

## 🔒 Private Credentials & Login Secrets

> **Note:** All private credentials (API keys, operator sign-in keys, database URLs, encryption secrets) are kept in the gitignored file:
> 
> 📄 **`local_credentials.txt`** *(in the project root)*

---

## ⚡ Quick Start: 4-Terminal Execution

Open **PowerShell** terminals in your workspace folder (`.../Razorpay Builathon challange`).

### 🖥️ **Terminal 1 — Backend API & Dashboard**
Runs FastAPI on `http://127.0.0.1:8000`, with the dashboard served at `/app`:
```powershell
cd "c:\Users\suvam\Desktop\VS code\Projects\Razorpay Builathon challange"; . .\scripts\dev_env.ps1; .\.venv\Scripts\python.exe -m revora.api.main
```

### ⚙️ **Terminal 2 — Background Worker**
Picks up queued jobs (ingestion, diagnosis, optimization, policy checks, execution, and outcome monitoring):
```powershell
cd "c:\Users\suvam\Desktop\VS code\Projects\Razorpay Builathon challange"; . .\scripts\dev_env.ps1; $env:REVORA_ROLE='worker'; .\.venv\Scripts\python.exe -m revora.jobs.main
```

### ⏰ **Terminal 3 — Ticker (the schedule)**
Creates the seven periodic sweep jobs, and moves a dead worker's `RUNNING` job back to `PENDING`. Nothing else creates those jobs. Without it, the worker has nothing periodic to pick up: cases never expire, intents never reconcile, payment state is never re-read, detection gaps are never filled in, customer data is never redacted, and a case that chose to hold back is never reviewed. None of this logs an error.
```powershell
cd "c:\Users\suvam\Desktop\VS code\Projects\Razorpay Builathon challange"; . .\scripts\dev_env.ps1; $env:REVORA_ROLE='ticker'; .\.venv\Scripts\python.exe -m revora.jobs.ticker_main
```

`enqueued=0` in its log is the normal steady state — it means every interval bucket already has a pending sweep, which is the dedupe key doing its job. To drive the same loop by hand instead, one tick at a time:
```powershell
cd "c:\Users\suvam\Desktop\VS code\Projects\Razorpay Builathon challange"; . .\scripts\dev_env.ps1; .\.venv\Scripts\python.exe scripts\dev_tick.py --due
```

### 📡 **Terminal 4 — Send Test Events (Webhook Simulator)**
Send fake Razorpay failed-payment events to kick off recovery flows:

- **Standard failure event:**
  ```powershell
  cd "c:\Users\suvam\Desktop\VS code\Projects\Razorpay Builathon challange"; . .\scripts\dev_env.ps1; .\.venv\Scripts\python.exe scripts\dev_webhook.py failed --slug default-merchant
  ```
- **High-amount failure event (₹20,000):**
  ```powershell
  cd "c:\Users\suvam\Desktop\VS code\Projects\Razorpay Builathon challange"; . .\scripts\dev_env.ps1; .\.venv\Scripts\python.exe scripts\dev_webhook.py failed --slug default-merchant --amount 2000000
  ```

---

## 🎨 Terminal 5 (Optional) — Frontend UI Hot-Reload

By default, the backend API serves the pre-built frontend SPA at `http://127.0.0.1:8000/app`. 

If you are modifying frontend UI code (`web/` directory) and want live hot-reloading:
```powershell
cd "c:\Users\suvam\Desktop\VS code\Projects\Razorpay Builathon challange\web"; npm run dev
```
Open browser at: **[http://localhost:5173](http://localhost:5173)** *(automatically proxies/redirects to `/app`)*.

---

## 🌐 Accessing the Dashboard UI

1. Open your browser: **[http://127.0.0.1:8000/app](http://127.0.0.1:8000/app)**
2. Enter the sign-in details:
   - **Merchant Slug:** `default-merchant`
   - **Operator Key:** *(check `local_credentials.txt` for `IM3V05ZcspAjmYyUHcK3bJplgsmagnc1`)*
3. Explore the dashboard sections:
   - **Cases:** View active, evaluated, scheduled, recovered, or expired payment cases.
   - **Metrics:** Trusted cohort metrics, net incremental lift, and confidence bounds.
   - **Unresolved:** Tracks how much money is still at stake across open decision cycles.
   - **Experiments:** Randomized control group allocation & lift comparisons.
   - **Consent:** Customer consent tracking and communication preferences.

---

## 🛠️ Handy Management Commands

### 1. Database Migrations (Alembic)
Apply the latest PostgreSQL schema migrations (includes `bigint` duration columns, `merchant_session`, etc.):
```powershell
cd "c:\Users\suvam\Desktop\VS code\Projects\Razorpay Builathon challange"; . .\scripts\dev_env.ps1; .\.venv\Scripts\python.exe -m alembic upgrade head
```

### 2. Wiring & Health Check
Verify database connection, crypto keys, and pipeline services:
```powershell
cd "c:\Users\suvam\Desktop\VS code\Projects\Razorpay Builathon challange"; . .\scripts\dev_env.ps1; .\.venv\Scripts\python.exe scripts\dev_check.py
```

### 3. Seed a New Merchant
Create a new tenant merchant with an operator key:
```powershell
cd "c:\Users\suvam\Desktop\VS code\Projects\Razorpay Builathon challange"; . .\scripts\dev_env.ps1; .\.venv\Scripts\python.exe scripts\dev_seed.py <new-merchant-slug>
```

### 4. Kill Stuck Process on Port 8000
If port 8000 is held by a leftover process:
```powershell
Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force }
```

### 5. Run the Test Suite
Run the full pytest suite (including unit, integration, and property tests):
```powershell
cd "c:\Users\suvam\Desktop\VS code\Projects\Razorpay Builathon challange"; . .\scripts\dev_env.ps1; .\.venv\Scripts\pytest.exe
```

---

## 📂 Project Architecture Reference

```
Razorpay Builathon challange/          # project root
├── revora/
│   ├── api/            # FastAPI app, routers, SPA mount (/app), webhook receiver
│   ├── cases/          # Case lifecycle state machine, consent, sweeper
│   ├── detection/      # At-risk payment failure detection
│   ├── diagnosis/      # Failure pattern classification & risk causes
│   ├── estimation/     # Baseline recovery probability & candidate scoring
│   ├── execution/      # Payment link generation, customer messaging, engine
│   ├── experiment/     # Randomized control groups, Bayesian/frequentist lift
│   ├── jobs/           # Job queue, pipeline handler registration, worker entrypoint
│   ├── memory/         # Long-term recovery observation store & learning
│   ├── metrics/        # Authoritative revenue reporting & unresolved exposure
│   ├── optimizer/      # Decision optimization & action selection
│   ├── persistence/    # SQLAlchemy 2.0 async/sync models, migrations & repos
│   └── platform/       # Config, crypto keys, secrets, logging, clocks
├── scripts/            # Dev CLI tools (dev_env, dev_check, dev_seed, dev_webhook)
├── web/                # React / Vite dashboard single-page application
├── local_credentials.txt # [GITIGNORED] Private local credentials & secrets
└── RUNBOOK.md          # This instruction guide
```

---

## 🧾 Verified Test-Mode Recoveries (manual, 3 required)

`DEMO_VERIFIED_RECOVERY_MIN_COUNT = 3`. A **Verified_Demo_Recovery** is a Demo_Batch case that reached `RECOVERED` because a trusted, direct read of the provider reported `captured = true` at an amount equal to the case's `payment_amount` — money that really moved in Razorpay **test mode**.

> **Why this is manual and not a script.** Razorpay's Payment Links API can create, fetch, update, cancel and re-notify a link. **It has no endpoint that pays one.** Payment happens on the customer-facing payment page, and in test mode that page is a mock that asks a person to pick success or failure. So `revora.synthetic.demo.verified_test_mode_capability()` returns `False`, and no automation is written for step 3 below. Automating it would mean faking a capture — the one thing this figure exists not to do.
>
> A harness or `pg` run uses the scriptable fake provider, so it reports `verified_test_mode_recoveries: 0`. That zero is correct: the reads are real, but they read a fake. Only the run below produces a non-zero count.

### Before you start

You need real Razorpay test credentials. They are read from `.env` by name — **never paste a secret value into a terminal, a log or this file**:

| Variable | What it must be |
|---|---|
| `REVORA_RAZORPAY_KEY_ID` | a test-mode key id (the `rzp_test_…` form) |
| `REVORA_RAZORPAY_KEY_SECRET` | its secret |
| `REVORA_WEBHOOK_SECRETS_<SLUG>` | the webhook secret configured on the same Razorpay account |

Check that the loaded key is a **test** key before running anything that spends money:

```powershell
cd "c:\Users\suvam\Desktop\VS code\Projects\Razorpay Builathon challange"; . .\scripts\dev_env.ps1; .\.venv\Scripts\python.exe -c "import os; k=os.environ.get('REVORA_RAZORPAY_KEY_ID',''); print('test mode:', k.startswith('rzp_test_'))"
```

If that prints `test mode: False`, **stop.** Nothing below should be run against a live key.

### Steps

1. **Start the four terminals** (API, worker, ticker, and the webhook simulator) exactly as in the Quick Start above, with the real Razorpay client rather than the fake — that is what `. .\scripts\dev_env.ps1` gives you.

2. **Send three failed-payment events** in the amount band where the optimizer picks `PAYMENT_LINK` (₹1,500–₹11,000). Anything below that band is correctly a null action, and anything above it is correctly sent to a human, so an amount outside the band still makes a valid case but no link:
   ```powershell
   1..3 | ForEach-Object { .\.venv\Scripts\python.exe scripts\dev_webhook.py failed --slug default-merchant --amount 400000 }
   ```

3. **Pay each link, by hand.** Find the three `plink_…` ids and their `short_url`s — either on the Razorpay test dashboard under Payment Links, or in the worker log line for the created link. Open each `short_url` in a browser, pick any test payment method, and on the mock page choose **success**. This is the step with no API.

4. **Let the capture land.** Either the real `payment.captured` webhook arrives at `/webhooks/razorpay/default-merchant`, or the payment-state reconciliation sweep reads the payment on its next tick. Both paths end in `observe_payment_outcome`, which does a genuine `fetch_payment` and declares recovery only from `captured = true` with `amount = payment_amount`. To drive the sweep by hand instead of waiting:
   ```powershell
   .\.venv\Scripts\python.exe scripts\dev_tick.py --due
   ```

5. **Confirm the evidence** from the rows, not from the log. `recovery_outcome.verified_by_read_id` is `NOT NULL` by design, so every recorded recovery names the read that verified it — this is the same query `authoritative_test_mode_recoveries` runs:
   ```powershell
   docker exec revora-pg18 psql -U revora -d revora -c "SELECT count(*) FROM recovery_outcome o JOIN payment_state_read r ON r.id = o.verified_by_read_id AND r.merchant_id = o.merchant_id JOIN recovery_case c ON c.id = o.case_id AND c.merchant_id = o.merchant_id WHERE c.state = 'RECOVERED' AND r.captured AND r.amount = c.payment_amount"
   ```
   Three or more is R28.C2 satisfied.

### What the label means

These three amounts land in `observed_recovered_revenue`, labelled `SYNTHETIC`, `CAUSALITY_NOT_ESTABLISHED` and `RECOVERY_GROSS_OF_REFUNDS` on every screen and every export (R28.C3, R28.C14). **The label is what makes this evidence and not a claim:** the money really moved in test mode, and nothing here says Revora caused it. `incremental_recovered_revenue` stays `NOT_ESTABLISHED` — a synthetic run can't support a causal claim, no matter how clean its lift is.
