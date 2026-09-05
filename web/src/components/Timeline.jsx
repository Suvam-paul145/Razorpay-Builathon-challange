/**
 * One case, as nine ordered stages and a sentence each (R26.C1–C13).
 *
 * The case detail view below this component is faithful to ten requirements and unreadable under
 * time pressure: ten sections, six tables, and no answer to "what happened?" that does not involve
 * reading all of them. This adds no data — every stage, status and sentence here is projected by the
 * server from the case's own Audit_Records — and its only job is to be readable in two minutes.
 *
 * Four things about the implementation, each of them a rule rather than a style.
 *
 * **No money is computed, formatted or touched here.** Currency figures arrive from the server as
 * *already-formatted strings* and are rendered through the shared `<Money>` in `Figure.jsx`. Note
 * what the timeline's wire deliberately does **not** carry: minor units. Every other read model sends
 * `minor` alongside `formatted` for sorting and CSV export, and `eslint.config.js` forbids arithmetic
 * on it; this one sends no integer at all, so client-side arithmetic on a timeline figure is not
 * lint-forbidden but *unexpressible*. `serverFigure` below is what adapts the string to `<Money>`'s
 * envelope, and it exists so there is still exactly one component that renders an amount.
 *
 * **An absent value says what is absent.** Where the projection could not be read within
 * `TIMELINE_QUERY_TIMEOUT`, the panel renders the server's marker through `<AbsentValue>` — the same
 * component the rest of the dashboard uses — and shows *no stages at all*. Not nine `UPCOMING` rows,
 * not a zero, not a status invented to fill a line (R26.C10). A fabricated row here would be worse
 * than an empty panel, because a stage list is read as a history.
 *
 * **A gap in the audit sequence is reported, not smoothed over.** Sequence numbers are allocated
 * inside the transaction that writes the record, so a gap means the allocation was bypassed rather
 * than that a record is late. The banner names the missing numbers and the stages still render, with
 * none of them claiming `DONE` on the strength of an absent record — that last guarantee is the
 * server's and this component only surfaces it (R26.C11).
 *
 * **Status is conveyed by a programmatic label, never by colour alone.** Every stage carries its
 * status as visible text and in the `<li>`'s accessible name, the order comes from native `<ol>`
 * semantics with a visible ordinal beside it, and the disclosure control is a real `<button>` — so it
 * is focusable, operable with Enter and Space, and carries `aria-expanded` without any of that being
 * reimplemented. Colour is added on top of text that already says the same thing.
 *
 * **On the accessibility claim, precisely.** WCAG 2.1 Level AA is the target standard for keyboard
 * operation, contrast, focus visibility and the programmatic labelling of stage status and order
 * (R26.C13). This component implements those four with native semantics. It does **not** constitute a
 * conformance claim: full WCAG 2.1 AA conformance validation requires manual testing with assistive
 * technologies and expert accessibility review, and neither has been done here.
 */

import { useState } from 'react'

import { useCaseTimeline } from '../api/queries'
import { PRESENT } from '../api/types'
import { Failure, Loading, Panel } from './Chrome'
import { AbsentValue, Enum, Money, humanise } from './Figure'

/**
 * Which stage fields are currency figures.
 *
 * A declared set rather than a heuristic on the key name. A guess like "any key containing `amount`
 * or `cost`" would silently start rendering a future non-money field through `<Money>`, and a
 * silently mis-rendered figure is the failure this whole arrangement exists to prevent. Adding a
 * money field to the projection therefore requires adding it here, which is the point.
 */
const MONEY_FIELDS = new Set([
  'payment_amount',
  'cheapest_total_action_cost',
  'net_recovery_value',
  'runner_up_net_recovery_value',
  'recovered_amount',
])

/**
 * Which stage fields are instants, so they render as instants rather than as raw ISO text.
 */
const INSTANT_FIELDS = new Set(['detected_at', 'next_review_at', 'submitted_at', 'instant'])

/**
 * How each stage's field keys read as labels. Presentation only; the server owns the vocabulary.
 *
 * Keys absent from this map fall through to `humanise`, which is correct rather than a gap: the
 * projection's field names are already readable snake_case, and a map that had to be total would be
 * a second place every field is named.
 */
