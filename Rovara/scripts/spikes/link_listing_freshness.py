"""Spike 2.1 — how long after creating a Payment Link does it become listable?

WRITES TO THE PROVIDER. Every iteration creates a real Payment Link in whichever
Razorpay account the credentials belong to. Test mode only; the key-id guard in
``_common.py`` refuses anything that does not look like a test key.

What is being measured and why it matters
-----------------------------------------
Revora has no idempotency header for Payment Link creation — design.md establishes
that as a fact, and establishes the substitute: ``reference_id`` is set to the
``Idempotency_Key``, and after any uncertain create, ``GET /v1/payment_links?
reference_id=<key>`` answers "does this effect already exist?".

That substitute has one failure mode, and it is the one that produces a duplicate
payment demand to a real customer. If a link created milliseconds ago is not yet
visible in the listing endpoint, the reconciliation read returns empty, the intent is
marked ``FAILED``, and a later attempt creates a second link for money the customer
was already asked for once. design.md tags the read-after-write visibility of that
listing endpoint [EVIDENCE INSUFFICIENT] and prescribes exactly this test.

So: create a link with a fresh unique ``reference_id``, then poll the listing endpoint
filtered by that ``reference_id`` until the link appears, and record how long that
took. Repeat ~50 times. Report min, median, p95, max, and — the number that actually
decides the parameters — how many never appeared inside the poll budget.

Verified surface this spike relies on (design.md, Provider Verification Findings)
--------------------------------------------------------------------------------
* ``POST /v1/payment_links`` with ``amount`` in integer minor units, ``currency``,
  ``description``, ``reference_id`` (documented as required-unique, max 40 chars),
  ``notify{sms,email}``, ``reminder_enable``, ``accept_partial``, ``notes``.
* Response carries ``id`` (``plink_…``), ``short_url``, ``status``.
* ``GET /v1/payment_links`` supports querying by ``reference_id``.
* Basic auth.

Deliberate departures from the production payload, so the spike cannot hurt anyone
----------------------------------------------------------------------------------
* ``notify: {sms: false, email: false}`` and no ``customer`` block. Production sets
  these true — that is how ``CUSTOMER_MESSAGE`` is delivered — but a spike that runs
  50 iterations must not send 50 notifications, even in test mode.
* ``expire_by`` omitted by default. See the ``--expire-after-seconds`` help text.

Webhook configuration: this spike needs none. It is a pure request/response
measurement over the REST API. Spike 2.2 (``payment_read_lag.py``) is the one that
needs a reachable webhook endpoint, and its requirements are documented there and in
``scripts/spikes/README.md``.
"""

from __future__ import annotations

import argparse
import sys
import time
import uuid
from pathlib import Path

import httpx
from _common import (
    WRITES_WARNING,
    JsonValue,
    ResultRecorder,
    SpikeSetupError,
    add_common_arguments,
    build_client,
    collection_items,
    configure_console,
    json_body,
    latency_summary,
    load_credentials,
    ns_to_ms,
)

SPIKE_NAME = "link_listing_freshness"
DESIGN_ITEM = "Reconciliation Read — read-after-write visibility [EVIDENCE INSUFFICIENT]"
REQUIREMENTS = ("R9.C15", "R9.C17")

CREATE_PATH = "/v1/payment_links"
LIST_PATH = "/v1/payment_links"

