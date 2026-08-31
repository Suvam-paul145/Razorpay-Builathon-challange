"""Shared plumbing for the provider verification spikes.

The spikes are not part of Revora. They are standalone scripts an operator runs by
hand against Razorpay **test-mode** credentials to close the four items the design
tags [EVIDENCE INSUFFICIENT]. Nothing under ``revora/`` imports this module and
nothing here is exercised by the automated test suite.

What this module exists to guarantee, so that four scripts do not each get it
slightly wrong:

* **Credentials are read from the environment and never printed.** The secret is
  scrubbed out of every JSON artifact before it is written, and the key id is
  redacted too — it identifies the merchant account and there is no reason for it
  to live in a file that gets committed or pasted into an issue.
* **A spike cannot be pointed at live credentials by accident.** Two of these
  scripts create real Payment Links. A live key would create real payment demands
  against real customers. The key-id check fails closed and is overridable only by
  an explicit flag, so the override shows up in shell history.
* **Timeouts are explicit.** ``httpx`` has a default timeout, but a spike measuring
  latency must not inherit a number it did not choose. Connect and read budgets are
  separate, because a connect failure and a read timeout mean different things to
  the design's response-classification table.
* **Durations stay integers.** Latencies are measured in nanoseconds from a
  monotonic clock and reported in whole milliseconds. Percentiles use nearest-rank,
  so every reported figure is a real observation rather than an interpolation
  between two of them.

Verified provider surface used here, per design.md's Provider Verification
Findings section: Basic auth over ``base64(key_id:key_secret)``. That is all this
module asserts. Endpoint paths belong to the individual spikes.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

import httpx

__all__ = [
    "DEFAULT_BASE_URL",
    "DEFAULT_OUTPUT_DIR",
    "ENV_KEY_ID",
    "ENV_KEY_SECRET",
    "ENV_WEBHOOK_SECRET",
    "REPO_ROOT",
    "TEST_KEY_PREFIX",
    "WRITES_WARNING",
    "Credentials",
    "JsonValue",
    "ResultRecorder",
    "SpikeSetupError",
    "add_common_arguments",
    "build_client",
    "collection_items",
    "configure_console",
    "json_body",
    "latency_summary",
    "load_credentials",
    "median_int",
    "ns_to_ms",
    "percentile_nearest_rank",
    "utc_now_iso",
]

JsonValue = str | int | bool | None | list["JsonValue"] | dict[str, "JsonValue"]
"""What a spike is allowed to put in its artifact. Deliberately excludes ``float``."""

ENV_KEY_ID: Final = "RAZORPAY_KEY_ID"
ENV_KEY_SECRET: Final = "RAZORPAY_KEY_SECRET"
ENV_WEBHOOK_SECRET: Final = "RAZORPAY_WEBHOOK_SECRET"

TEST_KEY_PREFIX: Final = "rzp_test_"
# TODO: unverified — design.md verifies the Basic-auth scheme but says nothing about
# the shape of a key id. The `rzp_test_` prefix is taken from the Razorpay dashboard,
# not from the verified surface. Needs checking against official documentation. The
# check fails closed either way: an unrecognised key id is refused rather than
# assumed to be test mode, and `--allow-live-credentials` is the documented escape.

DEFAULT_BASE_URL: Final = "https://api.razorpay.com"
# TODO: unverified — design.md gives paths (`/v1/payment_links`, `/v1/payments/{id}`)
# without a host. The API host above is not in the verified surface; `--base-url`
# exists so a correction does not require editing four scripts.

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT_DIR: Final = REPO_ROOT / "docs" / "spike-results"

WRITES_WARNING: Final = (
    "THIS SPIKE CREATES REAL OBJECTS in the Razorpay account the credentials belong to. "
    "Run it against test-mode credentials only."
)

_REDACTED: Final = "<redacted>"


def configure_console() -> None:
    """Make console output survive a cp1252 terminal.

    Windows PowerShell defaults to a code page that cannot encode every character these
    scripts print, and an operator losing a completed measurement to a
    ``UnicodeEncodeError`` in the final summary would be an absurd way to waste a run.
    Printed text is kept close to ASCII on purpose; this is the belt as well as the braces.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


class SpikeSetupError(RuntimeError):
    """A spike could not start. Distinct from a spike that ran and measured something.

    Raised for missing credentials, a key id that does not look like test mode, or a
    missing manual input. Callers map this to a non-zero exit code, which keeps
    "the measurement failed to start" separable from "the measurement completed and
    the answer was uncomfortable".
    """


