// @vitest-environment node
//
// The one file in this suite that is not a DOM test. It runs the real bundler, and esbuild refuses to
// start under jsdom: jsdom's `TextEncoder` does not produce a `Uint8Array` that passes esbuild's own
// `instanceof` invariant, so the build aborts with a message about a broken JavaScript environment.
// Node's globals are the correct ones here anyway — nothing below touches a document.

/**
 * Task 51.3. `npm run build` produces two artifacts, and the customer one carries neither dashboard
 * dependency.
 *
 * The build is run **here**, through Vite's Node API, rather than asserted about after the fact. The
 * alternative was to inspect whatever happened to be in `dist/` and `dist-customer/`, and that fails
 * in the worst direction: on a fresh clone neither directory exists, the assertions have nothing to
 * look at, and the test either errors for the wrong reason or skips — a skip that is indistinguishable
 * from "the second entry was deleted and nobody noticed". Building means the claim is about the
 * committed configuration.
 *
 * Two modes rather than two configs. `vite.config.js` branches on `mode === 'customer'`, so this file
 * calls the same config the shipped `npm run build` does; a separate config for the second entry would
 * be a second thing to keep in step, and this test would be checking the copy.
 *
 * The output directories are the real ones, because that is precisely the claim — `npm run build`
 * produces `dist/` **and** `dist-customer/`. It leaves both populated, which is the same state the
 * build leaves them in.
 *
 * The transferred-size assertion at the bottom is **excluded from gating** and runs only when
 * `REVORA_WEB_SMOKE=1` is set. Same rationale as the `smoke` marker in `pyproject.toml`: a size or
 * timing bound measures the toolchain's mood as much as the code — a minifier release moves it — and a
 * gate that can fail for reasons nobody can act on teaches everyone to re-run the build. Tagged rather
 * than deleted, so the budget stays written down where a person checks it.
 *
 *     REVORA_WEB_SMOKE=1 npx vitest run src/customer/build.test.js
 */

