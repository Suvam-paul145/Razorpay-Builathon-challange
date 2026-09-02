import js from '@eslint/js'
import globals from 'globals'
import react from 'eslint-plugin-react'
import reactHooks from 'eslint-plugin-react-hooks'

/**
 * The honesty rules, as lint.
 *
 * These matter more now than they did under TypeScript, not less. The compiler used to refuse an
 * unhandled absent-value arm; nothing refuses it in plain JavaScript. So lint is the only automated
 * check left on the rules below, and each one blocks a specific way a false figure reaches a screen.
 *
 * Note that the money rule was *always* lint rather than types, even before this project dropped
 * TypeScript: a branded `number` still permits `+`, `-`, `*` and `/`, because arithmetic operators
 * accept every number subtype. The syntax is the only place this can be enforced.
 */

const MONEY_MINOR = "MemberExpression[property.name='minor']"

const honesty = {
  'no-restricted-syntax': [
    'error',

    // 1. Arithmetic on minor units. `MoneyField.minor` exists for sorting and CSV export, not for
    //    display. The server has already divided by the currency's minor-unit digits, chosen the
    //    symbol and applied the grouping convention — INR renders `₹12,34,567.89`, which is not what
    //    any browser's default formatter produces. A component that divides by 100 will eventually
    //    do it in one place and not in another, and the two numbers will disagree on one screen.
    {
      selector: `BinaryExpression[operator=/^[-+*/%]$/] > ${MONEY_MINOR}`,
      message:
        'No arithmetic on money. Render <Money value={field} /> — the server already formatted it. ' +
        '`minor` is for sorting and export only.',
    },
    {
      selector: `AssignmentExpression[operator=/^[-+*/%]=$/] > ${MONEY_MINOR}`,
      message: 'No arithmetic on money. Render the server-formatted string.',
    },

    // 2. Interpolating minor units into text. `₹${x.minor}` renders paise as rupees — a figure a
    //    hundred times too large, in the currency a merchant is being invoiced in.
    {
      selector: `TemplateLiteral > ${MONEY_MINOR}`,
      message:
        'Do not interpolate `minor` into text; it is paise, not rupees. Use <Money value={field} />.',
    },

    // 3. Client-side currency formatting, in any form. If this appears, somebody has decided to
    //    format an amount in the browser, and the whole point of `formatted` arriving from the
    //    server is that there is exactly one implementation of the rules.
    {
      selector: "NewExpression[callee.object.name='Intl'][callee.property.name='NumberFormat']",
      message:
        "Currency formatting is the server's job (R14.C12). Render `field.formatted`. There is one " +
        'implementation of the grouping and symbol rules and it is not in this bundle.',
    },
    {
      selector: "CallExpression[callee.property.name='toLocaleString']",
      message: 'Server-formatted figures only. `toLocaleString` would produce a second format.',
    },
    {
      selector: "CallExpression[callee.property.name='toFixed']",
      message:
        'No client-side rounding. A rounded figure computed here can disagree with the same figure ' +
        'on the next screen.',
    },

    // 4. Substituting a fallback for an absent value. `?? 0` on an API figure is how
    //    NOT_YET_RECORDED becomes a zero, which is a false financial statement rather than a
    //    display shortcut (R14.C15). Absent values go through <Money>/<Rate>/<Enum>, which name
    //    what is absent and which case state explains it.
    {
      selector: "LogicalExpression[operator='??'][right.value=0]",
      message:
        'Never default a figure to zero. An absent value is not zero — render the marker so the ' +
        'case state that explains it stays visible.',
    },
    {
      selector: "LogicalExpression[operator='||'][right.value=0]",
      message: 'Never default a figure to zero. Render the absent-value marker instead.',
    },
  ],
}

export default [
  { ignores: ['dist', 'coverage', 'node_modules'] },
  js.configs.recommended,
  {
    files: ['src/**/*.{js,jsx}'],
    languageOptions: {
      ecmaVersion: 2022,
      sourceType: 'module',
      globals: { ...globals.browser },
      parserOptions: {
        ecmaFeatures: { jsx: true },
      },
    },
    settings: { react: { version: '18.3' } },
    plugins: { react, 'react-hooks': reactHooks },
    rules: {
      ...honesty,
      ...reactHooks.configs.recommended.rules,

      // The new JSX transform needs no React import, so the classic rules are wrong here.
      'react/react-in-jsx-scope': 'off',
      'react/jsx-uses-react': 'off',
      'react/jsx-uses-vars': 'error',

      // Off deliberately. Without TypeScript this would be the only prop documentation, and
      // `propTypes` is a runtime check that fires in the console after a component has already
      // rendered — too late to prevent the render and too quiet to notice. The prop shapes are
      // documented in JSDoc on each component and in `src/api/types.js`, where they can describe
      // the discriminated unions that matter rather than just listing names.
      'react/prop-types': 'off',

      // Unused variables are an error, not a warning: a warning in a project with no type checker
      // is a warning nobody reads.
      'no-unused-vars': ['error', { argsIgnorePattern: '^_', varsIgnorePattern: '^_' }],

      // `==` against null is the one loose comparison worth keeping, because these responses use
      // both null and undefined for absent optional fields and `x == null` covers both correctly.
      eqeqeq: ['error', 'always', { null: 'ignore' }],
    },
  },
  {
    files: ['src/**/*.test.{js,jsx}', 'src/test/**'],
    languageOptions: {
      globals: { ...globals.browser, ...globals.node },
    },
    rules: {
      // Tests deliberately construct malformed and absent-value payloads to prove the renderers
      // refuse them, so they need to write the shapes the app guards against.
      'no-restricted-syntax': 'off',
    },
  },
]
