/**
 * Task 51.3. The customer page renders its declared field set and nothing beyond it (R19.C2, R19.C3).
 *
 * ## On the field count: eight, not nine
 *
 * The task text says "renders nine fields and no tenth". It is a miscount, and it is the *same*
 * miscount the design makes: the design's prose says "nine fields" and then enumerates eight, in its
 * JSON sample and again in its bullet list, and the requirements enumerate the same eight (R19.C1's
 * seven presented items, with the amount's currency counted separately). The enumeration is the
 * requirement and the arithmetic is the error.
 *
 * It is not a judgement call, either, because the server settles it: `revora/customer/projection.py`
 * derives `PROJECTION_FIELDS` from a frozen, slotted dataclass and `as_document` **raises at runtime**
 * if the emitted key set diverges from it. Eight fields is what the endpoint can physically send.
 *
 * So this file asserts eight. Do not "fix" it back to nine — inventing a ninth field to satisfy the
 * count would be a disclosure decision taken by arithmetic, on the one surface in Revora where every
 * field is a disclosure decision (R19.C2). If a ninth field is ever genuinely wanted, it starts in
 * that dataclass.
 *
 * ## What each claim rules out
 *
 * * **the field set** — exactly the eight `data-field` elements, asserted in both directions. Rules
 *   out a ninth rendered from a response that grew, and an eighth quietly dropped;
 * * **the money element** — carries the server's `formatted` string verbatim and keeps `data-minor`
 *   as an attribute, with no divided or undivided form of the integer anywhere in the text. Rules out
 *   a page that recomputed a figure the server had already formatted;
 * * **`promise` is null and that is not an error** — null in every response today, and the page shows
 *   a plain sentence rather than a failure. Rules out the field lighting up every page in the system;
 * * **the refusals** — each status code gets its own sentence, 404 covers the tenant and all four
 *   token failures identically, and a 429 on a *write* leaves the projection on screen. Rules out a
 *   blank page for the customer who has already explained themselves;
 * * **the promise lead time** — rejected client-side without a request, while the server stays the
 *   authority.
 *
 * These tests do not constitute a WCAG conformance claim. They check mechanical things: native
 * `<form>`, `<fieldset>`, `<legend>`, `<label>` and `<select>` elements, a label bound to its control,
 * and a validation summary that is a live region referenced by `aria-describedby`. Full WCAG 2.1 AA
 * conformance validation requires manual testing with assistive technologies and expert accessibility
 * review, and neither has been done.
 */

import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { Page } from './Page'

const LINK = {
  merchantSlug: 'acme-tools',
  token: 'rvc_a2b3c4d5e6f7g2h3i4j5k6l7m2.AAAAAAAAAAAAAAAAAAAAAA',
}

/** The Indian grouping the server produced. No browser default formatter emits this. */
const FORMATTED = '₹12,34,567.89'
const MINOR = 123456789

/** The eight keys `revora/customer/projection.py` declares. Restated here so the test is readable. */
const EIGHT_FIELDS = [
  'merchant_display_name',
  'amount',
  'currency',
  'reason',
  'pay_url',
  'window_end_at',
  'promise',
  'signals_remaining',
]

/**
 * The projection exactly as the endpoint sends it.
 *
 * `promise: null` by default because that is what every response carries today — nothing writes a
 * Promise_To_Pay yet.
 */
function projection(overrides = {}) {
  return {
    merchant_display_name: 'Acme Tools',
    amount: { status: 'PRESENT', minor: MINOR, currency: 'INR', formatted: FORMATTED },
    currency: 'INR',
    reason:
      'The payment did not go through because there were not enough funds available at the time.',
    pay_url: 'https://rzp.io/i/abcd1234',
    window_end_at: '2026-09-15T18:30:00+00:00',
    promise: null,
    signals_remaining: 3,
    ...overrides,
  }
}

/**
 * Stub `fetch` with one read answer and, optionally, one write answer.
 *
 * A queue rather than a single response, because the write assertions need the read to succeed first —
 * the page has to be on screen before a refused write can be shown not to remove it.
 */
