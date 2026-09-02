# 🚀 Revora — Local Development & Execution Runbook

This guide contains everything you need to run, test, and develop the **Revora Autonomous Revenue Recovery System** (Backend API, Background Worker, Dashboard SPA, and Webhook Simulation).

---

## 🔒 Private Credentials & Login Secrets

> **Note:** All confidential credentials (API keys, operator sign-in keys, database URLs, encryption secrets) are stored in the gitignored file:
> 
> 📄 **`local_credentials.txt`** *(in the `Rovara/` directory)*

---

## ⚡ Quick Start: 3-Terminal Execution

Open **PowerShell** terminals in your workspace directory (`.../Razorpay Builathon challange/Rovara`).

### 🖥️ **Terminal 1 — Backend API & Dashboard**
Runs FastAPI on `http://127.0.0.1:8000` with the dashboard mounted at `/app`:
```powershell
cd "c:\Users\suvam\Desktop\VS code\Projects\Razorpay Builathon challange\Rovara"; . .\scripts\dev_env.ps1; .\.venv\Scripts\python.exe -m revora.api.main
```

### ⚙️ **Terminal 2 — Background Worker**
Pulls queued jobs (ingestion, diagnosis, optimization, policy checks, execution, and outcome monitoring):
```powershell
cd "c:\Users\suvam\Desktop\VS code\Projects\Razorpay Builathon challange\Rovara"; . .\scripts\dev_env.ps1; $env:REVORA_ROLE='worker'; .\.venv\Scripts\python.exe -m revora.jobs.main
```

### 📡 **Terminal 3 — Send Test Events (Webhook Simulator)**
Send mock Razorpay failed payment events to trigger recovery flows:

- **Standard failure event:**
  ```powershell
  cd "c:\Users\suvam\Desktop\VS code\Projects\Razorpay Builathon challange\Rovara"; . .\scripts\dev_env.ps1; .\.venv\Scripts\python.exe scripts\dev_webhook.py failed --slug default-merchant
  ```
- **High-amount failure event (₹20,000):**
  ```powershell
  cd "c:\Users\suvam\Desktop\VS code\Projects\Razorpay Builathon challange\Rovara"; . .\scripts\dev_env.ps1; .\.venv\Scripts\python.exe scripts\dev_webhook.py failed --slug default-merchant --amount 2000000
  ```

---

## 🎨 Terminal 4 (Optional) — Frontend UI Hot-Reload

By default, the backend API serves the pre-built frontend SPA at `http://127.0.0.1:8000/app`. 

If you are modifying frontend UI code (`web/` directory) and want live hot-reloading:
```powershell
cd "c:\Users\suvam\Desktop\VS code\Projects\Razorpay Builathon challange\Rovara\web"; npm run dev
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
   - **Metrics:** Authoritative cohort metrics, net incremental lift, confidence bounds.
   - **Unresolved:** Exposure tracking across open decision cycles.
   - **Experiments:** Randomized control group allocation & lift comparisons.
   - **Consent:** Customer consent tracking and communication preferences.

---

## 🛠️ Handy Management Commands

### 1. Database Migrations (Alembic)
Apply the latest PostgreSQL schema migrations (includes `bigint` duration columns, `merchant_session`, etc.):
```powershell
cd "c:\Users\suvam\Desktop\VS code\Projects\Razorpay Builathon challange\Rovara"; . .\scripts\dev_env.ps1; .\.venv\Scripts\python.exe -m alembic upgrade head
```

### 2. Wiring & Health Check
Verify database connection, crypto keys, and pipeline services:
```powershell
cd "c:\Users\suvam\Desktop\VS code\Projects\Razorpay Builathon challange\Rovara"; . .\scripts\dev_env.ps1; .\.venv\Scripts\python.exe scripts\dev_check.py
```

### 3. Seed a New Merchant
Create a new tenant merchant with an operator key:
```powershell
cd "c:\Users\suvam\Desktop\VS code\Projects\Razorpay Builathon challange\Rovara"; . .\scripts\dev_env.ps1; .\.venv\Scripts\python.exe scripts\dev_seed.py <new-merchant-slug>
```

### 4. Kill Stuck Process on Port 8000
If port 8000 is occupied by an orphaned process:
```powershell
Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force }
```

### 5. Run the Test Suite
Run the full pytest suite (including unit, integration, and property tests):
```powershell
cd "c:\Users\suvam\Desktop\VS code\Projects\Razorpay Builathon challange\Rovara"; . .\scripts\dev_env.ps1; .\.venv\Scripts\pytest.exe
```

---

## 📂 Project Architecture Reference

```
Rovara/
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
