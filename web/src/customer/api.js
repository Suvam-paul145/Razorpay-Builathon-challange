/**
 * The customer page's whole network surface: two functions, one read and one write.
 *
 * Nothing else in this entry calls `fetch`. That is not tidiness — it is the only way the claims
 * this page makes are checkable by reading one short file. There is no query client, no retry
 * policy, no interceptor and no client-side cache, and each of those absences is deliberate:
 *
 * **No `@tanstack/react-query`.** The dashboard's client is the right tool for twelve endpoints with
 * shared cache keys and background refetching. This page reads once and writes at most a handful of
 * times, and the library is a substantial fraction of a bundle whose entire justification is that it
 * arrives quickly over a mobile connection.
 *
 * **No retry.** Every non-2xx answer on this surface is a *decision* — see the table below — and
 * retrying a decision either changes nothing or spends one of the customer's counted submissions. A
 * 503 is the only genuinely transient answer and the page tells the person to try again, which puts
 * the retry where the information is.
 *
 * **No error class and no `throw`.** Callers must render specific copy for each of seven refusals,
 * and an exception carrying a status code would be a discriminated union wearing a costume. Both
 * functions return `{ ok, status, body }` and the component switches on `status`, so a status nobody
 * handled is visible as a missing branch rather than as an unhandled rejection.
 *
 * ## The status codes, and what each one means to the person reading the page
 *
 * | Status | Body | What happened |
 * | --- | --- | --- |
 * | 200 | the projection | eight fields; see `Page.jsx` |
 * | 201 | `{recorded, signals_remaining}` | the write was accepted |
 * | 404 | *empty* | unknown tenant, or **any** token failure: no such handle, wrong secret, retired signing key, malformed presentation. Deliberately indistinguishable, so this page must not attempt to distinguish them either |
 * | 410 | `{expired: true}` | expired **or** revoked; the server does not separate them |
 * | 429 | `{rate}` or `{rejected}` | a process-local flood guard, or a durable cap the customer has genuinely reached. Different sentences, so the body is read |
 * | 422 | `{field}` | a field name and nothing else — never the submitted value |
 * | 409 | `{rejected, detail}` | the case reached a terminal state |
 * | 415 | `{content_type}` | the request did not declare JSON. Should be unreachable from here; see `submit` |
 * | 503 | *empty* | unreadable signing credential, audit-write failure, or a read that exceeded its budget. Nothing was persisted |
 *
 * ## The token
 *
 * It is in the page's URL path and it stays there. It travels in an `Authorization` header and
 * nowhere else — never a query string, never a log line, never a third-party request, and never into
 * `sessionStorage`, which would outlive the tab's need for it without making the link any less of a
 * bearer capability. `credentials: 'omit'` because the API is token-authenticated and sending
 * cookies would create a CSRF surface for no benefit; it is also what lets the API run
 * `allow_credentials: false`, which matters more than its origin list does.
 */

/**
 * The API origin, or the empty string for same-origin.
 *
 * Empty by default so development goes through the Vite proxy (`/customer` in `vite.config.js`) and
 * a single-host deployment needs no configuration at all. The split-host deployment the design
 * assumes — frontend on one host, API on another — sets this at build time, which is also the list
 * the API's `REVORA_CUSTOMER_ORIGINS` has to contain for the preflight to pass.
 */
const API_BASE = import.meta.env.VITE_REVORA_API_BASE ?? ''

/** The API's mounted sub-application. `CUSTOMER_MOUNT` in `revora/api/routers/customer.py`. */
const MOUNT = '/customer'

/**
 * The three write shapes, as their path segments.
 *
 * Exported as a frozen object rather than as three string literals at the call sites, so a typo is a
 * missing property at the point of use instead of a 404 that reads like an authorization failure.
 * Not a function, so it does not count against "exactly two functions" — and it is data, which is
 * what it should have been either way.
 */
export const SHAPES = Object.freeze({
  delayReason: 'delay-reason',
  promise: 'promise',
  partialArrangement: 'partial-arrangement',
})

