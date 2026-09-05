/**
 * Every figure on every screen goes through one of these. That is the whole design.
 *
 * The rules being enforced (R14.C12, R14.C15, R14.C16) are not display preferences; each one blocks
 * a specific false statement about money:
 *
 * * `<Money>` renders `formatted` and touches nothing else. It has no access to a currency symbol
 *   table, no divisor and no rounding — so it *cannot* produce a figure that disagrees with the
 *   server's, because it has no way to compute one.
 *
 * * An absent value renders as a marker naming what is absent and which case state explains it.
 *   Never zero, never a dash. Substituting zero for an unrecorded amount is a false financial
 *   statement, not a display shortcut, and a dash is worse because a dash looks deliberate.
 *
 * * A figure that could not be computed says so and names itself, and only that figure degrades.
 *
 * These are components rather than helper functions on purpose. A helper returning a string can be
 * concatenated, interpolated and quietly turned back into a number; a component returning JSX
 * cannot.
 */

import { DATA_UNAVAILABLE, PRESENT, isAbsentMarker } from '../api/types'

/**
 * The marker for something the pipeline has not recorded yet, or a figure that timed out.
 *
 * Renders the case state, because the state *is* the explanation. "No policy decision" on a
 * `DETECTED` case means the pipeline has not got there; on a `BLOCKED` case it means policy stopped
 * it. Both are the same absence and they mean opposite things.
 *
 * Deliberately styled as neutral information, not as a warning. A not-yet is normal operation, and a
 * red indicator would teach an operator that a working system is broken.
 *
 * @param {{ marker: import('../api/types').Absent }} props
 */
export function AbsentValue({ marker }) {
  if (marker.status === DATA_UNAVAILABLE) {
    return (
      <span className="absent absent--unavailable" title={marker.detail}>
        <span className="absent__tag">DATA UNAVAILABLE</span>
        <span className="absent__what">{marker.figure}</span>
      </span>
    )
  }
  return (
    <span className="absent absent--pending" title={marker.detail}>
      <span className="absent__tag">NOT YET RECORDED</span>
      <span className="absent__what">case is {marker.case_state}</span>
    </span>
  )
}

/**
 * A currency figure.
 *
 * Renders `value.formatted` verbatim. `minor` is never read here — not to derive the string, not to
 * check it, not to decide a sign. The server chose the symbol, the minor-unit digits and the digit
 * grouping (INR groups in lakhs and crores), and there is exactly one implementation of those rules.
 *
 * @param {{ value: import('../api/types').Money, emphasis?: boolean }} props
 */
export function Money({ value, emphasis = false }) {
  if (value == null) return null
  if (value.status !== PRESENT) return <AbsentValue marker={value} />
  return (
    <span className={emphasis ? 'money money--emphasis' : 'money'} data-minor={value.minor}>
      {value.formatted}
    </span>
  )
}

/**
 * A rate or probability.
 *
 * Arrives as a string and is rendered as one. `UNDEFINED` is a legitimate value — a rate with a zero
 * denominator does not exist — and it passes through as that word rather than becoming an empty cell
 * or a zero, either of which would read as a measurement.
 *
 * @param {{ value: import('../api/types').Rate }} props
 */
export function Rate({ value }) {
  if (value == null) return null
  if (value.status !== PRESENT) return <AbsentValue marker={value} />
  if (value.value === 'UNDEFINED') {
    return (
      <span className="rate rate--undefined" title="No denominator: this rate does not exist yet.">
        UNDEFINED
      </span>
    )
  }
  return <span className="rate">{value.value}</span>
}

/**
 * A field that is either an enum value or an absent marker.
 *
 * The list columns are full of these — `risk_cause`, `selected_action`, `policy_decision` — and each
 * one is a string when recorded and a marker when not. Branching in every cell is what this replaces,
 * and it is why the case table has no empty cells anywhere.
 *
 * `label`, when supplied, is a **server-chosen** human label and is rendered in place of the
 * derived one, with the stored enumeration member kept beside it (R26.C14). That pairing is the
 * requirement: `DO_NOTHING` and `WAIT` both arrive labelled "Waiting and watching", and both still
 * show which of the two was actually recorded. The label is never composed here — a client that
 * mapped two enum values onto one string would be a second vocabulary, free to drift from the one
 * the API and the timeline share, and the drift would be in the direction of rendering restraint as
 * an ending.
 *
 * @param {{ value: string | import('../api/types').Absent, label?: string | null }} props
 */
export function Enum({ value, label = null }) {
  if (value == null) return null
  if (isAbsentMarker(value)) return <AbsentValue marker={value} />
  if (label != null) {
    return (
      <span className="enum">
        {label} <span className="enum__member">{String(value)}</span>
      </span>
    )
  }
  return <span className="enum">{humanise(String(value))}</span>
}

/**
 * `PAYMENT_LINK` → `Payment link`. Presentation only.
 *
 * @param {string} value
 * @returns {string}
 */
export function humanise(value) {
  const lower = String(value).replaceAll('_', ' ').toLowerCase()
  return lower.charAt(0).toUpperCase() + lower.slice(1)
}

/**
 * A provenance or causality label.
 *
 * Labels are not decoration. `CAUSALITY_NOT_ESTABLISHED` sitting beside an observed recovery figure
 * is the difference between a true statement and an overstatement, and `SYNTHETIC` beside a figure
 * derived from generated data is the difference between a demo and a claim. So they render as
 * prominent, legible text rather than as an icon.
 *
 * @param {{ text: string }} props
 */
export function Label({ text }) {
  const kind = LABEL_KINDS[text] ?? 'neutral'
  return (
    <span className={`label label--${kind}`} title={LABEL_EXPLANATIONS[text] ?? text}>
      {text.replaceAll('_', ' ')}
    </span>
  )
}

const LABEL_KINDS = {
  CAUSALITY_NOT_ESTABLISHED: 'caution',
  UNCALIBRATED: 'caution',
  UNVALIDATED_BASELINE: 'caution',
  CALIBRATION_UNVERIFIED: 'caution',
  COST_SPLIT_NOT_MEASURED: 'caution',
  SYNTHETIC: 'synthetic',
  RECOVERY_GROSS_OF_REFUNDS: 'neutral',
}

/**
 * Plain-language expansions, on hover.
 *
 * Kept client-side because they explain a *label*, which is a fixed vocabulary. The refusal
 * explanations — the ones about a specific case's numbers — come from the server for the opposite
 * reason: a client that composed those would eventually word one as a failure.
 */
const LABEL_EXPLANATIONS = {
  CAUSALITY_NOT_ESTABLISHED:
    'These recoveries were observed after Revora acted. Nothing here shows that they happened ' +
    'because Revora acted — that needs a completed holdout experiment.',
  SYNTHETIC: 'Derived from generated data. Not a statement about real payments.',
  RECOVERY_GROSS_OF_REFUNDS:
    'Refunds are recorded on every read and are not subtracted from these figures.',
  UNCALIBRATED: 'This estimate has not been checked against outcomes.',
  COST_SPLIT_NOT_MEASURED:
    'This row was priced before financial and communication cost were estimated separately. ' +
    'The whole recorded cost is in the financial figure and the communication figure is a zero ' +
    'nothing measured — not a measurement that it was free.',
  UNVALIDATED_BASELINE: 'Nothing has yet verified the do-nothing probability this was built on.',
  CALIBRATION_UNVERIFIED: 'A calibration check has not been run for this segment.',
}
