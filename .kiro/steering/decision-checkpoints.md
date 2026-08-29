# Decision Checkpoints

Full autonomy on execution. Ask before deciding.

I want you to work end-to-end without interruption on *how* to build something. I want to be
consulted on *what* gets built and on choices that are expensive to reverse later.

## Stop and ask me first

Ask a focused question (or a short numbered set of options) and wait for my answer before writing
code, when the work involves:

- **Product / feature scope** — what a feature does, what it does not do, which cases are in or out
  of scope, what the user-visible behavior is.
- **Product naming and copy** — anything a user reads: feature names, UI labels, error text sent to
  customers, email or notification wording.
- **Data model changes** — new tables or collections, new required fields, changed field semantics,
  anything that implies a migration.
- **Public contracts** — API request/response shapes, webhook payloads, event schemas, CLI flags.
  Anything another system depends on.
- **New dependencies or services** — adding a library, database, queue, or third-party API that
  wasn't already in the project.
- **Money, retries, and customer contact** — anything that charges, refunds, retries a payment,
  or sends a message to a real person. Confirm trigger conditions, limits, and idempotency with me
  explicitly.
- **Auth, permissions, and PII handling** — who can do what, what gets logged, what gets stored.
- **Architecture direction** — introducing a new pattern, layer, or abstraction; replacing an
  existing approach rather than extending it.
- **Genuine ambiguity** — if two reasonable readings of my request lead to materially different
  implementations, ask rather than guess. Do not silently pick one and note it at the end.

## Do not ask, just decide and proceed

- Local naming: variables, private functions, internal types, file names.
- Formatting, lint fixes, import ordering.
- Which internal helper to extract, how to structure a function's control flow.
- Test structure and test case naming.
- Reading files, searching, running builds, running tests, running linters.
- Any choice among options that are genuinely equivalent in outcome. Pick one, mention it in one
  line, move on.
- Fixing a bug whose correct behavior is unambiguous.

## How to ask

- Batch related questions into one message. Don't drip-feed one question per turn.
- Lead with a recommendation and your reasoning, then the alternatives. I usually want
  "I'd go with B because X — but A is viable if you care more about Y." Not an open-ended
  "what would you like?"
- Keep it to the decisions that actually matter for this task. Three sharp questions beat ten
  thorough ones.
- If you have already asked and I gave a direction, follow it exactly. Don't re-litigate it.

## After I answer

Implement the whole thing. Read what you need, make the change, run the build and the relevant
tests, fix what breaks. Don't come back mid-implementation to confirm details that follow from a
decision I already made.
