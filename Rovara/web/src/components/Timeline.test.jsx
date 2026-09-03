/**
 * Task 50.5. The timeline renders stages, absences and gaps without inventing anything (R26.C10–C13).
 *
 * Three claims, and each one has a specific wrong rendering it rules out:
 *
 * * **stage rendering** — nine `<li>` inside one `<ol>`, each with its ordinal, its status *as a
 *   word*, and both server sentences. Ruling out a list that conveyed status by colour alone, and one
 *   that rendered a sentence for a stage the server gave none for;
 * * **the absent-value path** — a timeline the server could not project shows the marker naming the
 *   case and **no stages at all**. Ruling out nine `UPCOMING` rows standing in for a projection that
 *   did not happen, which is R26.C10's "no substituted stage, no substituted status" exactly;
 * * **the gap banner** — the missing sequence numbers are named, the stages still render, and no
 *   stage claims `DONE` that the fixture did not mark `DONE`. Ruling out a banner that appeared
 *   beside a filled-in hole.
 *
 * Plus the money rule, which is asserted here as well as in `Figure.test.jsx` because this component
 * is the one place a currency figure arrives as a bare string rather than as an envelope: the wire
 * carries `formatted` and no `minor` at all, so the test checks the rendered text is the server's
 * string and that no derived form of it appears.
 *
 * These tests do not constitute a WCAG conformance claim. They check four mechanical things — native
 * `<ol>`/`<li>`, an accessible name carrying position and status, a focusable native `<button>`, and
 * `aria-expanded` moving with the disclosure. Full WCAG 2.1 AA conformance validation requires manual
 * testing with assistive technologies and expert accessibility review, and neither has been done.
 */

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { Timeline } from './Timeline'

const CASE_ID = 'c0000000-0000-4000-8000-000000000001'
const INSTANT = '2026-09-01T10:00:00+00:00'

const STAGE_NAMES = [
  'DETECTED',
  'DIAGNOSED',
  'BASELINE_ESTIMATED',
  'ALTERNATIVES_PRICED',
  'DECIDED',
  'POLICY_CHECKED',
  'EXECUTED',
  'CUSTOMER_RESPONDED',
  'OUTCOME_VERIFIED',
]

/** A stage as the server projects it. `DONE` stages carry an instant and two sentences. */
function stage(name, order, overrides = {}) {
  return {
    stage: name,
    status: 'UPCOMING',
    order,
    instant: null,
    decision_sentence: null,
    evidence_sentence: null,
    skip_reason: null,
    fields: {},
    ...overrides,
  }
}

/**
 * Nine stages: six done, one in progress, one skipped with a recorded reason, one not reached.
 *
 * All four statuses appear, deliberately. A fixture that produced only `DONE` rows would let a
 * component convey status by colour alone and still pass every assertion below, because there would
 * be nothing left to distinguish.
 */
function stages() {
  return STAGE_NAMES.map((name, index) => {
    const order = index + 1
    if (name === 'DETECTED') {
      return stage(name, order, {
        status: 'DONE',
        instant: INSTANT,
        decision_sentence: 'A failed payment of ₹12,34,567.89 was detected.',
        evidence_sentence: 'From provider payment pay_fixture.',
        // The money field arrives as the server's formatted string and carries no minor units.
        fields: { payment_amount: '₹12,34,567.89', provider_payment_id: 'pay_fixture' },
      })
    }
    if (name === 'DECIDED') {
      return stage(name, order, {
        status: 'DONE',
        instant: INSTANT,
        decision_sentence:
          'Chose Send a payment link (PAYMENT_LINK), worth ₹4,120.00. ' +
          'Runner-up Waiting and watching (WAIT) at ₹0.00.',
        evidence_sentence: 'Reason: it was worth the most.',
        fields: {
          selected_action: { label: 'Send a payment link', member: 'PAYMENT_LINK' },
          net_recovery_value: '₹4,120.00',
          runner_up_action: { label: 'Waiting and watching', member: 'WAIT' },
          reviews: [
            {
              sequence: 7,
              review_trigger: 'SCHEDULED_REVIEW',
              previous_selected_action: { label: 'Waiting and watching', member: 'DO_NOTHING' },
              new_selected_action: { label: 'Waiting and watching', member: 'WAIT' },
            },
          ],
        },
      })
    }
    if (name === 'EXECUTED') {
      return stage(name, order, { status: 'IN_PROGRESS' })
    }
    if (name === 'CUSTOMER_RESPONDED') {
      return stage(name, order, {
        status: 'SKIPPED',
        skip_reason: 'STATE_TRANSITION:EXPIRED',
      })
    }
    if (name === 'OUTCOME_VERIFIED') {
      // The one stage the case has not reached. `UPCOMING` and `SKIPPED` are both "no completing
      // record" and they mean opposite things about whether anything further will happen, so both
      // have to be in the fixture for the status-as-a-word assertion to mean anything.
      return stage(name, order)
    }
    return stage(name, order, {
      status: 'DONE',
      instant: INSTANT,
      decision_sentence: `Sentence for ${name}.`,
      evidence_sentence: `Evidence for ${name}.`,
    })
  })
}