function stubFetch(read, write = null) {
  const answers = write === null ? [read] : [read, write]
  let call = 0
  const fetchMock = vi.fn(() => {
    const answer = answers[Math.min(call, answers.length - 1)]
    call += 1
    return Promise.resolve({
      ok: answer.status >= 200 && answer.status < 300,
      status: answer.status,
      json: () =>
        answer.body === undefined
          ? Promise.reject(new Error('empty body'))
          : Promise.resolve(answer.body),
    })
  })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

async function mount(read, write = null) {
  const fetchMock = stubFetch(read, write)
  const rendered = render(<Page link={LINK} />)
  return { ...rendered, fetchMock }
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('the projection', () => {
  it('renders exactly its eight declared fields and no ninth', async () => {
    const { container } = await mount({ status: 200, body: projection() })

    await waitFor(() => {
      expect(container.querySelector('[data-field="amount"]')).not.toBeNull()
    })

    const rendered = [...container.querySelectorAll('[data-field]')].map((node) =>
      node.getAttribute('data-field'),
    )
    // Both directions. A one-way check would pass a page that rendered seven of the eight, and it
    // would also pass a page that rendered a ninth field the server never sent.
    expect(rendered.slice().sort()).toEqual(EIGHT_FIELDS.slice().sort())
    expect(rendered).toHaveLength(8)
  })

  it('renders each field with the value the server sent', async () => {
    const { container } = await mount({ status: 200, body: projection() })

    await waitFor(() => {
      expect(screen.getByText('Acme Tools')).toBeInTheDocument()
    })
    expect(container.querySelector('[data-field="currency"]').textContent).toBe('INR')
    expect(container.querySelector('[data-field="reason"]').textContent).toContain(
      'not enough funds',
    )
    // The machine-readable instant stays on the `<time>` element, whatever the reader's locale turns
    // the visible text into.
    expect(container.querySelector('[data-field="window_end_at"] time').getAttribute('datetime')).toBe(
      '2026-09-15T18:30:00+00:00',
    )
    // The payment link is a bearer capability the customer already holds; `noreferrer` is what stops
    // this page's URL — which contains the token — travelling with the navigation.
    const payLink = container.querySelector('[data-field="pay_url"] a')
    expect(payLink.getAttribute('href')).toBe('https://rzp.io/i/abcd1234')
    expect(payLink.getAttribute('rel')).toContain('noreferrer')
    expect(container.querySelector('[data-field="signals_remaining"]').textContent).toContain('3')
  })

  it('discloses none of the excluded fields, even when the response is padded with them', async () => {
    // R19.C2 and P34 from the client side. The server cannot send these — its document is assembled
    // from a slotted dataclass and the key set is checked at runtime — so this fixture is impossible
    // in production. The claim being tested is that the *page* renders no field it was not built to
    // render, which is what would fail if somebody iterated the response's keys.
    const { container } = await mount({
      status: 200,
      body: {
        ...projection(),
        net_recovery_value: { status: 'PRESENT', minor: 412000, currency: 'INR', formatted: '₹4,120.00' },
        baseline_recovery_probability: '0.31',
        total_action_cost: { status: 'PRESENT', minor: 300, currency: 'INR', formatted: '₹3.00' },
        policy_decision: 'APPROVED',
        customer_contact_masked: '+91 ****3210',
      },
    })

    await waitFor(() => {
      expect(container.querySelector('[data-field="amount"]')).not.toBeNull()
    })
    expect(container.querySelectorAll('[data-field]')).toHaveLength(8)
    const text = container.textContent ?? ''
    expect(text).not.toContain('₹4,120.00')
    expect(text).not.toContain('0.31')
    expect(text).not.toContain('APPROVED')
    expect(text).not.toContain('3210')
  })
})

describe('the money element', () => {
  it('carries the server string and derives nothing from it', async () => {
    const { container } = await mount({ status: 200, body: projection() })

    await waitFor(() => {
      expect(container.querySelector('.money')).not.toBeNull()
    })
    const money = container.querySelector('.money')
    // Rendered through the shared `<Money>` in `../components/Figure.jsx` — one implementation, and
    // this entry has no currency symbol table, no divisor and no `Intl.NumberFormat` call with which
    // to produce a second one.
    expect(money.textContent).toBe(FORMATTED)
    // `data-minor` rides along for assertions like this one and is never read for display.
    expect(money.getAttribute('data-minor')).toBe(String(MINOR))

    const text = container.textContent ?? ''
    // Neither the undivided integer nor a divided form of it. Both would be a figure computed here.
    expect(text).not.toContain('123456789')
    expect(text).not.toContain('1234567.89')
    expect(text).not.toContain('1,234,567.89')
  })
})

describe('the promise field', () => {
  it('shows a plain sentence when it is null, which is every response today', async () => {
    const { container } = await mount({ status: 200, body: projection() })

    await waitFor(() => {
      expect(container.querySelector('[data-field="promise"]')).not.toBeNull()
    })
    // Not an error, not a marker, not a warning. Nothing writes a Promise_To_Pay yet, so this is the
    // ordinary case and it reads as one.
    expect(container.querySelector('[data-field="promise"]').textContent).toContain(
      'No payment date recorded yet',
    )
    expect(container.querySelector('[role="alert"]')).toBeNull()
  })

  it('renders the recorded date and status when one exists', async () => {
    const { container } = await mount({
      status: 200,
      body: projection({
        promise: { promise_date: '2026-09-10T09:00:00+00:00', status: 'ACTIVE' },
      }),
    })

    await waitFor(() => {
      expect(container.querySelector('[data-field="promise"] time')).not.toBeNull()
    })
    expect(
      container.querySelector('[data-field="promise"] time').getAttribute('datetime'),
    ).toBe('2026-09-10T09:00:00+00:00')
    expect(container.querySelector('[data-field="promise"]').textContent).toContain('ACTIVE')
  })
})

describe('the forms', () => {
  it('are native elements with labels bound to their controls', async () => {
    const { container } = await mount({ status: 200, body: projection() })

    await waitFor(() => {
      expect(container.querySelectorAll('form')).toHaveLength(2)
    })
    // Native semantics rather than reimplemented ones: two `<form>`s, each with a `<fieldset>` and a
    // `<legend>`, a real `<select>`, and every control reachable by its label's text.
    expect(container.querySelectorAll('fieldset')).toHaveLength(2)
    expect(container.querySelectorAll('legend')).toHaveLength(2)
    const select = screen.getByLabelText('Reason')
    expect(select.tagName).toBe('SELECT')
    // Six members plus the unchosen placeholder.
    expect(select.querySelectorAll('option')).toHaveLength(7)
    expect(screen.getByLabelText('Date and time you expect to pay').tagName).toBe('INPUT')

    // The summary is a live region, present before it has anything to say, and named by the control
    // it describes.
    expect(select.getAttribute('aria-describedby')).toContain('cp-reason-summary')
    const summary = container.querySelector('#cp-reason-summary')
    expect(summary.getAttribute('aria-live')).toBe('polite')
  })

  it('rejects a promise date inside the courtesy lead time without asking the server', async () => {
    const { container, fetchMock } = await mount({ status: 200, body: projection() })

    await waitFor(() => {
      expect(container.querySelectorAll('form')).toHaveLength(2)
    })
    expect(fetchMock).toHaveBeenCalledTimes(1) // the read, and nothing else yet

    // Ten minutes from now: in the future, so the server would accept it today — the server enforces
    // only "in the future" and there is no configured `PROMISE_MIN_LEAD_TIME` to read. This is the
    // client being a courtesy, not the client being the authority.
    const soon = new Date(Date.now() + 10 * 60 * 1000)
    const local = new Date(soon.getTime() - soon.getTimezoneOffset() * 60 * 1000)
      .toISOString()
      .slice(0, 16)
    const input = screen.getByLabelText('Date and time you expect to pay')
    fireEvent.change(input, { target: { value: local } })
    fireEvent.submit(input.closest('form'))

    await waitFor(() => {
      expect(screen.getByText('Please choose a time at least an hour from now.')).toBeInTheDocument()
    })
    // No request was made, so no submission was spent. That is the entire benefit of the check.
    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(container.querySelector('#cp-promise-summary').textContent).toContain('Please check')
  })

  it('sends a valid promise as an instant, with JSON declared', async () => {
    const { container, fetchMock } = await mount(
      { status: 200, body: projection() },
      { status: 201, body: { recorded: true, signals_remaining: 2 } },
    )

    await waitFor(() => {
      expect(container.querySelectorAll('form')).toHaveLength(2)
    })
    const later = new Date(Date.now() + 3 * 24 * 60 * 60 * 1000)
    const local = new Date(later.getTime() - later.getTimezoneOffset() * 60 * 1000)
      .toISOString()
      .slice(0, 16)
    const input = screen.getByLabelText('Date and time you expect to pay')
    fireEvent.change(input, { target: { value: local } })
    fireEvent.submit(input.closest('form'))

    await waitFor(() => {
      expect(screen.getByText('Thank you. We have recorded that.')).toBeInTheDocument()
    })
    const [url, init] = fetchMock.mock.calls[1]
    expect(url).toBe(`/customer/${LINK.merchantSlug}/promise`)
    // Mandatory: the API answers 415 before parsing otherwise, and declaring it is also what forces
    // the CORS preflight so the origin list is consulted before the token is sent.
    expect(init.headers['content-type']).toBe('application/json')
    expect(init.headers.authorization).toBe(`Bearer ${LINK.token}`)
    expect(init.credentials).toBe('omit')
    // The body is the one declared field and nothing else — `extra="forbid"` on the server.
    const sent = JSON.parse(init.body)
    expect(Object.keys(sent)).toEqual(['promise_date'])
    expect(sent.promise_date).toMatch(/^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d/)

    // `signals_remaining` came from the server's 201 rather than being decremented here.
    await waitFor(() => {
      expect(container.querySelector('[data-field="signals_remaining"]').textContent).toContain('2')
    })
  })

  it('requires a reason to be chosen before sending one', async () => {
    const { container, fetchMock } = await mount({ status: 200, body: projection() })

    await waitFor(() => {
      expect(container.querySelectorAll('form')).toHaveLength(2)
    })
    fireEvent.submit(screen.getByLabelText('Reason').closest('form'))

    await waitFor(() => {
      expect(
        screen.getByText('Please choose a reason, so we know what to do next.'),
      ).toBeInTheDocument()
    })
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it('warns before a hard-stop reason is sent', async () => {
    const { container } = await mount({ status: 200, body: projection() })

    await waitFor(() => {
      expect(container.querySelectorAll('form')).toHaveLength(2)
    })
    // Both of these permanently suppress automated contact and escalate to a person. A customer
    // choosing one is making a larger decision than the sentence looks, so the page says so.
    fireEvent.change(screen.getByLabelText('Reason'), { target: { value: 'DISPUTES_THE_CHARGE' } })
    await waitFor(() => {
      expect(screen.getByText(/stop sending you reminders/)).toBeInTheDocument()
    })
  })

  it('are absent, not disabled, once no submissions remain', async () => {
    const { container } = await mount({ status: 200, body: projection({ signals_remaining: 0 }) })

    await waitFor(() => {
      expect(container.querySelector('[data-field="amount"]')).not.toBeNull()
    })
    // The read is still served at the cap, which is the point: a customer who has explained
    // themselves five times must still be able to see what they owe.
    expect(container.querySelector('.money').textContent).toBe(FORMATTED)
    expect(container.querySelectorAll('form')).toHaveLength(0)
  })
})

describe('the refusals', () => {
  it('says the same thing for an unknown tenant and every token failure', async () => {
    // 404 with an empty body covers an unknown slug, an unknown handle, a wrong secret, a retired
    // signing key and a malformed presentation — five conditions, one answer, deliberately
    // indistinguishable. A page that guessed which would leak the distinction the server hides.
    const { container, unmount } = await mount({ status: 404 })

    await waitFor(() => {
      expect(screen.getByText('This link does not work')).toBeInTheDocument()
    })
    expect(container.querySelector('[data-status="404"]')).not.toBeNull()
    const rejected = container.querySelector('.cp__refusal').textContent
    unmount()

    // A URL that cannot contain a token at all renders identically, without a request. Character for
    // character identically, which is the assertion — a difference of one word would be a difference a
    // probe could read.
    const unusable = render(<Page link={null} />)
    expect(unusable.container.querySelector('.cp__refusal').textContent).toBe(rejected)
  })

  it('distinguishes an expired link from a broken one', async () => {
    await mount({ status: 410, body: { expired: true } })
    // Expired and revoked, which the server does not separate either. A customer holding a dead link
    // needs to be told it is dead rather than shown something that reads as "wrong URL".
    await waitFor(() => {
      expect(screen.getByText('This link has expired')).toBeInTheDocument()
    })
  })

  it('separates the flood guard from the durable cap on a 429', async () => {
    const { unmount } = await mount({ status: 429, body: { rate: 'token' } })
    await waitFor(() => {
      // The process-local fixed-window limiter. Waiting genuinely clears this one.
      expect(screen.getByText('That was too quick')).toBeInTheDocument()
    })
    unmount()

    await mount({ status: 429, body: { rejected: 'CUSTOMER_SIGNAL_LIMIT_REACHED' } })
    await waitFor(() => {
      // The durable cap, incremented under a row lock. Waiting does not clear it, so the copy must
      // not suggest it will.
      expect(
        screen.getByText('You have already sent everything we can record'),
      ).toBeInTheDocument()
    })
  })

  it('keeps the projection on screen when a write is refused with a 429', async () => {
    const { container } = await mount(
      { status: 200, body: projection() },
      { status: 429, body: { rejected: 'CUSTOMER_SUBMISSION_LIMIT_REACHED' } },
    )

    await waitFor(() => {
      expect(container.querySelectorAll('form')).toHaveLength(2)
    })
    fireEvent.change(screen.getByLabelText('Reason'), { target: { value: 'OTHER' } })
    fireEvent.submit(screen.getByLabelText('Reason').closest('form'))

    await waitFor(() => {
      expect(
        screen.getByText(/You have already sent everything we can record/),
      ).toBeInTheDocument()
    })
    // The rule this test exists for. Reads are served at both caps, so a refused write reports itself
    // in the form's summary and leaves the amount, the reason and the payment link exactly where they
    // were.
    expect(container.querySelectorAll('[data-field]')).toHaveLength(8)
    expect(container.querySelector('.money').textContent).toBe(FORMATTED)
    expect(container.querySelector('[data-field="pay_url"] a')).not.toBeNull()
  })

  it('names the field on a 422 and nothing the customer typed', async () => {
    const { container } = await mount(
      { status: 200, body: projection() },
      { status: 422, body: { field: 'delay_reason' } },
    )

    await waitFor(() => {
      expect(container.querySelectorAll('form')).toHaveLength(2)
    })
    fireEvent.change(screen.getByLabelText('Reason'), { target: { value: 'OTHER' } })
    fireEvent.change(screen.getByLabelText(/Anything else/), {
      target: { value: 'a note the server never echoes' },
    })
    fireEvent.submit(screen.getByLabelText('Reason').closest('form'))

    await waitFor(() => {
      expect(screen.getByText(/delay reason was not accepted/)).toBeInTheDocument()
    })
    // The server sends a field name and no submitted value, so there is nothing here that could echo
    // one back — asserted because the note is text a stranger typed.
    expect(container.querySelector('#cp-reason-summary').textContent).not.toContain(
      'a note the server never echoes',
    )
  })

  it('says nothing was saved on a 503 and on an unreachable API', async () => {
    const { container, unmount } = await mount({ status: 503 })
    await waitFor(() => {
      // Unreadable signing credential, failed audit write, or a read over its budget. In all three
      // nothing was persisted, which is what makes "try again" honest rather than a duplicate risk.
      expect(screen.getByText(/cannot reach your payment details/)).toBeInTheDocument()
    })
    expect(container.querySelector('.cp__refusal').textContent).toContain(
      'Nothing has been saved or changed',
    )
    unmount()

    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.reject(new TypeError('Failed to fetch'))),
    )
    render(<Page link={LINK} />)
    await waitFor(() => {
      // Status 0: a refused connection, a DNS failure or a blocked preflight. Not dressed up as a
      // 5xx, because the server did not answer.
      expect(screen.getByText(/could not reach the seller/)).toBeInTheDocument()
    })
  })
})

describe('the token', () => {
  it('travels in an Authorization header and appears in no URL', async () => {
    const { container, fetchMock } = await mount({ status: 200, body: projection() })

    await waitFor(() => {
      expect(container.querySelector('[data-field="amount"]')).not.toBeNull()
    })
    const [url, init] = fetchMock.mock.calls[0]
    expect(url).toBe(`/customer/${LINK.merchantSlug}/case`)
    // Not a query string. A URL is the one place a credential routinely leaks — into referrers,
    // access logs and any third-party script on the page.
    expect(url).not.toContain(LINK.token)
    expect(init.headers.authorization).toBe(`Bearer ${LINK.token}`)
    expect(init.cache).toBe('no-store')
    expect(init.referrerPolicy).toBe('no-referrer')
    // And it is nowhere in the rendered page either.
    expect(container.innerHTML).not.toContain(LINK.token)
  })
})