const FIELD_LABELS = {
  payment_amount: 'Amount at risk',
  provider_payment_id: 'Provider payment',
  detected_at: 'Detected',
  cause: 'Cause',
  confidence: 'Confidence',
  method: 'Method',
  evidence_source: 'Read from',
  baseline_recovery_probability: 'Probability without acting',
  uncertainty_interval: 'Interval',
  priced_count: 'Options priced',
  unavailable_count: 'Marked unavailable',
  cheapest_total_action_cost: 'Cheapest available option',
  selected_action: 'Chosen',
  net_recovery_value: 'Worth',
  selection_reason: 'Because',
  runner_up_action: 'Runner-up',
  runner_up_net_recovery_value: 'Runner-up worth',
  decision_cycle_count: 'Decision cycle',
  max_recovery_attempts: 'Attempt bound',
  next_review_at: 'Next review',
  verdict: 'Verdict',
  primary_reason: 'Primary reason',
  recovered_amount: 'Recovered',
  outcome_classification: 'Classification',
  terminal_state: 'Ended as',
  terminal_reason: 'Ended because',
}

/**
 * Plain-language wording for each status, for the accessible name and the visible chip.
 *
 * The four statuses are four different claims and the wording keeps them apart. `Skipped` and
 * `Not yet reached` are both "no completing record"; one means the case went past this step and the
 * reason is on the record, the other means it has not got there. A reader shown one where the other
 * holds draws the opposite conclusion about whether anything further will happen.
 */
const STATUS_WORDS = {
  DONE: 'Done',
  IN_PROGRESS: 'In progress',
  UPCOMING: 'Not yet reached',
  SKIPPED: 'Skipped',
}

/**
 * Adapt a server-formatted currency string to `<Money>`'s envelope.
 *
 * The timeline's wire carries the formatted string and no minor units, so there is no integer to put
 * in `minor` and the `data-minor` attribute is simply absent on these elements. That is deliberate
 * and it is the stronger version of the rule the other read models rely on: elsewhere `minor` travels
 * for sorting and lint forbids arithmetic on it, whereas here a component that wanted to divide a
 * timeline figure by a hundred has nothing to divide.
 *
 * @param {string} formatted
 * @returns {{ status: 'PRESENT', formatted: string }}
 */
function serverFigure(formatted) {
  return { status: PRESENT, formatted }
}

/**
 * Whether a value is one of the projection's `{ label, member }` pairs (R26.C14).
 *
 * @param {unknown} value
 * @returns {boolean}
 */
function isLabelled(value) {
  return (
    typeof value === 'object' &&
    value !== null &&
    typeof value.label === 'string' &&
    typeof value.member === 'string'
  )
}

/**
 * The timeline panel: the first element of the case detail view (R26.C12).
 *
 * @param {{ caseId: string }} props
 */
export function Timeline({ caseId }) {
  const query = useCaseTimeline(caseId)

  return (
    <Panel
      title="What happened, in order"
      subtitle="Nine stages, projected from this case's own audit records. Nothing here is new data."
    >
      {query.isPending && <Loading what="the timeline" />}
      {query.isError && <Failure error={query.error} what="the timeline" />}
      {query.isSuccess && !query.data.available && (
        <div className="timeline-unavailable" role="status">
          {/* R26.C10. The server's own marker, naming this case. No stage is shown, because none
              was projected — and a substituted row would read as a history that did not happen. */}
          <AbsentValue marker={query.data.unavailable} />
          <p className="footnote">
            The audit trail for this case could not be read in time. Nothing above has been
            substituted with a status or a zero. The sections below are unaffected.
          </p>
        </div>
      )}
      {query.isSuccess && query.data.available && (
        <TimelineBody timeline={query.data.timeline} />
      )}
    </Panel>
  )
}

/** @param {{ timeline: Record<string, any> }} props */
function TimelineBody({ timeline }) {
  const sequence = timeline.audit_sequence
  return (
    <>
      {!sequence.complete && <GapBanner sequence={sequence} />}

      {/* Native `<ol>`. The ordinal beside each stage is decoration on top of semantics a screen
          reader already has, not a substitute for them. */}
      <ol className="timeline" aria-label="Recovery case stages, in order">
        {timeline.stages.map((stage) => (
          <TimelineStage key={stage.stage} stage={stage} total={timeline.stage_count} />
        ))}
      </ol>

      {timeline.ai_explanation !== null && (
        <AiExplanation
          text={timeline.ai_explanation}
          label={timeline.ai_explanation_label}
        />
      )}
    </>
  )
}

