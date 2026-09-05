# Provider Findings — Measured Against Razorpay Test Mode

This document is the measured half of the design's Provider Verification Findings
section. That section settled everything it could settle by reading the official
documentation, and listed what it could not. Four of those items need a live account to
answer. This is where their answers go.

**Nothing here has been run yet.** Every result field below is marked `NOT YET RUN`. A
result field must be filled in from a spike artifact under `docs/spike-results/` — not
from memory, and not from what you expect. Until a row says otherwise, the design default
stands *unconfirmed*, which is not the same as confirmed. A design default is an
`[ASSUMPTION]` until a measurement agrees with it.

How to run the spikes: `scripts/spikes/README.md`.

---

## Summary

| # | Item | Design tag | Spike | Status | Parameters it decides |
| --- | --- | --- | --- | --- | --- |
| 1 | Whether a just-created Payment Link shows up right away in the fetch-all listing | [EVIDENCE INSUFFICIENT] | `link_listing_freshness.py` | **NOT YET RUN** | `EXECUTION_RECONCILIATION_INTERVAL`, `MAX_EXECUTION_RECONCILIATION_ATTEMPTS` |
| 2 | How far behind the capture webhook the fetch-payment read lags | [EVIDENCE INSUFFICIENT] | `payment_read_lag.py` | **NOT YET RUN** | `OUTCOME_READ_LATENCY_BOUND`, `PAYMENT_STATE_RECONCILIATION_INTERVAL` |
| 3 | Whether a duplicate `reference_id` is rejected on create | [INFERENCE] — design does not depend on it | `duplicate_reference_id.py` | **NOT YET RUN** | None. Confirms something the design does not depend on |
| 4 | Whether server-side retry or payment-method-update exists on this account | [EVIDENCE INSUFFICIENT] | `retry_capability.py` | **NOT YET RUN** | Executable action set; the eligibility table in `revora/domain/actions.py` |

Status values: `NOT YET RUN`, `MEASURED — DEFAULTS SURVIVE`, `MEASURED — PARAMETERS CHANGED`, `INCONCLUSIVE`.

An `INCONCLUSIVE` row is a valid outcome and must not be quietly upgraded. Two of
these spikes need a human to complete a payment in a browser, and a sample of three
hand-made payments is honest evidence about very little.

---

## 0. Fetch All Payments request shape — **RESOLVED FROM DOCUMENTATION**

**Design tag:** the design verifies the *capability* (listing payments in a window) but not
the wire shape — no parameter names, no bounds, no response envelope.
**Resolved by:** the official documentation, not a spike.
**Source:** <https://razorpay.com/docs/api/payments/fetch-all-payments/>
**Blocks:** task 22, the detection-gap backfill.

This one needed no live call, because the endpoint is read-only and fully documented. It is
recorded here anyway, because the backfill's correctness depends on it, and a future reader
should not have to guess which parts were checked.

### What the documentation states

`GET /v1/payments`

| Parameter | Type | Documented constraint |
| --- | --- | --- |
| `from` | integer | UNIX seconds. Must be between `946684800` and `4765046400`; outside this the API returns 400 `from must be between 946684800 and 4765046400` |
| `to` | integer | UNIX seconds |
| `count` | integer | Default 10, **maximum 100**, minimum 1. 400 on `count=0` and on `count>100` |
| `skip` | integer | Minimum 0. 400 on a negative value |

Response envelope:

```json
{ "entity": "collection", "count": 2, "items": [ { "id": "pay_...", "entity": "payment", ... } ] }
```

Item fields match the `PaymentEntity` already parsed on the `fetch_payment` path — `id`,
`status` (`created` / `authorized` / `captured` / `refunded` / `failed`), `captured`,
`amount`, `amount_refunded`, `currency`, `order_id`, `method`, and the `error_*` group.

### What this decides in code

| Decision | Where | Why |
| --- | --- | --- |
| The 100-record page cap is a named constant and the backfill pages with `skip` | `MAX_PAYMENTS_PAGE_SIZE` in `providers/razorpay.py` | If the backfill asked for a whole lookback window at once, it would silently skip everything past the first 100 records — the very detection gap it exists to close |
| `from` / `to` / `count` / `skip` bounds are checked client-side **before** the call | `RazorpayClient.list_payments` | Breaking a bound earns a documented 400, which counts as a *definitive* `ClientError`. A careless caller reads a definitive failure on a list endpoint as "the window held nothing". Refusing locally keeps a caller's math mistake separate from the provider's real verdict — and "nothing" is exactly the answer that leaves a gap open |
| `parse_payment_list` requires a mapping with `items`, refusing a bare array | `providers/classification.py` | Stricter than `extract_entity_list`, on purpose. That function accepts an unnamed array because the payment-*links* envelope is unverified; this envelope is documented, so anything else is drift. Read permissively, a bare `[]` would report an empty window |
| The fake enforces the identical bounds | `tests/fakes/razorpay.py` | A fake looser than the real client would let the backfill ship with a window the provider rejects, and a rejected backfill is an outage nobody notices |

### Still unverified about this endpoint