# design.md: `reference_id` is capped at 40 characters, and production derives it as
# `rv_` + 16 hex chars. The spike keeps the same shape and length discipline so the
# measurement is taken against a key the production code could actually emit.
REFERENCE_ID_MAX_LENGTH = 40


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="link_listing_freshness.py",
        description=(
            "Measure how long a newly created Payment Link takes to become visible in "
            "GET /v1/payment_links?reference_id=… . " + WRITES_WARNING
        ),
        epilog=(
            "Sets or confirms: EXECUTION_RECONCILIATION_INTERVAL, "
            "MAX_EXECUTION_RECONCILIATION_ATTEMPTS. "
            "Requires RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET; needs no webhook endpoint."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--iterations",
        type=int,
        default=50,
        help="Create-then-poll cycles. design.md prescribes ~50.",
    )
    parser.add_argument(
        "--poll-interval-ms",
        type=int,
        default=250,
        help="Wait between listing polls. Below ~200ms you are mostly measuring rate limits.",
    )
    parser.add_argument(
        "--poll-budget-ms",
        type=int,
        default=15_000,
        help=(
            "How long to keep polling for one link before giving up. Default matches the "
            "PROVIDER_CALL_TIMEOUT default of 15s, which is the threshold the design cares "
            "about: a first-visible latency above it means reconciliation timing must change."
        ),
    )
    parser.add_argument(
        "--amount",
        type=int,
        default=100,
        help=(
            "Link amount in integer minor units (paise). Integer only — there is no float "
            "anywhere near a currency figure in this project."
        ),
    )
    parser.add_argument(
        "--currency",
        default="INR",
        help="ISO currency code sent with each link.",
    )
    parser.add_argument(
        "--expire-after-seconds",
        type=int,
        default=0,
        help=(
            "If positive, send expire_by = now + this. Left at 0 (omitted) by default: "
            "design.md verifies only the six-month ceiling on expire_by, not any minimum, "
            "so a small value could be rejected for a reason unrelated to what is being "
            "measured. Set it if you would rather the spike's links self-expire."
        ),
    )
    parser.add_argument(
        "--settle-ms",
        type=int,
        default=0,
        help="Pause between iterations. Raise it if the account starts rate-limiting.",
    )
    add_common_arguments(parser)
    return parser


def fresh_reference_id() -> str:
    """A unique, production-shaped ``reference_id``.

    ``rv_`` prefix and 16 hex characters, mirroring the design's derivation, with an
    ``s`` marking it as spike-generated so these rows are identifiable in the test
    account later.
    """
    reference_id = f"rvs_{uuid.uuid4().hex[:16]}"
    if len(reference_id) > REFERENCE_ID_MAX_LENGTH:
        raise SpikeSetupError(
            f"generated reference_id {len(reference_id)} chars exceeds the documented "
            f"{REFERENCE_ID_MAX_LENGTH}-character limit"
        )
    return reference_id


def create_payload(
    *, reference_id: str, amount: int, currency: str, expire_after_seconds: int
) -> dict[str, JsonValue]:
    """The create body, using only fields design.md verified.

    ``notify`` is false on both channels and ``customer`` is absent — see the module
    docstring. ``accept_partial`` is false to match production, where a partial payment
    must never be mistakable for recovery.
    """
    payload: dict[str, JsonValue] = {
        "amount": amount,
        "currency": currency,
        "description": "Revora provider spike: listing freshness measurement",
        "reference_id": reference_id,
        "notify": {"sms": False, "email": False},
        "reminder_enable": False,
        "accept_partial": False,
        "notes": {"revora_spike": SPIKE_NAME, "revora_reference_id": reference_id},
    }
    if expire_after_seconds > 0:
        payload["expire_by"] = int(time.time()) + expire_after_seconds
    return payload


def poll_until_visible(
    client: httpx.Client,
    *,
    reference_id: str,
    created_at_ns: int,
    poll_interval_ms: int,
    poll_budget_ms: int,
) -> dict[str, JsonValue]:
    """Poll the listing endpoint until the link appears or the budget runs out.

    Latency is measured from the moment the create response was *received*, because
    that is the earliest instant at which the effect is known to exist. Measuring from
    the request start would fold the create round-trip into the answer and overstate
    the invisibility window.
    """
    budget_ns = poll_budget_ms * 1_000_000
    polls = 0
    last_error: str | None = None

    while True:
        elapsed_ns = time.monotonic_ns() - created_at_ns
        if elapsed_ns > budget_ns:
            return {
                "visible": False,
                "polls": polls,
                "gave_up_after_ms": ns_to_ms(elapsed_ns),
                "last_error": last_error,
            }
        polls += 1
        try:
            response = client.get(LIST_PATH, params={"reference_id": reference_id})
        except httpx.HTTPError as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        else:
            observed_ns = time.monotonic_ns() - created_at_ns
            if response.status_code == 200:
                items = collection_items(json_body(response))
                if items:
                    first = items[0]
                    link_id = first.get("id") if isinstance(first, dict) else None
                    return {
                        "visible": True,
                        "polls": polls,
                        "first_visible_after_ms": ns_to_ms(observed_ns),
                        "matched_count": len(items),
                        "matched_link_id": link_id if isinstance(link_id, str) else None,
                    }
            else:
                last_error = f"HTTP {response.status_code}: {response.text[:200]}"
        time.sleep(poll_interval_ms / 1_000)


