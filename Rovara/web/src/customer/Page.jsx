/**
 * The whole customer response page: what is owed, why it failed, a way to pay, and two forms.
 *
 * This is the only surface in Revora a person outside the Merchant sees, so everything on it is a
 * disclosure decision and the projection it renders is a closed set of **eight** fields. Nothing here
 * derives a ninth, computes a figure, or explains the Merchant's reasoning — the probabilities, the
 * costs, the alternatives considered and the policy decision are all excluded by R19.C2 and none of
 * them arrives on the wire in the first place.
 *
 * Four rules about the implementation, each of them a rule rather than a style.
 *
 * **No money is computed, formatted or touched here.** The amount arrives as the server's envelope
 * — `{status, minor, currency, formatted}` — and renders through the shared `<Money>` in
 * `../components/Figure.jsx`, imported relatively so there is exactly one implementation. This entry
 * has no currency symbol table, no divisor and no `Intl.NumberFormat` call, so it *cannot* produce a
 * figure that disagrees with the server's: it has no way to compute one. `data-minor` stays on the
 * element for test assertions and is never read for display. That is the one module shared with the
 * dashboard, and sharing it is what keeps the two bundles from drifting into two ideas of what a
 * rupee looks like.
 *
 * **A refusal is a sentence, not a blank page.** Seven status codes reach this component and each one
 * gets copy written for the person reading it — see `refusalCopy`. Two are worth naming here: a 404
 * covers an unknown tenant *and* all four token failures, deliberately indistinguishable, so this
 * page says the same thing for all of them and does not guess which; and a 429 on a **write** never
 * clears the projection, because reads stay served at both caps and a customer who has explained
 * themselves five times must still be able to see what they owe.
 *
 * **`promise` is null today and null is not an error.** Nothing writes a Promise_To_Pay yet — that is
 * a later task — so the field is built, rendered when present, and shown as "no date recorded" when
 * absent. A field that treated its ordinary value as a failure would light up every page in the
 * system.
 *
 * **On the accessibility claim, precisely.** WCAG 2.1 Level AA is the target standard for keyboard
 * operation, contrast, focus visibility and programmatic labelling, and the forms below implement
 * those with native `<form>`, `<fieldset>`, `<legend>`, `<label>` and `<select>` elements, a visible
 * `:focus-visible` ring, and a validation summary announced through `aria-live` and linked to the
 * field it describes. This is **not** a conformance claim: full WCAG 2.1 AA conformance validation
 * requires manual testing with assistive technologies and expert accessibility review, and neither
 * has been done here. Same standard as `../components/Timeline.jsx`, same limitation.
 *
 * Serving: `revora/api/spa.py`'s `mount_spa` is unchanged and keeps `/app`. This page is served by
 * the frontend host at `/pay/*`. A second mount at `/pay` for a single-host deployment would follow
 * the same pattern and is not built.
 */

import { useCallback, useEffect, useRef, useState } from 'react'

import { Money } from '../components/Figure'
import { SHAPES, fetchCase, submit } from './api'

/**
 * The eight fields the server sends, in the order they read on the page.
 *
 * Declared as data because the disclosure surface is the point: `revora/customer/projection.py`
 * raises at runtime if the document's key set diverges from its dataclass, and this is the client
 * half of the same claim. A field arriving that is not in this list renders nowhere, and the test
 * asserts the count both ways so an added field is a failure rather than a silent disclosure.
 */
const FIELDS = Object.freeze([
  'merchant_display_name',
  'amount',
  'currency',
  'reason',
  'pay_url',
  'window_end_at',
  'promise',
  'signals_remaining',
])

/**
 * The six `DelayReason` members, with wording written for the person who owes the money.
 *
 * The enumeration is closed and the mapping to a `Risk_Cause` is declared per member on the server
 * (R20.C1, R20.C5), so this table is presentation of a fixed vocabulary and not a second vocabulary.
 * `OTHER` is last and deliberately vague, because a customer whose reason we did not anticipate must
 * not be pushed into one that is wrong — it maps to no cause at all.
 *
 * The final two are `Hard_Stop` reasons: both are objections to the debt rather than payment
 * problems, both permanently suppress automated contact within their scope, and both escalate the
 * case to a person. The form says so above the control, because a customer choosing "I do not think
 * I owe this" is making a bigger decision than the sentence looks.
 */
