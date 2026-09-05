/**
 * Sign-in. A merchant slug plus that merchant's operator key, exchanged for a session.
 *
 * There is no password field, and that is not a gap. `merchant_user` has no password column — the
 * design defers per-user credentials, roles and MFA — so inventing one here would mean inventing a
 * storage format and a hashing decision the rest of the system does not know about. The operator key
 * is a per-merchant secret held by whoever runs the deployment, and it mints a revocable session row.
 *
 * Failures show one message, because the API answers **401 byte-identically** for a bad key, an
 * unknown slug and a mismatched pair. The audit record keeps the distinction; the response does not,
 * so this screen cannot be used to discover which merchants exist. The message says so, because an
 * operator who mistyped a slug should not spend ten minutes checking the key.
 */

import { useState } from 'react'

import { useSignIn } from '../api/queries'

/** @param {{ onSignedIn: () => void }} props */
export function SignIn({ onSignedIn }) {
  const [merchantSlug, setMerchantSlug] = useState('')
  const [dashboardKey, setDashboardKey] = useState('')
  const signIn = useSignIn()

  function submit(event) {
    event.preventDefault()
    signIn.mutate(
      { merchantSlug: merchantSlug.trim(), dashboardKey },
      {
        onSuccess: () => {
          onSignedIn()
        },
      },
    )
  }

  function fillJudgeCredentials() {
    setMerchantSlug('razorpay-judge')
    setDashboardKey('razorpay-pass')
  }

  return (
    <main className="signin">
      <div className="signin__card">
        <p className="brand brand--large">
          Revora<span className="brand__dot">.</span>
        </p>
        <p className="signin__tagline">
          A failed payment is not necessarily lost revenue — and money that comes back is not
          necessarily money you recovered.
        </p>

        <form onSubmit={submit} className="signin__form">
          <label className="field">
            <span className="field__label">Merchant slug</span>
            <input
              className="field__input"
              value={merchantSlug}
              onChange={(event) => {
                setMerchantSlug(event.target.value)
              }}
              placeholder="e.g. razorpay-judge"
              autoComplete="username"
              required
            />
          </label>
          <label className="field">
            <span className="field__label">Operator key</span>
            <input
              className="field__input"
              type="password"
              value={dashboardKey}
              onChange={(event) => {
                setDashboardKey(event.target.value)
              }}
              placeholder="e.g. razorpay-pass"
              autoComplete="current-password"
              required
            />
          </label>
          <button
            type="submit"
            className="button button--primary button--wide"
            disabled={signIn.isPending}
          >
            {signIn.isPending ? 'Signing in…' : 'Sign in'}
          </button>
        </form>

        <div className="signin__evaluator">
          <div className="signin__evaluator-header">
            <span className="signin__evaluator-badge">Hackathon Evaluator</span>
          </div>
          <p className="signin__evaluator-desc">
            Evaluating Revora? Use quick credentials supporting 5+ concurrent judges:
          </p>
          <button
            type="button"
            className="button button--secondary button--wide signin__evaluator-btn"
            onClick={fillJudgeCredentials}
          >
            Fill Evaluator Credentials (<code>razorpay-judge</code>)
          </button>
        </div>

        {signIn.isError && (
          <p className="status status--error" role="alert">
            Sign-in refused. The API answers identically for an unknown merchant, a wrong key and a
            mismatched pair — deliberately, so this screen cannot be used to find out which merchants
            exist. Check both values.
          </p>
        )}
      </div>
    </main>
  )
}