@dataclass(frozen=True)
class Credentials:
    """API key id and secret. Never rendered, never serialized.

    ``__repr__`` is overridden rather than relying on discipline at every call site,
    because the one place a secret leaks is the place nobody was thinking about it —
    a traceback, a debugger, a ``print`` left in during a late-night spike run.
    """

    key_id: str
    key_secret: str

    def __repr__(self) -> str:
        return f"Credentials(key_id={_REDACTED!r}, key_secret={_REDACTED!r})"

    __str__ = __repr__

    @property
    def looks_like_test_mode(self) -> bool:
        return self.key_id.startswith(TEST_KEY_PREFIX)

    @property
    def key_id_hint(self) -> str:
        """A non-identifying hint, safe to print, that says only which mode we are in."""
        return f"{TEST_KEY_PREFIX}…" if self.looks_like_test_mode else "not-test-mode"

    def secret_strings(self) -> tuple[str, ...]:
        """Every value that must never appear in output. Used by the redactor."""
        return tuple(value for value in (self.key_id, self.key_secret) if value)


def add_common_arguments(parser: argparse.ArgumentParser) -> None:
    """Add the flags every spike shares, so the four scripts stay invocable alike."""
    parser.add_argument(
        "--base-url",
        default=DEFAULT_BASE_URL,
        help=f"Provider API base URL (default: {DEFAULT_BASE_URL}).",
    )
    parser.add_argument(
        "--connect-timeout-ms",
        type=int,
        default=5_000,
        help="Connection establishment budget in milliseconds (default: 5000).",
    )
    parser.add_argument(
        "--read-timeout-ms",
        type=int,
        default=15_000,
        help=(
            "Response read budget in milliseconds (default: 15000, matching the "
            "PROVIDER_CALL_TIMEOUT default of 15 seconds)."
        ),
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=f"Directory for the JSON artifact (default: {DEFAULT_OUTPUT_DIR}).",
    )
    parser.add_argument(
        "--allow-live-credentials",
        action="store_true",
        help=(
            "Permit a key id that does not look like test mode. Required to run against "
            "live keys; do not use it unless you intend real customer-visible effects."
        ),
    )


def load_credentials(*, allow_live: bool) -> Credentials:
    """Read the API key pair from the environment, or explain exactly what to set.

    Args:
        allow_live: If ``False``, a key id that does not carry the test-mode prefix is
            refused. This is the guard that stops a link-creating spike from being
            aimed at production because two terminals were open.

    Raises:
        SpikeSetupError: if either variable is absent or empty, or if the key id does
            not look like test mode and ``allow_live`` is ``False``.
    """
    key_id = os.environ.get(ENV_KEY_ID, "").strip()
    key_secret = os.environ.get(ENV_KEY_SECRET, "").strip()

    missing = [name for name, value in ((ENV_KEY_ID, key_id), (ENV_KEY_SECRET, key_secret))
               if not value]
    if missing:
        raise SpikeSetupError(
            "Missing provider credentials: "
            + ", ".join(missing)
            + ".\n\nGet a test-mode key pair from the Razorpay dashboard "
            "(Account & Settings -> API Keys, with the dashboard in Test Mode), then:\n\n"
            f'  PowerShell:  $env:{ENV_KEY_ID} = "rzp_test_…"\n'
            f'               $env:{ENV_KEY_SECRET} = "…"\n'
            f'  bash/zsh:    export {ENV_KEY_ID}="rzp_test_…"\n'
            f'               export {ENV_KEY_SECRET}="…"\n\n'
            "See scripts/spikes/README.md."
        )

    credentials = Credentials(key_id=key_id, key_secret=key_secret)
    if not credentials.looks_like_test_mode and not allow_live:
        raise SpikeSetupError(
            f"{ENV_KEY_ID} does not begin with {TEST_KEY_PREFIX!r}, so it may be a live key.\n"
            "These spikes create real Payment Links; against a live account that means real "
            "payment demands to real customers.\n"
            "If you are certain, re-run with --allow-live-credentials."
        )
    return credentials