const DELAY_REASONS = Object.freeze([
  { value: 'SALARY_OR_CASHFLOW_TIMING', label: 'I get paid later this month' },
  { value: 'BANK_OR_CARD_PROBLEM', label: 'My card or bank refused the payment' },
  { value: 'AMOUNT_TOO_HIGH_RIGHT_NOW', label: 'The amount is too much for me right now' },
  { value: 'DISPUTES_THE_CHARGE', label: 'I do not think I owe this' },
  { value: 'NO_LONGER_WANTS_THE_ORDER', label: 'I no longer want the order' },
  { value: 'OTHER', label: 'Something else' },
])

/** The two members that stop automated contact and pass the case to a person. */
const HARD_STOPS = Object.freeze(['DISPUTES_THE_CHARGE', 'NO_LONGER_WANTS_THE_ORDER'])

/**
 * How far ahead a promised date has to be for this form to send it. **A courtesy, not a bound.**
 *
 * The server is the authority and it currently enforces only the degenerate rule — the date must be
 * in the future. There is no configured lead time to read: `PROMISE_MIN_LEAD_TIME` is not in the
 * configuration catalogue at all, and a number invented here and presented as configured would be a
 * client-side rule wearing a server's authority. The configured bound arrives with the promise task;
 * when it does, this constant is deleted rather than tuned, because a client-side copy of a
 * server-side bound is the thing that drifts.
 *
 * One hour, so a person typing "today" gets told before they spend one of their counted submissions
 * rather than after.
 */
const PROMISE_COURTESY_LEAD_TIME_MS = 60 * 60 * 1000

/**
 * A refusal, as the sentence the person reading the page needs.
 *
 * The status code is the discriminator and each arm is written for a reader who cannot see a status
 * code and should not have to. The two that carry a body read it, and neither reads a submitted
 * value: a 422 names a field and a 409 names a state, which is all the server sends.
 *
 * @param {number} status
 * @param {any} body
 * @returns {{ heading: string, detail: string, retry: boolean }}
 */
export function refusalCopy(status, body) {
  if (status === 404) {
    // An unknown tenant and all four token failures — no such handle, wrong secret, retired signing
    // key, malformed presentation — are one answer with an empty body, on purpose. This page does
    // not speculate about which; a sentence that guessed would be wrong four times out of five and
    // would leak the distinction the server spent effort hiding.
    return {
      heading: 'This link does not work',
      detail:
        'It may have been copied incompletely, or it may no longer be valid. The message you ' +
        'received it in has the original link.',
      retry: false,
    }
  }
  if (status === 410) {
    // Expired and revoked, which the server does not distinguish either.
    return {
      heading: 'This link has expired',
      detail: 'Please contact the seller, who can send you a new one.',
      retry: false,
    }
  }
  if (status === 429) {
    const rejected = body === null ? null : body.rejected
    if (typeof rejected === 'string') {
      // The durable cap: the customer has genuinely run out of submissions. `accepted_submission_count`
      // is incremented under a row lock, so this is a real bound rather than a flood guard, and
      // waiting does not clear it.
      return {
        heading: 'You have already sent everything we can record',
        detail:
          'Your earlier answers are saved. If something has changed, please contact the seller ' +
          'directly.',
        retry: false,
      }
    }
    // The process-local, fixed-window flood guard. Waiting genuinely does clear this one.
    return {
      heading: 'That was too quick',
      detail: 'Please wait a moment and try again.',
      retry: true,
    }
  }
  if (status === 422) {
    const field = body === null ? null : body.field
    return {
      heading: 'Please check what you entered',
      detail:
        typeof field === 'string'
          ? `The ${field.replaceAll('_', ' ')} was not accepted.`
          : 'One of the answers was not accepted.',
      retry: true,
    }
  }
  if (status === 409) {
    // `detail` is a Terminal_State name. Shown as itself rather than translated, because the four
    // states mean genuinely different things and a single softened sentence would cover all of them.
    return {
      heading: 'This payment is already closed',
      detail:
        'Nothing further can be recorded against it. Please contact the seller if you think that ' +
        'is wrong.',
      retry: false,
    }
  }
  if (status === 415) {
    // Unreachable from this page — `submit` sets the header explicitly — and handled anyway, because
    // "unreachable" is a claim about today's code and a blank panel would be the cost of it being
    // wrong tomorrow.
    return {
      heading: 'Something went wrong sending that',
      detail: 'Please try again.',
      retry: true,
    }
  }
  if (status === 503) {
    // An unreadable signing credential, a failed audit write, or a read over its budget. In every
    // case nothing was persisted, which is what makes "try again" honest advice rather than a risk
    // of recording something twice.
    return {
      heading: 'We cannot reach your payment details right now',
      detail: 'Nothing has been saved or changed. Please try again in a few minutes.',
      retry: true,
    }
  }
  if (status === 0) {
    return {
      heading: 'We could not reach the seller’s system',
      detail: 'Please check your connection and try again.',
      retry: true,
    }
  }
  return {
    heading: 'Something went wrong',
    detail: 'Please try again in a few minutes.',
    retry: true,
  }
}

