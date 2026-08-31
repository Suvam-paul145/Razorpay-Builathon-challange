# Provider verification spikes

Four standalone scripts that close the four provider questions the design tags
**[EVIDENCE INSUFFICIENT]**. They are run by hand against Razorpay **test-mode**
credentials, they are not imported by anything under `revora/`, and they are not part of
the automated test suite. Their output is evidence, and their destination is
`docs/provider-findings.md`.

Each one measures exactly one thing and prints, in plain language, which configuration
default the result sets or confirms. That is the whole point: these numbers decide
`EXECUTION_RECONCILIATION_INTERVAL`, `OUTCOME_READ_LATENCY_BOUND` and the executable
action set, and it is much cheaper to measure them now than to discover them from a
duplicate payment demand later.

## What each one does, and what it costs to run

| Spike | Question | Creates provider objects? | Manual browser step? | Time |
| --- | --- | --- | --- | --- |
| `link_listing_freshness.py` | How long until a new Payment Link is listable by `reference_id`? | **Yes** — one link per iteration, ~50 by default | No | 2–5 min |
| `duplicate_reference_id.py` | Is a duplicate `reference_id` rejected? | **Yes** — up to two links per trial | No | under a minute |
| `retry_capability.py` | Does any server-side retry or payment-method-update exist on this account? | **Yes, conditionally** — sends non-GET requests to undocumented paths; `--no-write-probes` turns that off | **Yes** — you must first produce a `failed` payment, and read a dashboard checklist afterwards | 5–10 min |
| `payment_read_lag.py` | How far behind the capture webhook is the authoritative read? | No, but the payment you make does | **Yes** — you must complete test payments, and expose a local receiver | 15–30 min |

Every script that writes says so in its module docstring and in its `--help`.

## 1. Get test-mode credentials

In the Razorpay dashboard, switch to **Test Mode**, then Account & Settings -> API Keys,
and generate a key pair. You need:

| Variable | What it is | Needed by |
| --- | --- | --- |
| `RAZORPAY_KEY_ID` | Test-mode key id, `rzp_test_…` | all four |
| `RAZORPAY_KEY_SECRET` | Test-mode key secret | all four |
| `RAZORPAY_WEBHOOK_SECRET` | The secret on the webhook you configure — **a different secret from the key secret** | `payment_read_lag.py` in webhook mode |

Two things worth knowing before you generate keys:

- The same keys serve Payment Gateway and RazorpayX, so regenerating them has a blast
  radius beyond Revora. On a shared account, ask first.
- A key id that does not begin with `rzp_test_` is refused. Two of these scripts create
  Payment Links; against a live account that means real payment demands to real people.
  `--allow-live-credentials` overrides the check, exists only for completeness, and you
  should not need it.

### Setting the variables

Windows PowerShell, current session only:

```powershell
$env:RAZORPAY_KEY_ID = "rzp_test_xxxxxxxxxxxxxx"
$env:RAZORPAY_KEY_SECRET = "xxxxxxxxxxxxxxxxxxxxxxxx"
$env:RAZORPAY_WEBHOOK_SECRET = "xxxxxxxxxxxx"   # spike 2 only
```

POSIX shell (bash / zsh):

```bash
export RAZORPAY_KEY_ID="rzp_test_xxxxxxxxxxxxxx"
export RAZORPAY_KEY_SECRET="xxxxxxxxxxxxxxxxxxxxxxxx"
export RAZORPAY_WEBHOOK_SECRET="xxxxxxxxxxxx"   # spike 2 only
```

Use the shell environment, not a file. Nothing here reads `.env`, and the repository's
`.gitignore` should not be the only thing standing between a secret and a commit. No
script prints a credential, and the secret is scrubbed out of every JSON artifact before
it is written — but the cheapest way to keep a secret out of a file is not to put it in
one.

On a legacy Windows code page, `$env:PYTHONUTF8 = "1"` gives clean punctuation if you
pipe the output somewhere. Not required; the scripts will not crash without it.

## 2. Run them in this order

Cheapest and most decisive first, so a broken setup surfaces in thirty seconds rather
than half an hour in.