| Item | Why it is not resolved here |
| --- | --- |
| Whether `from` / `to` filter on `created_at` or on last update | Not stated. So the backfill overlaps its windows instead of assuming an exact split. A payment near a boundary is then seen twice — which is harmless, because the dedup index makes re-ingestion idempotent — rather than risking being seen never |
| Whether the window is inclusive at both ends | Same mitigation: overlapping windows make it moot |
| Ordering of `items` | The backfill does not depend on order. It ingests every failed payment it sees and lets the dedup index settle repeats |

---

## 1. Payment-link listing freshness after create

**Spike:** `scripts/spikes/link_listing_freshness.py`
**Requirements:** R9.C15, R9.C17
**Design item:** Reconciliation Read

### What was measured

Create a Payment Link with a fresh, unique `reference_id`, then poll
`GET /v1/payment_links?reference_id=…` until it appears. Record the time from receiving
the create response to the link first showing up in the listing. Roughly 50 iterations.

Why it matters: Revora has no idempotency header for link creation, so the reconciliation
read *is* what makes creation exactly-once. If a link created moments ago is not yet
listed, an empty result reads as `FAILED`, a later attempt creates a second link, and a
customer is asked twice for the same money.

### Raw numbers

| Figure | Value |
| --- | --- |
| Date run | NOT YET RUN |
| Artifact | NOT YET RUN |
| Iterations completed | NOT YET RUN |
| Create failures excluded | NOT YET RUN |
| Min first-visible latency | NOT YET RUN |
| Median first-visible latency | NOT YET RUN |
| p95 first-visible latency | NOT YET RUN |
| Max first-visible latency | NOT YET RUN |
| Never visible within poll budget | NOT YET RUN |
| Poll budget used | NOT YET RUN |

### Conclusion

NOT YET RUN.

### Configuration this sets or confirms

| Parameter | Design default | Measured decision |
| --- | --- | --- |
| `EXECUTION_RECONCILIATION_INTERVAL` | 5 minutes **[ASSUMPTION]** | NOT YET RUN |
| `MAX_EXECUTION_RECONCILIATION_ATTEMPTS` | 6 attempts **[ASSUMPTION]** | NOT YET RUN |
| `PROVIDER_CALL_TIMEOUT` | 15 seconds **[ASSUMPTION]** | NOT YET RUN |

If the max first-visible latency is longer than `PROVIDER_CALL_TIMEOUT`, both
reconciliation parameters must go up, and the rule that an empty result means `FAILED`
**only on the final attempt** becomes essential rather than just careful. That rule stays
in place either way: 50 iterations against a test account cover the common case, not the
rare tail under production load.

---

## 2. Fetch-payment consistency lag relative to the capture webhook

**Spike:** `scripts/spikes/payment_read_lag.py`
**Requirements:** R10.C2, R10.C6, R10.C13
**Design item:** Authoritative Payment State Read

### What was measured

When a `payment.captured` webhook arrives, acknowledge it immediately and then call
`GET /v1/payments/{id}` right away, recording whether the read agrees that the money was
captured. If it disagrees, re-read until it agrees, so the disagreement gets a duration
attached.

Needs a manual browser step — the spike cannot complete a payment. Record the sample
size honestly; a small real sample here is worth more than a large invented one.

### Raw numbers

| Figure | Value |
| --- | --- |
| Date run | NOT YET RUN |
| Artifact | NOT YET RUN |
| Mode (`webhook` / `payment-id`) | NOT YET RUN |
| Capture signals observed | NOT YET RUN |
| Reads disagreeing with the webhook | NOT YET RUN |
| Disagreeing fraction | NOT YET RUN |
| Min / median / p95 / max convergence lag | NOT YET RUN |
| Reads that never converged within budget | NOT YET RUN |
| Read round-trip median | NOT YET RUN |

### Conclusion

NOT YET RUN.

### Configuration this sets or confirms

| Parameter | Design default | Measured decision |
| --- | --- | --- |
| `OUTCOME_READ_LATENCY_BOUND` | 60 seconds **[ASSUMPTION]** | NOT YET RUN |
| `PAYMENT_STATE_RECONCILIATION_INTERVAL` | 15 minutes **[ASSUMPTION]** | NOT YET RUN |
| `MAX_PAYMENT_STATE_READ_ATTEMPTS` | 5 attempts **[ASSUMPTION]** | NOT YET RUN |

If lag is common, `OUTCOME_READ_LATENCY_BOUND` is too tight and
`PAYMENT_STATE_RECONCILIATION_INTERVAL` must get shorter, because the conflict-hold path
would then be the normal path rather than the exception. Every recovered case would sit
in `WAITING_FOR_OUTCOME` for a full reconciliation interval before recovery is declared —
a quarter of an hour of the dashboard being wrong about money that has already arrived.

---

## 3. Duplicate `reference_id` on create

**Spike:** `scripts/spikes/duplicate_reference_id.py`
**Requirements:** R9.C3, R9.C5
**Design item:** Payment Links

### What was measured

