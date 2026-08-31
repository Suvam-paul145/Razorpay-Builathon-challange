"""Spike 2.3 — is a duplicate ``reference_id`` rejected when creating a Payment Link?

WRITES TO THE PROVIDER. Each trial issues two create calls with the same
``reference_id``. If the provider does not reject the second one, this spike will have
created two real Payment Links for the same reference. Test mode only.

What is being measured and why it matters — and why the answer changes little
----------------------------------------------------------------------------
design.md verifies that ``reference_id`` is documented as required to be unique per
Payment Link, and separately that fetch-all supports querying by ``reference_id``.
Those are two different guarantees, and the design deliberately depends on only the
weaker one.

The exactly-once argument runs entirely through the query: after an uncertain create,
``GET /v1/payment_links?reference_id=<key>`` says whether the effect exists, and the
intent is resolved from that answer. Nothing in the design asks the provider to reject
a duplicate. design.md marks the rejection claim [INFERENCE] and refuses to build on
it, on the grounds that "documented as unique" is a statement about what callers must
send, not a promise about what the server enforces.

So this spike is a low-stakes measurement with an asymmetric payoff:

* **Negative result (duplicates accepted) changes nothing.** The design already
  assumes no enforcement. It is worth running precisely to confirm that the thing the
  design refused to rely on was correctly refused.
* **Positive result (duplicate rejected) is defence in depth, and nothing more.** It
  may be recorded as a second barrier against a duplicate payment demand. It must not
  be substituted for the reconciliation read: an undocumented behaviour that we cannot
  see change is a poor foundation for the one guarantee that stops a customer being
  asked twice for the same money.

What is recorded
----------------
Both HTTP statuses, both response bodies verbatim (including the provider's error
object fields — ``code``, ``description``, ``field``, ``source``, ``step``, ``reason``,
all verified in design.md), and whether two distinct ``plink_`` ids now exist, checked
both from the two create responses and independently from a listing query.

Verified surface this spike relies on (design.md, Provider Verification Findings)
--------------------------------------------------------------------------------
* ``POST /v1/payment_links`` with ``amount`` in integer minor units, ``currency``,
  ``description``, ``reference_id`` (required-unique, max 40 chars),
  ``notify{sms,email}``, ``reminder_enable``, ``accept_partial``, ``notes``.
* Response carries ``id`` (``plink_…``) and ``status``.
* ``GET /v1/payment_links`` supports querying by ``reference_id``.
* The error object shape, and ``code`` in {``BAD_REQUEST_ERROR``, ``GATEWAY_ERROR``,
  ``SERVER_ERROR``}.

As in spike 2.1, notifications are switched off and no ``customer`` block is sent, so
that a spike cannot message anybody.
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
    load_credentials,
    ns_to_ms,
)

SPIKE_NAME = "duplicate_reference_id"
DESIGN_ITEM = (
    "Payment Links — whether a duplicate reference_id is rejected on create [INFERENCE / "
    "EVIDENCE INSUFFICIENT]"
)
REQUIREMENTS = ("R9.C3", "R9.C5")

CREATE_PATH = "/v1/payment_links"
LIST_PATH = "/v1/payment_links"
REFERENCE_ID_MAX_LENGTH = 40

# Verified error-object field names, per design.md's errors-overview reading. Read only
# these; anything else present is preserved wholesale in the recorded body anyway.
ERROR_FIELDS = ("code", "description", "field", "source", "step", "reason")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="duplicate_reference_id.py",
        description=(
            "Create two Payment Links with an identical reference_id and record whether the "
            "provider rejects the second. " + WRITES_WARNING
        ),
        epilog=(
            "Confirms rather than sets configuration: a negative result changes nothing, "
            "because the design depends only on the documented ability to QUERY by "
            "reference_id, never on a duplicate being rejected. Requires RAZORPAY_KEY_ID "
            "and RAZORPAY_KEY_SECRET; needs no webhook endpoint and no browser step."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--trials",
        type=int,
        default=3,
        help=(
            "Independent (fresh reference_id, two creates) trials. More than one because a "
            "single rejection could be a rate limit or a transient rather than uniqueness "
            "enforcement."
        ),
    )
    parser.add_argument(
        "--amount",
        type=int,
        default=100,
        help="Link amount in integer minor units (paise). Integer only.",
    )
    parser.add_argument("--currency", default="INR", help="ISO currency code sent with each link.")
    parser.add_argument(
        "--gap-ms",
        type=int,
        default=0,
        help=(
            "Wait between the first and second create. 0 sends them back to back, which is "
            "the case the design actually fears: a retry moments after an uncertain create."
        ),
    )
    parser.add_argument(
        "--settle-ms",
        type=int,
        default=1_000,
        help="Wait between trials, and before the confirming listing query.",
    )
    add_common_arguments(parser)
    return parser


def fresh_reference_id() -> str:
    reference_id = f"rvd_{uuid.uuid4().hex[:16]}"
    if len(reference_id) > REFERENCE_ID_MAX_LENGTH:
        raise SpikeSetupError(
            f"generated reference_id {len(reference_id)} chars exceeds the documented "
            f"{REFERENCE_ID_MAX_LENGTH}-character limit"
        )
    return reference_id


def create_payload(
    *, reference_id: str, amount: int, currency: str, ordinal: int
) -> dict[str, JsonValue]:
    """Identical bodies apart from the description, so the only variable is the ordinal.

    The description differs purely so that two accepted links are distinguishable in the
    dashboard afterwards. If that difference is what causes the provider to accept both,
    the result is still the answer the design needs: the reference_id alone did not stop it.
    """
    return {
        "amount": amount,
        "currency": currency,
        "description": f"Revora provider spike: duplicate reference_id, call {ordinal}",
        "reference_id": reference_id,
        "notify": {"sms": False, "email": False},
        "reminder_enable": False,
        "accept_partial": False,
        "notes": {"revora_spike": SPIKE_NAME, "revora_call_ordinal": str(ordinal)},
    }


def attempt_create(
    client: httpx.Client, *, reference_id: str, amount: int, currency: str, ordinal: int
) -> dict[str, JsonValue]:
    """One create call, recorded verbatim.

    Verbatim matters here more than in the other spikes: the exact error the provider
    returns for a duplicate is what a future reader needs to decide whether the
    behaviour is uniqueness enforcement or something else wearing the same status code.
    """
    payload = create_payload(
        reference_id=reference_id, amount=amount, currency=currency, ordinal=ordinal
    )
    started_ns = time.monotonic_ns()
    try:
        response = client.post(CREATE_PATH, json=payload)
    except httpx.HTTPError as exc:
        return {
            "ordinal": ordinal,
            "http_status": None,
            "transport_error": f"{type(exc).__name__}: {exc}",
            "latency_ms": ns_to_ms(time.monotonic_ns() - started_ns),
        }

    body = json_body(response)
    link_id = body.get("id") if isinstance(body, dict) else None
    error_object = body.get("error") if isinstance(body, dict) else None
    result: dict[str, JsonValue] = {
        "ordinal": ordinal,
        "http_status": response.status_code,
        "latency_ms": ns_to_ms(time.monotonic_ns() - started_ns),
        "link_id": link_id if isinstance(link_id, str) else None,
        "link_status": body.get("status") if isinstance(body, dict) else None,
        "body": body,
    }
    if isinstance(error_object, dict):
        result["error_fields"] = {
            name: error_object.get(name) for name in ERROR_FIELDS if name in error_object
        }
    return result


def list_by_reference_id(client: httpx.Client, reference_id: str) -> dict[str, JsonValue]:
    """Independent confirmation, because the create responses are not the ground truth.

    A provider could return an error and still have created the object. The listing is
    the same read the design's reconciliation uses, so this doubles as a sanity check
    that the read works at all.
    """
    try:
        response = client.get(LIST_PATH, params={"reference_id": reference_id})
    except httpx.HTTPError as exc:
        return {"http_status": None, "transport_error": f"{type(exc).__name__}: {exc}"}
    body = json_body(response)
    items = collection_items(body)
    ids = sorted(
        {
            item["id"]
            for item in items
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        }
    )
    return {
        "http_status": response.status_code,
        "matched_count": len(items),
        "distinct_link_ids": list(ids),
    }


def run_trial(
    client: httpx.Client, recorder: ResultRecorder, *, index: int, args: argparse.Namespace
) -> dict[str, JsonValue]:
    reference_id = fresh_reference_id()
    first = attempt_create(
        client, reference_id=reference_id, amount=args.amount, currency=args.currency, ordinal=1
    )
    if args.gap_ms > 0:
        time.sleep(args.gap_ms / 1_000)
    second = attempt_create(
        client, reference_id=reference_id, amount=args.amount, currency=args.currency, ordinal=2
    )

    for attempt in (first, second):
        link_id = attempt.get("link_id")
        if isinstance(link_id, str):
            recorder.note_created(link_id)

    if args.settle_ms > 0:
        time.sleep(args.settle_ms / 1_000)
    listing = list_by_reference_id(client, reference_id)

    listed_ids = listing.get("distinct_link_ids")
    listed_count = len(listed_ids) if isinstance(listed_ids, list) else 0
    both_accepted = isinstance(first.get("link_id"), str) and isinstance(
        second.get("link_id"), str
    )
    second_status = second.get("http_status")
    second_rejected = isinstance(second_status, int) and second_status >= 400

    return {
        "trial": index,
        "reference_id": reference_id,
        "first_create": first,
        "second_create": second,
        "listing_after": listing,
        "both_creates_returned_a_link_id": both_accepted,
        "second_create_rejected": second_rejected,
        "distinct_link_ids_now_existing": listed_count,
    }


def interpret(recorder: ResultRecorder, trials: list[dict[str, JsonValue]]) -> None:
    if not trials:
        recorder.conclude("No trial completed; nothing was measured.")
        return

    rejected = [trial for trial in trials if trial.get("second_create_rejected") is True]
    duplicated: list[dict[str, JsonValue]] = []
    for trial in trials:
        listed = trial.get("distinct_link_ids_now_existing")
        if isinstance(listed, int) and listed > 1:
            duplicated.append(trial)

    recorder.record("trials_completed", len(trials))
    recorder.record("trials_where_second_create_rejected", len(rejected))
    recorder.record("trials_where_two_distinct_links_exist", len(duplicated))

    if duplicated:
        recorder.conclude(
            f"{len(duplicated)} of {len(trials)} trial(s) ended with two distinct plink_ ids "
            "under one reference_id. Uniqueness is a caller obligation, not a server "
            "guarantee."
        )
        recorder.add_consequence(
            "Negative result — and it changes nothing. The design deliberately depends only "
            "on the documented ability to QUERY by reference_id, never on a duplicate being "
            "rejected, so the exactly-once argument stands exactly as written. This run "
            "confirms that refusing to depend on rejection was the right call."
        )
        return

    if len(rejected) == len(trials):
        recorder.conclude(
            f"All {len(trials)} trial(s) had the second create rejected, and no trial ended "
            "with two distinct links. See the recorded error fields for what the provider "
            "actually said."
        )
        recorder.add_consequence(
            "Positive result — record it as defence in depth, not as a substitute for "
            "reconciliation. The reconciliation read by reference_id stays exactly as "
            "designed: this behaviour is undocumented, so it can change without notice, and "
            "it must not become the thing that stops a customer being asked twice for the "
            "same money."
        )
        return

    recorder.conclude(
        f"Mixed result: {len(rejected)} of {len(trials)} second creates were rejected and no "
        "trial produced two distinct links. Inconsistent rejection is weaker than no "
        "rejection at all, because it invites depending on behaviour that only usually "
        "happens."
    )
    recorder.add_consequence(
        "Treat as a negative result. Nothing in the design changes; the reconciliation read "
        "remains the only mechanism relied upon."
    )


def main(argv: list[str] | None = None) -> int:
    configure_console()
    args = build_parser().parse_args(argv)
    if args.trials <= 0:
        print("--trials must be positive", file=sys.stderr)
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

    print(f"\n{WRITES_WARNING}")
    print(f"Key mode: {credentials.key_id_hint}. Trials: {args.trials}.\n")

    trials: list[dict[str, JsonValue]] = []
    with client:
        for index in range(1, args.trials + 1):
            trial = run_trial(client, recorder, index=index, args=args)
            trials.append(trial)
            print(
                f"[{index}/{args.trials}] second create "
                f"{'rejected' if trial['second_create_rejected'] else 'accepted'}; "
                f"{trial['distinct_link_ids_now_existing']} distinct link id(s) listed"
            )

    recorder.record("gap_between_creates_ms", args.gap_ms)
    recorder.record("amount_minor_units", args.amount)
    recorder.record("currency", args.currency)
    interpret(recorder, trials)
    recorder.record("trials", list(trials))
    recorder.finish()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