```powershell
# Sanity check: --help works with no credentials set at all.
python scripts/spikes/link_listing_freshness.py --help

# 1. Duplicate reference_id. No manual step, seconds, and it either confirms a
#    non-dependency or hands you a free extra guarantee.
python scripts/spikes/duplicate_reference_id.py --trials 3

# 2. Listing freshness. This is the one that decides the reconciliation parameters.
python scripts/spikes/link_listing_freshness.py --iterations 50

# 3. Retry capability. Needs a pay_… id that is already `failed`.
python scripts/spikes/retry_capability.py --payment-id pay_XXXXXXXXXXXX

# 4. Read lag. Needs a tunnel and your patience. See below.
python scripts/spikes/payment_read_lag.py --mode webhook --iterations 10
```

Every script takes `--help`, works without credentials present, and exits 0 doing so.
Iteration counts, timeouts and the output directory are all flags — read `--help` before
assuming a default.

Exit codes: `0` means a measurement completed, even if the answer was uncomfortable.
Non-zero means the spike never got started — missing credentials, a live-looking key, a
missing payment id, or a payment that was not in the state the spike needs.

## 3. The manual steps, in detail

### `retry_capability.py` needs a failed payment

Pay for something in the test-mode checkout using a Razorpay test instrument documented
to fail, then pass the resulting `pay_…` id. The spike refuses a payment that is not in
the `failed` state, because probing retry against a captured payment answers a different
question and would produce a negative result for the wrong reason.

Afterwards it prints a dashboard checklist. Do the checklist. A settings page read by a
human is stronger evidence than a script guessing at URLs, and the script says so.

### `payment_read_lag.py` needs a reachable webhook endpoint

The script runs a small local receiver and times the authoritative read against real
webhook arrival. To get Razorpay to reach it:

1. Start the spike. It prints the local URL it is listening on.
2. Expose that port with a tunnel Razorpay accepts. The verified constraints are public
   HTTPS on port 80 or 443 and TLS 1.2+, and several tunnelling and request-bin domains
   are blacklisted — including `ngrok.io`, `webhook.site`, `requestbin.com` and
   `beeceptor.com`. Razorpay's own documentation points to **zrok**, so use that.
3. In the test-mode dashboard, add a webhook at `<tunnel-url>/webhooks/spike` (or
   whatever you passed to `--path`), subscribe to `payment.captured`, and copy the
   webhook secret into `RAZORPAY_WEBHOOK_SECRET`.
4. Complete test payments in the browser. Each capture produces one observation.

`GET` on the receiver path returns a liveness line, which is the quickest way to tell
whether the tunnel reaches you before you start paying for things.

Signatures are verified by default and a mismatch is rejected. If you cannot set the
webhook secret, `--skip-signature-verification` exists, and the artifact will record that
the input was unverified.

If you cannot get a tunnel up at all, `--mode payment-id` still records whether the read
agrees with a completed payment. It cannot time the read against webhook arrival, so it
answers a weaker version of the question — and labels itself accordingly.

## 4. What to do with the results

1. **Read the printed summary.** Each script ends with a `CONSEQUENCE FOR THE DESIGN`
   block naming the parameters the result decides.
2. **Keep the artifact.** JSON lands in `docs/spike-results/` (override with
   `--output-dir`). It holds every observation, the verbatim provider responses, and the
   list of assumptions that run relied on. It contains no credentials.
3. **Fill in `docs/provider-findings.md`.** Every result field starts as `NOT YET RUN`.
   Transcribe the numbers, write the conclusion, set the status, and reference the
   artifact filename so a number can be traced to the run that produced it.
4. **Carry changed values into the configuration seed (task 5.6).** Where a default
   survived, record that explicitly. "Confirmed by measurement" and "never checked" must
   not look the same in that document.
5. **Clean up the test account if you care to.** Every created object id is in the
   artifact. Leaving spike links in a test account is harmless; leaving them without a
   record of what created them is untidy.

If a spike comes back inconclusive, say so and leave the design's `[ASSUMPTION]` in
place. An unmeasured parameter documented as unmeasured is a known risk. One recorded as
confirmed when nobody measured it is worse than not running the spike at all.