Two create calls with the same `reference_id`, back to back. Record both HTTP statuses,
both response bodies word for word (including the provider's error object), and whether
two different `plink_` ids now exist — checked from the create responses and, separately,
from a listing query.

### Raw numbers

| Figure | Value |
| --- | --- |
| Date run | NOT YET RUN |
| Artifact | NOT YET RUN |
| Trials completed | NOT YET RUN |
| Trials where the second create was rejected | NOT YET RUN |
| Trials ending with two distinct `plink_` ids | NOT YET RUN |
| Verbatim error on rejection (`code` / `description` / `reason` / `field`) | NOT YET RUN |

### Conclusion

NOT YET RUN.

### Configuration this sets or confirms

None. This spike changes no parameter, and that is the point of running it.

- **A negative result changes nothing.** The design relies only on the documented
  ability to *query* by `reference_id`, never on a duplicate being rejected. A negative
  result confirms that not depending on rejection was the right call.
- **A positive result is defence in depth, and nothing more.** Record it here as a second
  barrier. It must not replace the reconciliation read: undocumented behaviour can change
  without warning, and it must not become the only thing standing between a customer and
  a second demand for the same money.

---

## 4. Server-side retry and payment-method-update capability

**Spike:** `scripts/spikes/retry_capability.py`
**Requirements:** R6.C9
**Design item:** Two Candidate_Actions Have No Verified Provider Capability

### What was measured

Three things, in decreasing order of strength, and the spike is explicit about which is which:

1. A verified read of a `failed` payment, recording its status and `error_*` fields.
2. Probes of candidate capabilities against paths that are **not** in the design's verified
   surface, recording the provider's exact response. A 404 from a guessed path is weak
   evidence: it cannot tell "no such capability" apart from "wrong URL".
3. A manual dashboard check of whether Subscriptions or Optimizer is enabled on the
   account — the resolving action the design actually names.

### Raw numbers

| Figure | Value |
| --- | --- |
| Date run | NOT YET RUN |
| Artifact | NOT YET RUN |
| Payment id used, and its status | NOT YET RUN |
| `error_reason` / `error_step` / `error_source` on that payment | NOT YET RUN |
| Retry probe — status and verbatim error | NOT YET RUN |
| Payment-mutation probe — status and verbatim error | NOT YET RUN |
| Subscriptions reachable | NOT YET RUN |
| Dashboard: Subscriptions enabled | NOT YET RUN |
| Dashboard: Optimizer enabled | NOT YET RUN |

### Conclusion

NOT YET RUN.

### Configuration this sets or confirms

| Item | Design position | Measured decision |
| --- | --- | --- |
| Executable action set | `DO_NOTHING`, `WAIT`, `PAYMENT_LINK`, `CUSTOMER_MESSAGE`, `HUMAN_ESCALATION` | NOT YET RUN |
| `RETRY`, `DELAYED_RETRY` | `UNAVAILABLE` at simulation time | NOT YET RUN |
| `PAYMENT_METHOD_UPDATE` | `UNAVAILABLE` at simulation time | NOT YET RUN |

A positive result brings back `RETRY` and `PAYMENT_METHOD_UPDATE` as executable actions,
which changes the eligibility table in `revora/domain/actions.py` and adds execution
paths to task 20. Do not make that change on a probe artifact alone — an action that
moves money needs a documented, idempotent capability, not a URL that happened to
return 200.

---

## Assumptions the spikes themselves rely on

The scripts are held to the same standard as the design: nothing invented, and anything
outside the verified surface is named. Each script prints these and writes them into its
artifact under `unverified_assumptions`. Brought together here:

| Assumption | Used by | What needs checking |
| --- | --- | --- |
| API host `https://api.razorpay.com` | all four | design.md gives paths without a host. Overridable with `--base-url` |
| Key-id prefix `rzp_test_` marks test mode | all four (the live-credential guard) | The guard fails closed, so a wrong prefix convention makes it refuse to run rather than run against live keys |
| Collection envelope `count` / `items` on fetch-all Payment Links | 1, 3 | design.md verifies that querying by `reference_id` is supported, not the envelope field names |
| Webhook envelope: top-level `event`, and `payload.payment.entity` | 2 | design.md verifies event names and the entity's `error_*` fields, not the envelope |
| Any minimum permitted `expire_by` offset | 1, only when `--expire-after-seconds` is used | design.md verifies only the six-month ceiling |
| Candidate retry / mutation paths | 4 | Not documented anywhere in the verified surface. Present to capture exact errors, not to claim these paths exist |

---

## After filling this in

1. Attach the artifact filenames from `docs/spike-results/` to each section, so a number
   in this document can be traced to the run that produced it.
2. Carry any changed value into the configuration seed (task 5.6). Where a default
   survived measurement, say so plainly in the table above — "confirmed by
   measurement" and "never checked" must not look the same in this document.
3. Where a spike came back `INCONCLUSIVE`, leave the design's `[ASSUMPTION]` in place and
   leave the row saying so. An unmeasured parameter that is documented as unmeasured is a
   known risk; one recorded as confirmed is a lie in a document whose only value is that
   it contains none.
