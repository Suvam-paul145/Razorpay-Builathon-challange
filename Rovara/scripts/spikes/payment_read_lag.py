"""Spike 2.2 — how far behind the capture webhook is the authoritative payment read?

Creates no provider objects itself. The manual step it asks you to perform does: you
complete a test payment in a browser, which creates a real payment in the test-mode
account. Test mode only.

What is being measured and why it matters
-----------------------------------------
Revora never declares recovery on a webhook. design.md is explicit that a webhook is a
claim, and that ``RECOVERED`` requires an authoritative ``GET /v1/payments/{id}``
saying the money was captured. The read-after-write consistency of that endpoint
relative to webhook emission is tagged [EVIDENCE INSUFFICIENT], and the prescribed
test is exactly this: on receipt of ``payment.captured``, immediately call
fetch-payment and record whether the read agrees.

The stake is which code path is the normal one. If the read routinely lags the
webhook, then ``PAYMENT_STATE_CONFLICT`` and the conflict-hold loop stop being the
exceptional branch and become the path every recovered case takes — at which point a
60-second ``OUTCOME_READ_LATENCY_BOUND`` and a 15-minute
``PAYMENT_STATE_RECONCILIATION_INTERVAL`` are the wrong numbers, and every recovery
declaration is delayed by a quarter of an hour for no reason.

Honest limitation, stated up front
----------------------------------
**This script cannot complete a payment.** Razorpay's test-mode payment flow is a
browser flow with a test instrument; there is no API call that makes a payment
succeed. So the spike cannot loop 50 times unattended. It offers two modes, both of
which require you to do the paying:

``--mode webhook`` (preferred)
    Runs a small local HTTP receiver. You point a test-mode webhook at it through a
    tunnel, then complete test payments in the browser. Each ``payment.captured``
    arrival is timestamped on a monotonic clock, acknowledged immediately, and
    followed by an immediate fetch-payment. This is the only mode that measures lag
    *relative to webhook arrival*, which is what the design question is about.

``--mode payment-id``
    You supply a payment id you have just paid. The read happens immediately and
    agreement is recorded. Lag is only measurable if you also pass ``--signal-at``,
    and even then it is a wall-clock difference against a timestamp you typed, which
    is weaker evidence than the webhook mode's monotonic measurement. Use this mode
    when you cannot get a tunnel up.

Reaching the local receiver
---------------------------
design.md records verified transport constraints: the webhook URL must be public
HTTPS on port 80 or 443, TLS 1.2+, and several tunnelling and request-bin domains are
blacklisted by Razorpay — including ``ngrok.io``, ``webhook.site``, ``requestbin.com``
and ``beeceptor.com``. The documentation points to ``zrok``, so that is what this
spike assumes. Configure the tunnel's public URL plus ``--path`` as the webhook
endpoint in the test-mode dashboard, subscribe to ``payment.captured``, and copy the
webhook secret into ``RAZORPAY_WEBHOOK_SECRET`` — it is a different secret from the
API key secret.

Verified surface this spike relies on (design.md, Provider Verification Findings)
--------------------------------------------------------------------------------
* ``GET /v1/payments/{id}`` returns the payment entity with ``status`` in
  {``created``, ``authorized``, ``captured``, ``refunded``, ``failed``}, a boolean
  ``captured``, and integer ``amount`` and ``amount_refunded``.
* Recovery means ``status == "captured"``, or ``authorized`` with ``captured``
  true. ``authorized`` alone is not recovery — the money is not captured.
* ``payment.captured`` is a subscribed webhook event.
* Signature: HMAC-SHA256, key = webhook secret, message = raw request body, carried
  in the ``X-Razorpay-Signature`` header. The raw bytes must be used.
* ``x-razorpay-event-id`` is a request header, unique per event.
* Acknowledge within 5 seconds or the delivery is retried — hence ack-then-read
  rather than read-then-ack, which also mirrors what production does.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import sys
import threading
import time
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
from _common import (
    ENV_WEBHOOK_SECRET,
    JsonValue,
    ResultRecorder,
    SpikeSetupError,
    add_common_arguments,
    build_client,
    configure_console,
    json_body,
    latency_summary,
    load_credentials,
    ns_to_ms,
)

SPIKE_NAME = "payment_read_lag"
DESIGN_ITEM = (
    "Authoritative Payment State Read — read-after-write consistency relative to the "
    "capture webhook [EVIDENCE INSUFFICIENT]"
)
REQUIREMENTS = ("R10.C2", "R10.C6", "R10.C13")

PAYMENT_PATH_TEMPLATE = "/v1/payments/{payment_id}"
CAPTURE_EVENT = "payment.captured"
SIGNATURE_HEADER = "X-Razorpay-Signature"
EVENT_ID_HEADER = "x-razorpay-event-id"

MAX_INBOUND_BODY_BYTES = 1_048_576  # MAX_INBOUND_PAYLOAD_SIZE default: 1 MB

# TODO: unverified — design.md verifies the event *names* and that the payment entity in
# webhook payloads carries the error_* fields, but does not state the envelope shape.
# The top-level `event` string field and the `payload.payment.entity` path below are
# taken from the Razorpay webhook payload convention and need checking against official
# documentation. Both extractions below fail soft: a shape mismatch records the raw keys
# it saw instead of guessing, so a wrong assumption is visible in the artifact.
EVENT_NAME_FIELD = "event"
ENTITY_PATH = ("payload", "payment", "entity")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="payment_read_lag.py",
        description=(
            "Measure the lag between a payment.captured webhook and the authoritative "
            "GET /v1/payments/{id} read agreeing with it. Creates no provider objects "
            "itself; the manual browser payment it asks for does."
        ),
        epilog=(
            "Sets or confirms: OUTCOME_READ_LATENCY_BOUND, "
            "PAYMENT_STATE_RECONCILIATION_INTERVAL. Requires RAZORPAY_KEY_ID and "
            "RAZORPAY_KEY_SECRET. Webhook mode also requires RAZORPAY_WEBHOOK_SECRET "
            "(distinct from the API key secret) and a tunnel Razorpay will accept — "
            "ngrok.io, webhook.site, requestbin.com and beeceptor.com are blacklisted; "
            "the documentation points to zrok. THIS SPIKE NEEDS A MANUAL BROWSER STEP: "
            "you must complete a test payment yourself."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--mode",
        choices=("webhook", "payment-id"),
        default="webhook",
        help="webhook: time the read against real webhook arrival. payment-id: read only.",
    )
    parser.add_argument(
        "--iterations",
        type=int,
        default=50,
        help=(
            "Capture events to collect before stopping (webhook mode). design.md "
            "prescribes ~50; you will be completing that many payments by hand, so a "
            "smaller run with an honest sample count recorded is better than a fabricated one."
        ),
    )
    parser.add_argument(
        "--wait-seconds",
        type=int,
        default=900,
        help="How long webhook mode waits for events before giving up and reporting.",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Receiver bind address.")
    parser.add_argument("--port", type=int, default=8787, help="Receiver bind port.")
    parser.add_argument(
        "--path",
        default="/webhooks/spike",
        help="Receiver path. Append this to your tunnel URL when configuring the webhook.",
    )
    parser.add_argument(
        "--skip-signature-verification",
        action="store_true",
        help=(
            "Accept unsigned or unverified deliveries. Only for a receiver that is not "
            "publicly reachable. Without this, a missing RAZORPAY_WEBHOOK_SECRET is a "
            "setup failure, because a spike that trusts unverified input is measuring "
            "something other than the provider."
        ),
    )
    parser.add_argument(
        "--payment-id",
        action="append",
        default=[],
        help="Payment id to read (payment-id mode). Repeatable.",
    )
    parser.add_argument(
        "--signal-at",
        default=None,
        help=(
            "ISO-8601 UTC timestamp of when you saw the capture signal (payment-id mode). "
            "Enables a wall-clock lag figure; weaker evidence than webhook mode."
        ),
    )
    parser.add_argument(
        "--converge-budget-ms",
        type=int,
        default=60_000,
        help=(
            "On a disagreeing read, keep re-reading for this long to measure how long the "
            "read takes to catch up. Default matches the OUTCOME_READ_LATENCY_BOUND "
            "default of 60 seconds."
        ),
    )
    parser.add_argument(
        "--converge-interval-ms",
        type=int,
        default=1_000,
        help="Wait between convergence re-reads.",
    )
    add_common_arguments(parser)
    return parser


def verify_signature(*, body: bytes, header_value: str, secret: str) -> bool:
    """HMAC-SHA256 over the raw bytes, constant-time compared.

    The raw body is used, never a re-serialized parse — design.md records that a
    re-encoded JSON string will not match, and the receiver below is built around that
    constraint.
    """
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(digest, header_value)


def extract_event_name(payload: JsonValue) -> str | None:
    if isinstance(payload, dict):
        name = payload.get(EVENT_NAME_FIELD)
        if isinstance(name, str):
            return name
    return None


def extract_payment_entity(payload: JsonValue) -> dict[str, JsonValue] | None:
    """Walk ``payload.payment.entity``, returning ``None`` rather than guessing."""
    current: JsonValue = payload
    for key in ENTITY_PATH:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current if isinstance(current, dict) else None


def read_agrees_with_capture(entity: dict[str, JsonValue]) -> bool:
    """Does an authoritative read confirm captured money?

    Mirrors the design's rule rather than inventing a looser one: ``captured``, or
    ``authorized`` with the boolean ``captured`` true. ``authorized`` on its own is not
    recovery, and a spike that counted it as agreement would report a comfortable
    result for the wrong reason.
    """
    status = entity.get("status")
    captured_flag = entity.get("captured")
    if status == "captured":
        return True
    return status == "authorized" and captured_flag is True


def fetch_payment(client: httpx.Client, payment_id: str) -> tuple[int, JsonValue, int]:
    """Read a payment. Returns (status code, body, round-trip in whole ms).

    Transport failures are returned as status ``0`` with the exception recorded: a read
    that could not be performed is a distinct observation from a read that disagreed,
    and collapsing them would inflate the disagreement fraction.
    """
    started_ns = time.monotonic_ns()
    try:
        response = client.get(PAYMENT_PATH_TEMPLATE.format(payment_id=payment_id))
    except httpx.HTTPError as exc:
        return 0, {"transport_error": f"{type(exc).__name__}: {exc}"}, ns_to_ms(
            time.monotonic_ns() - started_ns
        )
    return response.status_code, json_body(response), ns_to_ms(time.monotonic_ns() - started_ns)


def measure_convergence(
    client: httpx.Client,
    *,
    payment_id: str,
    from_ns: int,
    budget_ms: int,
    interval_ms: int,
) -> dict[str, JsonValue]:
    """Re-read until the read agrees, so a disagreement gets a duration attached.

    "The read disagreed" is not actionable on its own. "The read disagreed for 4
    seconds" is what decides whether PAYMENT_STATE_RECONCILIATION_INTERVAL of 15
    minutes is absurdly long.
    """
    budget_ns = budget_ms * 1_000_000
    attempts = 0
    while time.monotonic_ns() - from_ns < budget_ns:
        time.sleep(interval_ms / 1_000)
        attempts += 1
        status_code, body, _ = fetch_payment(client, payment_id)
        if status_code == 200 and isinstance(body, dict) and read_agrees_with_capture(body):
            return {
                "converged": True,
                "converged_after_ms": ns_to_ms(time.monotonic_ns() - from_ns),
                "extra_reads": attempts,
            }
    return {
        "converged": False,
        "gave_up_after_ms": ns_to_ms(time.monotonic_ns() - from_ns),
        "extra_reads": attempts,
    }


class SpikeState:
    """Shared, lock-guarded state between the receiver threads and the main thread."""

    def __init__(self, *, target_iterations: int) -> None:
        self._lock = threading.Lock()
        self.target_iterations = target_iterations
        self.observations: list[JsonValue] = []
        self.ignored_events: dict[str, int] = {}
        self.rejected_signatures = 0
        self.duplicate_event_ids = 0
        self._seen_event_ids: set[str] = set()
        self.complete = threading.Event()

    def add_observation(self, observation: JsonValue) -> int:
        with self._lock:
            self.observations.append(observation)
            count = len(self.observations)
        if count >= self.target_iterations:
            self.complete.set()
        return count

    def note_ignored(self, event_name: str) -> None:
        with self._lock:
            self.ignored_events[event_name] = self.ignored_events.get(event_name, 0) + 1

    def note_rejected_signature(self) -> None:
        with self._lock:
            self.rejected_signatures += 1

    def is_duplicate(self, event_id: str) -> bool:
        """At-least-once delivery is verified provider behaviour, so dedupe here too."""
        with self._lock:
            if event_id in self._seen_event_ids:
                self.duplicate_event_ids += 1
                return True
            self._seen_event_ids.add(event_id)
            return False


def make_handler(
    *,
    client: httpx.Client,
    state: SpikeState,
    args: argparse.Namespace,
    webhook_secret: str | None,
) -> type[BaseHTTPRequestHandler]:
    """Build the receiver class over the dependencies it needs.

    A closure rather than globals, so two receivers in one process could not interfere
    and so the dependencies are visible at the construction site.
    """

    class CaptureReceiver(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "revora-spike/0.1"

        def log_message(self, log_format: str, *log_args: object) -> None:
            """Silence the default stderr access log; this script prints its own lines."""

        def _respond(self, code: int, message: str) -> None:
            body = message.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            self.wfile.flush()

        def do_POST(self) -> None:
            if self.path.split("?")[0] != args.path:
                self._respond(404, "not the spike path")
                return

            declared_length = int(self.headers.get("Content-Length") or 0)
            if declared_length > MAX_INBOUND_BODY_BYTES:
                self._respond(413, "payload too large")
                return
            raw_body = self.rfile.read(declared_length)
            arrival_ns = time.monotonic_ns()
            arrival_wall = datetime.now(UTC).isoformat(timespec="milliseconds")

            signature = self.headers.get(SIGNATURE_HEADER, "")
            event_id = self.headers.get(EVENT_ID_HEADER, "")

            if webhook_secret is not None:
                if not signature or not verify_signature(
                    body=raw_body, header_value=signature, secret=webhook_secret
                ):
                    state.note_rejected_signature()
                    self._respond(401, "signature mismatch")
                    print("  ! delivery rejected: signature mismatch")
                    return
                signature_state = "verified"
            else:
                signature_state = "not_verified_by_operator_choice"

            # Acknowledge before doing any provider work. The verified deadline is 5
            # seconds and exceeding it costs a duplicate delivery, so the read happens
            # after the response is on the wire — which is also the production order.
            self._respond(200, "ok")

            try:
                payload: JsonValue = json.loads(raw_body)
            except ValueError:
                state.note_ignored("unparseable_body")
                print("  ! delivery had an unparseable body")
                return

            event_name = extract_event_name(payload)
            if event_name != CAPTURE_EVENT:
                observed_keys = sorted(payload) if isinstance(payload, dict) else []
                state.note_ignored(event_name or f"no_event_field(keys={observed_keys})")
                return

            if event_id and state.is_duplicate(event_id):
                print("  · duplicate delivery of an event already measured; ignored")
                return

            entity = extract_payment_entity(payload)
            if entity is None:
                state.note_ignored("capture_without_extractable_entity")
                print(
                    "  ! payment.captured arrived but payload.payment.entity did not "
                    "resolve — see the unverified-assumptions note"
                )
                return

            payment_id = entity.get("id")
            if not isinstance(payment_id, str):
                state.note_ignored("capture_entity_without_id")
                return

            status_code, body, read_ms = fetch_payment(client, payment_id)
            agrees = (
                status_code == 200 and isinstance(body, dict) and read_agrees_with_capture(body)
            )
            observation: dict[str, JsonValue] = {
                "source": "webhook",
                "signature_state": signature_state,
                "webhook_arrived_at": arrival_wall,
                "payment_id": payment_id,
                "webhook_entity_status": entity.get("status"),
                "read_http_status": status_code,
                "read_round_trip_ms": read_ms,
                "lag_from_webhook_ms": ns_to_ms(time.monotonic_ns() - arrival_ns),
                "read_status": body.get("status") if isinstance(body, dict) else None,
                "read_captured_flag": body.get("captured") if isinstance(body, dict) else None,
                "read_amount_minor_units": body.get("amount") if isinstance(body, dict) else None,
                "read_amount_refunded_minor_units": (
                    body.get("amount_refunded") if isinstance(body, dict) else None
                ),
                "read_agrees_with_webhook": agrees,
            }
            if not agrees:
                observation["convergence"] = measure_convergence(
                    client,
                    payment_id=payment_id,
                    from_ns=arrival_ns,
                    budget_ms=args.converge_budget_ms,
                    interval_ms=args.converge_interval_ms,
                )
            if status_code != 200:
                observation["read_body"] = body

            count = state.add_observation(observation)
            verdict = "agrees" if agrees else "DISAGREES"
            print(
                f"  [{count}/{state.target_iterations}] {payment_id}: read {verdict} "
                f"({observation['lag_from_webhook_ms']}ms after webhook arrival)"
            )

        def do_GET(self) -> None:
            """A liveness probe, so you can confirm the tunnel reaches the receiver."""
            self._respond(200, "revora payment_read_lag spike receiver is listening")

    return CaptureReceiver


def run_webhook_mode(
    client: httpx.Client,
    recorder: ResultRecorder,
    args: argparse.Namespace,
) -> list[JsonValue]:
    webhook_secret = os.environ.get(ENV_WEBHOOK_SECRET, "").strip() or None
    if webhook_secret is None and not args.skip_signature_verification:
        raise SpikeSetupError(
            f"{ENV_WEBHOOK_SECRET} is not set.\n\n"
            "It is the webhook secret from the test-mode dashboard webhook you configured, "
            "not the API key secret — design.md records that they are distinct.\n"
            "Set it, or pass --skip-signature-verification if this receiver is not "
            "publicly reachable and you accept unverified input."
        )
    if webhook_secret is not None:
        recorder.secrets = (*recorder.secrets, webhook_secret)

    state = SpikeState(target_iterations=args.iterations)
    handler = make_handler(client=client, state=state, args=args, webhook_secret=webhook_secret)
    server = ThreadingHTTPServer((args.host, args.port), handler)
    server.daemon_threads = True

    thread = threading.Thread(target=server.serve_forever, name="spike-receiver", daemon=True)
    thread.start()

    print(f"Receiver listening on http://{args.host}:{args.port}{args.path}")
    print(f"Signature verification: {'on' if webhook_secret else 'OFF (operator choice)'}")
    print("\nMANUAL STEP — this spike cannot pay for you:")
    print("  1. Expose this port with a tunnel Razorpay accepts (zrok; not ngrok.io,")
    print("     webhook.site, requestbin.com or beeceptor.com — those are blacklisted).")
    print(f"  2. In the test-mode dashboard, set the webhook URL to <tunnel>{args.path}")
    print(f"     and subscribe to {CAPTURE_EVENT}.")
    print("  3. Create and pay a test-mode payment in the browser, using a Razorpay test")
    print("     instrument. Repeat until the sample below is large enough to mean something.")
    print(f"\nWaiting up to {args.wait_seconds}s for {args.iterations} capture event(s).")
    print("Ctrl-C stops early and still reports what was collected.\n")

    try:
        state.complete.wait(timeout=args.wait_seconds)
    except KeyboardInterrupt:
        print("\nInterrupted; reporting what was collected.")
    finally:
        server.shutdown()
        server.server_close()

    recorder.record("receiver_path", args.path)
    recorder.record("signature_verification", bool(webhook_secret))
    recorder.record("rejected_signatures", state.rejected_signatures)
    recorder.record("duplicate_event_ids_ignored", state.duplicate_event_ids)
    recorder.record("ignored_events", dict(state.ignored_events))
    return list(state.observations)


def run_payment_id_mode(
    client: httpx.Client,
    recorder: ResultRecorder,
    args: argparse.Namespace,
) -> list[JsonValue]:
    if not args.payment_id:
        raise SpikeSetupError(
            "payment-id mode needs at least one --payment-id.\n\n"
            "Complete a payment in the test-mode browser flow, copy its pay_… id, and pass "
            "it here. Add --signal-at with the UTC time you saw the capture signal if you "
            "want a lag figure as well as an agreement verdict."
        )

    signal_at: datetime | None = None
    if args.signal_at:
        try:
            signal_at = datetime.fromisoformat(args.signal_at)
        except ValueError as exc:
            raise SpikeSetupError(f"--signal-at is not an ISO-8601 timestamp: {exc}") from exc
        if signal_at.tzinfo is None:
            signal_at = signal_at.replace(tzinfo=UTC)
        recorder.note_unverified(
            "In payment-id mode the lag figure is a wall-clock difference against an "
            "operator-supplied timestamp, not a monotonic measurement against real webhook "
            "arrival. Weaker evidence than --mode webhook."
        )

    observations: list[JsonValue] = []
    for payment_id in args.payment_id:
        from_ns = time.monotonic_ns()
        status_code, body, read_ms = fetch_payment(client, payment_id)
        agrees = status_code == 200 and isinstance(body, dict) and read_agrees_with_capture(body)

        lag_ms: int | None = None
        if signal_at is not None:
            elapsed = datetime.now(UTC) - signal_at
            lag_ms = int(elapsed.total_seconds() * 1_000)

        observation: dict[str, JsonValue] = {
            "source": "payment-id",
            "payment_id": payment_id,
            "read_http_status": status_code,
            "read_round_trip_ms": read_ms,
            "lag_from_webhook_ms": lag_ms,
            "read_status": body.get("status") if isinstance(body, dict) else None,
            "read_captured_flag": body.get("captured") if isinstance(body, dict) else None,
            "read_amount_minor_units": body.get("amount") if isinstance(body, dict) else None,
            "read_amount_refunded_minor_units": (
                body.get("amount_refunded") if isinstance(body, dict) else None
            ),
            "read_agrees_with_webhook": agrees,
        }
        if not agrees:
            observation["convergence"] = measure_convergence(
                client,
                payment_id=payment_id,
                from_ns=from_ns,
                budget_ms=args.converge_budget_ms,
                interval_ms=args.converge_interval_ms,
            )
            observation["read_body"] = body
        observations.append(observation)
        print(f"  {payment_id}: read {'agrees' if agrees else 'DISAGREES'} ({read_ms}ms)")
    return observations


def exact_fraction(numerator: int, denominator: int) -> str:
    """A fraction as a 4-place decimal string. Decimal, not float — see domain/money.py."""
    if denominator == 0:
        return "0.0000"
    value = (Decimal(numerator) / Decimal(denominator)).quantize(
        Decimal("0.0001"), rounding=ROUND_HALF_UP
    )
    return str(value)


def interpret(recorder: ResultRecorder, observations: list[JsonValue]) -> None:
    total = len(observations)
    if total == 0:
        recorder.conclude(
            "No capture signal was observed, so nothing was measured. "
            "OUTCOME_READ_LATENCY_BOUND and PAYMENT_STATE_RECONCILIATION_INTERVAL remain "
            "[EVIDENCE INSUFFICIENT] — do not record this run as confirmation."
        )
        return

    disagreeing = [
        item
        for item in observations
        if isinstance(item, dict) and item.get("read_agrees_with_webhook") is not True
    ]
    convergence_lags = [
        item["convergence"]["converged_after_ms"]
        for item in disagreeing
        if isinstance(item, dict)
        and isinstance(item.get("convergence"), dict)
        and isinstance(item["convergence"].get("converged_after_ms"), int)
    ]
    never_converged = sum(
        1
        for item in disagreeing
        if isinstance(item, dict)
        and isinstance(item.get("convergence"), dict)
        and item["convergence"].get("converged") is False
    )

    recorder.record("total_reads", total)
    recorder.record("disagreeing_reads", len(disagreeing))
    recorder.record("disagreeing_fraction", exact_fraction(len(disagreeing), total))
    recorder.record("convergence_lag", latency_summary([int(v) for v in convergence_lags]))
    recorder.record("never_converged_within_budget", never_converged)

    if not disagreeing:
        recorder.conclude(
            f"All {total} authoritative read(s) agreed with the capture webhook on the first "
            "attempt. No lag observed at this sample size."
        )
        recorder.add_consequence(
            "The design defaults survive measurement: OUTCOME_READ_LATENCY_BOUND = 60 "
            "seconds and PAYMENT_STATE_RECONCILIATION_INTERVAL = 15 minutes need no change, "
            "and the conflict-hold path stays the exception it was designed to be. Keep the "
            "conflict-hold path regardless — a small manual sample bounds the common case, "
            "not the tail."
        )
        return

    recorder.conclude(
        f"{len(disagreeing)} of {total} read(s) disagreed with the capture webhook "
        f"(fraction {exact_fraction(len(disagreeing), total)})."
    )
    if never_converged:
        recorder.conclude(
            f"{never_converged} read(s) never caught up within the convergence budget. That "
            "is a stronger finding than lag: it means a read can stay wrong for longer than "
            "OUTCOME_READ_LATENCY_BOUND."
        )
    recorder.add_consequence(
        "Lag is not rare, therefore OUTCOME_READ_LATENCY_BOUND is too tight and "
        "PAYMENT_STATE_RECONCILIATION_INTERVAL must shorten, because the conflict-hold path "
        "is then the normal path rather than the exception — every recovered case would sit "
        "in WAITING_FOR_OUTCOME for a full reconciliation interval before recovery is "
        "declared."
    )
    if convergence_lags:
        recorder.add_consequence(
            f"Observed convergence lag ran from {min(convergence_lags)}ms to "
            f"{max(convergence_lags)}ms; "
            "set PAYMENT_STATE_RECONCILIATION_INTERVAL from the upper end of that range with "
            "margin, not from the 15-minute default."
        )


def main(argv: list[str] | None = None) -> int:
    configure_console()
    args = build_parser().parse_args(argv)

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
        creates_objects=False,
        secrets=credentials.secret_strings(),
    )
    recorder.note_unverified(
        "The webhook payload envelope: the top-level `event` field and the "
        "`payload.payment.entity` path. design.md verifies event names and the entity's "
        "error_* fields, not the envelope."
    )
    recorder.note_unverified(
        "The API host (https://api.razorpay.com) and the `rzp_test_` key-id prefix are not "
        "part of design.md's verified surface."
    )
    recorder.record("mode", args.mode)

    with client:
        try:
            if args.mode == "webhook":
                observations = run_webhook_mode(client, recorder, args)
            else:
                observations = run_payment_id_mode(client, recorder, args)
        except SpikeSetupError as exc:
            print(f"\nSETUP FAILURE\n{exc}\n", file=sys.stderr)
            return 2

    recorder.record("read_round_trip", latency_summary(
        [
            int(item["read_round_trip_ms"])
            for item in observations
            if isinstance(item, dict) and isinstance(item.get("read_round_trip_ms"), int)
        ]
    ))
    recorder.record("lag_from_webhook", latency_summary(
        [
            int(item["lag_from_webhook_ms"])
            for item in observations
            if isinstance(item, dict) and isinstance(item.get("lag_from_webhook_ms"), int)
        ]
    ))
    interpret(recorder, observations)
    recorder.record("observations", observations)
    recorder.finish()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
