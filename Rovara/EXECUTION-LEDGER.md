# Execution ledger - spec: .kiro/specs/revora-customer-response-loop/tasks.md

## Done
34-40, 49.1 (earlier sessions)
Ticker: Role.TICKER + revora/jobs/ticker.py + migration 0014. reclaim_stale now has a caller.
41 delay reason capture + confidence floor validator + P36/P37/P38 + note retention
42 contact suppression. Migration 0015 widened terminal_reason CHECK - it was UNSTORABLE before.
43 partial arrangement as signal only
44 Promise_To_Pay. Migration 0016 seeded FIVE promise bounds. promise field now non-null.
46 payment link resend + own classification. Added EXECUTING->DECISION_PENDING edge.
50 case timeline read model
51 customer frontend, second Vite entry, dist-customer/

## Remaining
53 demo loader (DEMO BLOCKER), 47, 48, 49.2-49.5, 54, checkpoints 45/52/55
Skipping optional: 34.6, 35.4, 46.5, 49.6

## Rulings
Ruling: 53 before 47/48/49 - acceptance is a working demo; dashboard reads 0.00. Cost if wrong: those land later.
Ruling: 53 serial not parallel with 47 - 47 changes action availability, would move 53 measurements.
Ruling: verify the demo by running it, not by trusting a subagent report.

## Gates verified this session
ruff clean | no_float 70 | lint-imports 6/0 | mypy 0 err 162 files
pure+model 848 pass 1 fail (known beta_cdf) | pg 310 pass 1 skip | eslint clean | vitest 81 pass
Alembic head 0016 (16 files). EXPECTED_REVISION 0016. NEON AT 0015 - upgrade before next redeploy.

## Hard rules
NEVER point tests at the Neon URL in .env - live deployment; pg fixtures create roles and insert merchants.
Local test container revora-pg18 port 5545 at 0016.
Leave alone: tests/properties/test_beta_interval.py::test_cdf_is_monotone
## Session: demo brought up end to end

Ruling: The 7 pg failures and the 4x slowdown were NOT code regressions. Two environmental causes:
a stale `revora.jobs.main` worker running for a day had enqueued ~420k jobs, and `run_once` claims
globally, so the fixture's fresh detection job was starved behind them; and a leftover
`tests/integration/test_tmp_probe.py` was `pg`-marked and ran a 200-case demo batch inside the pg
tier. Killed six abandoned processes, dropped/recreated the test database, deleted the probe. Pg
tier returned to 310 passed in 5:15. My `synthetic_run_id`-column theory was wrong: that column
exists from migration 0001. Verified before acting, which is why no migration was written for it.

Ruling: `RECOVERED` was unreachable from mid-pipeline states. A verified capture arriving while a
case sat in DECISION_PENDING or EXECUTING recorded RECOVERY_RECORDED, counted the money, and had
its transition refused - measured 62 recoveries against 39 RECOVERED, 23 cases EXPIRED carrying a
recovery record, zero DELAYED_RECOVERY_RECONCILED. Added verified-capture-gated edges from every
non-terminal state. Rejected routing through WAITING_FOR_OUTCOME (task 46 already ruled that state
must be entered only through a confirmed effect).

Ruling: that fix also repaired the memory pipeline. `observation_writer` runs on transition success
only, so before it every prior-cohort recovery wrote no observation and every baseline was the flat
0.5 prior - the demo's prior cohort was doing nothing at all.

Ruling: the prior cohort then inflated the GLOBAL baseline to 0.906, because backoff counts a
specific cell's observations at every more general level and no other cell reaches
MIN_SEGMENT_SAMPLE_SIZE. Revora correctly declined to intervene everywhere: 14 intents and zero
customer signals against 71 and 24 before. Fixed by splitting the cohort into a designated half
that recovers and a PRIOR_CONTRAST half that does not, and by running the prior windows out before
the main cohort is seeded so non-recoveries are history too.

Ruling: STOPPED was unreachable by waiting. `list_due_for_review` excludes capped cases on purpose,
so a capped case rests at POLICY_CHECK and the lifecycle sweep expires it instead. R30.C10 belongs
to the review handler, so the batch delivers a second signed `payment.failed` on the same payment;
detection attaches it and the review finds the budget spent.

Ruling: `promise_status:MISSED` needed two blockers cleared, not one. First, the promise sweep
enqueued a decision cycle only from POLICY_CHECK, but every case that can hold a promise stands in
WAITING_FOR_OUTCOME - so the previously-uncalled REENTRY edge had no caller and nothing selected
the follow-up. Second, PAYMENT_LINK strictly dominates PROMISE_TO_PAY_FOLLOW_UP at every amount
(net difference 0.02*amount + 725), so it was never selected even once reachable.

Ruling: cleared the dominance by fixing a real defect rather than by re-pricing anything. A second
decision cycle was minting a SECOND live payable link for one debt, nothing cancelling the first -
measured on all four promise cases. R24.C10 forbids it; it had only been read on the follow-up
path. Added ExclusionReason.LIVE_PAYMENT_LINK_EXISTS (migration 0018) with the "live link"
definition moved to `ExecutionIntentRepository.live_payment_link` so estimation and execution share
one reader. Refused the alternatives: re-pricing the follow-up does not flip selection at demo
amounts, and raising its uplift would be inventing evidence to produce a demo outcome.

Ruling: migration head is now 0018 and EXPECTED_REVISION matches. Neon is at 0015, so
`alembic upgrade head` must run against it before the next deploy or the API refuses to start.

Demo verified by running it, not by a passing suite. 280 cases: coverage.complete true, missing
empty, observed_recovered_revenue 25,964,040 minor units, incremental_recovered_revenue
NOT_ESTABLISHED, demonstration_incremental_revenue carried with both labels, terminal states
RECOVERED 111 / EXPIRED 139 / ESCALATED 22 / STOPPED 8, promises KEPT 4 / MISSED 4 /
BEYOND_WINDOW_ESCALATED 4, audit_sequence_gaps empty, zero REAL-provenance rows, max one payment
link per case.
