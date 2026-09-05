import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'

import { App } from './App'
import './styles.css'

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      // Refetching on every window focus would re-poll a merchant's whole dashboard whenever they
      // alt-tabbed. Per-hook where it earns its cost — webhook health opts in, because staleness
      // there is itself the risk being monitored.
      refetchOnWindowFocus: false,
      retry: 1,
    },
  },
})

const container = document.getElementById('root')
if (container === null) throw new Error('#root is missing from index.html')

createRoot(container).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      {/*
        `basename` must match `APP_PREFIX` in `revora/api/spa.py` and the redirect in
        `vite.config.js`. The dashboard is served under `/app` because it shares an origin with the
        API — one origin means one URL space, and the API already owns `/cases` and `/metrics`.

        A mismatch does not error. The router matches nothing outside its basename and renders
        nothing, so the page is **blank with a clean console** apart from one React Router warning.
        That is exactly what happened when the Vite dev server served `/` with no redirect. A Python
        test greps this file and asserts the two values agree.
      */}
      <BrowserRouter basename="/app">
        <App />
      </BrowserRouter>
    </QueryClientProvider>
  </StrictMode>,
)
