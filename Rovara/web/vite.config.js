import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

/** The dashboard's URL prefix. Must match `APP_PREFIX` in `revora/api/spa.py`. */
const APP_PREFIX = '/app'

/**
 * The customer response page's build target, as three values used in several places each.
 *
 * `mode` rather than a second config file. Two files would duplicate the plugin list, the proxy and
 * the vitest block, and the copy that drifted would be the one nobody runs — `npx vitest run` reads
 * this file, so a second config's `test` section would be dead configuration that still looks alive.
 */
const CUSTOMER_MODE = 'customer'
const CUSTOMER_ENTRY = 'index-customer.html'
const CUSTOMER_OUT_DIR = 'dist-customer'

/** Where the customer page is served from on the frontend host. See `src/customer/main.jsx`. */
const PAY_PREFIX = '/pay/'

/**
 * Send a bare `/` to `/app/` in dev, the way the API does in production.
 *
 * Without this, opening http://localhost:5173 renders a **blank page with no error**: the router is
 * mounted at `basename="/app"`, so at `/` it matches nothing and renders nothing. React Router logs
 * a warning to the console and that is the only signal.
 *
 * The prefix exists because the SPA and the API share an origin, so they share one URL space — and
 * the API already owns `/cases` and `/metrics`. Rather than drop the prefix in dev and diverge from
 * what ships, dev gets the same redirect the API serves.
 */
function redirectRootToApp() {
  return {
    name: 'revora-redirect-root-to-app',
    configureServer(server) {
      server.middlewares.use((request, response, next) => {
        if (request.url === '/' || request.url === '') {
          response.writeHead(302, { Location: `${APP_PREFIX}/` })
          response.end()
          return
        }
        next()
      })
    },
  }
}

/**
 * Serve the customer entry under `/pay/*` in dev, the way the frontend host does in production.
 *
 * Dev-only, and it exists for the same reason the redirect above does: the page reads its token out
 * of `window.location.pathname`, so a developer who opens `/index-customer.html` gets a page with no
 * token and sees the not-found panel rather than the page they are working on. Rewriting means the
 * URL a developer types is the URL a customer receives.
 *
 * A rewrite rather than a redirect. A redirect would change the address bar and take the token out
 * of the path, which is the one thing this page's URL is for.
 */
function serveCustomerEntryUnderPay() {
  return {
    name: 'revora-serve-customer-entry-under-pay',
    configureServer(server) {
      server.middlewares.use((request, _response, next) => {
        if (request.url !== undefined && request.url.startsWith(PAY_PREFIX)) {
          request.url = `/${CUSTOMER_ENTRY}`
        }
        next()
      })
    },
  }
}

/**
 * Emit the customer entry as `dist-customer/index.html`, not `dist-customer/index-customer.html`.
 *
 * Vite names an HTML output after its input path, and the input has to be `index-customer.html`
 * because both entries share one project root. The rename is presentation of the artifact rather
 * than of the page: a static host serving `/pay/*` wants one directory whose default document is
 * `index.html`, and every host spells "rewrite everything to this other filename" differently. One
 * rename here removes that configuration from every deployment target.
 *
 * The hashed asset references inside the document are untouched — only the document's own filename
 * changes, after the references were written.
 */
function customerEntryAsIndex() {
  return {
    name: 'revora-customer-entry-as-index',
    enforce: 'post',
    generateBundle(_options, bundle) {
      const document = bundle[CUSTOMER_ENTRY]
      if (document === undefined) return
      delete bundle[CUSTOMER_ENTRY]
      document.fileName = 'index.html'
      bundle['index.html'] = document
    },
  }
}

/**
 * Two static entries, built one at a time. No SSR, no server-side secrets, nothing here that talks
 * to anything but the Revora API.
 *
 * **Why two entries rather than a `/pay/:token` route in the dashboard.** A route inside the
 * existing SPA would ship an unauthenticated stranger holding a payment link the entire
 * administrative surface as readable source. Nothing in it is secret, but it is a map. The customer
 * page also has to make zero third-party requests under its own stricter CSP, and it is opened from
 * an SMS on a cold mobile connection by somebody being asked for money — so it should be a few
 * kilobytes, not the dashboard's bundle. The accepted cost is a second build target and two places
 * a shared component could drift, mitigated by sharing exactly one module: `<Money>` from
 * `src/components/Figure.jsx`, imported relatively.
 *
 * `mode === 'customer'` selects the second target. `npm run build` runs both passes, and the
 * dashboard's pass is unchanged — same `outDir`, same budget, same plugins — so a regression in
 * `dist/` cannot come from this branch.
 *
 * The dev proxy exists so local development runs **same-origin** against the dashboard API, which is
 * the deployment that ships: `create_app` installs no CORS middleware at all when
 * `REVORA_API_CORS_ORIGINS` is unset, and no middleware is stronger than one with an allowlist.
 * `/customer` is proxied for a different reason — that sub-application is genuinely cross-origin in
 * production and carries its own CORS middleware — but it still has to be reachable from the dev
 * server or the customer page cannot be developed at all.
 */
export default defineConfig(({ mode }) => {
  const customer = mode === CUSTOMER_MODE

  return {
    plugins: customer
      ? [react(), customerEntryAsIndex()]
      : [react(), redirectRootToApp(), serveCustomerEntryUnderPay()],

    build: customer
      ? {
          outDir: CUSTOMER_OUT_DIR,
          rollupOptions: { input: CUSTOMER_ENTRY },
          // Set just above what React and React DOM alone weigh, because the whole argument for a
          // second entry is that this page carries neither `react-router-dom` (~25 kB) nor
          // `@tanstack/react-query` (~40 kB). A budget below the framework floor would warn on every
          // build and be ignored; this one warns only if something was added.
          chunkSizeWarningLimit: 175,
          sourcemap: true,
        }
      : {
          outDir: 'dist',
          // A real budget rather than the default warning. This is a data-dense dashboard with no
          // charting library and no component framework; if a bundle crosses this, something was
          // added that should have been a table.
          chunkSizeWarningLimit: 300,
          sourcemap: true,
        },

    server: {
      port: 5173,
      proxy: {
        '/cases': 'http://127.0.0.1:8000',
        '/metrics': 'http://127.0.0.1:8000',
        '/experiments': 'http://127.0.0.1:8000',
        '/consent': 'http://127.0.0.1:8000',
        '/auth': 'http://127.0.0.1:8000',
        '/health': 'http://127.0.0.1:8000',
        // The mounted customer sub-application (`CUSTOMER_MOUNT` in
        // `revora/api/routers/customer.py`). Absent until task 51 and the page was therefore
        // unreachable from the dev server, which is not a thing a developer would notice as a
        // missing proxy — the fetch simply 404s against Vite.
        '/customer': 'http://127.0.0.1:8000',
      },
    },

    test: {
      environment: 'jsdom',
      globals: true,
      setupFiles: ['./src/test/setup.js'],
      include: ['src/**/*.test.{js,jsx}'],
    },
  }
})
