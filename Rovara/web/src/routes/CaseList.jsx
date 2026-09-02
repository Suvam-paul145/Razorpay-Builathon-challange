/**
 * The case list (R14.C2). Every column the requirement names, paged, newest failure first.
 *
 * Nine columns and not one is derived here. `payment_amount` and `recovered_amount` arrive formatted;
 * `risk_cause`, `selected_action`, `executed_action`, `policy_decision` and `outcome_classification`
 * each arrive as either an enum value or an absent marker, and each cell renders whichever it got.
 *
 * **A case with no recovered amount is not a case that recovered nothing.** It is usually a case that
 * has not finished, and on a `RECOVERED` case with no executed action it is the *best* outcome the
 * product produces — the customer paid without being contacted. Both render as markers naming the
 * state rather than as a zero or a blank, which is why this table has no empty cells anywhere.
 */

import { useState } from 'react'
import { Link } from 'react-router-dom'

import { useCases } from '../api/queries'
import { Empty, Failure, Loading, Panel, StateBadge, When } from '../components/Chrome'
import { Enum, Money } from '../components/Figure'

/** Offered as filters because these are the states an operator triages by. */
const FILTER_STATES = [
  'WAITING_FOR_OUTCOME',
  'ACTION_SCHEDULED',
  'POLICY_CHECK',
  'RECOVERED',
  'ESCALATED',
  'BLOCKED',
  'EXPIRED',
  'STOPPED',
  'FAILED',
]

export function CaseList() {
  const [state, setState] = useState(null)
  const [offset, setOffset] = useState(0)
  const query = useCases(state, offset)

  const filters = (
    <div className="filters" role="group" aria-label="Filter cases by state">
      <button
        type="button"
        className={state === null ? 'chip chip--on' : 'chip'}
        onClick={() => {
          setState(null)
          setOffset(0)
        }}
      >
        All
      </button>
      {FILTER_STATES.map((candidate) => (
        <button
          key={candidate}
          type="button"
          className={state === candidate ? 'chip chip--on' : 'chip'}
          onClick={() => {
            setState(candidate)
            setOffset(0)
          }}
        >
          {candidate.replaceAll('_', ' ').toLowerCase()}
        </button>
      ))}
    </div>
  )

  return (
    <Panel
      title="Recovery cases"
      subtitle="Newest detected failure first. Every figure is formatted by the server."
      aside={filters}
    >
      {query.isPending && <Loading what="cases" />}
      {query.isError && <Failure error={query.error} what="cases" />}
      {query.isSuccess && query.data.cases.length === 0 && (
        <Empty>
          No cases in this view. That is a real answer, not a missing one — if you expected cases,
          check the webhook health panel, because a silent webhook means no detection at all.
        </Empty>
      )}
      {query.isSuccess && query.data.cases.length > 0 && (
        <>
          <div className="table-scroll">
            <table className="grid">
              <caption className="sr-only">
                Recovery cases, ordered by detection time descending
              </caption>
              <thead>
                <tr>
                  <th scope="col">Detected</th>
                  <th scope="col">State</th>
                  <th scope="col" className="num">
                    Amount
                  </th>
                  <th scope="col">Cause</th>
                  <th scope="col">Selected</th>
                  <th scope="col">Executed</th>
                  <th scope="col">Policy</th>
                  <th scope="col" className="num">
                    Recovered
                  </th>
                  <th scope="col">Classification</th>
                  <th scope="col">
                    <span className="sr-only">Open</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {query.data.cases.map((row) => (
                  <tr key={row.case_id}>
                    <td>
                      <When iso={row.detected_at} />
                    </td>
                    <td>
                      <StateBadge state={row.state} />
                      {row.human_owner_user_id !== null && (
                        <span
                          className="owned"
                          title="A human owns this case; automation is suspended"
                        >
                          owned
                        </span>
                      )}
                    </td>
                    <td className="num">
                      <Money value={row.payment_amount} />
                    </td>
                    <td>
                      <Enum value={row.risk_cause} />
                    </td>
                    <td>
                      <Enum value={row.selected_action} />
                    </td>
                    <td>
                      <Enum value={row.executed_action} />
                    </td>
                    <td>
                      <Enum value={row.policy_decision} />
                    </td>
                    <td className="num">
                      <Money value={row.recovered_amount} />
                    </td>
                    <td>
                      <Enum value={row.outcome_classification} />
                      {row.provenance !== 'REAL' && (
                        <span className="label label--synthetic">{row.provenance}</span>
                      )}
                    </td>
                    <td>
                      <Link className="link" to={`/cases/${row.case_id}`}>
                        Why?
                      </Link>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <nav className="pager" aria-label="Pagination">
            <button
              type="button"
              className="button"
              disabled={offset === 0}
              onClick={() => {
                setOffset(Math.max(0, offset - query.data.page_size))
              }}
            >
              Previous
            </button>
            <span className="pager__where">
              {query.data.returned} shown, from offset {query.data.offset}
            </span>
            <button
              type="button"
              className="button"
              disabled={!query.data.has_more}
              onClick={() => {
                setOffset(offset + query.data.page_size)
              }}
            >
              Next
            </button>
          </nav>
        </>
      )}
    </Panel>
  )
}