function timeline(overrides = {}) {
  return {
    case_id: CASE_ID,
    stage_count: 9,
    stages: stages(),
    audit_sequence: {
      complete: true,
      record_count: 9,
      first_seq: 1,
      last_seq: 9,
      missing: [],
      starts_at_one: true,
      detail: null,
    },
    ai_explanation: null,
    ai_explanation_label: 'AI_GENERATED',
    ...overrides,
  }
}

function mountWith(payload) {
  vi.stubGlobal(
    'fetch',
    vi.fn(() =>
      Promise.resolve({
        ok: true,
        status: 200,
        headers: new Headers({ 'x-correlation-id': 'test' }),
        json: () => Promise.resolve(payload),
      }),
    ),
  )
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <Timeline caseId={CASE_ID} />
    </QueryClientProvider>,
  )
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('stage rendering', () => {
  it('renders nine stages in one ordered list, in order', async () => {
    const { container } = mountWith({ case_id: CASE_ID, available: true, timeline: timeline() })

    await waitFor(() => {
      expect(container.querySelectorAll('.timeline__item')).toHaveLength(9)
    })
    // Native `<ol>`, so position is conveyed by the platform rather than by the ordinals.
    const list = container.querySelector('ol.timeline')
    expect(list).not.toBeNull()
    expect(list.getAttribute('aria-label')).toMatch(/in order/)

    const ordinals = [...container.querySelectorAll('.timeline__ordinal')].map(
      (node) => node.textContent,
    )
    expect(ordinals).toEqual(['1', '2', '3', '4', '5', '6', '7', '8', '9'])
  })

  it('conveys every status as a word, not only as a colour', async () => {
    const { container } = mountWith({ case_id: CASE_ID, available: true, timeline: timeline() })

    await waitFor(() => {
      expect(container.querySelectorAll('.timeline__status')).toHaveLength(9)
    })
    // Each of the four statuses in the fixture appears as text. A class name alone would satisfy the
    // colour and fail the requirement.
    const words = [...container.querySelectorAll('.timeline__status')].map(
      (node) => node.textContent,
    )
    expect(words).toContain('Done')
    expect(words).toContain('In progress')
    expect(words).toContain('Skipped')
    expect(words).toContain('Not yet reached')

    // And the same three facts — position, stage, status — are in the control's accessible name, so a
    // reader who sees neither the ordinal nor the chip still gets all of them.
    const controls = [...container.querySelectorAll('.timeline__control')].map(
      (node) => node.textContent,
    )
    expect(controls[0]).toContain('Stage 1 of 9')
    expect(controls[0]).toContain('Detected')
    expect(controls[0]).toContain('Done')
  })

  it('renders both server sentences for a done stage and neither for one without a record', async () => {
    const { container } = mountWith({ case_id: CASE_ID, available: true, timeline: timeline() })

    await waitFor(() => {
      expect(screen.getByText('Reason: it was worth the most.')).toBeInTheDocument()
    })
    expect(
      screen.getByText('A failed payment of ₹12,34,567.89 was detected.'),
    ).toBeInTheDocument()

    // The IN_PROGRESS stage has no completing record, so the server sends no sentence and none is
    // invented here. A component that filled in "in progress…" prose would be asserting something
    // the audit trail does not say.
    const inProgress = container.querySelector('.timeline__item--in_progress')
    expect(inProgress.querySelector('.timeline__decision')).toBeNull()
    expect(inProgress.querySelector('.timeline__evidence')).toBeNull()
    expect(inProgress.querySelector('.timeline__instant')).toBeNull()
  })

  it('names a skipped stage by the record that evidences it', async () => {
    const { container } = mountWith({ case_id: CASE_ID, available: true, timeline: timeline() })

    await waitFor(() => {
      expect(container.querySelector('.timeline__item--skipped')).not.toBeNull()
    })
    // R26.C5. An audit event type, not prose, so it can be grepped against the trail below.
    expect(screen.getByText('STATE_TRANSITION:EXPIRED')).toBeInTheDocument()
  })

  it('renders money as the server string and derives nothing from it', async () => {
    const { container } = mountWith({ case_id: CASE_ID, available: true, timeline: timeline() })

    await waitFor(() => {
      expect(container.querySelectorAll('.timeline__item')).toHaveLength(9)
    })
    const text = container.textContent ?? ''
    // The Indian grouping the server produced, which no browser default formatter emits.
    expect(text).toContain('₹12,34,567.89')
    // No divided form, no undivided integer. The timeline's wire carries no minor units at all, so
    // neither could be produced here — this asserts that stays true.
    expect(text).not.toContain('123456789')
    expect(text).not.toContain('1,234,567.89')
  })

  it('shows a stage field set through a keyboard-operable disclosure', async () => {
    const { container } = mountWith({ case_id: CASE_ID, available: true, timeline: timeline() })

    await waitFor(() => {
      expect(container.querySelectorAll('.timeline__control')).toHaveLength(9)
    })
    // A real <button>: focusable, Enter and Space handled by the platform, tab order automatic.
    const control = container.querySelectorAll('.timeline__control')[4]
    expect(control.tagName).toBe('BUTTON')
    expect(control.getAttribute('type')).toBe('button')
    expect(control.getAttribute('aria-expanded')).toBe('false')

    const panelId = control.getAttribute('aria-controls')
    expect(container.querySelector(`#${panelId}`).hidden).toBe(true)

    control.focus()
    expect(document.activeElement).toBe(control)
    fireEvent.click(control)

    await waitFor(() => {
      expect(control.getAttribute('aria-expanded')).toBe('true')
    })
    expect(container.querySelector(`#${panelId}`).hidden).toBe(false)
    // R26.C14. The label and the stored member both render, through the shared <Enum>.
    expect(screen.getAllByText('Send a payment link').length).toBeGreaterThan(0)
    expect(screen.getAllByText('PAYMENT_LINK').length).toBeGreaterThan(0)
    // R30.C14. The review carries its trigger and both actions, and DO_NOTHING/WAIT share one label.
    expect(screen.getByText('SCHEDULED_REVIEW')).toBeInTheDocument()
    expect(screen.getAllByText('DO_NOTHING').length).toBeGreaterThan(0)
  })
})