/**
 * R26.C11. The audit sequence is incomplete, and these are the numbers that are missing.
 *
 * `role="alert"` rather than `role="status"`, and that is a judgement about what this means. A
 * missing sequence number is not a slow record: numbers are allocated by incrementing the case row's
 * counter inside the transaction that writes the record, so a rolled-back transaction rolls the
 * number back with it and a hole means the allocation was bypassed. That is worth interrupting a
 * reader for, because everything below it is being read as a complete history.
 *
 * @param {{ sequence: Record<string, any> }} props
 */
function GapBanner({ sequence }) {
  return (
    <div className="timeline-banner" role="alert">
      <p className="timeline-banner__head">
        <strong>This case&rsquo;s audit sequence is incomplete.</strong> Missing{' '}
        {sequence.missing.map((number, index) => (
          <span key={number}>
            {index > 0 && ', '}
            <code>{number}</code>
          </span>
        ))}
        .
      </p>
      <p className="timeline-banner__detail">{sequence.detail}</p>
      <p className="timeline-banner__detail">
        {sequence.record_count} records read, numbered {sequence.first_seq} to{' '}
        {sequence.last_seq}
        {!sequence.starts_at_one && ' — and the sequence does not start at 1'}. Every stage below
        is projected from the records that are present; none is shown as done on the strength of an
        absent record.
      </p>
    </div>
  )
}

/**
 * One stage: its ordinal, its status, its two sentences and its field set behind a disclosure.
 *
 * The disclosure is a real `<button>`, not a `<div onClick>`. That is where the keyboard operation
 * and the focus ring come from — Enter and Space, tab order and `:focus-visible` all work because
 * the element is the one the platform already defines for this, and none of it is reimplemented.
 *
 * `aria-expanded` and `aria-controls` tie the control to the region it reveals, so the state of the
 * disclosure is available programmatically rather than only visible in a rotated caret.
 *
 * @param {{ stage: Record<string, any>, total: number }} props
 */
function TimelineStage({ stage, total }) {
  const [open, setOpen] = useState(false)
  const status = stage.status
  const word = STATUS_WORDS[status] ?? humanise(status)
  const panelId = `timeline-fields-${stage.stage}`
  const entries = Object.entries(stage.fields ?? {}).filter(
    ([, value]) => value !== null && value !== undefined,
  )

  return (
    <li className={`timeline__item timeline__item--${status.toLowerCase()}`}>
      <button
        type="button"
        className="timeline__control"
        aria-expanded={open}
        aria-controls={panelId}
        onClick={() => setOpen((current) => !current)}
      >
        {/* The accessible name carries the position, the stage and the status as words. Everything a
            sighted reader gets from the ordinal, the position in the list and the colour of the chip
            is in here as text, which is R26.C13's "programmatic labelling ... rather than by colour
            alone" stated as one string. */}
        <span className="sr-only">
          Stage {stage.order} of {total}: {humanise(stage.stage)}, {word}.
        </span>
        <span aria-hidden="true" className="timeline__ordinal">
          {stage.order}
        </span>
        <span aria-hidden="true" className="timeline__name">
          {humanise(stage.stage)}
        </span>
        <span aria-hidden="true" className={`timeline__status timeline__status--${status.toLowerCase()}`}>
          {word}
        </span>
      </button>

      <div className="timeline__body">
        {/* A DONE stage has an instant and two sentences, and a stage that is not DONE has neither —
            the server does not render a sentence for a stage with no completing record, so there is
            nothing here to guard against. */}
        {stage.instant !== null && (
          <p className="timeline__instant">
            <time dateTime={stage.instant}>{stage.instant}</time>
          </p>
        )}
        {stage.decision_sentence !== null && (
          <p className="timeline__decision">{stage.decision_sentence}</p>
        )}
        {stage.evidence_sentence !== null && (
          <p className="timeline__evidence">{stage.evidence_sentence}</p>
        )}
        {stage.skip_reason !== null && (
          <p className="timeline__skip">
            {/* R26.C5. The reason is an audit event type rather than prose, because it is named from
                the persisted records — so it is shown as the record it is, greppable against the
                audit trail below. */}
            Skipped, recorded as <code>{stage.skip_reason}</code>.
          </p>
        )}

        <div className="timeline__fields" hidden={!open} id={panelId}>
          {entries.length === 0 ? (
            <p className="muted">No fields recorded for this stage.</p>
          ) : (
            <dl className="facts facts--tight">
              {entries.map(([key, value]) => (
                <StageField key={key} name={key} value={value} />
              ))}
            </dl>
          )}
        </div>
      </div>
    </li>
  )
}