def run_iteration(
    client: httpx.Client,
    recorder: ResultRecorder,
    *,
    index: int,
    args: argparse.Namespace,
) -> dict[str, JsonValue]:
    reference_id = fresh_reference_id()
    payload = create_payload(
        reference_id=reference_id,
        amount=args.amount,
        currency=args.currency,
        expire_after_seconds=args.expire_after_seconds,
    )

    started_ns = time.monotonic_ns()
    try:
        response = client.post(CREATE_PATH, json=payload)
    except httpx.HTTPError as exc:
        return {
            "iteration": index,
            "reference_id": reference_id,
            "create_ok": False,
            "create_error": f"{type(exc).__name__}: {exc}",
        }
    created_at_ns = time.monotonic_ns()

    body = json_body(response)
    if response.status_code not in (200, 201):
        return {
            "iteration": index,
            "reference_id": reference_id,
            "create_ok": False,
            "create_status": response.status_code,
            "create_body": body,
        }

    link_id = body.get("id") if isinstance(body, dict) else None
    link_status = body.get("status") if isinstance(body, dict) else None
    if isinstance(link_id, str):
        recorder.note_created(link_id)

    visibility = poll_until_visible(
        client,
        reference_id=reference_id,
        created_at_ns=created_at_ns,
        poll_interval_ms=args.poll_interval_ms,
        poll_budget_ms=args.poll_budget_ms,
    )
    return {
        "iteration": index,
        "reference_id": reference_id,
        "create_ok": True,
        "create_status": response.status_code,
        "create_latency_ms": ns_to_ms(created_at_ns - started_ns),
        "link_id": link_id if isinstance(link_id, str) else None,
        "link_status": link_status if isinstance(link_status, str) else None,
        **visibility,
    }


def interpret(
    recorder: ResultRecorder,
    *,
    visible_latencies: list[int],
    never_visible: int,
    create_failures: int,
    poll_budget_ms: int,
    provider_call_timeout_ms: int,
) -> None:
    """Turn the numbers into the design statement they justify.

    Kept separate from measurement so the threshold logic is readable in one place —
    it is the part a reviewer will want to argue with.
    """
    if not visible_latencies and never_visible == 0:
        recorder.conclude("No successful create; nothing was measured. Fix setup and re-run.")
        return

    if create_failures:
        recorder.conclude(
            f"{create_failures} create call(s) failed and were excluded from the latency "
            "sample. If this is most of the run, the measurement is not trustworthy."
        )

    if never_visible:
        recorder.conclude(
            f"{never_visible} link(s) never appeared in the listing within the "
            f"{poll_budget_ms}ms poll budget. This is the result that matters most: an "
            "empty listing does not mean the link does not exist."
        )
        recorder.add_consequence(
            "First-visible latency exceeds the poll budget at least sometimes, therefore "
            "EXECUTION_RECONCILIATION_INTERVAL and MAX_EXECUTION_RECONCILIATION_ATTEMPTS "
            "must both increase so that total reconciliation time comfortably exceeds the "
            f"worst observed invisibility window (> {poll_budget_ms}ms)."
        )
        recorder.add_consequence(
            "The rule 'an empty result means FAILED only on the final attempt' is "
            "load-bearing, not merely careful: treating an earlier empty result as FAILED "
            "would let a retry create a second payment demand for money already requested."
        )
        return

    worst = max(visible_latencies)
    recorder.conclude(
        f"Every link became listable within the poll budget; worst observed "
        f"first-visible latency was {worst}ms."
    )
    if worst > provider_call_timeout_ms:
        recorder.add_consequence(
            f"Worst first-visible latency ({worst}ms) exceeds PROVIDER_CALL_TIMEOUT "
            f"({provider_call_timeout_ms}ms), therefore EXECUTION_RECONCILIATION_INTERVAL "
            "and MAX_EXECUTION_RECONCILIATION_ATTEMPTS must both increase, and the "
            "'empty result means FAILED only on the final attempt' rule becomes "
            "load-bearing rather than merely careful."
        )
    else:
        recorder.add_consequence(
            f"Worst first-visible latency ({worst}ms) is inside PROVIDER_CALL_TIMEOUT "
            f"({provider_call_timeout_ms}ms), so the design defaults of "
            "EXECUTION_RECONCILIATION_INTERVAL = 5 minutes and "
            "MAX_EXECUTION_RECONCILIATION_ATTEMPTS = 6 survive measurement and need no "
            "change. Keep the final-attempt-only rule anyway: 50 iterations in test mode "
            "bound the common case, not the tail under production load."
        )


