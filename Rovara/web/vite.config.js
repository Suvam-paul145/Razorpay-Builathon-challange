import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

/** The dashboard's URL prefix. Must match `APP_PREFIX` in `revora/api/spa.py`. */
const APP_PREFIX = '/app'

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
 * Static SPA. No SSR, no server-side secrets, nothing here that talks to anything but the Revora API.
 *
 * The dev proxy exists so local development runs **same-origin**, which is the deployment this is
 * built for: the API installs no CORS middleware at all when `REVORA_API_CORS_ORIGINS` is unset, and
 * that is stronger than installing one with an empty list. Developing cross-origin and deploying
 * same-origin would mean the CORS configuration is exercised only in dev and never as it ships.
 */
export default defineConfig({
  plugins: [react(), redirectRootToApp()],
  build: {
    outDir: 'dist',
    // A real budget rather than the default warning. This is a data-dense dashboard with no charting
    // library and no component framework; if a bundle crosses this, something was added that should
    // have been a table.
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
    },
  },
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./src/test/setup.js'],
    include: ['src/**/*.test.{js,jsx}'],
  },
})
