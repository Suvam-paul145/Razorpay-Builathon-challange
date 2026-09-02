/**
 * Proves the money lint rule actually fires.
 *
 * A lint rule nobody has seen fail is a rule that might be silently misconfigured — a typo in an
 * ESLint selector does not error, it just matches nothing, and the config would pass forever while
 * enforcing precisely nothing.
 *
 * This matters more since the project dropped TypeScript. The compiler used to refuse an unhandled
 * absent-value arm; lint is now the only automated check on the money and absent-value rules, so
 * "the lint rules work" is load-bearing rather than reassuring.
 *
 * Each case is a snippet ESLint is run against in-process using the project's real config, so what is
 * tested is the shipped configuration and not a copy of it.
 */

import { rm, writeFile } from 'node:fs/promises'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

import { ESLint } from 'eslint'
import { describe, expect, it } from 'vitest'

const WEB_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..')

/**
 * The shipped config, loaded by absolute path.
 *
 * `overrideConfigFile` rather than relying on discovery from a cwd: vitest's cwd is not guaranteed to
 * be the web root, and a failure to discover the config would surface as "no messages" — which every
 * assertion below would read as "the rule did not fire", turning a broken harness into a green suite
 * that tests nothing.
 */
const eslint = new ESLint({
  cwd: WEB_ROOT,
  overrideConfigFile: path.join(WEB_ROOT, 'eslint.config.js'),
})

let probeCounter = 0

/**
 * Write the snippet to a real file inside `src`, lint it, remove it.
 *
 * **A unique filename per call.** A single shared probe path was the first version, and it failed in a
 * way worth recording: one snippet exceeded the default timeout, and the aborted test's `finally` then
 * deleted the file the *next* test had just written — so one slow test became two failures with
 * unrelated messages. Independent paths mean a slow or failing case cannot corrupt its neighbours.
 *
 * Removed in `finally` regardless, so a failing assertion cannot leave a file behind that would then
 * break `npm run lint` for everyone.
 *
 * @param {string} code
 * @returns {Promise<string[]>}
 */
async function messagesFor(code) {
  probeCounter += 1
  const probe = path.join(WEB_ROOT, 'src', `__lint_probe_${probeCounter}__.js`)
  await writeFile(probe, code, 'utf8')
  try {
    const results = await eslint.lintFiles([probe])
    const first = results[0]
    if (first === undefined) throw new Error('eslint returned no result for the probe')
    return first.messages.map((message) => message.message)
  } finally {
    await rm(probe, { force: true })
  }
}

const LINT_TIMEOUT_MS = 60_000

describe('the money lint rule', () => {
  it(
    'rejects dividing minor units',
    async () => {
      // The canonical mistake. `formatted` already exists; this recomputes it, badly, and only here.
      const messages = await messagesFor('const a = { minor: 1 }\nexport const x = a.minor / 100\n')
      expect(messages.join(' ')).toContain('No arithmetic on money')
    },
    LINT_TIMEOUT_MS,
  )

  it(
    'rejects adding two money figures',
    async () => {
      // Costs are summed by the server precisely so this never has to happen. A client-side sum is
      // free to disagree with the stored `total_cost` sitting in the next column.
      const messages = await messagesFor(
        'const a = { minor: 1 }\nconst b = { minor: 2 }\nexport const x = a.minor + b.minor\n',
      )
      expect(messages.join(' ')).toContain('No arithmetic on money')
    },
    LINT_TIMEOUT_MS,
  )

  it(
    'rejects interpolating minor units into text',
    async () => {
      // Renders paise as rupees: a figure a hundred times too large, in the currency being invoiced.
      const messages = await messagesFor('const a = { minor: 1 }\nexport const x = `Rs ${a.minor}`\n')
      expect(messages.join(' ')).toContain('paise, not rupees')
    },
    LINT_TIMEOUT_MS,
  )

  it(
    'rejects formatting currency in the browser',
    async () => {
      const messages = await messagesFor("export const f = new Intl.NumberFormat('en-IN')\n")
      expect(messages.join(' ')).toContain("Currency formatting is the server's job")
    },
    LINT_TIMEOUT_MS,
  )

  it(
    'rejects rounding in the browser',
    async () => {
      const messages = await messagesFor('const n = 1.5\nexport const x = n.toFixed(2)\n')
      expect(messages.join(' ')).toContain('No client-side rounding')
    },
    LINT_TIMEOUT_MS,
  )

  it(
    'rejects defaulting an absent figure to zero',
    async () => {
      // R14.C15 as lint. `?? 0` reads as defensive and is a false financial statement.
      const messages = await messagesFor('const n = null\nexport const x = n ?? 0\n')
      expect(messages.join(' ')).toContain('Never default a figure to zero')
    },
    LINT_TIMEOUT_MS,
  )

  it(
    'permits reading minor units without computing on them',
    async () => {
      // The rule has to leave the legitimate use alone, or it would be turned off. Sorting a column
      // and writing a CSV both need the integer.
      const messages = await messagesFor(
        'const rows = [{ minor: 1 }]\n' +
          'export const sorted = [...rows].sort((a, b) => (a.minor < b.minor ? -1 : 1))\n',
      )
      expect(messages.join(' ')).not.toContain('No arithmetic on money')
    },
    LINT_TIMEOUT_MS,
  )
})
