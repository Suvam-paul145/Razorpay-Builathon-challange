/**
 * Task 41.4. A customer's own words, presented as a stranger's assertion (R20.C12, R29.C11).
 *
 * Three claims, and each one rules out a specific wrong rendering.
 *
 * **The note is labelled.** R20.C12 requires it presented *marked as* customer-supplied unverified
 * text, and the mark is a fact about the data rather than a styling choice — so the server chooses
 * the words and this asserts they are rendered. A panel that showed the note with no label would be
 * a screen on which a stranger's assertion reads exactly like a finding Revora reached.
 *
 * **Markup is text.** The note fixture is the canonical XSS payload. After render there is no
 * `<script>` element in the document and the payload is present as visible characters. That is the
 * whole of R29.C11 at the surface that renders it: React escapes a text child, and this is what
 * fails if somebody ever reaches for `dangerouslySetInnerHTML` — which a `pure`-tier Python test
 * also forbids across the whole tree, because a second component must inherit the guarantee rather
 * than be trusted to repeat it.
 *
 * **Every signal appears, including the silent ones.** A `PAGE_VIEWED` row caused nothing and is
 * still evidence: "the customer opened the link and said nothing" is the answer to a merchant asking
 * whether the message landed. A redacted note renders as a redaction rather than as an absence,
 * because "wrote nothing" and "wrote something we may no longer hold" are different histories and
 * only one of them lets a reader conclude the customer stayed silent.
 *
 * Only `/cases/:id` is stubbed with data. The timeline and audit reads are allowed to fail, and the
 * panel under test still renders — which is itself the arrangement R26.C10 describes and the reason
 * the timeline is a separate query.
 */

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { CaseDetail } from './CaseDetail'

const CASE_ID = 'c0000000-0000-4000-8000-000000000041'
const PAYLOAD = '<script>alert(1)</script>'
const LABEL = 'customer-supplied unverified text'

/** The absent marker the server sends for a figure the pipeline has not produced. */
function absent(figure) {
  return {
    status: 'NOT_YET_RECORDED',
    case_state: 'POLICY_CHECK',
    detail: `no ${figure} has been recorded yet; the case is POLICY_CHECK`,
  }
}

function money(minor) {
  return { status: 'PRESENT', minor, currency: 'INR', formatted: `₹${(minor / 100).toFixed(2)}` }
}

/**
 * A case detail document with three signals: a stated reason with a markup note, a hard stop whose
 * note the retention sweep removed, and a silent page view.
 */
function detailDocument(overrides = {}) {
  return {
    case: {
      case_id: CASE_ID,
      state: 'POLICY_CHECK',
      state_label: 'Decision recorded',
      detected_at: '2026-09-01T00:00:00+00:00',
      window_end_at: '2026-09-08T00:00:00+00:00',
      payment_amount: money(249_900),
      recovered_amount: absent('recovered amount'),
      outcome_classification: absent('outcome'),
      provider_payment_id: 'pay_signals',
      customer_contact_masked: '+91******01',
      risk_cause: 'INSUFFICIENT_FUNDS',
      selected_action: null,
      selected_action_label: null,
      waiting: null,
      human_owner_user_id: null,
      provenance: 'REAL',
    },
    counters: {
      executed_action_count: 0,
      customer_message_count: 0,
      decision_cycle_count: 1,
      max_recovery_attempts: 3,
      max_customer_messages: 2,
      last_outbound_at: null,
    },
    diagnosis: absent('diagnosis'),
    baseline: absent('baseline estimate'),
    recommendation: absent('recommendation'),
    policy_decisions: absent('policy decision'),
    executed_actions: absent('executed action'),
    authoritative_reads: absent('authoritative provider read'),
    outcome: absent('verified outcome'),
    consent: absent('consent record'),
    contact_suppression: null,
    customer_signals: [
      {
        signal_id: 's0000000-0000-4000-8000-000000000001',
        kind: 'DELAY_REASON',
        submitted_at: '2026-09-02T09:00:00+00:00',
        delay_reason: 'SALARY_OR_CASHFLOW_TIMING',
        hard_stop_label: null,
        note: {
          status: 'PRESENT',
          label: LABEL,
          verified: false,
          render_as: 'TEXT_ONLY',
          text: PAYLOAD,
          text_escaped: '&lt;script&gt;alert(1)&lt;/script&gt;',
          truncated: true,
          redacted_at: null,
        },
        retention_config_version: null,
        provenance: 'REAL',
      },
      {
        signal_id: 's0000000-0000-4000-8000-000000000002',
        kind: 'DELAY_REASON',
        submitted_at: '2026-09-03T09:00:00+00:00',
        delay_reason: 'DISPUTES_THE_CHARGE',
        hard_stop_label: 'Disputes the charge',
        note: {
          status: 'REDACTED',
          label: LABEL,
          verified: false,
          render_as: 'TEXT_ONLY',
          redacted_at: '2027-04-01T00:00:00+00:00',
          detail:
            'this customer wrote a note and it has passed CUSTOMER_DATA_RETENTION, so it was ' +
            'deleted. The signal itself is retained.',
        },
        retention_config_version: '2025.01.0-assumption-baseline',
        provenance: 'REAL',
      },
      {
        signal_id: 's0000000-0000-4000-8000-000000000003',
        kind: 'PAGE_VIEWED',
        submitted_at: '2026-09-01T12:00:00+00:00',
        delay_reason: null,
        hard_stop_label: null,
        note: null,
        retention_config_version: null,
        provenance: 'REAL',
      },
    ],
    // R23.C14. `null` by default, which is the ordinary case: most customers never name a date.
    promise: null,
    terminal_reason: null,
    ...overrides,
  }
}

