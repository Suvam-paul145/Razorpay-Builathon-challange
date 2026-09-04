from __future__ import annotations

import json
import os
import time
from decimal import Decimal

from sqlalchemy import create_engine, text

from revora.persistence.repositories.config import ConfigurationRepository
from revora.persistence.repositories.session import tenant_transaction
from revora.synthetic import demo
from tests.demo_support import demo_harness

URL = "postgresql+psycopg://revora:revora_ci@127.0.0.1:5545/revora"
CASES = int(os.environ.get("DEMO_CASES", "60"))
PRIOR = int(os.environ.get("DEMO_PRIOR", "40"))
MDE = os.environ.get("DEMO_MDE", "")

if MDE:
    demo.DEMO_MINIMUM_DETECTABLE_EFFECT = Decimal(MDE)

engine = create_engine(URL, future=True)
started = time.time()
try:
    for harness in demo_harness(engine):
        with tenant_transaction(harness.tenant.merchant_id) as session:
            config = ConfigurationRepository(session).load(harness.tenant.merchant_id)
        report = demo.run_demo_batch(
            harness.tenant,
            transport=harness.transport,
            worker=harness.worker,
            advance=harness.advance,
            config=config,
            synthetic_run_id=harness.synthetic_run_id,
            case_count=CASES,
            prior_cohort_size=PRIOR,
            script_payment=harness.script_payment,
        )
        elapsed = time.time() - started
        print("ELAPSED_SEC", round(elapsed, 1), "CASES", CASES, flush=True)
        print("DEMO_REPORT", json.dumps(report.as_document(), indent=2), flush=True)
        with engine.begin() as connection:
            print(
                "STATES",
                connection.execute(
                    text(
                        "SELECT state, terminal_reason, count(*) FROM recovery_case "
                        "WHERE merchant_id = :m GROUP BY state, terminal_reason ORDER BY 3 DESC"
                    ),
                    {"m": str(harness.tenant.merchant_id)},
                ).all(),
                flush=True,
            )
            print(
                "PROVENANCE",
                connection.execute(
                    text(
                        "SELECT provenance, count(*) FROM recovery_case "
                        "WHERE merchant_id = :m GROUP BY provenance"
                    ),
                    {"m": str(harness.tenant.merchant_id)},
                ).all(),
                flush=True,
            )
finally:
    engine.dispose()