/**
 * One field of a stage's R26.C4 field set.
 *
 * Four shapes, dispatched on the value rather than on the key: a money string, a `{ label, member }`
 * pair, a list of sub-records, and everything else. Dispatching on shape means a field added to the
 * projection renders correctly without an entry here — and `MONEY_FIELDS` is the one case that
 * cannot be inferred from shape, because a formatted amount and a probability are both strings.
 *
 * @param {{ name: string, value: unknown }} props
 */
function StageField({ name, value }) {
  const label = FIELD_LABELS[name] ?? humanise(name)

  if (MONEY_FIELDS.has(name) && typeof value === 'string') {
    return (
      <div className="fact">
        <dt>{label}</dt>
        <dd>
          <Money value={serverFigure(value)} />
        </dd>
      </div>
    )
  }

  if (isLabelled(value)) {
    // R26.C14. The server's label with the stored member beside it, through the one component that
    // renders that pairing — never composed here.
    return (
      <div className="fact">
        <dt>{label}</dt>
        <dd>
          <Enum value={value.member === '' ? value.label : value.member} label={value.label} />
        </dd>
      </div>
    )
  }

  if (Array.isArray(value)) {
    return (
      <div className="fact fact--wide">
        <dt>{label}</dt>
        <dd>
          {value.length === 0 ? (
            <span className="muted">none recorded</span>
          ) : (
            <ul className="timeline__sublist">
              {value.map((item, index) => (
                <li key={index}>
                  <SubRecord item={item} />
                </li>
              ))}
            </ul>
          )}
        </dd>
      </div>
    )
  }

  if (INSTANT_FIELDS.has(name) && typeof value === 'string') {
    return (
      <div className="fact">
        <dt>{label}</dt>
        <dd>
          <time dateTime={value}>{value}</time>
        </dd>
      </div>
    )
  }

  return (
    <div className="fact">
      <dt>{label}</dt>
      <dd>{String(value)}</dd>
    </div>
  )
}

/**
 * One element of a stage's list field: a signal, an execution attempt, or a review.
 *
 * Rendered generically from its own keys, because the three shapes have nothing in common and three
 * bespoke renderers would be three places the server's key names are restated. Labelled pairs go
 * through `<Enum>` for the same reason they do above.
 *
 * @param {{ item: Record<string, any> }} props
 */
function SubRecord({ item }) {
  return (
    <span className="timeline__subrecord">
      {Object.entries(item)
        .filter(([, value]) => value !== null && value !== undefined)
        .map(([key, value]) => (
          <span className="timeline__subfield" key={key}>
            <span className="timeline__subkey">{FIELD_LABELS[key] ?? humanise(key)}</span>{' '}
            {isLabelled(value) ? (
              <Enum value={value.member === '' ? value.label : value.member} label={value.label} />
            ) : (
              String(value)
            )}
          </span>
        ))}
    </span>
  )
}

/**
 * R26.C9. The reasoning layer's paragraph, marked as what it is.
 *
 * Adjacent to the timeline rather than inside the `DECIDED` stage, which is the requirement's own
 * word taken literally: inside the stage it would be one more entry in that stage's field set, and a
 * generic field renderer would then present advisory prose in the same register as a recorded figure.
 * Beside it, every sentence above is unchanged whether this exists or not — and the server guarantees
 * that by construction, since no sentence template reads this field.
 *
 * The label is rendered as prominent text rather than as an icon. `AI_GENERATED` beside a paragraph is
 * the difference between an explanation and evidence, and it is the one place on this screen where a
 * model's words could be mistaken for a recorded fact.
 *
 * @param {{ text: string, label: string }} props
 */
function AiExplanation({ text, label }) {
  return (
    <aside className="timeline-ai" aria-label="Model-generated explanation">
      <p className="timeline-ai__label">
        <span className="label label--caution">{label.replaceAll('_', ' ')}</span>
      </p>
      <p className="timeline-ai__text">{text}</p>
      <p className="footnote">
        Advisory prose. No figure above is derived from it, and every sentence above is the same
        whether or not it exists.
      </p>
    </aside>
  )
}