/**
 * A recorded promise whose follow-up was **clamped** by the window.
 *
 * The numbers are chosen so the clamp is the interesting thing about them: the customer promised the
 * 7th at 23:30, the window closes on the 8th at 00:00, and the follow-up is the 7th at 23:00 — the
 * window end minus the safety margin, and *earlier than the date the customer gave*. Rendered
 * without the window end beside it that follow-up is an arbitrary time, and worse than arbitrary:
 * it is Revora chasing before the day it agreed to wait for. The adjacency is what makes it read as
 * a clamp instead, and it is what the assertions below check the order of.
 */
function clampedPromise(overrides = {}) {
  return {
    promise_id: 'p0000000-0000-4000-8000-000000000001',
    customer_signal_id: 's0000000-0000-4000-8000-000000000001',
    status: 'RECORDED',
    promise_date: '2026-09-07T23:30:00+00:00',
    window_end_at: '2026-09-08T00:00:00+00:00',
    follow_up_at: '2026-09-07T23:00:00+00:00',
    recorded_at: '2026-09-02T09:00:00+00:00',
    kept_at: null,
    missed_at: null,
    seconds_promise_to_payment: null,
    voided_by_terminal_state: null,
    ...overrides,
  }
}

function mountWith(detail) {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url) => {
      const path = String(url)
      if (path.endsWith(`/cases/${CASE_ID}`)) {
        return { ok: true, status: 200, headers: new Headers(), json: async () => detail }
      }
      // The timeline and the audit trail are deliberately unavailable. Every section below them
      // still renders, which is the arrangement R26.C10 describes.
      return {
        ok: false,
        status: 503,
        headers: new Headers(),
        json: async () => ({ detail: 'unavailable' }),
      }
    }),
  )
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <MemoryRouter initialEntries={[`/cases/${CASE_ID}`]}>
      <QueryClientProvider client={client}>
        <Routes>
          <Route path="/cases/:caseId" element={<CaseDetail />} />
        </Routes>
      </QueryClientProvider>
    </MemoryRouter>,
  )
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('what the customer said', () => {
  it('labels the note as customer-supplied unverified text', async () => {
    mountWith(detailDocument())

    await waitFor(() => {
      expect(screen.getAllByText(LABEL).length).toBeGreaterThan(0)
    })
    // The label is the server's string, rendered verbatim. A client that composed the phrase would
    // be a second vocabulary free to soften it, and the direction it would drift is towards reading
    // a stranger's assertion as a finding.
    expect(screen.getAllByText(LABEL)[0]).toHaveClass('label--caution')
  })

  it('renders markup in a note as characters and never as elements', async () => {
    mountWith(detailDocument())

    await waitFor(() => {
      expect(screen.getByText(PAYLOAD)).toBeInTheDocument()
    })
    // R29.C11. The payload is visible text, and no element came out of it.
    expect(document.querySelector('script')).toBeNull()
    expect(document.querySelector('.note blockquote')?.textContent).toBe(PAYLOAD)
    // The escaped copy is for a surface that interpolates into markup. Rendering it here would
    // double-escape, so a customer who typed `<3` would read back `&lt;3`.
    expect(screen.queryByText('&lt;script&gt;alert(1)&lt;/script&gt;')).toBeNull()
  })

  it('shows the truncation, the hard stop, the redaction and the silent signal', async () => {
    mountWith(detailDocument())

    await waitFor(() => {
      expect(screen.getByText(/truncated at the stored length/)).toBeInTheDocument()
    })
    // The statement that caused the suppression, flagged as an objection rather than left for the
    // reader to recognise from the member's name.
    // Twice, and both are wanted: once as the stated reason, once as the flagged objection. The
    // words agree because the server's HARD_STOP_LABELS and the client's `humanise` happen to
    // produce the same sentence for this member, so the flag class is what distinguishes them.
    const objection = screen.getAllByText('Disputes the charge')
    expect(objection.length).toBeGreaterThanOrEqual(2)
    expect(objection.some((node) => node.classList.contains('flag--stop'))).toBe(true)
    // A redaction is a third state, not an absence. Collapsing it into "no note" would make a
    // compliance action look like a customer who stayed silent.
    expect(screen.getByText(/passed CUSTOMER_DATA_RETENTION/)).toBeInTheDocument()
    expect(
      screen.getByText(/under configuration 2025.01.0-assumption-baseline/),
    ).toBeInTheDocument()
    // Every signal, including the one that caused nothing.
    expect(screen.getByText('Page viewed')).toBeInTheDocument()
  })

  it('says so in words when the customer submitted nothing', async () => {
    mountWith(detailDocument({ customer_signals: [] }))

    await waitFor(() => {
      expect(screen.getByText('Nothing submitted on the customer page.')).toBeInTheDocument()
    })
  })
})