/**
 * A readable instant, in the reader's own time zone.
 *
 * A date, not a figure. The server owns every currency rule and this bundle has no way to format an
 * amount; a timestamp is a different kind of value, and rendering `2026-09-15T18:30:00+00:00` to
 * somebody being asked for money would be a worse page for no gain. The machine-readable form stays
 * in the `<time datetime>` attribute, so nothing is lost.
 *
 * Falls back to the raw string rather than throwing: an unparseable instant is still information, and
 * an empty panel would not be.
 *
 * @param {string} iso
 * @returns {string}
 */
function readableInstant(iso) {
  const parsed = new Date(iso)
  if (Number.isNaN(parsed.getTime())) return iso
  return new Intl.DateTimeFormat(undefined, { dateStyle: 'long', timeStyle: 'short' }).format(
    parsed,
  )
}

/**
 * The page.
 *
 * @param {{ link: { merchantSlug: string, token: string } | null }} props
 *   `null` when the URL is not a usable `/pay/<slug>/<token>` path. Rendered identically to a 404,
 *   because a truncated link and a rejected one are the same problem to the person holding it.
 */
export function Page({ link }) {
  const [state, setState] = useState({ phase: 'loading' })

  useEffect(() => {
    if (link === null) {
      setState({ phase: 'refused', status: 404, body: null })
      return undefined
    }
    let live = true
    setState({ phase: 'loading' })
    void fetchCase(link).then((answer) => {
      if (!live) return
      setState(
        answer.ok
          ? { phase: 'ready', projection: answer.body }
          : { phase: 'refused', status: answer.status, body: answer.body },
      )
    })
    return () => {
      live = false
    }
  }, [link])

  /**
   * Fold an accepted write's answer back into the projection.
   *
   * Only `signals_remaining` moves, and it is taken from the server's 201 body rather than
   * decremented here. A client that counted down would be a second implementation of the bound, and
   * the two would disagree the moment a write was accepted in another tab.
   */
  const recordAccepted = useCallback((remaining) => {
    setState((current) => {
      if (current.phase !== 'ready') return current
      return {
        ...current,
        projection: { ...current.projection, signals_remaining: remaining },
      }
    })
  }, [])

  if (state.phase === 'loading') {
    return (
      <main className="cp">
        <p className="cp__loading" role="status">
          Loading your payment details…
        </p>
      </main>
    )
  }

  if (state.phase === 'refused') {
    return (
      <main className="cp">
        <Refusal status={state.status} body={state.body} />
      </main>
    )
  }

  return (
    <main className="cp">
      <Projection projection={state.projection} />
      <Forms
        link={link}
        signalsRemaining={state.projection.signals_remaining}
        onAccepted={recordAccepted}
      />
      <Footnote />
    </main>
  )
}

/**
 * A refusal that replaced the whole page: the read never succeeded, so there is nothing to keep.
 *
 * `role="alert"` because it arrives after the page has been read once and it replaces what was there.
 *
 * @param {{ status: number, body: any }} props
 */
function Refusal({ status, body }) {
  const copy = refusalCopy(status, body)
  return (
    <section className="cp__refusal" role="alert" data-status={status}>
      <h1 className="cp__heading">{copy.heading}</h1>
      <p className="cp__detail">{copy.detail}</p>
      {copy.retry && (
        <p className="cp__detail cp__detail--quiet">
          Nothing has been saved or changed by this attempt.
        </p>
      )}
    </section>
  )
}

/**
 * The eight fields (R19.C1), each carrying `data-field` so the disclosure surface is countable.
 *
 * The attribute exists for the test that asserts the rendered field set is exactly `FIELDS` — eight,
 * and no ninth. Counting the *rendered* fields rather than the response's keys is what makes that
 * test about this component: a field could arrive on the wire and be dropped here, or be invented
 * here and never arrive, and only one of those is visible from the response alone.
 *
 * @param {{ projection: Record<string, any> }} props
 */
