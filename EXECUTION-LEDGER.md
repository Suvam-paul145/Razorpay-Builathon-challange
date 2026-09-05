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

## Session: spec completed end to end

Ruling: the reported demo "regression" (65 cases stranded ACTION_SCHEDULED, 7 RECOVERED, 68 REAL-provenance
rows) was NOT a code regression and NOT a torn tree. The test container had accumulated 2813 merchants and
33028 cases across every prior pg run and probe; `run_once` claims across merchants and `claimable_merchant_ids`
orders by OLDEST WORK FIRST under a 100-merchant scan limit, so a freshly seeded demo merchant sorted last
behind everyone else's backlog and spent its bounded MAX_DRAIN_PASSES draining it. The REAL-provenance count
was container-wide, not run-scoped - pg-tier webhook tests legitimately write REAL rows. On a clean database
the same 280-case batch went from never finishing to 166 seconds. Diagnosed with faulthandler.dump_traceback_later
after xact_commit showed 325 commits/sec with zero new rows: busy, not hung.

Ruling: rejected the reasoning-credential hypothesis before acting on it. REVORA_LLM_CREDENTIAL is in .env but
nothing in revora/ loads .env (only uvicorn, via --env-file), so credential_available() is False in every test
and probe process. Verified by running it rather than by reading the file.

Ruling: MY OWN MISTAKE, recorded so it is not repeated. TRUNCATE over the application tables included
`app_config`, which holds the 72 MIGRATION-SEEDED configuration defaults that no fixture recreates. That broke
four unrelated pg tests (`assert 0 == 72`, "no bound fell back to a code placeholder"). The correct reset is
DROP DATABASE / CREATE DATABASE / alembic upgrade head - never TRUNCATE app_config.

Ruling: the demo loader silently under-seeded its designed batch. INGEST_RATE_LIMIT is 600 accepted ingestion
requests per minute per source identifier; _seed_cohort delivered all 820 main-cohort webhooks in one tight loop
against a frozen ManualClock, so 600 were accepted and 220 came back 429 - and the loader logged a warning per
refusal and carried on, reporting seeded_case_count 780 against case_count 1000 with both arms below the
447-per-arm requirement it had computed for itself. Fixed in the loader, not the limit: a per-run _IngestPacer
paces every delivery and advances the clock one rate window when the allowance is spent (61s per window, ~3
minutes across a 1000-case batch against a 7-day RECOVERY_WINDOW_DURATION, and window_end_at is immutable so no
window shortens). Refused raising, overriding or special-casing INGEST_RATE_LIMIT - it guards the only
unauthenticated endpoint. Added SeedDeliveryShortfallError so a short cohort now raises instead of being reported.

Ruling: verified_test_mode_recoveries was reporting the CONSTANT DEMO_VERIFIED_RECOVERY_MIN_COUNT whenever a
capability check passed, not a count of anything. Replaced with authoritative_test_mode_recoveries(), which counts
recovery_outcome -> payment_state_read (via verified_by_read_id) -> recovery_case rows where the case is RECOVERED,
the read says captured, and read.amount = case.payment_amount. Reported only when script_payment is None.

Ruling: design open question 15 ANSWERED, not routed around. Razorpay's Payment Links API can create, fetch,
update, cancel and re-notify a link; it has NO endpoint that pays one. Test-mode payment happens on a mock page a
person drives. So verified_test_mode_capability() stays False, no automation was fabricated, and R28.C2's three
Verified_Demo_Recoveries are a documented manual RUNBOOK.md step.

Ruling: two task-text clauses contradict the built system and the system won, stated where asserted.
`webhook_event` and `audit_record` have no provenance column, so R28.C16 is checked against the five tables that
do - with an information_schema assertion that fails loudly if a migration ever adds one. And the 54.3 chain
"resend confirmed -> payment.captured -> read -> RECOVERED -> promise KEPT" is unreachable in that order: a
CONFIRMED execution enqueues its own outcome observation, so the read following a confirmed follow-up IS
R23.C11's missed-promise condition. The reachable composition is staged instead and the reason documented.

Demo verified by running it, on a properly seeded database, 1000/1000 seeded, zero refusals, 748s:
observed_recovered_revenue 108,523,787 minor units (Rs 10,85,237.87); incremental_recovered_revenue
NOT_ESTABLISHED (refusal DISQUALIFYING_LABEL only - the sample-size refusal is gone now both arms clear 447);
demonstration_incremental_revenue +33,763,071 with lift 0.1148 and interval [0.0576, 0.1721], which EXCLUDES zero
and CONTAINS the planted 0.1500; coverage.complete true, missing empty, audit_sequence_gaps empty, every
real_provenance_rows entry zero; 312 RECOVERED / 658 EXPIRED / 22 ESCALATED / 8 STOPPED, promises KEPT 4 /
MISSED 4 / BEYOND_WINDOW_ESCALATED 4.

Checkpoint 52 verified by breaking it on purpose: a deliberate revora.reasoning -> revora.persistence import
makes lint-imports exit 1 with the contract BROKEN and the exact edge named, and returns to 6 kept / 0 broken
when removed.

## Final gate (all green)
ruff clean | no_float 72 files | lint-imports 6 kept 0 broken | mypy 100 source files 0 errors
pure+model 987 passed 1 failed (known beta_cdf) | pg 393 passed 1 skipped 0 failed in 11:38
smoke 3 passed | harness null-scenario exit 0 | web lint 0, 10 test files passed, build 0, both bundles present
Alembic head 0018. NEON IS AT 0015 - run `alembic upgrade head` against it before the next redeploy or the API
refuses to start.

## Spec status
Every non-optional task complete. Only 34.6, 35.4, 46.5, 49.6 left undone - all four are marked optional.
