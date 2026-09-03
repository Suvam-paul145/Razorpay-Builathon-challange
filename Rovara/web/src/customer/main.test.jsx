/**
 * Task 51.3. The entry reads its token out of the path, with no router (R19.C11, R29.C7).
 *
 * Deliberately does **not** import `@testing-library/react`. That library sets
 * `IS_REACT_ACT_ENVIRONMENT` globally, and this file mounts the real entry module — a bare
 * `createRoot(...).render(...)` at import time, which is the thing under test — so importing it would
 * turn every assertion below into a wall of `act(...)` warnings about code that is behaving correctly.
 * `Page.test.jsx` uses the library, for a component that is rendered rather than an entry that mounts
 * itself.
 *
 * Two claims:
 *
 * * **the parse** — `/pay/<slug>/<token>` yields both halves, and anything else yields `null`, which
 *   the page renders identically to a 404. A table rather than one happy case, because the failing
 *   inputs are the real ones: a link wrapped by a messaging app, a link copied without its tail, a
 *   path with a plausible-looking token that is one character short;
 * * **the mount** — importing the entry with a token in `window.location.pathname` reads that token
 *   and sends it as a bearer credential. This is the end-to-end version of "no router": there is no
 *   `basename` to agree with anything, so the blank-page-with-a-clean-console failure the dashboard's
 *   `basename` test exists to catch cannot occur here.
 */

import { afterAll, beforeAll, describe, expect, it, vi } from 'vitest'

const SLUG = 'acme-tools'
const TOKEN = 'rvc_a2b3c4d5e6f7g2h3i4j5k6l7m2.AAAAAAAAAAAAAAAAAAAAAA'

let parseLink
let fetchMock

/**
 * Mount the entry once, the way a browser does: a `#root` element, a real path, and nothing else.
 *
 * The module is imported *after* the path is set, because the entry reads
 * `window.location.pathname` at module scope — which is the whole point of it having no router.
 */
beforeAll(async () => {
  document.body.innerHTML = '<div id="root"></div>'
  window.history.pushState({}, '', `/pay/${SLUG}/${TOKEN}`)
  fetchMock = vi.fn(() =>
    Promise.resolve({
      ok: true,
      status: 200,
      json: () =>
        Promise.resolve({
          merchant_display_name: 'Acme Tools',
          amount: { status: 'PRESENT', minor: 123456789, currency: 'INR', formatted: '₹12,34,567.89' },
          currency: 'INR',
          reason: 'The payment did not go through.',
          pay_url: null,
          window_end_at: '2026-09-15T18:30:00+00:00',
          promise: null,
          signals_remaining: 3,
        }),
    }),
  )
  vi.stubGlobal('fetch', fetchMock)
  const entry = await import('./main.jsx')
  parseLink = entry.parseLink
  // `createRoot(...).render(...)` schedules the render; the effect that reads the case runs after it.
  // Two macrotasks is enough and it is not a race being papered over — React commits synchronously
  // once scheduled, and the assertions below fail loudly rather than flakily if it has not.
  await new Promise((resolve) => setTimeout(resolve, 0))
  await new Promise((resolve) => setTimeout(resolve, 0))
})

afterAll(() => {
  vi.unstubAllGlobals()
})

describe('parseLink', () => {
  it('reads the slug and the token out of the path', () => {
    expect(parseLink(`/pay/${SLUG}/${TOKEN}`)).toEqual({ merchantSlug: SLUG, token: TOKEN })
    // A trailing slash is the same URL. Messaging clients add and remove them.
    expect(parseLink(`/pay/${SLUG}/${TOKEN}/`)).toEqual({ merchantSlug: SLUG, token: TOKEN })
  })

  it('returns null for every path that is not one', () => {
    const rejected = [
      '/',
      '/pay',
      `/pay/${SLUG}`,
      `/pay/${SLUG}/${TOKEN}/extra`,
      // The prefix has to be `/pay`, or an unrelated deep link would try to authenticate with a path
      // segment.
      `/app/${SLUG}/${TOKEN}`,
      // Truncated by one character: a link cut short by a client that wrapped it. The server would
      // answer 404 for this, and so does the page — without spending a request to be told so.
      `/pay/${SLUG}/${TOKEN.slice(0, -1)}`,
      // Uppercase handle. The wire form is unpadded *lowercase* base32.
      `/pay/${SLUG}/${TOKEN.toUpperCase()}`,
      // No prefix at all.
      `/pay/${SLUG}/notatoken`,
    ]
    for (const path of rejected) {
      expect(parseLink(path), path).toBeNull()
    }
  })
})

describe('the mount', () => {
  it('sends the token from the path as a bearer credential', () => {
    expect(fetchMock).toHaveBeenCalled()
    const [url, init] = fetchMock.mock.calls[0]
    expect(url).toBe(`/customer/${SLUG}/case`)
    expect(init.headers.authorization).toBe(`Bearer ${TOKEN}`)
    // Never in the URL. This page's path *is* the credential, and a query string is where URLs leak.
    expect(url).not.toContain(TOKEN)
  })

  it('imports no router and no query client', () => {
    // The client half of the assertion `tests/api/test_spa_mount.py` makes. Here it is checked over
    // *import specifiers* rather than over the source text, and that distinction is load-bearing: the
    // modules under test explain at length why they carry neither dependency, so a substring search
    // for `react-router` matches the prose that says it is absent. The names have to be read from the
    // one place they mean an import.
    const modules = import.meta.glob(['./*.{js,jsx}', '!./*.test.{js,jsx}'], {
      query: '?raw',
      import: 'default',
      eager: true,
    })
    // Anti-vacuity: three modules, so a glob that silently matched nothing is a failure rather than a
    // pass.
    expect(Object.keys(modules).length).toBeGreaterThanOrEqual(3)

    const all = []
    for (const [name, source] of Object.entries(modules)) {
      const specifiers = [...String(source).matchAll(/^\s*import\s[^\n]*?['"]([^'"]+)['"]/gm)].map(
        (match) => match[1],
      )
      for (const specifier of specifiers) {
        all.push(specifier)
        expect(specifier, `${name} imports ${specifier}`).not.toMatch(/react-router|@tanstack/)
      }
    }
    // The other half of the anti-vacuity check: the regex found real specifiers. `api.js` imports
    // nothing at all, so this is asserted across the entry rather than per file.
    expect(all).toContain('react')
    expect(all).toContain('react-dom/client')
  })
})