function Projection({ projection }) {
  const promise = projection.promise
  return (
    <section className="cp__case" aria-labelledby="cp-amount-heading">
      <p className="cp__to" data-field="merchant_display_name">
        Payment to <strong>{projection.merchant_display_name}</strong>
      </p>

      <h1 className="cp__heading" id="cp-amount-heading">
        <span className="cp__amount" data-field="amount">
          {/* The shared component, from the server's `formatted` string. `data-minor` rides along on
              the element and is never read for display. */}
          <Money value={projection.amount} emphasis />
        </span>{' '}
        <span className="cp__currency" data-field="currency">
          {projection.currency}
        </span>
      </h1>

      <p className="cp__reason" data-field="reason">
        {projection.reason}
      </p>

      <p className="cp__window" data-field="window_end_at">
        We will stop following this up after{' '}
        <time dateTime={projection.window_end_at}>
          {readableInstant(projection.window_end_at)}
        </time>
        .
      </p>

      <p className="cp__pay" data-field="pay_url">
        {projection.pay_url === null ? (
          // A real state, not a failure: a case whose action was a message or a human escalation has
          // no payment link. Inventing one would be inventing an external effect no policy decision
          // approved.
          <span className="cp__pay-absent">
            There is no payment link for this yet. The seller can send you one.
          </span>
        ) : (
          // `rel="noreferrer"` as well as the document's `no-referrer` policy. This URL's path
          // carries the token, and a referrer on this one navigation would hand it to the payment
          // provider — belt and braces, because the document header is the only other thing stopping
          // it and a static host may not send one.
          <a className="cp__pay-link" href={projection.pay_url} rel="noreferrer noopener">
            Pay now
          </a>
        )}
      </p>

      <p className="cp__promise" data-field="promise">
        {promise === null ? (
          // Null in every response today; nothing writes a Promise_To_Pay yet. An ordinary value,
          // rendered as one.
          <span className="cp__promise-absent">No payment date recorded yet.</span>
        ) : (
          <>
            You told us you would pay by{' '}
            <time dateTime={promise.promise_date}>{readableInstant(promise.promise_date)}</time>{' '}
            <span className="cp__promise-status">({promise.status})</span>.
          </>
        )}
      </p>

      <p className="cp__remaining" data-field="signals_remaining">
        {projection.signals_remaining === 0
          ? 'You have sent everything we can record for this payment.'
          : `You can send us ${String(projection.signals_remaining)} more answer${
              projection.signals_remaining === 1 ? '' : 's'
            } about this payment.`}
      </p>
    </section>
  )
}

/**
 * The two forms, or the reason there are none.
 *
 * Two, matching this task: a Delay_Reason with an optional note, and a Promise_To_Pay. The API
 * accepts a third shape — a Partial_Arrangement_Request — and no control here submits it; the shape
 * exists in `api.js` as `SHAPES.partialArrangement` and adding a form for it is a product decision
 * rather than a wiring one.
 *
 * At zero remaining submissions both forms are gone rather than disabled. A disabled control invites
 * a reader to work out what would re-enable it, and nothing will.
 *
 * @param {{ link: { merchantSlug: string, token: string }, signalsRemaining: number,
 *           onAccepted: (remaining: number) => void }} props
 */
function Forms({ link, signalsRemaining, onAccepted }) {
  if (signalsRemaining === 0) {
    return (
      <section className="cp__forms cp__forms--closed">
        <p className="cp__detail">
          Your earlier answers are saved. You can still see what is owed above at any time.
        </p>
      </section>
    )
  }
  return (
    <section className="cp__forms">
      <DelayReasonForm link={link} onAccepted={onAccepted} />
      <PromiseForm link={link} onAccepted={onAccepted} />
    </section>
  )
}

/**
 * Shared submit plumbing for both forms: one in-flight request, one outcome, one announcement.
 *
 * A hook rather than a component so each form keeps its own native markup. What it centralises is the
 * part that must not differ between the two — a single in-flight request, a refusal turned into the
 * same copy, and an accepted write reporting its `signals_remaining` upward.
 *
 * @param {{ link: { merchantSlug: string, token: string }, shape: string,
 *           onAccepted: (remaining: number) => void }} args
 */