/**
 * Turn a `fetch` outcome into the result shape, parsing a body only when there is one.
 *
 * Not exported, because it is not part of this module's contract. Several refusals answer with a
 * genuinely empty body (404 and 503) and `response.json()` on an empty body rejects, so the parse is
 * attempted and its failure is normal rather than notable.
 *
 * @param {Response} response
 * @returns {Promise<{ ok: boolean, status: number, body: any }>}
 */
async function result(response) {
  let body = null
  try {
    body = await response.json()
  } catch {
    // An empty body is the documented answer for 404 and 503, and `Content-Length: 0` is not an
    // error to report. A parse failure here is never treated as a different status than the one the
    // server sent.
  }
  return { ok: response.ok, status: response.status, body }
}

/**
 * The failure that is not a status code: the request never reached the API.
 *
 * Status `0` rather than an invented 5xx. A refused connection, a DNS failure and a blocked
 * cross-origin preflight are all "we do not know what the server would have said", and dressing that
 * up as a 503 would tell the reader that the server answered.
 *
 * @param {unknown} _cause
 * @returns {{ ok: false, status: 0, body: null }}
 */
function unreachable(_cause) {
  // Deliberately not logged. The only interesting context would be the URL, and the URL contains the
  // token.
  return { ok: false, status: 0, body: null }
}

/**
 * Read the case this token names (R19.C1).
 *
 * `cache: 'no-store'`, matching the API's own `Cache-Control: no-store, private`. A cached
 * projection on a shared phone would outlive the tab, and the amount owed is the one figure on this
 * page that changes when the customer pays.
 *
 * **Reads are served at both caps.** A customer who has already explained themselves the maximum
 * number of times still sees what they owe, so a 429 here is a flood guard rather than an ending and
 * a 429 on a *write* must not blank this page — see `Page.jsx`, which keeps the projection it
 * already has.
 *
 * @param {{ merchantSlug: string, token: string }} link
 *   Both halves come out of `window.location.pathname`. The task text names this parameter `token`
 *   and describes the page URL as `/pay/:token`; the built API's routes are
 *   `/customer/{merchant_slug}/...`, so the tenant segment has to travel too. The server is the
 *   authority on its own paths, so the link carries both and `main.jsx` parses both.
 * @returns {Promise<{ ok: boolean, status: number, body: any }>}
 */
export async function fetchCase(link) {
  try {
    const response = await fetch(`${API_BASE}${MOUNT}/${link.merchantSlug}/case`, {
      method: 'GET',
      headers: { authorization: `Bearer ${link.token}` },
      credentials: 'omit',
      cache: 'no-store',
      referrerPolicy: 'no-referrer',
    })
    return await result(response)
  } catch (cause) {
    return unreachable(cause)
  }
}

/**
 * Submit one Customer_Signal (R19.C4). `shape` is a member of {@link SHAPES}.
 *
 * **`Content-Type: application/json` is set explicitly and that is load-bearing twice over.** The
 * API answers 415 before parsing anything otherwise — it will not read a body whose declared type is
 * not the type its schema is declared in — and requiring the header is also what forces a CORS
 * preflight, so the permitted-origin list is consulted *before* the request carrying the token is
 * sent. `fetch` would otherwise send `text/plain` for a string body and the request would qualify as
 * a simple request, which skips the preflight entirely.
 *
 * The body is passed through as given rather than assembled here. Each shape has exactly the fields
 * the server declares with `extra="forbid"`, so a field this module added for convenience would come
 * back as a 422 naming it.
 *
 * @param {{ merchantSlug: string, token: string }} link
 * @param {string} shape One of {@link SHAPES}.
 * @param {Record<string, unknown>} body
 * @returns {Promise<{ ok: boolean, status: number, body: any }>}
 */
export async function submit(link, shape, body) {
  try {
    const response = await fetch(`${API_BASE}${MOUNT}/${link.merchantSlug}/${shape}`, {
      method: 'POST',
      headers: {
        authorization: `Bearer ${link.token}`,
        'content-type': 'application/json',
      },
      credentials: 'omit',
      cache: 'no-store',
      referrerPolicy: 'no-referrer',
      body: JSON.stringify(body),
    })
    return await result(response)
  } catch (cause) {
    return unreachable(cause)
  }
}