def build_client(
    credentials: Credentials,
    *,
    base_url: str,
    connect_timeout_ms: int,
    read_timeout_ms: int,
) -> httpx.Client:
    """A thin, explicit client: Basic auth, TLS verification on, no implicit retries.

    No provider SDK, for the same reason the design refuses one on the execution path:
    a spike measuring how the provider behaves must not have a library deciding on its
    behalf when to retry or how long to wait.

    ``verify=True`` is passed rather than left to the default so that a later reader
    can see it was a decision.
    """
    if connect_timeout_ms <= 0 or read_timeout_ms <= 0:
        raise SpikeSetupError("timeouts must be positive integers in milliseconds")
    timeout = httpx.Timeout(
        connect=connect_timeout_ms / 1_000,
        read=read_timeout_ms / 1_000,
        write=read_timeout_ms / 1_000,
        pool=connect_timeout_ms / 1_000,
    )
    return httpx.Client(
        base_url=base_url,
        auth=httpx.BasicAuth(credentials.key_id, credentials.key_secret),
        timeout=timeout,
        verify=True,
        follow_redirects=False,
        headers={"Content-Type": "application/json", "User-Agent": "revora-spike/0.1"},
    )


def json_body(response: httpx.Response) -> JsonValue:
    """Decode a response body, or return the raw text when it is not JSON.

    A body that will not parse is itself a finding — the design's response
    classification table has a row for "HTTP 200 with an unparseable body" and routes
    it to ``UNCERTAIN``. So an unparseable body is preserved as text rather than
    swallowed.
    """
    try:
        decoded: JsonValue = response.json()
    except ValueError:
        return {"unparseable_body_text": response.text[:4_000]}
    return decoded


def collection_items(body: JsonValue) -> list[JsonValue]:
    """Pull the entity list out of a provider collection response.

    TODO: unverified — design.md verifies that fetch-all Payment Links *supports
    querying by* ``reference_id``, but does not state the field names of the
    collection envelope. ``count`` and ``items`` are taken from the Razorpay
    collection convention and need checking against official documentation. Handled
    permissively here: a bare list is accepted too, so a wrong guess about the
    envelope shows up as a shape mismatch in the artifact rather than as a silent
    "no links found", which is the reading that would corrupt this measurement.
    """
    if isinstance(body, list):
        return list(body)
    if isinstance(body, dict):
        items = body.get("items")
        if isinstance(items, list):
            return list(items)
    return []


def utc_now_iso() -> str:
    """Wall-clock UTC, for stamping the artifact. Never used to measure a duration."""
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def ns_to_ms(nanoseconds: int) -> int:
    """Whole milliseconds, rounded half-up. Sub-millisecond precision is noise here."""
    return (nanoseconds + 500_000) // 1_000_000


def median_int(values: Sequence[int]) -> int:
    """Median of a non-empty integer sample, rounded half-up on an even-length tie."""
    if not values:
        raise ValueError("median of an empty sample is undefined")
    ordered = sorted(values)
    count = len(ordered)
    middle = count // 2
    if count % 2 == 1:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle] + 1) // 2