def main(argv: list[str] | None = None) -> int:
    configure_console()
    args = build_parser().parse_args(argv)
    if args.iterations <= 0:
        print("--iterations must be positive", file=sys.stderr)
        return 2

    try:
        credentials = load_credentials(allow_live=args.allow_live_credentials)
        client = build_client(
            credentials,
            base_url=args.base_url,
            connect_timeout_ms=args.connect_timeout_ms,
            read_timeout_ms=args.read_timeout_ms,
        )
    except SpikeSetupError as exc:
        print(f"\nSETUP FAILURE\n{exc}\n", file=sys.stderr)
        return 2

    recorder = ResultRecorder(
        spike=SPIKE_NAME,
        design_item=DESIGN_ITEM,
        requirements=REQUIREMENTS,
        output_dir=Path(args.output_dir),
        creates_objects=True,
        secrets=credentials.secret_strings(),
    )
    recorder.note_unverified(
        "The collection envelope of GET /v1/payment_links (the `items` array and `count` "
        "field) is not part of design.md's verified surface."
    )
    recorder.note_unverified(
        "The API host (https://api.razorpay.com) and the `rzp_test_` key-id prefix are not "
        "part of design.md's verified surface."
    )
    if args.expire_after_seconds > 0:
        recorder.note_unverified(
            "Any minimum permitted `expire_by` offset. design.md verifies only the "
            "six-month ceiling."
        )

    print(f"\n{WRITES_WARNING}")
    print(f"Key mode: {credentials.key_id_hint}. Iterations: {args.iterations}.\n")

    observations: list[JsonValue] = []
    visible_latencies: list[int] = []
    never_visible = 0
    create_failures = 0

    with client:
        for index in range(1, args.iterations + 1):
            observation = run_iteration(client, recorder, index=index, args=args)
            observations.append(observation)

            if not observation.get("create_ok"):
                create_failures += 1
                print(f"[{index}/{args.iterations}] create FAILED")
            elif observation.get("visible"):
                latency = observation["first_visible_after_ms"]
                if isinstance(latency, int):
                    visible_latencies.append(latency)
                print(
                    f"[{index}/{args.iterations}] visible after {latency}ms "
                    f"({observation.get('polls')} poll(s))"
                )
            else:
                never_visible += 1
                print(f"[{index}/{args.iterations}] NEVER VISIBLE within poll budget")

            if args.settle_ms > 0 and index < args.iterations:
                time.sleep(args.settle_ms / 1_000)

    recorder.record("iterations_requested", args.iterations)
    recorder.record("poll_interval_ms", args.poll_interval_ms)
    recorder.record("poll_budget_ms", args.poll_budget_ms)
    recorder.record("amount_minor_units", args.amount)
    recorder.record("currency", args.currency)
    recorder.record("create_failures", create_failures)
    recorder.record("never_visible_within_budget", never_visible)
    recorder.record("first_visible_latency", latency_summary(visible_latencies))
    recorder.record("observations", observations)

    interpret(
        recorder,
        visible_latencies=visible_latencies,
        never_visible=never_visible,
        create_failures=create_failures,
        poll_budget_ms=args.poll_budget_ms,
        provider_call_timeout_ms=15_000,
    )
    recorder.finish()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