function useSubmission({ link, shape, onAccepted }) {
  const [sending, setSending] = useState(false)
  const [problems, setProblems] = useState([])
  const [accepted, setAccepted] = useState(false)
  const summary = useRef(null)

  /**
   * Announce a client-side validation failure and put focus where it can be read.
   *
   * Focus moves to the summary rather than to the field, because the summary is what says *why*.
   * `tabIndex={-1}` on it is what makes that possible without adding it to the tab order.
   */
  const reject = useCallback((messages) => {
    setProblems(messages)
    setAccepted(false)
    if (summary.current !== null) summary.current.focus()
  }, [])

  const send = useCallback(
    async (body) => {
      setSending(true)
      setProblems([])
      const answer = await submit(link, shape, body)
      setSending(false)
      if (answer.ok) {
        setAccepted(true)
        onAccepted(answer.body.signals_remaining)
        return true
      }
      // A refused write keeps the projection above it intact — this is the 429-must-not-blank-the-page
      // rule, and it holds by construction because a form's failure never reaches the read's state.
      const copy = refusalCopy(answer.status, answer.body)
      setAccepted(false)
      setProblems([`${copy.heading}. ${copy.detail}`])
      if (summary.current !== null) summary.current.focus()
      return false
    },
    [link, shape, onAccepted],
  )

  return { sending, problems, accepted, summary, reject, send }
}

/**
 * The validation and outcome summary, announced through `aria-live`.
 *
 * `role="status"` with `aria-live="polite"` rather than `role="alert"`: this region announces both a
 * rejection and an acceptance, and an assertive live region interrupting a screen reader to say "that
 * worked" is worse than waiting for a pause. It is always in the DOM so the live region exists before
 * it has anything to say — a region inserted at the same moment as its first message is frequently
 * not announced at all.
 *
 * @param {{ id: string, problems: string[], accepted: boolean,
 *           innerRef: import('react').RefObject<HTMLDivElement> }} props
 */
function Summary({ id, problems, accepted, innerRef }) {
  return (
    <div
      className="cp__summary"
      id={id}
      ref={innerRef}
      role="status"
      aria-live="polite"
      tabIndex={-1}
    >
      {problems.length > 0 && (
        <>
          <p className="cp__summary-head">Please check the following.</p>
          <ul className="cp__summary-list">
            {problems.map((problem) => (
              <li key={problem}>{problem}</li>
            ))}
          </ul>
        </>
      )}
      {accepted && problems.length === 0 && (
        <p className="cp__summary-ok">Thank you. We have recorded that.</p>
      )}
    </div>
  )
}

/**
 * Why the payment is late (R19.C4, R20.C1, R20.C2).
 *
 * A native `<select>` over the six members rather than six radio buttons or a combobox widget: it is
 * one control, it is operable from the keyboard and from every mobile platform's own picker, and
 * there is nothing here to reimplement.
 *
 * The note has **no `maxLength`**. Its bound is `DELAY_NOTE_MAX_LENGTH`, a configuration value the
 * projection deliberately does not disclose (R19.C2), and the server truncates a longer submission
 * and marks the stored value `TRUNCATED` rather than rejecting it. A number guessed here would be a
 * second copy of a bound this page is not told, and it would be the copy that is wrong.
 *
 * @param {{ link: { merchantSlug: string, token: string },
 *           onAccepted: (remaining: number) => void }} props
 */