def percentile_nearest_rank(values: Sequence[int], percentile: int) -> int:
    """Nearest-rank percentile: always an observed value, never an interpolation.

    Interpolating a p95 over 50 samples would invent a latency the provider never
    exhibited, and this measurement exists to set a timeout. Reporting a real
    observation is the conservative choice.
    """
    if not values:
        raise ValueError("percentile of an empty sample is undefined")
    if not 0 < percentile <= 100:
        raise ValueError("percentile must be in (0, 100]")
    ordered = sorted(values)
    rank = -(-percentile * len(ordered) // 100)  # ceil, integer-only
    return ordered[min(rank, len(ordered)) - 1]


def latency_summary(samples: Sequence[int]) -> dict[str, JsonValue]:
    """min / median / p95 / max over whole-millisecond samples, plus the sample count."""
    if not samples:
        return {"sample_count": 0, "min_ms": None, "median_ms": None, "p95_ms": None,
                "max_ms": None}
    return {
        "sample_count": len(samples),
        "min_ms": min(samples),
        "median_ms": median_int(samples),
        "p95_ms": percentile_nearest_rank(samples, 95),
        "max_ms": max(samples),
    }


def _redact(value: JsonValue, secrets: Sequence[str]) -> JsonValue:
    """Recursively replace any occurrence of a secret string anywhere in the artifact.

    Provider responses are echoed into the artifact verbatim, which is the point — an
    error body is evidence. Verbatim echoing is also how a credential ends up in a
    committed file, so every string is scrubbed on the way out rather than at each
    call site.
    """
    if isinstance(value, str):
        scrubbed = value
        for secret in secrets:
            if secret and secret in scrubbed:
                scrubbed = scrubbed.replace(secret, _REDACTED)
        return scrubbed
    if isinstance(value, list):
        return [_redact(item, secrets) for item in value]
    if isinstance(value, dict):
        return {key: _redact(item, secrets) for key, item in value.items()}
    return value


@dataclass
class ResultRecorder:
    """Accumulates a spike's measurement, prints a readable summary, writes JSON.

    The artifact is the durable evidence; the printed summary is for the operator who
    just ran it. Both come from the same dict, so the file cannot say something the
    terminal did not.
    """

    spike: str
    design_item: str
    requirements: tuple[str, ...]
    output_dir: Path
    creates_objects: bool
    secrets: tuple[str, ...] = ()
    started_at: str = field(default_factory=utc_now_iso)
    measurement: dict[str, JsonValue] = field(default_factory=dict)
    conclusion: list[str] = field(default_factory=list)
    consequence: list[str] = field(default_factory=list)
    unverified: list[str] = field(default_factory=list)
    created_object_ids: list[str] = field(default_factory=list)

    def record(self, key: str, value: JsonValue) -> None:
        self.measurement[key] = value

    def conclude(self, line: str) -> None:
        self.conclusion.append(line)

    def add_consequence(self, line: str) -> None:
        """A statement of what the design must change if this result holds."""
        self.consequence.append(line)

    def note_unverified(self, item: str) -> None:
        """Record something this spike assumed that design.md did not verify."""
        if item not in self.unverified:
            self.unverified.append(item)

    def note_created(self, object_id: str) -> None:
        """Track a provider object this run created, so the account can be cleaned up."""
        self.created_object_ids.append(object_id)

    def finish(self) -> Path:
        """Print the summary, write the JSON artifact, return its path."""
        payload: dict[str, JsonValue] = {
            "spike": self.spike,
            "design_item": self.design_item,
            "requirements": list(self.requirements),
            "started_at": self.started_at,
            "finished_at": utc_now_iso(),
            "creates_provider_objects": self.creates_objects,
            "created_object_ids": list(self.created_object_ids),
            "measurement": self.measurement,
            "conclusion": list(self.conclusion),
            "design_consequence": list(self.consequence),
            "unverified_assumptions": list(self.unverified),
        }
        safe = _redact(payload, self.secrets)

        self.output_dir.mkdir(parents=True, exist_ok=True)
        stamp = self.started_at.replace(":", "").replace("-", "").replace(".", "")
        path = self.output_dir / f"{self.spike}-{stamp}.json"
        path.write_text(json.dumps(safe, indent=2, sort_keys=False) + "\n", encoding="utf-8")

        self._print_summary(path)
        return path

    def _print_summary(self, path: Path) -> None:
        write = sys.stdout.write
        rule = "=" * 78
        write(f"\n{rule}\n{self.spike}  —  {self.design_item}\n{rule}\n")
        for key, value in self.measurement.items():
            if isinstance(value, dict):
                write(f"{key}:\n")
                for inner_key, inner_value in value.items():
                    write(f"    {inner_key}: {_short(inner_value)}\n")
            elif isinstance(value, list):
                write(f"{key}: {len(value)} entries (see artifact)\n")
            else:
                write(f"{key}: {_short(value)}\n")

        if self.conclusion:
            write("\nCONCLUSION\n")
            for line in self.conclusion:
                write(f"  - {line}\n")
        if self.consequence:
            write("\nCONSEQUENCE FOR THE DESIGN\n")
            for line in self.consequence:
                write(f"  - {line}\n")
        if self.unverified:
            write("\nUNVERIFIED ASSUMPTIONS THIS RUN RELIED ON\n")
            for line in self.unverified:
                write(f"  - {line}\n")
        if self.created_object_ids:
            write(
                f"\nCREATED {len(self.created_object_ids)} PROVIDER OBJECT(S) "
                "in the test-mode account. Ids are in the artifact.\n"
            )
        write(f"\nArtifact: {path}\n")
        write("Next: transcribe the numbers into docs/provider-findings.md.\n\n")


def _short(value: JsonValue, limit: int = 300) -> str:
    text = value if isinstance(value, str) else json.dumps(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"
