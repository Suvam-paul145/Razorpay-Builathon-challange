"""Create a merchant and an operator user so the dashboard has something to sign in to.

    python scripts/dev_seed.py            # slug "demo"
    python scripts/dev_seed.py acme       # slug "acme"

Idempotent: running it twice leaves one merchant and one user. Reads ``REVORA_DATABASE_URL``.

**The slug decides two environment variable names**, because webhook secrets and operator keys are
per-merchant. Slug ``demo`` means ``REVORA_WEBHOOK_SECRETS_DEMO`` and
``REVORA_DASHBOARD_KEYS_DEMO``. The script prints the names it expects, so there is no guessing.

There is deliberately no password. ``merchant_user`` has no password column — the design defers
per-user credentials, roles and MFA — so a dashboard session is minted with a per-merchant *operator
key* held by whoever runs the deployment, and that key is what the sign-in form asks for.
"""

from __future__ import annotations

import os
import sys
import uuid

from sqlalchemy import create_engine, text

from revora.platform.secrets import _slug_to_env


def main() -> int:
    slug = (sys.argv[1] if len(sys.argv) > 1 else "demo").strip().lower()
    url = os.environ.get("REVORA_DATABASE_URL", "").strip()
    if not url:
        print("REVORA_DATABASE_URL is not set", file=sys.stderr)
        return 2

    engine = create_engine(url, future=True)
    with engine.begin() as connection:
        existing = connection.execute(
            text("SELECT id FROM merchant WHERE slug = :slug"), {"slug": slug}
        ).scalar_one_or_none()

        if existing is None:
            merchant_id = uuid.uuid4()
            connection.execute(
                text(
                    """
                    INSERT INTO merchant (id, slug, display_name, default_currency, state,
                                          reporting_timezone, created_at)
                    VALUES (:id, :slug, :name, 'INR', 'ACTIVE', 'Asia/Kolkata', now())
                    """
                ),
                {"id": str(merchant_id), "slug": slug, "name": f"{slug.title()} Retail"},
            )
            print(f"created merchant {slug} ({merchant_id})")
        else:
            merchant_id = uuid.UUID(str(existing))
            print(f"merchant {slug} already exists ({merchant_id})")

        user = connection.execute(
            text("SELECT id FROM merchant_user WHERE merchant_id = :m LIMIT 1"),
            {"m": str(merchant_id)},
        ).scalar_one_or_none()
        if user is None:
            user_id = uuid.uuid4()
            connection.execute(
                text(
                    """
                    INSERT INTO merchant_user (id, merchant_id, email_masked, email_key,
                                               role, is_active, created_at)
                    VALUES (:id, :m, '****ops@example.invalid', :key, 'operator', true, now())
                    """
                ),
                {"id": str(user_id), "m": str(merchant_id), "key": f"emailkey-{user_id}"},
            )
            print(f"created operator user {user_id}")
        else:
            print(f"operator user already exists ({user})")

    engine.dispose()

    suffix = _slug_to_env(slug)
    print()
    print("Sign in at  http://127.0.0.1:8000/app")
    print(f"  merchant slug : {slug}")
    print(f"  operator key  : whatever REVORA_DASHBOARD_KEYS_{suffix} is set to")
    print()
    print("Webhook endpoint")
    print(f"  POST http://127.0.0.1:8000/webhooks/razorpay/{slug}")
    print(f"  signed with REVORA_WEBHOOK_SECRETS_{suffix}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