describe('what the customer promised', () => {
  it('puts the window end immediately after the promised date, so a clamp reads as one', async () => {
    mountWith(detailDocument({ promise: clampedPromise() }))

    await waitFor(() => {
      expect(screen.getByText('Promised date')).toBeInTheDocument()
    })

    // R23.C14's adjacency, asserted as DOM order rather than as "both are on the page". Both being
    // present is the weaker claim and it is the one that would pass if somebody moved the window end
    // to the bottom of the panel — at which point a reader comparing two dates has to hold one of
    // them in their head, which is exactly the reading the clause exists to prevent.
    const labels = Array.from(document.querySelectorAll('[data-field="promise"] dt')).map(
      (node) => node.textContent,
    )
    expect(labels.slice(0, 3)).toEqual([
      'Promised date',
      'Recovery window closes',
      'Follow-up',
    ])

    // The machine-readable instants stay on the `<time>` elements whatever the reader's locale turns
    // the visible text into. Three of them, in the same order.
    const instants = Array.from(
      document.querySelectorAll('[data-field="promise"] time'),
    ).map((node) => node.getAttribute('datetime'))
    expect(instants.slice(0, 3)).toEqual([
      '2026-09-07T23:30:00+00:00',
      '2026-09-08T00:00:00+00:00',
      '2026-09-07T23:00:00+00:00',
    ])

    // And the clamp is said in words, because a reader who does not do the arithmetic should not
    // have to. Derived from comparing the three instants given, never by recomputing the configured
    // offset — see the panel's own note on why.
    expect(screen.getByText(/the window closes first/)).toBeInTheDocument()
  })

  it('presents a date past the window as an escalation, not as a follow-up not yet scheduled', async () => {
    mountWith(
      detailDocument({
        promise: clampedPromise({
          status: 'BEYOND_WINDOW_ESCALATED',
          promise_date: '2026-10-01T09:00:00+00:00',
          follow_up_at: null,
        }),
      }),
    )

    await waitFor(() => {
      expect(screen.getByText(/none scheduled/)).toBeInTheDocument()
    })
    // An empty cell would read as "we have not got to it yet". Nothing is pending: the window is
    // never extended, so there is nothing to schedule and a person has the case.
    expect(screen.getByText(/the case is\s+with a person/)).toBeInTheDocument()
    // The footnote's own wording, not the subtitle's — both say the window is never
    // extended, and matching on the shared phrase would find two nodes and pass for the
    // wrong reason.
    expect(
      screen.getByText(/escalates\s+to a person instead of stretching it/),
    ).toBeInTheDocument()
  })

  it('reports a promise kept early as early, because the interval is signed', async () => {
    mountWith(
      detailDocument({
        promise: clampedPromise({
          status: 'KEPT',
          kept_at: '2026-09-06T09:00:00+00:00',
          seconds_promise_to_payment: -86_400,
        }),
      }),
    )

    await waitFor(() => {
      expect(screen.getByText(/86400 seconds before/)).toBeInTheDocument()
    })
    // The sign is the whole content of this assertion. A surface that took the absolute value would
    // report a customer who paid a day early identically to one who paid a day late.
    expect(screen.queryByText(/86400 seconds after/)).toBeNull()
  })

  it('says so in words when no date was promised, which is most cases', async () => {
    mountWith(detailDocument())

    await waitFor(() => {
      expect(screen.getByText('No payment date recorded.')).toBeInTheDocument()
    })
  })
})
