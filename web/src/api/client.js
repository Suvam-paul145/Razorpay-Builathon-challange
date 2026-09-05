/**
 * The one place this app talks to the network.
 *
 * Three decisions worth stating.
 *
 * **Relative URLs, no base.** The SPA is served same-origin with the API, which is the deployment
 * `create_app` is built for — it installs no CORS middleware at all when `REVORA_API_CORS_ORIGINS`
 * is unset, and no middleware is stronger than a middleware with an allowlist. A configurable API
 * base would make the cross-origin path the normal one.
 *
 * **The session token lives in `sessionStorage`, not `localStorage`.** It is a bearer credential for
 * a merchant's payment data. `sessionStorage` dies with the tab, so a shared or forgotten browser
 * does not keep a live session indefinitely. Neither is XSS-proof — only an httpOnly cookie is, and
 * this API is deliberately token-authenticated rather than cookie-authenticated so it carries no
 * CSRF surface. The tradeoff is stated rather than hidden.
 *
 * **A 401 clears the session.** The server treats an expired session as unauthenticated and
 * revocation is real (there is a `merchant_session` row, and `DELETE /auth/session` removes it), so
 * a client holding a dead token must stop using it rather than retrying.
 */

const TOKEN_KEY = 'revora.session.token'

/** The dashboard-key header that mints a session. Named here so the string appears once. */
export const DASHBOARD_KEY_HEADER = 'x-revora-dashboard-key'

export class ApiError extends Error {
  /**
   * @param {number} status
   * @param {string} detail
   * @param {string | null} correlationId
   *   The same id the audit records written during this request carry, so "the page said something
   *   odd at 14:32" is answerable with one query. Surfaced on every error for that reason.
   */
  constructor(status, detail, correlationId) {
    super(`${String(status)}: ${detail}`)
    this.name = 'ApiError'
    this.status = status
    this.detail = detail
    this.correlationId = correlationId
  }
}

/** @returns {string | null} */
export function storedToken() {
  try {
    return window.sessionStorage.getItem(TOKEN_KEY)
  } catch {
    // Storage throws in a hardened or private context. An unreadable store means no session, which
    // sends the operator to the sign-in screen — the correct outcome, not a crash.
    return null
  }
}

/** @param {string} token */
export function storeToken(token) {
  window.sessionStorage.setItem(TOKEN_KEY, token)
}

export function clearToken() {
  try {
    window.sessionStorage.removeItem(TOKEN_KEY)
  } catch {
    /* nothing to clear if the store is unavailable */
  }
}

/**
 * @param {string} path
 * @param {{ method?: 'GET' | 'POST' | 'DELETE', body?: unknown,
 *           headers?: Record<string, string>, anonymous?: boolean }} [options]
 *   `anonymous` is set for the sign-in call, which mints a session and so has none to send.
 * @returns {Promise<any>}
 */
export async function request(path, options = {}) {
  const { method = 'GET', body, headers = {}, anonymous = false } = options

  const finalHeaders = { ...headers }
  if (body !== undefined) finalHeaders['content-type'] = 'application/json'
  if (!anonymous) {
    const token = storedToken()
    if (token !== null) finalHeaders['authorization'] = `Bearer ${token}`
  }

  const init = {
    method,
    headers: finalHeaders,
    // No cookies are used by this API, and sending them would create a CSRF surface the
    // bearer-token design specifically avoids.
    credentials: 'omit',
  }
  if (body !== undefined) init.body = JSON.stringify(body)

  const response = await fetch(path, init)
  const correlationId = response.headers.get('x-correlation-id')

  if (response.status === 401 && !anonymous) {
    clearToken()
    throw new ApiError(401, 'session expired or revoked', correlationId)
  }

  if (!response.ok) {
    let detail = response.statusText
    try {
      const parsed = await response.json()
      if (typeof parsed.detail === 'string') detail = parsed.detail
    } catch {
      /* a body-less error response is normal here; several endpoints answer bare status codes */
    }
    throw new ApiError(response.status, detail, correlationId)
  }

  if (response.status === 204) return undefined
  return response.json()
}