describe('the absent-value path', () => {
  it('shows the marker naming the case and no stages at all', async () => {
    const { container } = mountWith({
      case_id: CASE_ID,
      available: false,
      timeline: null,
      unavailable: {
        status: 'DATA_UNAVAILABLE',
        figure: `case_timeline:${CASE_ID}`,
        detail:
          'the audit trail for this case could not be read within TIMELINE_QUERY_TIMEOUT. ' +
          'No stage is shown, because none was projected.',
      },
    })

    await waitFor(() => {
      expect(screen.getByText('DATA UNAVAILABLE')).toBeInTheDocument()
    })
    // R26.C10. The marker names this case, through the same <AbsentValue> the rest of the dashboard
    // uses — so a reader recognises the shape of the absence.
    expect(screen.getByText(`case_timeline:${CASE_ID}`)).toBeInTheDocument()
    expect(container.querySelector('.absent--unavailable')).not.toBeNull()

    // And nothing is substituted: no list, no rows, no statuses, no zero.
    expect(container.querySelector('ol.timeline')).toBeNull()
    expect(container.querySelectorAll('.timeline__item')).toHaveLength(0)
    expect(container.querySelectorAll('.timeline__status')).toHaveLength(0)
    expect(container.textContent).not.toMatch(/(^|\W)0(\W|$)/)
  })
})

