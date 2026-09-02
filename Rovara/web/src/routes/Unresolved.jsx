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
 */

import { useUnresolved, useWebhookHealth } from '../api/queries'
import { Fact, Failure, Loading, Panel } from '../components/Chrome'
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
                        <strong>{humanise(group.state)}</strong>
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
            <p className="footnote">
              {query.data.reporting_period.start} to {query.data.reporting_period.end} · computed{' '}
              {query.data.computed_at}. These figures move: a delayed capture reconciles an expired
              case to recovered weeks later, which removes it from this grouping retroactively.
            </p>
          </>
        )}
      </Panel>
    </div>
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
