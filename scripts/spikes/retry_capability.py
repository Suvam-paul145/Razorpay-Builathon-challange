"""Spike 2.4 — does this merchant account expose any server-side retry or
payment-method-update capability for a one-off payment?

WRITES TO THE PROVIDER, conditionally. The capability probes include non-GET requests
against undocumented paths. They are expected to fail, but a request that succeeds
would by definition have changed something, so this runs against test-mode credentials
only and the write probes can be turned off with ``--no-write-probes``.

What is being measured and why it matters
-----------------------------------------
design.md found no documented API to server-side retry a failed one-off Payment Gateway
payment: a failed payment is terminal at the provider, and a further attempt is a new
payment initiated by the customer. Automatic retry is documented for Subscriptions,
which is a different object and outside MVP scope. ``PAYMENT_METHOD_UPDATE`` has no
one-off analogue either. On that basis the design demotes ``RETRY``, ``DELAYED_RETRY``
and ``PAYMENT_METHOD_UPDATE`` to ``UNAVAILABLE`` — they still appear in a Recommendation
with their estimates, which is why the demotion is honest rather than a quiet dropping
of scope, but they cannot execute.

Whether a *merchant-account-specific* capability exists is tagged [EVIDENCE
INSUFFICIENT]. This spike is how that gets closed. A positive result restores ``RETRY``
and ``PAYMENT_METHOD_UPDATE`` as executable actions, which changes the eligibility
table in ``revora/domain/actions.py`` and adds execution paths to task 20 — a
meaningful amount of work, which is exactly why it is worth ten minutes to find out
before that work is planned around the negative answer.

Why this script cannot simply enumerate account capabilities
------------------------------------------------------------
There is no endpoint in design.md's verified surface that lists the products enabled on
a merchant account, and inventing one would produce a confident-looking negative result
that means nothing. So the enumeration is split honestly in three:

1. **A verified read.** ``GET /v1/payments/{id}`` against the failed payment id you
   supply. This confirms the payment really is ``failed`` and records the error fields
   the provider gives us — which is the machinery Revora actually has for reasoning
   about a failure, retry capability or not.
2. **Declared, explicitly unverified probes.** A small table of candidate operations,
   every one of them marked ``TODO: unverified`` with the exact thing that needs
   checking. Their purpose is to record the provider's verbatim response, not to assert
   that any of these endpoints exist. A 404 from a guessed path is weak evidence and is
   reported as weak evidence.
3. **A manual dashboard checklist**, printed at the end. Whether Optimizer or
   Subscriptions are enabled on the account is visible in the dashboard, and the design
   names that as the resolving action. A human reading a settings page is stronger
   evidence than this script guessing at URLs.

Verified surface this spike relies on (design.md, Provider Verification Findings)
--------------------------------------------------------------------------------
* ``GET /v1/payments/{id}`` returns the payment entity, ``status`` in {``created``,
  ``authorized``, ``captured``, ``refunded``, ``failed``}.
* The payment entity carries ``error_code``, ``error_description``, ``error_reason``,
  ``error_source``, ``error_step``.
* The error object shape ``code`` / ``description`` / ``field`` / ``source`` / ``step``
  / ``reason``, with ``code`` in {``BAD_REQUEST_ERROR``, ``GATEWAY_ERROR``,
  ``SERVER_ERROR``}.
* Basic auth.

Everything else this script touches is marked unverified, in code and in the artifact.
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import httpx
from _common import (
    JsonValue,
    ResultRecorder,
    SpikeSetupError,
    add_common_arguments,
    build_client,
    configure_console,
    json_body,
    load_credentials,
    ns_to_ms,
)

SPIKE_NAME = "retry_capability"
DESIGN_ITEM = (
    "Two Candidate_Actions Have No Verified Provider Capability — whether a "
    "merchant-account-specific retry or payment-method-update exists [EVIDENCE INSUFFICIENT]"
)
REQUIREMENTS = ("R6.C9",)

PAYMENT_PATH_TEMPLATE = "/v1/payments/{payment_id}"
PAYMENT_ENTITY_ERROR_FIELDS = (
    "error_code",
    "error_description",
    "error_reason",
    "error_source",
    "error_step",
)
ERROR_OBJECT_FIELDS = ("code", "description", "field", "source", "step", "reason")

WRITES_WARNING = (
    "THIS SPIKE SENDS NON-GET REQUESTS to undocumented paths in the account the "
    "credentials belong to. They are expected to fail; a success would mean something "
    "changed. Test-mode credentials only, or pass --no-write-probes."
)


@dataclass(frozen=True)
class Probe:
    """One candidate capability request.

    ``unverified`` is not decoration. It is the sentence that goes into the artifact so
    that a reader six weeks from now knows this path was a guess and that a 404 against
    it does not prove the capability is absent — only that this URL is not it.
    """

    name: str
    method: str
    path_template: str
    what_a_success_would_prove: str
    unverified: str
    body: dict[str, JsonValue] | None = None

    @property
    def is_write(self) -> bool:
        return self.method.upper() not in ("GET", "HEAD")


# Every entry below is outside design.md's verified surface. Named individually rather
# than hidden behind a loop so each guess is auditable.
PROBES: tuple[Probe, ...] = (
    Probe(
        name="subscriptions_product_reachable",
        method="GET",
        path_template="/v1/subscriptions",
        what_a_success_would_prove=(
            "The Subscriptions product is reachable on this account. Relevant because "
            "design.md found that automatic retry is documented for Subscriptions and "
            "nowhere else, so a reachable Subscriptions product is the one place a retry "
            "capability could plausibly live."
        ),
        # TODO: unverified — the path `/v1/subscriptions` is not in design.md's verified
        # surface. Needs checking against official documentation. Even a 200 here proves
        # only that the product responds, not that a one-off failed payment can be retried.
        unverified="Path /v1/subscriptions and the meaning of its response for account capability.",
    ),
    Probe(
        name="one_off_payment_retry",
        method="POST",
        path_template="/v1/payments/{payment_id}/retry",
        what_a_success_would_prove=(
            "A failed one-off payment can be retried server-side, which would restore RETRY "
            "and DELAYED_RETRY as executable actions."
        ),
        # TODO: unverified — no retry endpoint for a one-off payment appears anywhere in
        # design.md's verified surface; design.md's conclusion is that none is documented.
        # This path is a guess whose only job is to capture the provider's verbatim error.
        unverified="Path POST /v1/payments/{id}/retry. Not documented; expected to 404 or 400.",
        body={},
    ),
    Probe(
        name="payment_entity_mutable",
        method="PATCH",
        path_template="/v1/payments/{payment_id}",
        what_a_success_would_prove=(
            "The payment entity accepts mutation at all, which is the precondition for any "
            "PAYMENT_METHOD_UPDATE action on a one-off payment."
        ),
        # TODO: unverified — design.md verifies PATCH on the payment entity nowhere. An empty
        # body is sent deliberately so that, in the unlikely event the method is accepted,
        # nothing is actually changed.
        unverified="Method PATCH on /v1/payments/{id}. Not documented; empty body sent.",
        body={},
    ),
)

DASHBOARD_CHECKLIST = (
    "Log into the Razorpay dashboard in Test Mode and record, in "
    "docs/provider-findings.md:",
    "  1. Account & Settings -> is the Subscriptions product enabled on this account?",
    "  2. Account & Settings -> is Optimizer enabled on this account?",
    "  3. If either is enabled, does its documentation describe retrying a FAILED one-off",
    "     Payment Gateway payment — as opposed to retrying a subscription charge?",
    "A settings page read by a human is stronger evidence than the URL guesses above.",
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="retry_capability.py",
        description=(
            "Determine whether any server-side retry or payment-method-update capability "
            "exists on this merchant account for a one-off payment. " + WRITES_WARNING
        ),
        epilog=(
            "A positive result restores RETRY and PAYMENT_METHOD_UPDATE as executable "
            "actions, changing the eligibility table in revora/domain/actions.py and adding "
            "execution paths to task 20. Requires RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET, and "
            "--payment-id of a payment already in the `failed` state. NEEDS A MANUAL STEP: "
            "produce a failed test-mode payment in the browser first, and read the dashboard "
            "checklist this script prints at the end."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--payment-id",
        required=False,
        default=None,
        help=(
            "A pay_… id whose status is `failed`. Produce one in test mode by paying with a "
            "Razorpay test instrument that is documented to fail."
        ),
    )
    parser.add_argument(
        "--no-write-probes",
        action="store_true",
        help=(
            "Run only the verified read and the read-only probes. Use this if you would "
            "rather not send undocumented non-GET requests at all."
        ),
    )
    parser.add_argument(
        "--force-non-failed-payment",
        action="store_true",
        help=(
            "Proceed even if the supplied payment is not in the `failed` state. Off by "
            "default: probing retry against a captured payment would answer a different "
            "question and could look like a negative result for the wrong reason."
        ),
    )
    parser.add_argument(
        "--probe-gap-ms",
        type=int,
        default=250,
        help="Wait between probes, to stay clear of rate limiting.",
    )
    add_common_arguments(parser)
    return parser


def read_payment(client: httpx.Client, payment_id: str) -> dict[str, JsonValue]:
    """The one verified call in this script."""
    started_ns = time.monotonic_ns()
    try:
        response = client.get(PAYMENT_PATH_TEMPLATE.format(payment_id=payment_id))
    except httpx.HTTPError as exc:
        return {
            "http_status": None,
            "transport_error": f"{type(exc).__name__}: {exc}",
            "latency_ms": ns_to_ms(time.monotonic_ns() - started_ns),
        }
    body = json_body(response)
    result: dict[str, JsonValue] = {
        "http_status": response.status_code,
        "latency_ms": ns_to_ms(time.monotonic_ns() - started_ns),
        "status": body.get("status") if isinstance(body, dict) else None,
        "captured": body.get("captured") if isinstance(body, dict) else None,
        "amount_minor_units": body.get("amount") if isinstance(body, dict) else None,
        "method": body.get("method") if isinstance(body, dict) else None,
    }
    if isinstance(body, dict):
        result["error_fields_present"] = {
            name: body.get(name) for name in PAYMENT_ENTITY_ERROR_FIELDS if name in body
        }
        result["entity_keys"] = sorted(body)
    else:
        result["body"] = body
    return result


def run_probe(client: httpx.Client, probe: Probe, *, payment_id: str) -> dict[str, JsonValue]:
    """Issue one candidate request and record the response exactly as it came back.

    The verbatim error is the deliverable. "Retry is not available" is a claim; the
    provider's own words, with its own error code, is evidence — and it is what a future
    reader needs in order to tell "this capability does not exist" apart from "this URL
    was wrong".
    """
    path = probe.path_template.format(payment_id=payment_id)
    started_ns = time.monotonic_ns()
    try:
        response = client.request(
            probe.method, path, json=probe.body if probe.body is not None else None
        )
    except httpx.HTTPError as exc:
        return {
            "probe": probe.name,
            "method": probe.method,
            "path": path,
            "http_status": None,
            "transport_error": f"{type(exc).__name__}: {exc}",
            "latency_ms": ns_to_ms(time.monotonic_ns() - started_ns),
            "unverified": probe.unverified,
        }

    body = json_body(response)
    error_object = body.get("error") if isinstance(body, dict) else None
    outcome: dict[str, JsonValue] = {
        "probe": probe.name,
        "method": probe.method,
        "path": path,
        "http_status": response.status_code,
        "latency_ms": ns_to_ms(time.monotonic_ns() - started_ns),
        "verbatim_body": body,
        "unverified": probe.unverified,
        "what_a_success_would_prove": probe.what_a_success_would_prove,
    }
    if isinstance(error_object, dict):
        outcome["error_fields"] = {
            name: error_object.get(name) for name in ERROR_OBJECT_FIELDS if name in error_object
        }
    return outcome


def interpret(
    recorder: ResultRecorder,
    *,
    payment: dict[str, JsonValue],
    probe_results: list[dict[str, JsonValue]],
    write_probes_run: bool,
) -> None:
    successes = [
        result
        for result in probe_results
        if isinstance(result.get("http_status"), int)
        and 200 <= int(str(result["http_status"])) < 300
        and result.get("probe") != "subscriptions_product_reachable"
    ]
    subscriptions = next(
        (r for r in probe_results if r.get("probe") == "subscriptions_product_reachable"), None
    )

    recorder.conclude(
        f"Supplied payment reads as status={payment.get('status')!r}, "
        f"method={payment.get('method')!r}."
    )
    if subscriptions is not None:
        recorder.conclude(
            "Subscriptions probe returned HTTP "
            f"{subscriptions.get('http_status')} — weak evidence either way; confirm from the "
            "dashboard checklist."
        )

    if successes:
        recorder.conclude(
            "At least one capability probe succeeded: "
            + ", ".join(str(result.get("probe")) for result in successes)
            + ". Verify by hand before acting on it."
        )
        recorder.add_consequence(
            "Positive result. RETRY and PAYMENT_METHOD_UPDATE are restored as executable "
            "actions, which changes the eligibility table in revora/domain/actions.py and "
            "adds execution paths to task 20. Do not make that change on this artifact "
            "alone: confirm the capability is documented and idempotent before an action "
            "that moves money is built on it."
        )
        return

    if not write_probes_run:
        recorder.conclude(
            "Write probes were skipped, so no retry attempt was actually made. This run "
            "cannot close the [EVIDENCE INSUFFICIENT] tag on its own."
        )
        recorder.add_consequence(
            "Inconclusive. Re-run without --no-write-probes, or close the item from the "
            "dashboard checklist. Until then RETRY, DELAYED_RETRY and PAYMENT_METHOD_UPDATE "
            "stay UNAVAILABLE as the design has them."
        )
        return

    recorder.conclude(
        "No probe exposed a retry or payment-method-update capability. Every failure is "
        "recorded verbatim in the artifact, including the guessed paths — a 404 against a "
        "guessed path is weak evidence and is not on its own proof of absence."
    )
    recorder.add_consequence(
        "Negative result, consistent with design.md. RETRY, DELAYED_RETRY and "
        "PAYMENT_METHOD_UPDATE remain UNAVAILABLE at simulation time: they still appear in a "
        "Recommendation with their estimates, and the eligibility table in "
        "revora/domain/actions.py and task 20's execution paths are unchanged. The executable "
        "action set stays DO_NOTHING, WAIT, PAYMENT_LINK, CUSTOMER_MESSAGE, HUMAN_ESCALATION."
    )
    recorder.add_consequence(
        "Finish the item from the dashboard checklist. The probes above cannot distinguish "
        "'this capability does not exist' from 'these URLs were the wrong guesses'."
    )


def main(argv: list[str] | None = None) -> int:
    configure_console()
    args = build_parser().parse_args(argv)

    try:
        if not args.payment_id:
            raise SpikeSetupError(
                "--payment-id is required.\n\n"
                "This spike needs a payment already in the `failed` state, and it cannot "
                "create one: making a payment fail is a browser flow with a Razorpay test "
                "instrument, not an API call.\n"
                "Produce a failed test-mode payment, then pass its pay_… id here."
            )
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
        creates_objects=not args.no_write_probes,
        secrets=credentials.secret_strings(),
    )
    recorder.note_unverified(
        "The API host (https://api.razorpay.com) and the `rzp_test_` key-id prefix are not "
        "part of design.md's verified surface."
    )
    for probe in PROBES:
        recorder.note_unverified(f"{probe.name}: {probe.unverified}")

    print(f"\n{WRITES_WARNING}")
    print(f"Key mode: {credentials.key_id_hint}. Payment: {args.payment_id}\n")

    with client:
        payment = read_payment(client, args.payment_id)
        print(f"Payment reads as status={payment.get('status')!r}")

        if payment.get("status") != "failed" and not args.force_non_failed_payment:
            recorder.record("payment_under_test", payment)
            recorder.conclude(
                "Aborted before probing: the supplied payment is not in the `failed` state, "
                "so a probe result would answer a different question."
            )
            recorder.finish()
            print(
                "\nSupply a payment whose status is `failed`, or pass "
                "--force-non-failed-payment if you know what you are measuring.\n",
                file=sys.stderr,
            )
            return 2

        probe_results: list[dict[str, JsonValue]] = []
        write_probes_run = False
        for index, probe in enumerate(PROBES):
            if probe.is_write and args.no_write_probes:
                probe_results.append(
                    {
                        "probe": probe.name,
                        "method": probe.method,
                        "skipped": "write probes disabled by --no-write-probes",
                        "unverified": probe.unverified,
                    }
                )
                print(f"  · {probe.name}: skipped")
                continue
            result = run_probe(client, probe, payment_id=args.payment_id)
            write_probes_run = write_probes_run or probe.is_write
            probe_results.append(result)
            print(f"  · {probe.name}: HTTP {result.get('http_status')}")
            if args.probe_gap_ms > 0 and index < len(PROBES) - 1:
                time.sleep(args.probe_gap_ms / 1_000)

    recorder.record("payment_under_test", payment)
    recorder.record("write_probes_enabled", not args.no_write_probes)
    recorder.record("probe_results", list(probe_results))
    interpret(
        recorder,
        payment=payment,
        probe_results=probe_results,
        write_probes_run=write_probes_run,
    )
    recorder.finish()

    print("MANUAL STEP THIS SCRIPT CANNOT DO FOR YOU")
    for line in DASHBOARD_CHECKLIST:
        print(line)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
