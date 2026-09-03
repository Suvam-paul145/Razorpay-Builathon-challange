"""Confirm the configured database is migrated, seeded, and reachable before starting anything.

    python scripts/dev_check.py

Answers the four questions that go wrong first, in the order a failure would bite:

1. Can this process reach the database at all?
2. Is it at the revision this build expects? The API verifies this at startup and *refuses to
   serve* on a mismatch, so a wrong answer here means the API exits immediately rather than
   serving wrong figures.
3. Are the configurable bounds seeded? An empty ``app_config`` means every bound falls back to its
   code default, which works but is not what the deployment was configured to do.
4. Is there a merchant to sign in as, and does its slug match the per-merchant credentials that are
   actually set? A slug with no matching ``REVORA_DASHBOARD_KEYS_*`` cannot be signed into, and the
   only symptom is a 401 that looks identical to a mistyped key.

Prints no secret values — only whether each is present.
"""

from __future__ import annotations

import os
import sys

from sqlalchemy import create_engine, text

from revora.persistence.repositories.schema import EXPECTED_REVISION
from revora.platform.secrets import _slug_to_env

SENTINEL_SLUG = "__platform_defaults__"
"""The config-defaults tenant seeded by migration 0004. Not a merchant; never signed into.

Duplicated from the migration rather than imported: an Alembic revision is a historical record and
importing from one would make this script break the day that file is squashed away."""

_REQUIRED = (
    "REVORA_DATABASE_URL",
    "REVORA_PAYLOAD_ENCRYPTION_KEYS",
    "REVORA_CUSTOMER_KEY_SECRET",
    "REVORA_CUSTOMER_TOKEN_SIGNING_SECRETS",
    "REVORA_SESSION_TOKEN_SECRET",
    "REVORA_RAZORPAY_KEY_ID",
    "REVORA_RAZORPAY_KEY_SECRET",
)


def main() -> int:
    url = os.environ.get("REVORA_DATABASE_URL", "").strip()
    if not url:
        print("REVORA_DATABASE_URL is not set. Run: . .\\scripts\\dev_env.ps1", file=sys.stderr)
        return 2

    host = url.split("@")[-1].split("/")[0] if "@" in url else "?"
    print(f"database : {host}")

    problems: list[str] = []

    missing = [name for name in _REQUIRED if not os.environ.get(name, "").strip()]
    if missing:
        problems.append(f"credentials not set: {', '.join(missing)}")
    print(f"secrets  : {len(_REQUIRED) - len(missing)}/{len(_REQUIRED)} required present")

    engine = create_engine(url, future=True)
    try:
        with engine.begin() as connection:
            revision = connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one_or_none()
            tables = connection.execute(
                text(
                    "SELECT count(*) FROM information_schema.tables "
                    "WHERE table_schema = 'public'"
                )
            ).scalar_one()
            bounds = connection.execute(text("SELECT count(*) FROM app_config")).scalar_one()
            slugs = [row[0] for row in connection.execute(text("SELECT slug FROM merchant"))]
            users = connection.execute(text("SELECT count(*) FROM merchant_user")).scalar_one()
            cases = connection.execute(text("SELECT count(*) FROM recovery_case")).scalar_one()
    except Exception as exc:
        print(f"\nCANNOT REACH THE DATABASE: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        engine.dispose()

    print(f"revision : {revision} (this build expects {EXPECTED_REVISION})")
    print(f"tables   : {tables}")
    print(f"bounds   : {bounds} rows in app_config")
    print(f"merchants: {slugs or '(none)'}")
    print(f"users    : {users}")
    print(f"cases    : {cases}")

    if revision != EXPECTED_REVISION:
        problems.append(
            f"schema is at {revision}, this build expects {EXPECTED_REVISION}. "
            "The API refuses to serve on a mismatch — run: python -m alembic upgrade head"
        )
    if bounds == 0:
        problems.append("app_config is empty; every configurable bound falls back to its default")
    if not slugs:
        problems.append("no merchant; run: python scripts/dev_seed.py default-merchant")

    for slug in slugs:
        # The sentinel tenant is not a merchant. Migration 0004 seeds it to hold the ~50 platform
        # config defaults that every real merchant inherits, so it has no operator and no webhook
        # and expecting credentials for it would report a problem that is the schema working.
        if slug == SENTINEL_SLUG:
            continue
        suffix = _slug_to_env(slug)
        if not os.environ.get(f"REVORA_DASHBOARD_KEYS_{suffix}", "").strip():
            problems.append(
                f"merchant '{slug}' has no REVORA_DASHBOARD_KEYS_{suffix}, so it cannot be "
                "signed into — sign-in would answer 401 exactly as a wrong key does"
            )
        if not os.environ.get(f"REVORA_WEBHOOK_SECRETS_{suffix}", "").strip():
            problems.append(
                f"merchant '{slug}' has no REVORA_WEBHOOK_SECRETS_{suffix}, so every webhook "
                "for it is rejected 401"
            )

    print()
    if problems:
        print("PROBLEMS")
        for problem in problems:
            print(f"  - {problem}")
        return 1

    print("ready. Start the API, the worker and the ticker, then send a webhook.")
    print("  the ticker (REVORA_ROLE=ticker, python -m revora.jobs.ticker_main) is what")
    print("  produces the seven periodic sweeps. Without it the worker has nothing")
    print("  periodic to claim and no case ever expires — with nothing in any log.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
