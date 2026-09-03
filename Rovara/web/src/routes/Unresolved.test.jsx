/**
 * Task 42.5. A suppressed case is presented under its escalated reason (R21.C11).
 *
 * The requirement asks for three things about every Recovery_Case holding a Contact_Suppression —
 * the Hard_Stop_Reason, the suppression instant and the unresolved payment_amount — presented inside
 * the unresolved grouping of R14.C10 rather than somewhere new. Each test below rules out a specific
 * wrong rendering:
 *
 * * the three values are present, and the reason appears as the customer's words *and* the stored
 *   member — ruling out a table that says only "contact suppressed", which names the consequence
 *   and leaves the merchant to guess whether to expect a chargeback or a refund request;
 * * the money comes from the server's `formatted` string — ruling out the browser dividing by a
 *   hundred, which is R14.C12 and which no test elsewhere covers for *this* table;
 * * the five aggregate rows are untouched and the escalated total still equals what the server sent
 *   — ruling out the tempting implementation, a sixth group, which would split one grouping's money
 *   across two places and make the total stop being the sum of its rows;
 * * an empty list renders nothing at all — deliberately unlike the aggregate rows, which always
 *   render their zeros. A zero in an aggregate says "we looked"; an empty table of individual cases
 *   says only that there are none, and the escalated row above already carries that number.
 */

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { Unresolved } from './Unresolved'

const SUPPRESSED_AT = '2026-09-04T09:30:00+00:00'

const GROUPS = [
  { state: 'STOPPED', label: 'Stopped trying', case_count: 1, amount: money('₹500.00', 50_000) },
  { state: 'BLOCKED', label: 'Blocked by policy', case_count: 0, amount: money('₹0.00', 0) },
  { state: 'EXPIRED', label: 'Recovery window closed', case_count: 0, amount: money('₹0.00', 0) },
  { state: 'ESCALATED', label: 'With a person', case_count: 2, amount: money('₹4,998.00', 499_800) },
  { state: 'FAILED', label: 'Failed', case_count: 0, amount: money('₹0.00', 0) },
]

/** A server-formatted money field. The client never sees a bare integer it is allowed to divide. */
function money(formatted, minor) {
  return { status: 'PRESENT', minor, currency: 'INR', formatted }
}

function suppressedCase(overrides = {}) {
  return {
    case_id: 'c0000000-0000-4000-8000-0000000000aa',
    state: 'ESCALATED',
    terminal_reason: 'CUSTOMER_DISPUTED_CHARGE',
    hard_stop_reason: 'DISPUTES_THE_CHARGE',
    hard_stop_label: 'Disputes the charge',
    suppressed_at: SUPPRESSED_AT,
    unresolved_amount: money('₹2,499.00', 249_900),
    ...overrides,
  }
}

function mountWith(suppressed) {
  vi.stubGlobal(
    'fetch',
    vi.fn((input) => {
      const url = String(input)
      // The page issues two queries. The webhook-health one is not what these tests are about, so
      // it is answered plausibly rather than being left to reject and clutter the output.
      const body = url.includes('webhook')
        ? {
            last_event_at: '2026-09-04T09:00:00+00:00',
            seconds_since_last_event: 1800,
            events_last_24h: 4,
            verified_events_last_24h: 4,
            checked_at: '2026-09-04T09:30:00+00:00',
            detail: 'healthy',
          }
        : {
            reporting_period: { start: '2026-08-01', end: '2026-09-05' },
            computed_at: '2026-09-04T09:30:00+00:00',
            currency: 'INR',
            groups: GROUPS,
            suppressed,
            total_case_count: 3,
            total_amount: money('₹5,498.00', 549_800),
          }
      return Promise.resolve({
        ok: true,
        status: 200,
        headers: new Headers({ 'x-correlation-id': 'test' }),
        json: () => Promise.resolve(body),
      })
    }),
  )
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <MemoryRouter>
      <QueryClientProvider client={client}>
        <Unresolved />
      </QueryClientProvider>
    </MemoryRouter>,
  )
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('a suppressed case in the unresolved grouping', () => {
  it('names the reason, the instant and the unresolved amount', async () => {
    mountWith([
      suppressedCase(),
      suppressedCase({
        case_id: 'c0000000-0000-4000-8000-0000000000bb',
        terminal_reason: 'CUSTOMER_CANCELLED_ORDER',
        hard_stop_reason: 'NO_LONGER_WANTS_THE_ORDER',
        hard_stop_label: 'No longer wants the order',
        unresolved_amount: money('₹2,499.00', 249_900),
      }),
    ])

    await waitFor(() => {
      expect(screen.getByText('Disputes the charge')).toBeInTheDocument()
    })
    // Both reasons, kept apart. A single "objected to the charge" label would collapse a possible
    // chargeback and a fulfilment question into one queue for two different people.
    expect(screen.getByText('No longer wants the order')).toBeInTheDocument()
    // The stored members beside the words, so the row is reconcilable against an audit record.
    expect(screen.getByText('DISPUTES_THE_CHARGE')).toBeInTheDocument()
    expect(screen.getByText('NO_LONGER_WANTS_THE_ORDER')).toBeInTheDocument()
    // The instant, as a machine-readable `<time>` carrying the exact ISO value the server sent.
    expect(document.querySelector(`time[dateTime="${SUPPRESSED_AT}"]`)).not.toBeNull()
  })

  it('renders the amount from the server string and performs no arithmetic', async () => {
    mountWith([suppressedCase()])

    await waitFor(() => {
      expect(screen.getAllByText('₹2,499.00').length).toBeGreaterThan(0)
    })
    // The minor-unit integer must not reach the page in any form. 249900 rendered anywhere would
    // mean the value travelled as a number the browser could divide, which is exactly what
    // R14.C12 forbids and what the ESLint rule in this workspace exists to catch statically.
    expect(document.body.textContent).not.toContain('249900')
  })

  it('leaves the five aggregate rows and the escalated total alone', async () => {
    mountWith([suppressedCase()])

    await waitFor(() => {
      expect(screen.getByText('With a person')).toBeInTheDocument()
    })
    // Still five groups: the breakdown is a detail of a row that is already there, not a sixth row.
    for (const group of GROUPS) {
      expect(screen.getByText(group.label)).toBeInTheDocument()
    }
    // The escalated money is reported once, by the aggregate row, and the per-case list does not
    // add to it. A sixth group would have made the footer total stop being the sum of the rows.
    expect(screen.getByText('₹4,998.00')).toBeInTheDocument()
    expect(screen.getByText('₹5,498.00')).toBeInTheDocument()
  })

  it('renders no table when nothing is suppressed', async () => {
    mountWith([])

    await waitFor(() => {
      expect(screen.getByText('With a person')).toBeInTheDocument()
    })
    // The table's caption is the string that must be absent. Writing this assertion caught a real
    // defect on the way: an earlier version of the escalated row's prose said "listed below", which
    // promised a table that is absent in the ordinary case where nothing is suppressed.
    expect(screen.queryByText(/Escalated because the customer objected/)).toBeNull()
  })
})
