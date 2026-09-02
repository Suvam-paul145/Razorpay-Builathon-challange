/**
 * The rendering rules the product's honesty depends on (R14.C12, R14.C15, R14.C16).
 *
 * These are the frontend equivalents of the no-float gate on the backend, and they matter **more**
 * now than they did under TypeScript: the compiler used to refuse an unhandled absent-value arm and
 * nothing refuses it in plain JavaScript. These tests plus the eslint rules are what is left.
 *
 * The most important assertion in this file is the negative one: that a zero never appears where a
 * figure is absent. Substituting zero for an unrecorded amount is a false financial statement, and it
 * is the single easiest bug to introduce here because `?? 0` reads as defensive.
 */

import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { AbsentValue, Money, Rate } from './Figure'

const AMOUNT = {
  status: 'PRESENT',
  minor: 123_456_789,
  currency: 'INR',
  // Indian grouping, produced by the server. No browser default renders this.
  formatted: '₹12,34,567.89',
}

describe('Money', () => {
  it('renders the server string verbatim, including lakh-and-crore grouping', () => {
    render(<Money value={AMOUNT} />)
    expect(screen.getByText('₹12,34,567.89')).toBeInTheDocument()
  })

  it('never derives a figure from minor units', () => {
    // The whole failure mode in one assertion. A component that divided by 100 would render
    // "1234567.89" or "₹1,234,567.89"; one that forgot to divide would render "123456789". Neither
    // string may appear anywhere in the output.
    const { container } = render(<Money value={AMOUNT} />)
    const text = container.textContent ?? ''
    expect(text).toBe('₹12,34,567.89')
    expect(text).not.toContain('123456789')
    expect(text).not.toContain('1,234,567')
  })

  it('keeps minor units available for sorting without rendering them', () => {
    // `minor` is legitimate for sorting a column and for CSV export, so it travels in the DOM as a
    // data attribute — reachable by a sort comparator, invisible to a reader.
    const { container } = render(<Money value={AMOUNT} />)
    expect(container.querySelector('[data-minor="123456789"]')).not.toBeNull()
  })

  it('renders an unrecorded amount as a marker naming the case state, never as zero', () => {
    render(
      <Money
        value={{
          status: 'NOT_YET_RECORDED',
          case_state: 'DETECTED',
          detail: 'no recovered amount has been recorded yet; the case is DETECTED',
        }}
      />,
    )
    const text = screen.getByTitle(/no recovered amount/).textContent ?? ''
    expect(text).toContain('NOT YET RECORDED')
    // The state is what makes the absence explicable: "no recovered amount" on a DETECTED case means
    // the pipeline has not got there, and on a BLOCKED case it means policy stopped it.
    expect(text).toContain('DETECTED')
    expect(text).not.toMatch(/(^|\W)0(\W|$)/)
    expect(text).not.toContain('₹')
    expect(text).not.toContain('—')
  })

  it('names the single figure that could not be computed', () => {
    render(
      <Money
        value={{
          status: 'DATA_UNAVAILABLE',
          figure: 'incremental_recovered_revenue',
          detail: 'the experiment analysis did not complete within DASHBOARD_METRICS_TIMEOUT',
        }}
      />,
    )
    expect(screen.getByText('DATA UNAVAILABLE')).toBeInTheDocument()
    expect(screen.getByText('incremental_recovered_revenue')).toBeInTheDocument()
  })
})

describe('Rate', () => {
  it('renders UNDEFINED as that word rather than as a zero', () => {
    // A rate with a zero denominator does not exist. Rendering it as 0 would be a false measurement,
    // and rendering it as an empty cell would look like a bug.
    render(<Rate value={{ status: 'PRESENT', value: 'UNDEFINED' }} />)
    const element = screen.getByText('UNDEFINED')
    expect(element).toBeInTheDocument()
    expect(element.textContent).not.toBe('0')
  })

  it('renders a real rate as the server string, undivided', () => {
    render(<Rate value={{ status: 'PRESENT', value: '0.4211' }} />)
    expect(screen.getByText('0.4211')).toBeInTheDocument()
  })
})

describe('AbsentValue', () => {
  it('distinguishes not-yet-recorded from data-unavailable', () => {
    // Two different claims that a single "no data" marker would conflate. One means the pipeline has
    // not reached this; the other means it was asked for and could not be computed, and only the
    // second is a degradation.
    const { rerender, container } = render(
      <AbsentValue
        marker={{ status: 'NOT_YET_RECORDED', case_state: 'POLICY_CHECK', detail: 'pending' }}
      />,
    )
    expect(container.querySelector('.absent--pending')).not.toBeNull()

    rerender(
      <AbsentValue marker={{ status: 'DATA_UNAVAILABLE', figure: 'x', detail: 'timeout' }} />,
    )
    expect(container.querySelector('.absent--unavailable')).not.toBeNull()
  })
})
