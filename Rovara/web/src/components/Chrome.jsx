/** Shared shell pieces: state badges, panels, load and error states, and the timestamp renderer. */

import { ApiError } from '../api/client'
import { humanise } from './Figure'

/**
 * A case state, coloured by *kind* rather than by good and bad.
 *
 * The colouring is a product decision, not a palette choice. `BLOCKED` and `ESCALATED` are **not**
 * failures — one means policy correctly stopped an action, the other means a human was asked — and
 * showing them in the same red as `FAILED` teaches an operator that a working system is broken.
 *
 * `label` is the server's wording when the response carries one (R26.C14); `humanise` is the
 * fallback for the surfaces that have not been given one yet. The member name stays in `title`
 * either way, so the stored value is always readable beside the label.
 *
 * @param {{ state: string, label?: string | null }} props
 */
export function StateBadge({ state, label = null }) {
  return (
    <span className={`state state--${STATE_KIND[state] ?? 'active'}`} title={state}>
      {label ?? humanise(state)}
    </span>
  )
}

/**
 * Which states read as an ending.
 *
 * `POLICY_CHECK` is deliberately absent and that absence is R30.C13's client half: a case resting
 * there has recorded a decision, not reached a conclusion, and it falls through to `'active'`. A
 * case that chose restraint is *the* case this mapping must not group with the endings — it was the
 * whole defect R30 exists to fix, and colouring it as ended would restate that defect on the screen
 * after it was fixed in the pipeline.
 */
const STATE_KIND = {
  RECOVERED: 'recovered',
  BLOCKED: 'ended',
  ESCALATED: 'ended',
  STOPPED: 'ended',
  EXPIRED: 'ended',
  FAILED: 'failed',
}

export { STATE_KIND }

/**
 * @param {{ title: string, subtitle?: string, aside?: import('react').ReactNode,
 *           children: import('react').ReactNode }} props
 */
export function Panel({ title, subtitle, aside, children }) {
  return (
    <section className="panel">
      <header className="panel__head">
        <div>
          <h2 className="panel__title">{title}</h2>
          {subtitle !== undefined && <p className="panel__subtitle">{subtitle}</p>}
        </div>
        {aside !== undefined && <div className="panel__aside">{aside}</div>}
      </header>
      <div className="panel__body">{children}</div>
    </section>
  )
}

/** @param {{ what: string }} props */
export function Loading({ what }) {
  return (
    <p className="status status--loading" role="status">
      Loading {what}…
    </p>
  )
}

/**
 * An error, with its correlation id.
 *
 * The id is shown because it is the one thing that makes the failure findable: it is the same id the
 * audit records written during the request carry, so an operator reporting a problem can be answered
 * with one query rather than a search through a time window.
 *
 * @param {{ error: Error, what: string }} props
 */
export function Failure({ error, what }) {
  const correlation = error instanceof ApiError ? error.correlationId : null
  return (
    <div className="status status--error" role="alert">
      <p>
        Could not load {what}: {error.message}
      </p>
      {correlation !== null && (
        <p className="status__correlation">
          Reference <code>{correlation}</code> — quote this and the exact time; the audit records for
          this request carry the same id.
        </p>
      )}
    </div>
  )
}

/**
 * `Intl.DateTimeFormat`, not `toLocaleString`.
 *
 * Two reasons. The lint rule forbids `toLocaleString` outright — it is the method somebody reaches
 * for to format an amount, and the rule would rather reject a legitimate date than let a currency
 * figure through. And it is constructed once at module scope: `Intl` constructors are expensive
 * enough to matter in a table of a hundred rows each holding two timestamps.
 */
const TIMESTAMP_FORMAT = new Intl.DateTimeFormat(undefined, {
  year: 'numeric',
  month: 'short',
  day: '2-digit',
  hour: '2-digit',
  minute: '2-digit',
})

/**
 * A UTC instant, rendered in the reader's zone with the ISO string kept in `title`.
 *
 * Every timing evaluation in Revora is done against stored UTC, so a merchant reconciling a
 * dashboard reading against an audit record needs the unambiguous form available.
 *
 * @param {{ iso: string }} props
 */
export function When({ iso }) {
  const parsed = new Date(iso)
  if (Number.isNaN(parsed.getTime())) return <span className="when">{iso}</span>
  return (
    <time className="when" dateTime={iso} title={iso}>
      {TIMESTAMP_FORMAT.format(parsed)}
    </time>
  )
}

/** @param {{ children: import('react').ReactNode }} props */
export function Empty({ children }) {
  return <p className="status status--empty">{children}</p>
}

/**
 * One label-and-value pair inside a `<dl className="facts">`.
 *
 * Lives here rather than being redefined in four route files, which is what happened under the
 * TypeScript version.
 *
 * @param {{ label: string, children: import('react').ReactNode }} props
 */
export function Fact({ label, children }) {
  return (
    <div className="fact">
      <dt>{label}</dt>
      <dd>{children}</dd>
    </div>
  )
}
