"""Drive the production ticker by hand, one tick or on a loop, and report what is due.

    python scripts/dev_tick.py                     # one tick, every sweep, then exit
    python scripts/dev_tick.py --watch             # tick every 30s until Ctrl-C
    python scripts/dev_tick.py --only case_review  # one kind only
    python scripts/dev_tick.py --due               # just report what is due, enqueue nothing

**This script no longer contains a ticker.** It used to, and that was the problem: the loop it
held was the *only* caller of ``revora.jobs.scheduler.enqueue_sweep`` anywhere in the
repository, so the seven periodic sweeps existed in a deployment and never ran. The loop now
lives in :mod:`revora.jobs.ticker` and runs as a third process role — ``REVORA_ROLE=ticker``,
``python -m revora.jobs.ticker_main`` — and this script is a hand-operated front end for the
same code.

That is the point of the rewrite, not tidiness. A development script with its own copy of the
schedule is a script that tests itself: the copy here mapped sweep kinds by hard-coded string
literal, so renaming a constant in ``revora.jobs.scheduler`` silently demoted the renamed kind
to a 300-second fallback, and running this script by hand would have looked completely normal
while doing the wrong thing. One implementation with two callers means a bug found here is a
bug in what the sidecar runs.

What this script still owns is the parts a sidecar has no use for: the ``--due`` report, which
is the read a reviewer wants first, and printing to a terminal rather than to a log stream.

Every enqueue is dedupe-keyed by kind and interval bucket, so running this twice inside one
bucket enqueues nothing the second time. That is the sweep machinery working, not a failure —
``enqueued 0/7`` on a second tick is the expected answer.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import UTC, datetime

from sqlalchemy import create_engine, text

from revora.jobs.scheduler import PERIODIC_SWEEP_KINDS
from revora.jobs.ticker import tick
from revora.persistence.repositories.engine import build_engine, set_engine


def _report_due(url: str) -> None:
    """What is waiting, without enqueueing anything. The read a reviewer wants first."""
    engine = create_engine(url, future=True)
    try:
        with engine.begin() as connection:
            print("\ncases at POLICY_CHECK with a review appointment:")
            rows = connection.execute(
                text(
                    """
                    SELECT id, state, decision_cycle_count, next_review_at, window_end_at,
                           next_review_at <= now() AS due_now
                      FROM recovery_case
                     WHERE state = 'POLICY_CHECK' AND next_review_at IS NOT NULL
                     ORDER BY next_review_at
                    """
                )
            ).mappings().all()
            if not rows:
                print("  (none) — no case is currently waiting on a review")
            for row in rows:
                flag = "DUE NOW" if row["due_now"] else "not yet"
                print(
                    f"  {row['id']}  cycle={row['decision_cycle_count']}  "
                    f"review_at={row['next_review_at']}  [{flag}]"
                )

            print("\npending jobs by kind:")
            for row in connection.execute(
                text(
                    "SELECT kind, state, count(*) AS n FROM job "
                    "GROUP BY kind, state ORDER BY kind, state"
                )
            ).mappings():
                print(f"  {row['kind']:<32} {row['state']:<10} {row['n']}")
    finally:
        engine.dispose()


def _tick(slug: str | None, only: str | None) -> int:
    """One tick through the production loop. Returns a process exit code.

    ``--only`` is validated by :func:`revora.jobs.ticker.tick`, which raises rather than
    enqueueing nothing — an unrecognised kind and a bucket that is already occupied would
    otherwise both print ``enqueued 0``.
    """
    stamp = datetime.now(UTC).strftime("%H:%M:%S")
    try:
        report = tick(merchant_slug=slug, kinds=None if only is None else (only,))
    except ValueError as exc:
        print(f"{exc}", file=sys.stderr)
        return 2

    if report.merchants == 0:
        print(f"[{stamp}] no merchant ticked{'' if slug is None else f' (slug {slug!r})'}")
        return 1

    considered = report.merchants * (len(PERIODIC_SWEEP_KINDS) if only is None else 1)
    print(
        f"[{stamp}] {report.merchants} merchant(s): enqueued {report.enqueued}/{considered}"
        f"{'' if report.enqueued else ' (already pending)'}"
        f"{f', reclaimed {report.reclaimed} stale job(s)' if report.reclaimed else ''}"
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--slug", default=None, help="one merchant slug (default: all)")
    parser.add_argument("--only", default=None, help="one sweep kind (default: all seven)")
    parser.add_argument("--watch", action="store_true", help="tick until Ctrl-C")
    parser.add_argument("--every", type=int, default=30, help="seconds between ticks with --watch")
    parser.add_argument("--due", action="store_true", help="report what is due, enqueue nothing")
    args = parser.parse_args()

    url = os.environ.get("REVORA_DATABASE_URL", "").strip()
    if not url:
        print("REVORA_DATABASE_URL is not set. Run: . .\\scripts\\dev_env.ps1", file=sys.stderr)
        return 2

    if args.due:
        _report_due(url)
        return 0

    # The sidecar resolves its engine lazily from the same variable; this installs it up front
    # so a bad URL fails here rather than inside the first sweep's transaction.
    set_engine(build_engine(url))

    if not args.watch:
        code = _tick(args.slug, args.only)
        if code == 0:
            _report_due(url)
        return code

    print(f"ticking every {args.every}s. Ctrl-C to stop.")
    try:
        while True:
            _tick(args.slug, args.only)
            time.sleep(args.every)
    except KeyboardInterrupt:
        print("\nstopped.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
