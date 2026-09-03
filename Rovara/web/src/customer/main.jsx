/**
 * The customer response page's entry point. **No router, by design.**
 *
 * `tests/api/test_spa_mount.py` greps `src/main.jsx` and asserts the dashboard's `BrowserRouter`
 * `basename` equals `APP_PREFIX`, because a mismatch does not raise: the router matches nothing
 * outside its basename and renders nothing, so the page is blank with a clean console. That is the
 * most confusing failure the dashboard has, and the cheapest thing to do about it here is to not
 * have a router — one page, one URL shape, no client-side routing to configure. The same Python test
 * asserts this entry imports no router, so nobody reintroduces one without also reintroducing the
 * question of what its basename should be.
 *
 * There is also no `QueryClientProvider`, no `StrictMode` double-render concern worth a note, and no
 * `styles.css` from the dashboard. This entry's stylesheet is its own and small; the dashboard's is
 * around a thousand lines of table and panel rules none of which this page uses.
 *
 * ## The URL
 *
 *     https://<frontend-host>/pay/<merchant-slug>/rvc_<26 base32>.<22 base64url>
 *
 * The token is in the **path**, never a query string, which is why the document sets
 * `referrer: no-referrer` and `base-uri 'none'` and why the API sends the same two headers. A query
 * string is the one place a URL routinely leaks — into referrers, server access logs and analytics
 * scripts — and this URL is a bearer credential.
 *
 * **On the tenant segment.** The design and task text write this URL as `/pay/:token`. The built API
 * routes are `/customer/{merchant_slug}/case` and the three write shapes, so a page holding only a
 * token cannot address them; the segment is therefore in the path too. The alternative — one
 * frontend host per merchant — is a deployment change, and the server is already built and verified.
 * The token stays the last segment, so nothing about "the token is in the path" changes.
 */

import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'

import { Page } from './Page'
import './styles.css'

/** Where the frontend host serves this page. Mirrored in `vite.config.js` for the dev server. */
const PAY_PREFIX = 'pay'

/**
 * The token's wire form, from `revora/customer/tokens.py`: 26 unpadded **lowercase** base32
 * characters, a dot, then 22 unpadded base64url characters.
 *
 * Checked here so a truncated link — one wrapped by a messaging app, or copied without its tail —
 * renders the same panel as a rejected one instead of spending a request to be told so. That is not
 * a distinction the server hides: it hides *why* a token failed, and this check cannot tell the
 * difference between any two of those reasons either. It only refuses to send something that cannot
 * possibly be a token.
 */
const TOKEN = /^rvc_[a-z2-7]{26}\.[A-Za-z0-9_-]{22}$/

/**
 * Split `/pay/<merchant-slug>/<token>` into its two halves. `null` when the path is not that shape.
 *
 * Exported for the tests rather than for reuse — there is one call site, three lines below. A pure
 * function of a string is testable against a dozen malformed paths in a millisecond, where the same
 * logic inline would need a jsdom navigation per case.
 *
 * @param {string} pathname
 * @returns {{ merchantSlug: string, token: string } | null}
 */
export function parseLink(pathname) {
  const segments = pathname.split('/').filter((segment) => segment.length > 0)
  if (segments.length !== 3) return null
  if (segments[0] !== PAY_PREFIX) return null
  if (!TOKEN.test(segments[2])) return null
  return { merchantSlug: segments[1], token: segments[2] }
}

const container = document.getElementById('root')
if (container === null) throw new Error('#root is missing from index-customer.html')

createRoot(container).render(
  <StrictMode>
    {/* `null` is passed through rather than branched on here. `Page` renders an unusable link and a
        rejected one identically, which is the whole point — see its `NotFound`. */}
    <Page link={parseLink(window.location.pathname)} />
  </StrictMode>,
)
