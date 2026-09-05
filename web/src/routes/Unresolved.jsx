/**
 * Unresolved revenue, grouped by how the case ended (R14.C10) — plus the webhook health surface
 * (R13.C7), which is on this page because both answer "what is not working right now?".
 *
 * **The grouping matters more than the total.** `BLOCKED` and `EXPIRED` are the same money and
 * completely different problems. Blocked means policy stopped Revora — often correctly, and a large
 * blocked total is worth investigating because it may mean a threshold is wrong. Expired means the
 * window closed with nothing having worked. Escalated means a human was asked and has not answered.
 * One "unresolved: ₹4,20,000" figure hides all three, and the merchant's next action differs in each.
 *
 * **All five rows always render, zeros included.** The server guarantees five groups; this renders
 * whatever it is given without filtering. A row omitted because its count was zero reads as "we did
 * not look", and a reader used to five rows who sees four will not notice which one went.
 *
 * **Suppressed cases are listed under the escalated grouping, not as a sixth row** (R21.C11). A
 * customer who disputed a charge or cancelled an order ends the case `ESCALATED`, so its money is
 * already in that row — a sixth group would split one grouping across two places and the total
 * would stop being the sum of the rows above it. The list is a breakdown of a row that is already
 * there.
 *
 * **Partial arrangement requests are the second breakdown of that same row** (R22.C9), and for the
 * same reason rather than a different one: a customer asking to pay less or in instalments ends the
 * case `ESCALATED` too, so its full `payment_amount` is already counted there. Two lists rather
 * than one, because the next action differs — a suppressed case needs somebody to decide whether
 * contact may resume, and an arrangement request needs somebody to answer a question. These are the
 * only places on this screen that name individual cases, because they are the only groups whose
 * next action is *per case* rather than about a threshold.
 */

import { Link } from 'react-router-dom'

import { useUnresolved, useWebhookHealth } from '../api/queries'
import { Fact, Failure, Loading, Panel, When } from '../components/Chrome'
import { Money, humanise } from '../components/Figure'

/** What each ending means and what to do about it. The operational half of the grouping. */
const GROUP_MEANING = {
  STOPPED:
    'Attempts were exhausted below the escalation amount threshold. A large total here is a finding ' +
    'about that threshold, not about these cases.',
  BLOCKED:
    'Policy stopped the action — often correctly. A large total is worth investigating, because it ' +
    'may mean a bound is set wrong rather than that these recoveries were impossible.',
  EXPIRED: 'The recovery window closed with nothing having worked.',
  ESCALATED:
    'A human was asked and has not resolved it. This queue is somebody’s work, not a metric.',
  FAILED: 'The case ended in a genuine failure — the only group here that indicates a fault.',
}

export function Unresolved() {
  const query = useUnresolved()
  return (
    <div className="detail">
      <WebhookHealth />
      <Panel
        title="Unresolved revenue"
        subtitle="Grouped by how the case ended. The same money, five different problems."
      >
        {query.isPending && <Loading what="the unresolved grouping" />}
        {query.isError && <Failure error={query.error} what="the unresolved grouping" />}
        {query.isSuccess && (
          <>
            <div className="table-scroll">
              <table className="grid">
                <caption className="sr-only">Unresolved cases grouped by terminal state</caption>
                <thead>
                  <tr>
                    <th scope="col">Ended as</th>
                    <th scope="col" className="num">
                      Cases
                    </th>
                    <th scope="col" className="num">
                      Amount
                    </th>
                    <th scope="col">What it means</th>
                  </tr>
                </thead>
                <tbody>
                  {query.data.groups.map((group) => (
                    <tr key={group.state} className={group.case_count === 0 ? 'row--zero' : ''}>
                      <td>
                        {/* R26.C14: the server's label, with the stored member beside it. This cell
                            used to call `humanise(group.state)`, which made the one place a case's
                            ending is named the one place nobody had chosen the words. */}
                        <strong>{group.label ?? humanise(group.state)}</strong>{' '}
                        <span className="enum__member">{group.state}</span>
                      </td>
                      <td className="num">{group.case_count}</td>
                      <td className="num">
                        <Money value={group.amount} />
                      </td>
                      <td className="cell--prose">{GROUP_MEANING[group.state] ?? ''}</td>
                    </tr>
                  ))}
                </tbody>
                <tfoot>
                  <tr>
                    <th scope="row">Total</th>
                    <td className="num">{query.data.total_case_count}</td>
                    <td className="num">
                      <Money value={query.data.total_amount} emphasis />
                    </td>
                    <td />
                  </tr>
                </tfoot>
              </table>
            </div>
            <SuppressedCases cases={query.data.suppressed} />
            <ArrangementRequests cases={query.data.partial_arrangements} />
            <p className="footnote">
              {query.data.reporting_period.start} to {query.data.reporting_period.end} · computed{' '}
              {query.data.computed_at}. These figures move: a delayed capture reconciles an expired
              case to recovered weeks later, which removes it from this grouping retroactively.
            </p>
            {/* R30.C13. Said out loud, because a reader looking for a case they know is unresolved
                and not finding it here would otherwise conclude the grouping is incomplete. Every
                row above is a Terminal_State; a case that chose restraint is still being worked and
                is not one. */}
            <p className="footnote">
              Every row here is a case that <em>ended</em>. A case Revora decided to wait on is still
              being worked — it holds a scheduled review, appears in no group above, and is on the
              case list marked “Waiting and watching” with the instant it will next be looked at.
            </p>
          </>
        )}
      </Panel>
    </div>
  )
}

