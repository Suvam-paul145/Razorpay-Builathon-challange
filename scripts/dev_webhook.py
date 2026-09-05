"""Send a correctly signed Razorpay-shaped webhook, so the dashboard has real cases to show.

    python scripts/dev_webhook.py failed            # a ₹1,000 failure -> payment link path
    python scripts/dev_webhook.py failed --amount 2000000   # ₹20,000 -> human escalation path
    python scripts/dev_webhook.py captured pay_XXXX # the customer paid; triggers a real read

    python scripts/dev_webhook.py failed --slug acme --url http://127.0.0.1:8000

The signature is the part you cannot do with ``curl`` by hand: it is an HMAC-SHA256 over the **exact
request bytes**, so any reformatting of the body invalidates it. This script signs the bytes it
sends.

**Amount decides which branch runs, and both are correct product behaviour.** The priors put a human
escalation above a payment link on net value from roughly ₹12,000 up, so a small failure produces a
payment link and a large one produces an ``ESCALATED`` case with zero provider calls. If you want to
see the recovery path end to end, use the default amount.

**Record consent first.** The twelve policy checks include ``CONSENT_MISSING``, so a case for a
customer with no consent on file is blocked — correctly, and visibly, on the case detail page. Use
the Consent page in the dashboard (contact ``+919876543210``) before sending a failure, or send one
first and watch it block.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.request
import uuid

DEFAULT_CONTACT = "+919876543210"
DEFAULT_AMOUNT = 100_000  # ₹1,000 in paise


def failed_body(payment_id: str, event_id: str, amount: int, contact: str) -> bytes:
    """A ``payment.failed`` envelope with a reason the deterministic table maps.

    ``insufficient_funds`` resolves to ``INSUFFICIENT_FUNDS``, whose eligibility row permits a
    payment link — so the optimizer has a real action to weigh rather than falling through to the
    null actions for lack of a candidate.
    """
    now = int(time.time())
    return json.dumps(
        {
            "entity": "event",
            "event": "payment.failed",
            "contains": ["payment"],
            "created_at": now,
            "payload": {
                "payment": {
                    "entity": {
                        "id": payment_id,
                        "amount": amount,
                        "currency": "INR",
                        "status": "failed",
                        "order_id": f"order_{event_id}",
                        "method": "card",
                        "contact": contact,
                        "email": "buyer@example.invalid",
                        "error_code": "BAD_REQUEST_ERROR",
                        "error_description": "insufficient balance",
                        "error_reason": "insufficient_funds",
                        "error_source": "issuer_bank",
                        "error_step": "payment_authorization",
                        "created_at": now,
                    }
                }
            },
        },
        separators=(",", ":"),
    ).encode("utf-8")


def captured_body(payment_id: str, amount: int) -> bytes:
    """A ``payment.captured`` envelope — the success *signal*, never the proof.

    Revora does not declare a recovery from this. It triggers an authoritative ``fetch_payment``
    against the provider, and that read decides. With test-mode Razorpay credentials the read will
    answer whatever Razorpay actually knows about the id, so a made-up payment id will *not* recover
    the case — which is the guarantee working, not a bug.
    """
    now = int(time.time())
    return json.dumps(
        {
            "entity": "event",
            "event": "payment.captured",
            "contains": ["payment"],
            "created_at": now,
            "payload": {
                "payment": {
                    "entity": {
                        "id": payment_id,
                        "amount": amount,
                        "amount_refunded": 0,
                        "captured": True,
                        "currency": "INR",
                        "status": "captured",
                        "method": "card",
                        "created_at": now,
                    }
                }
            },
        },
        separators=(",", ":"),
    ).encode("utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kind", choices=["failed", "captured"])
    parser.add_argument("payment_id", nargs="?", default=None)
    parser.add_argument("--slug", default="demo")
    parser.add_argument("--amount", type=int, default=DEFAULT_AMOUNT)
    parser.add_argument("--contact", default=DEFAULT_CONTACT)
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    args = parser.parse_args()

    suffix = args.slug.strip().upper().replace("-", "_")
    secret = os.environ.get(f"REVORA_WEBHOOK_SECRETS_{suffix}", "").strip()
    if not secret:
        print(f"REVORA_WEBHOOK_SECRETS_{suffix} is not set", file=sys.stderr)
        return 2
    # A rotation list is `secret1,secret2`; sign with the first, which is the active one.
    secret = secret.split(",")[0].strip()

    payment_id = args.payment_id or f"pay_{uuid.uuid4().hex[:14]}"
    event_id = f"evt_{uuid.uuid4().hex[:14]}"
    body = (
        failed_body(payment_id, event_id, args.amount, args.contact)
        if args.kind == "failed"
        else captured_body(payment_id, args.amount)
    )
    signature = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()

    request = urllib.request.Request(
        f"{args.url.rstrip('/')}/webhooks/razorpay/{args.slug}",
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Razorpay-Signature": signature,
            "X-Razorpay-Event-Id": event_id,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            status = response.status
    except urllib.error.HTTPError as exc:
        status = exc.code

    print(f"{args.kind:9} payment={payment_id} event={event_id} -> HTTP {status}")
    if status == 200:
        print("  accepted. The worker picks it up on its next poll (about a second).")
    elif status == 401:
        print("  rejected. The signing secret does not match what the API resolved.")
    elif status == 202:
        print("  quarantined. The payload was accepted but could not be canonicalized.")
    elif status == 503:
        print("  the database is unreachable. Nothing was persisted; resend when it is back.")
    return 0 if status in (200, 202) else 1


if __name__ == "__main__":
    raise SystemExit(main())