import { existsSync, readFileSync, readdirSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { gzipSync } from 'node:zlib'

import { build } from 'vite'
import { afterAll, beforeAll, describe, expect, it } from 'vitest'

const WEB_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..')
const DASHBOARD_DIR = path.join(WEB_ROOT, 'dist')
const CUSTOMER_DIR = path.join(WEB_ROOT, 'dist-customer')

/** Vite's own default. Two production builds of this project take a couple of seconds each. */
const BUILD_TIMEOUT_MS = 300_000

/**
 * Everything a browser would transfer for one entry: the document plus the hashed assets.
 *
 * Source maps are excluded. They are emitted for both entries and no browser fetches one unless a
 * developer opens the tools, so counting them would make the budget a measurement of the wrong thing.
 *
 * @param {string} directory
 * @returns {{ name: string, bytes: number, gzip: number }[]}
 */
function transferred(directory) {
  const files = []
  const walk = (current) => {
    for (const entry of readdirSync(current, { withFileTypes: true })) {
      const full = path.join(current, entry.name)
      if (entry.isDirectory()) {
        walk(full)
        continue
      }
      if (entry.name.endsWith('.map')) continue
      const contents = readFileSync(full)
      files.push({
        name: path.relative(directory, full).replaceAll('\\', '/'),
        bytes: contents.byteLength,
        gzip: gzipSync(contents).byteLength,
      })
    }
  }
  walk(directory)
  return files
}

/**
 * Every emitted script in one directory, concatenated. Source maps excluded, as above.
 *
 * @param {string} directory
 * @returns {string}
 */
function allJs(directory) {
  return transferred(directory)
    .filter((file) => file.name.endsWith('.js'))
    .map((file) => readFileSync(path.join(directory, file.name), 'utf8'))
    .join('\n')
}

let dashboard
let customer
let originalNodeEnv

beforeAll(async () => {
  /*
   * `NODE_ENV=production` explicitly, and this is not incidental.
   *
   * Vite derives `isProduction` from `NODE_ENV || VITE_USER_NODE_ENV || mode`, and `vite build` on the
   * command line sets `NODE_ENV=production` only when it is unset. Under vitest it is already `test`,
   * so a build started from here would resolve as a *development* build: React's development runtime,
   * no minification, and a customer bundle of 352 kB against the 154 kB `npm run build` emits. Every
   * size assertion below would then be measuring an artifact nobody ships — which is worse than not
   * measuring, because the number would look like a result.
   *
   * Set here rather than in the vitest config so it is scoped to this file's worker and is visible
   * beside the assertion that depends on it.
   */
  originalNodeEnv = process.env.NODE_ENV
  process.env.NODE_ENV = 'production'
  // Sequential, not concurrent. Two Vite builds in one process share the plugin container's state and
  // the second would be racing the first's `generateBundle`.
  await build({ root: WEB_ROOT, logLevel: 'warn' })
  await build({ root: WEB_ROOT, mode: 'customer', logLevel: 'warn' })
  dashboard = transferred(DASHBOARD_DIR)
  customer = transferred(CUSTOMER_DIR)
}, BUILD_TIMEOUT_MS)

afterAll(() => {
  process.env.NODE_ENV = originalNodeEnv
})

describe('the two build targets', () => {
  it('produces both dist/ and dist-customer/', () => {
    expect(existsSync(DASHBOARD_DIR)).toBe(true)
    expect(existsSync(CUSTOMER_DIR)).toBe(true)
    // Each has its own entry document. `dist-customer/index.html` rather than
    // `index-customer.html`, so a static host serving `/pay/*` needs no filename rewrite of its own.
    expect(dashboard.map((file) => file.name)).toContain('index.html')
    expect(customer.map((file) => file.name)).toContain('index.html')
    // And each has hashed assets, so neither directory is an empty shell with a document in it.
    expect(dashboard.filter((file) => file.name.endsWith('.js')).length).toBeGreaterThan(0)
    expect(customer.filter((file) => file.name.endsWith('.js')).length).toBeGreaterThan(0)
  })

  it('keeps the two documents pointing at their own entries', () => {
    const customerHtml = readFileSync(path.join(CUSTOMER_DIR, 'index.html'), 'utf8')
    const dashboardHtml = readFileSync(path.join(DASHBOARD_DIR, 'index.html'), 'utf8')
    // The rename in `customerEntryAsIndex` changes the document's filename after its asset references
    // were written. If it ever ran too early, this is what would catch it.
    expect(customerHtml).toMatch(/src="\/assets\/index-customer-[^"]+\.js"/)
    expect(dashboardHtml).toMatch(/src="\/assets\/index-[^"]+\.js"/)
    // The customer document's own policy, which is the point of two entries having two documents.
    expect(customerHtml).toContain('no-referrer')
    expect(customerHtml).toContain("base-uri 'none'")
  })

  it('built both entries in production mode, which every size claim below depends on', () => {
    // The guard on the `NODE_ENV` note in `beforeAll`. A development build bundles React's development
    // runtime and skips minification — 352 kB rather than 154 kB — and would quietly turn the size
    // assertions into measurements of an artifact nobody ships.
    //
    // Asserted on a *string* rather than on a byte count, because "is this the production runtime" is
    // a fact and "is it under n bytes" is a budget. `Minified React error` is emitted only by the
    // production build; the development build carries `react-dom.development` instead.
    for (const directory of [DASHBOARD_DIR, CUSTOMER_DIR]) {
      const js = allJs(directory)
      expect(js, `${directory} is not a production build`).toContain('Minified React error')
      expect(js).not.toContain('react-dom.development')
    }
  })

  it('ships no router and no query client in the customer bundle', () => {
    // The strongest form of the claim: not what the source imports, but what the bundler emitted. A
    // transitive dependency added three modules deep would still show up here.
    const js = allJs(CUSTOMER_DIR)
    expect(js.length).toBeGreaterThan(1000)
    for (const marker of ['react-router', 'tanstack', 'QueryClient', 'BrowserRouter', 'useQuery']) {
      expect(js, `the customer bundle contains ${marker}`).not.toContain(marker)
    }
  })

  it('is smaller than the dashboard, which is the whole argument for two entries', () => {
    const total = (files) => files.reduce((sum, file) => sum + file.gzip, 0)
    // A relative assertion, not a byte budget — that one is the smoke test below. This holds as long
    // as the reason for the split holds, and it fails the moment somebody merges the two.
    expect(total(customer)).toBeLessThan(total(dashboard))
  })
})

describe('the transferred size', () => {
  /**
   * Excluded from gating, per the rationale at the top of this file. Set `REVORA_WEB_SMOKE=1` to run.
   *
   * The budget is gzipped bytes across the document, the JavaScript and the CSS, which is what a phone
   * on a cold connection actually pulls. Generous on purpose: React and React DOM are most of it, and
   * the number that matters is whether a dashboard-sized dependency crept in — not whether the
   * minifier saved four hundred bytes this month.
   */
  it.runIf(process.env.REVORA_WEB_SMOKE === '1')('stays within its budget', () => {
    const total = customer.reduce((sum, file) => sum + file.gzip, 0)
    const budget = 70 * 1024
    expect(
      total,
      `customer bundle is ${String(total)} gzipped bytes across ${String(customer.length)} files`,
    ).toBeLessThan(budget)
  })
})
