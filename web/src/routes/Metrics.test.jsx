/**
 * The claim the whole product rests on, asserted at the surface that makes it (R14.C7, R14.C9, P21).
 *
 * `incremental_recovered_revenue` must render `NOT ESTABLISHED` and must **not** render the observed
 * figure, a zero, or an empty slot. This is the one test that would catch the most tempting possible
 * regression: a well-meaning change that "falls back to the observed number when incremental is not
 * available", which is precisely the overstatement Revora exists to refuse.
 */

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { Metrics } from './Metrics'

const OBSERVED_FORMATTED = '₹4,52,000.00'

/**
 * Every amount in the fixture is a *distinct* string.
 *
 * That is what makes the count assertions below exact rather than approximate. If several figures
 * shared a formatted value, "the observed figure appears once" could not distinguish the headline
 * from an accidental second rendering of it in the incremental slot — which is the specific
 * regression these tests exist to catch.
 */
function report(overrides = {}) {
  const money = (minor, formatted) => ({ status: 'PRESENT', minor, currency: 'INR', formatted })
  const rate = { status: 'PRESENT', value: '0.1200' }
  return {
    reporting_period: { start: '2026-08-01T00:00:00+00:00', end: '2026-09-01T00:00:00+00:00' },
    computed_at: '2026-09-01T09:00:00+00:00',
    segment: {},
    case_count: 120,
    recovered_case_count: 14,
    revenue_at_risk: money(310_000_000, '₹31,00,000.00'),
    observed_recovered_revenue: money(45_200_000, OBSERVED_FORMATTED),
    natural_recovered_revenue: money(9_100_000, '₹91,000.00'),
    total_recovery_cost: money(120_000, '₹1,200.00'),
    net_recovered_revenue: money(45_080_000, '₹4,50,800.00'),
    unresolved_revenue: money(74_000_000, '₹7,40,000.00'),
    financial_cost: money(2_700, '₹27.00'),
    communication_cost: money(225, '₹2.25'),
    risk_cost: money(0, '₹0.00'),
    customer_cost: money(9_000, '₹90.00'),
    total_action_cost: money(11_925, '₹119.25'),
    incremental_recovered_revenue: {
      status: 'NOT_ESTABLISHED',
      value: 'NOT_ESTABLISHED',
      refusal_codes: ['NO_COMPLETED_EXPERIMENT'],
      detail:
        'No completed, adequately powered experiment with a lift interval entirely above zero ' +
        'supports a causal claim for this period.',
    },
    recovery_rate: rate,
    intervention_rate: rate,
    action_success_rate: rate,
    escalation_rate: rate,
    average_hours_to_recovery: rate,
    blocked_case_count: 3,
    escalated_case_count: 1,
    intervened_case_count: 9,
    confirmed_action_count: 9,
    successful_action_count: 5,
    unnecessary_action_count: 2,
    cycles_without_action_count: 7,
    labels: ['CAUSALITY_NOT_ESTABLISHED', 'RECOVERY_GROSS_OF_REFUNDS', 'UNCALIBRATED'],
    causality_established: false,
    is_synthetic: false,
    ...overrides,
  }
}

function mountWith(body) {
  vi.stubGlobal(
    'fetch',
    vi.fn(() =>
      Promise.resolve({
        ok: true,
        status: 200,
        headers: new Headers({ 'x-correlation-id': 'test' }),
        json: () => Promise.resolve(body),
      }),
    ),
  )
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={client}>
      <Metrics />
    </QueryClientProvider>,
  )
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('the incremental revenue figure', () => {
  it('renders NOT ESTABLISHED, and never substitutes the observed figure for it', async () => {
    mountWith({ report: report(), incremental_available: true })

    await waitFor(() => {
      expect(screen.getByText('NOT ESTABLISHED')).toBeInTheDocument()
    })

    // Exactly once: the headline. Every other amount in the fixture is a distinct string, so a
    // second occurrence could only mean the incremental slot rendered the observed figure.
    expect(screen.getAllByText(OBSERVED_FORMATTED).length).toBe(1)

    // The reason is shown, because "why not?" is the immediate next question.
    expect(screen.getByText('NO_COMPLETED_EXPERIMENT')).toBeInTheDocument()
  })

  it('keeps the causality caveat beside observed recovery', async () => {
    mountWith({ report: report(), incremental_available: true })
    await waitFor(() => {
      expect(screen.getAllByText('CAUSALITY NOT ESTABLISHED').length).toBeGreaterThan(0)
    })
  })

  it('renders a timed-out incremental figure as unavailable and keeps every other figure', async () => {
    // R14.C16. One figure degrades; the page does not. And the caveat stays on, because a figure that
    // could not be computed is certainly not a causal claim that was established.
    mountWith({
      report: report({
        incremental_recovered_revenue: {
          status: 'DATA_UNAVAILABLE',
          figure: 'incremental_recovered_revenue',
          detail: 'the experiment analysis did not complete within DASHBOARD_METRICS_TIMEOUT',
        },
      }),
      incremental_available: false,
    })

    await waitFor(() => {
      expect(screen.getByText('DATA UNAVAILABLE')).toBeInTheDocument()
    })
    expect(screen.getAllByText(OBSERVED_FORMATTED).length).toBe(1)
    expect(screen.getAllByText('CAUSALITY NOT ESTABLISHED').length).toBeGreaterThan(0)
  })

  it('shows a real amount only when an experiment established it', async () => {
    mountWith({
      report: report({
        incremental_recovered_revenue: {
          status: 'ESTABLISHED',
          amount: {
            status: 'PRESENT',
            minor: 12_000_000,
            currency: 'INR',
            formatted: '₹1,20,000.00',
          },
          experiment_id: 'exp-1',
          control_case_count: 800,
          treatment_case_count: 800,
          lift: '0.0410',
          lift_interval: '[0.0120, 0.0700]',
        },
        causality_established: true,
      }),
      incremental_available: true,
    })

    await waitFor(() => {
      expect(screen.getByText('₹1,20,000.00')).toBeInTheDocument()
    })
    expect(screen.queryByText('NOT ESTABLISHED')).toBeNull()
    // The interval travels with the lift. A lift shown without one supports nothing.
    expect(screen.getByText('[0.0120, 0.0700]')).toBeInTheDocument()
  })

  it('reports absent arm counts rather than defaulting them to zero', async () => {
    // A null arm count means the analysis did not report one. Zero would claim an empty control arm,
    // making an established causal claim look unsupported by its own numbers.
    mountWith({
      report: report({
        incremental_recovered_revenue: {
          status: 'ESTABLISHED',
          amount: {
            status: 'PRESENT',
            minor: 12_000_000,
            currency: 'INR',
            formatted: '₹1,20,000.00',
          },
          experiment_id: 'exp-1',
          control_case_count: null,
          treatment_case_count: null,
          lift: '0.0410',
          lift_interval: '[0.0120, 0.0700]',
        },
      }),
      incremental_available: true,
    })

    await waitFor(() => {
      expect(screen.getByText('arm counts not reported')).toBeInTheDocument()
    })
    expect(screen.queryByText('0 / 0')).toBeNull()
  })
})

describe('the counts nobody else reports', () => {
  it('shows unnecessary actions and cycles without action', async () => {
    // Two figures that make Revora look worse and are reported anyway: money spent on customers who
    // would have paid regardless, and cycles where it deliberately did nothing.
    mountWith({ report: report(), incremental_available: true })
    await waitFor(() => {
      expect(screen.getByText('Unnecessary actions')).toBeInTheDocument()
    })
    expect(screen.getByText('Cycles without action')).toBeInTheDocument()
  })
})