/**
 * R21.C11. Every case holding a live Contact_Suppression, under its escalated reason.
 *
 * Three columns because the requirement names three things: the Hard_Stop_Reason, the suppression
 * instant, and the unresolved amount. The reason is the customer's own words rather than
 * "contact suppressed", which would name the consequence and leave the merchant to guess the
 * cause — and the cause is what decides who picks the case up. A dispute is a possible chargeback;
 * a cancellation is a fulfilment and refund question.
 *
 * **The amount comes through `<Money>` from the server's formatted string.** Every figure on this
 * screen does, and this one is no different: the browser performs no arithmetic on minor units and
 * chooses no currency symbol, which is R14.C12 and which the ESLint rule in `web/eslint.config.js`
 * enforces rather than merely requests.
 *
 * Renders nothing when the list is empty — deliberately unlike the five aggregate rows above,
 * which always render. A zero row in an aggregate says "we looked and it is zero"; an empty table
 * of individual cases says only that there are none, and the escalated row above already carries
 * that number. A "no suppressed cases" panel on every dashboard in the ordinary case would be
 * furniture.
 *
 * @param {{ cases: Array<Record<string, any>> | undefined }} props
 */
function SuppressedCases({ cases }) {
  if (!cases || cases.length === 0) return null
  return (
    <div className="table-scroll">
      <table className="grid">
        <caption>
          Escalated because the customer objected — {cases.length}{' '}
          {cases.length === 1 ? 'case' : 'cases'}. Automated contact on these is permanently
          stopped and only a named person can lift it.
        </caption>
        <thead>
          <tr>
            <th scope="col">Case</th>
            <th scope="col">What the customer said</th>
            <th scope="col">Since</th>
            <th scope="col" className="num">
              Unresolved
            </th>
          </tr>
        </thead>
        <tbody>
          {cases.map((entry) => (
            <tr key={entry.case_id}>
              <td>
                <Link to={`/cases/${entry.case_id}`}>
                  <code>{entry.case_id.slice(0, 8)}</code>
                </Link>
              </td>
              <td>
                <strong>{entry.hard_stop_label}</strong>{' '}
                <span className="enum__member">{entry.hard_stop_reason}</span>
              </td>
              <td>
                <When iso={entry.suppressed_at} />
              </td>
              <td className="num">
                <Money value={entry.unresolved_amount} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

/**
 * R22.C9. Every case that ended because the customer asked to pay differently.
 *
 * Three columns because the requirement names three things: the request instant, the accompanying
 * note, and the unresolved amount. The note carries more weight here than a note usually would —
 * the request itself has no amount, no instalment count and no schedule, because
 * `PartialArrangementSubmission` declares none and `customer_signal` has no column for any of them
 * (R22.C1). So what the customer wrote is the *only* content of the ask, and a row without it says
 * a negotiation was requested and nothing about what was proposed.
 *
 * **The amount is the full `payment_amount` and it is not an offer.** R22.C7 leaves the amount, the
 * currency and the recovery window unchanged, so this figure is the same integer that sits inside
 * the escalated total above. It is labelled *Unresolved* and not *Requested* for exactly that
 * reason: a column of numbers next to the words "asked to pay in parts" is the one place on this
 * dashboard a reader could take a figure for a settlement Revora had agreed to, and Revora agreed
 * to nothing.
 *
 * **Any live payment link is still live** (R22.C8), which is why the caption says so. A merchant
 * looking at this queue is deciding whether to phone somebody, and the fact that the customer can
 * still pay in full — and that doing so reconciles the case to recovered under R10.C14 — changes
 * that decision.
 *
 * The note comes through the server's note document and is rendered as a text child. React escapes
 * a text child, so `note.text` is what appears; `note.text_escaped` exists for a surface that
 * builds markup and using it here would double-escape, so a customer who typed `I <3 this` would
 * read back `I &lt;3 this`. `dangerouslySetInnerHTML` appears nowhere in `web/src` and a test
 * asserts its absence across the whole tree, which is what makes R29.C11 a property of the source
 * rather than of this comment.
 *
 * Renders nothing when the list is empty, like `SuppressedCases` and unlike the five aggregate rows
 * above. A zero in an aggregate says "we looked and it is zero"; an empty table of named cases says
 * only that there are none, and the escalated row already carries that number.
 *
 * @param {{ cases: Array<Record<string, any>> | undefined }} props
 */
function ArrangementRequests({ cases }) {
  if (!cases || cases.length === 0) return null
  return (
    <div className="table-scroll">
      <table className="grid">
        <caption>
          Escalated because the customer asked to pay differently — {cases.length}{' '}
          {cases.length === 1 ? 'case' : 'cases'}. Revora accepted no amount, no instalment count
          and no schedule; any payment link already sent is still live, so paying in full still
          resolves these.
        </caption>
        <thead>
          <tr>
            <th scope="col">Case</th>
            <th scope="col">Asked</th>
            <th scope="col">What the customer wrote</th>
            <th scope="col" className="num">
              Unresolved
            </th>
          </tr>
        </thead>
        <tbody>
          {cases.map((entry) => (
            <tr key={entry.case_id}>
              <td>
                <Link to={`/cases/${entry.case_id}`}>
                  <code>{entry.case_id.slice(0, 8)}</code>
                </Link>
              </td>
              <td>
                {entry.requested_at === null ? (
                  <span className="muted">not recorded</span>
                ) : (
                  <When iso={entry.requested_at} />
                )}
              </td>
              <td className="cell--prose">
                <ArrangementNote note={entry.note} />
              </td>
              <td className="num">
                <Money value={entry.unresolved_amount} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

/**
 * The accompanying note, or the reason there is not one.
 *
 * Three states and they are three different histories: the customer wrote nothing, the customer
 * wrote something, and the customer wrote something the retention sweep has since removed
 * (R29.C10). Collapsing the third into the first would make a compliance action look like silence,
 * which is the one reading that would let a merchant conclude nothing was ever said.
 *
 * The label is the server's, rendered verbatim. R20.C12 asks for the note presented *marked as*
 * customer-supplied unverified text, and that mark is a fact about the data rather than a styling
 * choice, so `revora/api/rendering.py` chooses the words.
 *
 * @param {{ note: Record<string, any> | null | undefined }} props
 */
function ArrangementNote({ note }) {
  if (note == null) return <span className="muted">no note</span>
  if (note.status === 'REDACTED') return <span className="muted">{note.detail}</span>
  return (
    <>
      <span className="label label--caution">{note.label}</span>{' '}
      {/* A text child. React escapes it; see this component's sibling above on why
          `text_escaped` is deliberately not used on this surface. */}
      <q>{note.text}</q>
      {note.truncated && (
        <span className="fact__note">
          {' '}
          truncated at the stored length — the customer may have written more
        </span>
      )}
    </>
  )
}

/**
 * R13.C7. Time since the last received webhook.
 *
 * Near the top because a disabled webhook is not a degraded signal — it is **silent total detection
 * loss**. No cases open at all, every figure on every other screen stays flat, and nothing looks
 * broken. That is the failure mode this panel exists for, which is also why it is the one query in
 * the app that polls.
 */
function WebhookHealth() {
  const query = useWebhookHealth()
  return (
    <Panel
      title="Webhook health"
      subtitle="A silent webhook means no detection at all, and nothing else on this dashboard would show it"
    >
      {query.isPending && <Loading what="webhook health" />}
      {query.isError && <Failure error={query.error} what="webhook health" />}
      {query.isSuccess && (
        <>
          {query.data.last_event_at === null ? (
            <p className="notice notice--warn">
              <strong>No webhook event has ever been received.</strong> The endpoint was never wired
              up, or the signing secret does not match. Detection is not running.
            </p>
          ) : (
            <>
              <dl className="facts">
                <Fact label="Last event">{query.data.last_event_at}</Fact>
                <Fact label="Silent for">
                  {query.data.seconds_since_last_event === null
                    ? 'unknown'
                    : `${query.data.seconds_since_last_event} seconds`}
                </Fact>
                <Fact label="Events in 24h">{query.data.events_last_24h}</Fact>
                <Fact label="Signature verified">
                  {query.data.verified_events_last_24h} of {query.data.events_last_24h}
                </Fact>
                <Fact label="Checked">{query.data.checked_at}</Fact>
              </dl>
              {/* Deliveries arriving and failing verification is a rotated or mismatched signing
                  secret. From the outside it looks like a perfectly healthy webhook, and it produces
                  exactly as little detection as a disabled one — so it gets its own warning rather
                  than being left for a reader to spot by comparing two numbers. */}
              {query.data.events_last_24h > 0 &&
                query.data.verified_events_last_24h < query.data.events_last_24h && (
                  <p className="notice notice--warn">
                    <strong>
                      {query.data.events_last_24h - query.data.verified_events_last_24h} deliveries
                      failed signature verification.
                    </strong>{' '}
                    The endpoint is reachable but the signing secret does not match, so those events
                    were rejected and produced no detection.
                  </p>
                )}
            </>
          )}
          <p className="footnote">{query.data.detail}</p>
        </>
      )}
    </Panel>
  )
}
