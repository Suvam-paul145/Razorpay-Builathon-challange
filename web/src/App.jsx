/**
 * The shell. Routes, navigation, and the one piece of session state the app holds.
 *
 * Session presence is React state seeded from `sessionStorage` rather than read on every render, so
 * a 401 clearing the token (which `client.js` does) causes a single deliberate transition back to
 * sign-in rather than a component tree that disagrees with itself about whether anyone is signed in.
 */

import { useState } from 'react'
import { Link, Navigate, Route, Routes, useLocation } from 'react-router-dom'
import { useIsFetching } from '@tanstack/react-query'

import { clearToken, storedToken } from './api/client'
import { CaseDetail } from './routes/CaseDetail'
import { CaseList } from './routes/CaseList'
import { Consent } from './routes/Consent'
import { Experiments } from './routes/Experiments'
import { Metrics } from './routes/Metrics'
import { SignIn } from './routes/SignIn'
import { Unresolved } from './routes/Unresolved'

export function App() {
  const [signedIn, setSignedIn] = useState(() => storedToken() !== null)
  const isFetching = useIsFetching()

  if (!signedIn) {
    return (
      <>
        {isFetching > 0 && <div className="top-loader" aria-hidden="true" />}
        <SignIn
          onSignedIn={() => {
            setSignedIn(true)
          }}
        />
      </>
    )
  }

  return (
    <div className="shell">
      {isFetching > 0 && <div className="top-loader" aria-hidden="true" />}
      <Nav
        onSignOut={() => {
          clearToken()
          setSignedIn(false)
        }}
      />
      <main className="shell__main">
        <Routes>
          <Route path="/" element={<Navigate to="/metrics" replace />} />
          <Route path="/metrics" element={<Metrics />} />
          <Route path="/unresolved" element={<Unresolved />} />
          <Route path="/cases" element={<CaseList />} />
          <Route path="/cases/:caseId" element={<CaseDetail />} />
          <Route path="/experiments" element={<Experiments />} />
          <Route path="/consent" element={<Consent />} />
          <Route path="*" element={<Navigate to="/metrics" replace />} />
        </Routes>
      </main>
    </div>
  )
}

const NAV = [
  { to: '/metrics', label: 'Performance' },
  { to: '/cases', label: 'Cases' },
  { to: '/unresolved', label: 'Unresolved' },
  { to: '/experiments', label: 'Experiments' },
  { to: '/consent', label: 'Consent' },
]

/** @param {{ onSignOut: () => void }} props */
function Nav({ onSignOut }) {
  const { pathname } = useLocation()
  return (
    <header className="shell__nav">
      <p className="brand">
        Revora<span className="brand__dot">.</span>
      </p>
      <nav aria-label="Sections">
        <ul className="nav">
          {NAV.map((item) => (
            <li key={item.to}>
              <Link
                className={pathname.startsWith(item.to) ? 'nav__link nav__link--on' : 'nav__link'}
                to={item.to}
              >
                {item.label}
              </Link>
            </li>
          ))}
        </ul>
      </nav>
      <button type="button" className="button button--quiet" onClick={onSignOut}>
        Sign out
      </button>
    </header>
  )
}