function DelayReasonForm({ link, onAccepted }) {
  const { sending, problems, accepted, summary, reject, send } = useSubmission({
    link,
    shape: SHAPES.delayReason,
    onAccepted,
  })
  const [reason, setReason] = useState('')
  const [note, setNote] = useState('')

  const onSubmit = (event) => {
    event.preventDefault()
    if (reason === '') {
      reject(['Please choose a reason, so we know what to do next.'])
      return
    }
    const body = { delay_reason: reason }
    // Omitted rather than sent empty. The field is optional and `extra="forbid"` is not the issue —
    // an empty string is a stored note that says nothing, and a stored note is evidence.
    if (note.trim() !== '') body.note = note
    void send(body)
  }

  return (
    <form className="cp__form" onSubmit={onSubmit} noValidate>
      <fieldset className="cp__fieldset" disabled={sending}>
        <legend className="cp__legend">Tell us why it is late</legend>

        <Summary
          id="cp-reason-summary"
          problems={problems}
          accepted={accepted}
          innerRef={summary}
        />

        <p className="cp__help" id="cp-reason-help">
          The last two answers stop our automatic reminders for good and pass this to a person at the
          seller.
        </p>

        <div className="cp__field">
          <label className="cp__label" htmlFor="cp-reason">
            Reason
          </label>
          <select
            className="cp__select"
            id="cp-reason"
            name="delay_reason"
            value={reason}
            aria-describedby="cp-reason-help cp-reason-summary"
            aria-invalid={problems.length > 0}
            onChange={(event) => setReason(event.target.value)}
          >
            <option value="">Choose one…</option>
            {DELAY_REASONS.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
          {HARD_STOPS.includes(reason) && (
            <p className="cp__help cp__help--caution" role="status">
              We will stop sending you reminders about this and ask someone to look at it.
            </p>
          )}
        </div>

        <div className="cp__field">
          <label className="cp__label" htmlFor="cp-note">
            Anything else you want the seller to know (optional)
          </label>
          <textarea
            className="cp__textarea"
            id="cp-note"
            name="note"
            rows={3}
            value={note}
            onChange={(event) => setNote(event.target.value)}
          />
        </div>

        <button className="cp__button" type="submit">
          {sending ? 'Sending…' : 'Send this reason'}
        </button>
      </fieldset>
    </form>
  )
}

/**
 * When the customer will pay (R19.C4).
 *
 * `datetime-local` rather than `date`, because the API takes an ISO-8601 **instant** and a date-only
 * control would leave this component inventing a time of day — midnight in whose zone, and is a
 * promise for "the 15th" broken at 00:01 on the 15th? The picker asks, the browser interprets the
 * value in the reader's own zone, and `toISOString` converts once.
 *
 * The lead-time check is a courtesy and the comment on `PROMISE_COURTESY_LEAD_TIME_MS` says so: the
 * server is the authority, it currently enforces only that the date is in the future, and the
 * configured bound arrives with the promise task.
 *
 * @param {{ link: { merchantSlug: string, token: string },
 *           onAccepted: (remaining: number) => void }} props
 */
function PromiseForm({ link, onAccepted }) {
  const { sending, problems, accepted, summary, reject, send } = useSubmission({
    link,
    shape: SHAPES.promise,
    onAccepted,
  })
  const [when, setWhen] = useState('')

  const onSubmit = (event) => {
    event.preventDefault()
    if (when === '') {
      reject(['Please choose the date you expect to pay.'])
      return
    }
    const chosen = new Date(when)
    if (Number.isNaN(chosen.getTime())) {
      reject(['That date could not be read. Please choose it again.'])
      return
    }
    if (chosen.getTime() - Date.now() < PROMISE_COURTESY_LEAD_TIME_MS) {
      reject(['Please choose a time at least an hour from now.'])
      return
    }
    void send({ promise_date: chosen.toISOString() })
  }

  return (
    <form className="cp__form" onSubmit={onSubmit} noValidate>
      <fieldset className="cp__fieldset" disabled={sending}>
        <legend className="cp__legend">Tell us when you will pay</legend>

        <Summary
          id="cp-promise-summary"
          problems={problems}
          accepted={accepted}
          innerRef={summary}
        />

        <p className="cp__help" id="cp-promise-help">
          We will hold off on reminders until then.
        </p>

        <div className="cp__field">
          <label className="cp__label" htmlFor="cp-promise">
            Date and time you expect to pay
          </label>
          <input
            className="cp__input"
            id="cp-promise"
            name="promise_date"
            type="datetime-local"
            value={when}
            aria-describedby="cp-promise-help cp-promise-summary"
            aria-invalid={problems.length > 0}
            onChange={(event) => setWhen(event.target.value)}
          />
        </div>

        <button className="cp__button" type="submit">
          {sending ? 'Sending…' : 'Send this date'}
        </button>
      </fieldset>
    </form>
  )
}

/**
 * What this page is, and what it is not.
 *
 * Present because the page asks a stranger for money and gives them two forms; a reader deciding
 * whether to type anything into it is entitled to know that nothing here charges them and that the
 * amount was not computed in their browser.
 */
function Footnote() {
  return (
    <footer className="cp__footnote">
      <p>
        Nothing on this page charges you. The amount shown is the amount the seller recorded, and
        this page does not calculate it.
      </p>
    </footer>
  )
}

export { FIELDS as PROJECTION_FIELDS }
