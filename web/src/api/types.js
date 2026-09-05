/**
 * The wire contract: the status sentinels, the runtime guards, and JSDoc for every shape.
 *
 * Under TypeScript this file was a wall of `interface` declarations and the compiler refused an
 * unhandled absent-value arm. In plain JavaScript nothing refuses it, so what used to be a type
 * declaration is now **three things that work at runtime**:
 *
 * 1. The status constants, so no component compares against a string literal it might mistype.
 * 2. `isPresent` and `isAbsentMarker`, which are the actual branch every renderer takes.
 * 3. JSDoc `@typedef`s, which give editor completion and document the shapes without pretending to
 *    enforce them.
 *
 * **Why the shapes are unions at all.** The API's read models are `dict[str, object]` by design:
 * their shape *varies with what has been recorded*, because a case with no policy decision returns a
 * marker where a decision would be, and those are genuinely different things. Collapsing that into
 * "a field that might be null" is what lets a marker get rendered as a value by accident.
 */

/** A real value. Named on the wire so a client branches on one field rather than on `== null`. */
export const PRESENT = 'PRESENT'

/** The pipeline has not produced this yet. Carries the case state, which is the explanation. */
export const NOT_YET_RECORDED = 'NOT_YET_RECORDED'

/** This figure was asked for and could not be computed. Applies to one figure, not the response. */
export const DATA_UNAVAILABLE = 'DATA_UNAVAILABLE'

/**
 * @typedef {object} MoneyPresent
 * @property {'PRESENT'} status
 * @property {number} minor
 *   Integer minor units, for sorting and CSV export. **Arithmetic on this is a lint error** — see
 *   `eslint.config.js`. The server has already done the division, chosen the symbol and applied the
 *   grouping convention.
 * @property {string} currency
 * @property {string} formatted
 *   What gets rendered, always, by `<Money>` and nothing else. INR groups in lakhs and crores
 *   (`₹12,34,567.89`), which no browser default produces.
 */

/**
 * @typedef {object} NotYetRecorded
 * @property {'NOT_YET_RECORDED'} status
 * @property {string} case_state
 *   Load-bearing, and why this is not simply `null`. No recommendation on a `DETECTED` case means
 *   the pipeline has not got there; on a `BLOCKED` case it means policy stopped it. Same absence,
 *   opposite meanings, and a reader who cannot tell them apart reads the first as a bug and the
 *   second as working.
 * @property {string} detail
 */

/**
 * @typedef {object} DataUnavailable
 * @property {'DATA_UNAVAILABLE'} status
 * @property {string} figure
 * @property {string} detail
 */

/** @typedef {NotYetRecorded | DataUnavailable} Absent */
/** @typedef {MoneyPresent | Absent} Money */

/**
 * A rate or probability, always a string on the wire.
 *
 * `UNDEFINED` is one of its values: a rate with a zero denominator does not exist, and emitting it
 * as a JSON number would force the sentinel to be `null` (renders as an empty cell) or `0` (renders
 * as a false measurement). A string forces the consumer to look.
 *
 * @typedef {{ status: 'PRESENT', value: string } | Absent} Rate
 */

/**
 * Whether a figure carries a real value.
 *
 * @param {{ status?: string } | null | undefined} field
 * @returns {boolean}
 */
export function isPresent(field) {
  return field != null && field.status === PRESENT
}

/**
 * Whether a value is an absent-value marker rather than a plain enum string.
 *
 * Several list columns are "a string when recorded, a marker when not" — `risk_cause`,
 * `selected_action`, `policy_decision`, `outcome_classification`. This is the check that tells them
 * apart, and it is why those cells never render empty.
 *
 * @param {unknown} value
 * @returns {boolean}
 */
export function isAbsentMarker(value) {
  if (typeof value !== 'object' || value === null) return false
  const status = value.status
  return status === NOT_YET_RECORDED || status === DATA_UNAVAILABLE
}

/**
 * The incremental-revenue figure, which is the whole product in one field.
 *
 * Three arms. `NOT_ESTABLISHED` is a sentinel string, never `null` and never `0`: an empty cell
 * reads as "nothing recovered" and a zero reads as "we measured, and it was nothing". The claim
 * being made is neither — it is "no completed, adequately powered experiment licenses a causal
 * claim here".
 *
 * @typedef {(
 *   | { status: 'NOT_ESTABLISHED', value: string, refusal_codes: string[], detail: string }
 *   | { status: 'ESTABLISHED', amount: MoneyPresent, experiment_id: string | null,
 *       control_case_count: number | null, treatment_case_count: number | null,
 *       lift: string | null, lift_interval: string | null }
 *   | DataUnavailable
 * )} Incremental
 */

/**
 * @typedef {object} CaseSummary
 * @property {string} case_id
 * @property {string} state
 * @property {string} detected_at
 * @property {string} window_end_at
 * @property {Money} payment_amount
 * @property {string} provider_payment_id
 * @property {string | null} customer_contact_masked
 * @property {string | Absent} risk_cause
 * @property {string | Absent} selected_action
 * @property {string | Absent} executed_action
 * @property {string | Absent} policy_decision
 * @property {Money} recovered_amount
 * @property {string | Absent} outcome_classification
 * @property {string | null} human_owner_user_id
 * @property {string} provenance
 */

/**
 * One candidate action, priced. Excluded ones are included — the case detail *is* the comparison,
 * not the winner.
 *
 * @typedef {object} Candidate
 * @property {string} action
 * @property {boolean} is_executable
 * @property {string} incremental_probability
 * @property {Money} expected_incremental_revenue
 * @property {Money} action_cost
 * @property {Money} risk_cost
 * @property {Money} customer_cost
 * @property {Money} total_cost  Summed server-side from the three above, so no client adds money.
 * @property {Money} net_recovery_value
 * @property {boolean} excluded
 * @property {string | null} exclusion_reason
 * @property {number | null} rank
 */

/**
 * Why doing nothing was the answer. Present only when the selection was `DO_NOTHING` or `WAIT`.
 *
 * All three thresholds are included even though usually one decided, because a merchant asking
 * "why not?" is asking about the whole comparison — showing only the failing bound invites "so
 * lower it", and the answer to that is the other two.
 *
 * @typedef {object} Refusal
 * @property {string} reason
 * @property {string} explanation  Server-composed prose. A client that worded this would eventually
 *   word it as a failure, and "we chose not to act" being read as "we could not act" is the
 *   misreading this product can least afford.
 * @property {string | NotYetRecorded} baseline_probability
 * @property {string | null} incremental_probability
 * @property {Money | null} net_recovery_value
 * @property {{ min_net_value_threshold: number, min_incremental_probability: string,
 *   max_cost_to_value_ratio: string, high_baseline_threshold: string }} compared_thresholds
 */

/**
 * One policy check result.
 *
 * `outcome` is a plain string rather than a closed set. A closed set would make the renderer
 * exhaustive over four values, and a server that gained a fifth outcome would render it as an
 * unstyled blank — the failure this whole surface exists to prevent. As a string it renders whatever
 * it is given, visibly. `NOT_RECORDED` is a real value: a check with no recorded result is shown as
 * such rather than omitted, because omitting it would shorten the list and a reader used to twelve
 * rows seeing eleven will not notice which one went.
 *
 * @typedef {object} PolicyCheck
 * @property {number} check_order
 * @property {string} check_id
 * @property {string} outcome
 * @property {string | null} detail
 */