describe('the gap banner', () => {
  it('names the missing sequence numbers and still renders every stage', async () => {
    const { container } = mountWith({
      case_id: CASE_ID,
      available: true,
      timeline: timeline({
        audit_sequence: {
          complete: false,
          record_count: 7,
          first_seq: 1,
          last_seq: 9,
          missing: [4, 6],
          starts_at_one: true,
          detail:
            "This case's audit sequence is incomplete. Sequence numbers are allocated inside " +
            'the transaction that writes the record, so a gap means the allocation was bypassed.',
        },
      }),
    })

    await waitFor(() => {
      expect(container.querySelector('.timeline-banner')).not.toBeNull()
    })
    // R26.C11. The numbers are named, not counted: "one record is missing" is not actionable and
    // "sequence 4 is missing" can be reconciled against the writer's own logs.
    const banner = container.querySelector('.timeline-banner')
    expect(banner.getAttribute('role')).toBe('alert')
    expect(banner.textContent).toContain('4')
    expect(banner.textContent).toContain('6')
    expect(banner.textContent).toMatch(/allocation was bypassed/)

    // The timeline still renders. A gap suppresses nothing.
    expect(container.querySelectorAll('.timeline__item')).toHaveLength(9)
    // And no stage was promoted: exactly the fixture's six DONE stages, none filled in to close
    // the hole the banner just admitted to.
    expect(container.querySelectorAll('.timeline__status--done')).toHaveLength(6)
  })

  it('shows no banner when the sequence is whole', async () => {
    const { container } = mountWith({ case_id: CASE_ID, available: true, timeline: timeline() })

    await waitFor(() => {
      expect(container.querySelectorAll('.timeline__item')).toHaveLength(9)
    })
    // The anti-vacuity half. A banner rendered unconditionally would pass the test above and put a
    // "the allocation was bypassed" claim on every case in the system.
    expect(container.querySelector('.timeline-banner')).toBeNull()
  })
})

describe('the model-generated paragraph', () => {
  it('is absent by default and marked AI_GENERATED when present', async () => {
    const { container, unmount } = mountWith({
      case_id: CASE_ID,
      available: true,
      timeline: timeline(),
    })

    await waitFor(() => {
      expect(container.querySelectorAll('.timeline__item')).toHaveLength(9)
    })
    // Nothing writes `ai_invocation` today, so this is the ordinary case and it renders nothing —
    // not an empty box and not a placeholder.
    expect(container.querySelector('.timeline-ai')).toBeNull()
    unmount()

    const withExplanation = mountWith({
      case_id: CASE_ID,
      available: true,
      timeline: timeline({
        ai_explanation: 'The link was chosen because it was worth the most.',
        ai_explanation_label: 'AI_GENERATED',
      }),
    })

    await waitFor(() => {
      expect(withExplanation.container.querySelector('.timeline-ai')).not.toBeNull()
    })
    // R26.C9. The label is prominent text beside the prose, because this is the one place on the
    // screen where a model's words could be read as a recorded fact.
    expect(screen.getByText('AI GENERATED')).toBeInTheDocument()
    // And the deterministic sentences are untouched — the server guarantees it, and this is the
    // client half of the same claim.
    expect(
      screen.getByText('A failed payment of ₹12,34,567.89 was detected.'),
    ).toBeInTheDocument()
  })
})
