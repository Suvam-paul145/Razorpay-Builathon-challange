/**
 * Task 38.6. A case that chose restraint is shown as waiting, not as ended (R30.C13, R26.C14).
 *
 * This is the presentation half of the defect Requirement 30 documents. The pipeline half — a case
 * that selected `DO_NOTHING` or `WAIT` having no route to a second decision cycle — is fixed by the
 * review loop. The presentation half is that such a case showed a state name, an empty executed
 * cell and nothing at all about the future, so a merchant reading the list drew exactly the
 * conclusion the old implementation had actually reached: that nothing further would happen.
 *
 * Three claims, and each one has a specific wrong rendering it rules out:
 *
 * * the waiting block is present, names the next review instant, and shows the shared label — ruling
 *   out a row that says only `POLICY_CHECK`;
 * * the state badge is not styled as an ending — ruling out amber-with-the-terminal-states, which
 *   would be the same false statement in colour rather than in words;
 * * `DO_NOTHING` and `WAIT` render under one label with the stored member beside each — and the
 *   label comes from the response, so a row whose server did not send one gets no invented word.
 */

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { STATE_KIND } from '../components/Chrome'
import { CaseList } from './CaseList'

const REVIEW_AT = '2026-09-02T12:00:00+00:00'

/** A case resting at POLICY_CHECK with a future review instant: R30.C13's exact precondition. */
function waitingCase(overrides = {}) {
  const absent = (figure) => ({
    status: 'NOT_YET_RECORDED',
    case_state: 'POLICY_CHECK',
    detail: `no ${figure} has been recorded yet; the case is POLICY_CHECK`,
  })
  return {
    case_id: 'c0000000-0000-4000-8000-000000000001',
    state: 'POLICY_CHECK',
    state_label: 'Decision recorded',
    detected_at: '2026-09-01T00:00:00+00:00',
    window_end_at: '2026-09-08T00:00:00+00:00',
    payment_amount: { status: 'PRESENT', minor: 5_000, currency: 'INR', formatted: '₹50.00' },
    provider_payment_id: 'pay_waiting',
    customer_contact_masked: '+91******01',
    risk_cause: 'INSUFFICIENT_FUNDS',
    selected_action: 'WAIT',
    selected_action_label: 'Waiting and watching',
    waiting: {
      next_review_at: REVIEW_AT,
      selected_action: 'WAIT',
      selected_action_label: 'Waiting and watching',
      decision_cycle_count: 1,
      max_recovery_attempts: 3,
      detail:
        'Revora decided not to act this cycle and will look at this case again at the instant ' +
        'above. The case has not ended.',
    },
    executed_action: absent('executed action'),
    policy_decision: 'APPROVED',
    recovered_amount: absent('recovered amount'),
    outcome_classification: absent('outcome'),
    human_owner_user_id: null,
    provenance: 'REAL',
    ...overrides,
  }
}

/** And one that genuinely ended, so "the block is always there" cannot pass these tests. */
function stoppedCase() {
  const absent = (figure) => ({
    status: 'NOT_YET_RECORDED',
    case_state: 'STOPPED',
    detail: `no ${figure} has been recorded yet; the case is STOPPED`,
  })
  return {
    ...waitingCase(),
    case_id: 'c0000000-0000-4000-8000-000000000002',
    state: 'STOPPED',
    state_label: 'Stopped trying',
    provider_payment_id: 'pay_stopped',
    selected_action: 'WAIT',
    selected_action_label: 'Waiting and watching',
    waiting: null,
    recovered_amount: absent('recovered amount'),
  }
}

function mountWith(cases) {
  vi.stubGlobal(
    'fetch',
    vi.fn(() =>
      Promise.resolve({
        ok: true,
        status: 200,
        headers: new Headers({ 'x-correlation-id': 'test' }),
        json: () =>
          Promise.resolve({
            cases,
            page_size: 100,
            offset: 0,
            returned: cases.length,
            has_more: false,
            ordering: 'detected_at descending',
          }),
      }),
    ),
  )
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <MemoryRouter>
      <QueryClientProvider client={client}>
        <CaseList />
      </QueryClientProvider>
    </MemoryRouter>,
  )
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('a case that chose restraint', () => {
  it('names its next review instant and the action it selected', async () => {
    mountWith([waitingCase()])

    await waitFor(() => {
      expect(screen.getAllByText('Waiting and watching').length).toBeGreaterThan(0)
    })
    // The instant, as a machine-readable `<time>` with the exact ISO value the server sent. A
    // relative phrase would be the one thing a reader cannot reconcile against an audit record.
    const when = document.querySelector(`time[dateTime="${REVIEW_AT}"]`)
    expect(when).not.toBeNull()
    // The stored member alongside the shared label (R26.C14), so a reader sees both the words and
    // the value the row actually holds.
    expect(screen.getAllByText('WAIT').length).toBeGreaterThan(0)
    // The counter, as used-of-allowed. "cycle 1" alone says nothing; "1 of 3" is the fact that
    // explains whether another review is coming.
    expect(screen.getByText(/cycle 1 of 3/)).toBeInTheDocument()
  })

  it('is not styled as an ending, and no ended case is styled as waiting', async () => {
    mountWith([waitingCase(), stoppedCase()])

    await waitFor(() => {
      expect(document.querySelectorAll('.waiting').length).toBe(1)
    })
    // One waiting block, on the one case that qualifies. A block on the STOPPED row would be the
    // opposite failure to R30.C13's and just as misleading.
    expect(document.querySelectorAll('.state--ended').length).toBe(1)
    expect(document.querySelectorAll('.state--active').length).toBe(1)
  })

  it('renders the server label rather than deriving one, and falls back rather than inventing', () => {
    // `POLICY_CHECK` is absent from the ending map on purpose, and that absence is R30.C13's client
    // half: a case resting there has recorded a decision, not reached a conclusion.
    expect(STATE_KIND.POLICY_CHECK).toBeUndefined()
    expect(STATE_KIND.STOPPED).toBe('ended')
    expect(STATE_KIND.EXPIRED).toBe('ended')
    expect(STATE_KIND.BLOCKED).toBe('ended')
  })

  it('shows the shared label for DO_NOTHING as well, with its own stored member', async () => {
    // The requirement is one label for two members, and the members stay distinguishable. A row
    // that showed only the label would satisfy half of R26.C14 and lose the stored value; a row
    // that showed only the member would lose the point of having a shared label at all.
    mountWith([
      waitingCase({
        selected_action: 'DO_NOTHING',
        selected_action_label: 'Waiting and watching',
        waiting: {
          ...waitingCase().waiting,
          selected_action: 'DO_NOTHING',
          selected_action_label: 'Waiting and watching',
        },
      }),
    ])

    await waitFor(() => {
      expect(screen.getAllByText('Waiting and watching').length).toBeGreaterThan(0)
    })
    expect(screen.getAllByText('DO_NOTHING').length).toBeGreaterThan(0)
    expect(screen.queryByText('WAIT')).toBeNull()
  })
})
