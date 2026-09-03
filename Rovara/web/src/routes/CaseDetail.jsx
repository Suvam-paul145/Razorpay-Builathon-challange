/**
 * One case, and the answer to "why did Revora do that?" (R14.C3–C6, R14.C14, R11.C5).
 *
 * The order of the sections is the order the pipeline produced them, because that ordering *is* the
 * explanation: what failed, what it was diagnosed as, what doing nothing was worth, every action that
 * was priced, which was chosen and why, what policy decided across all twelve checks, what actually
 * executed, what the provider said when asked, and how the result is classified.
 *
 * Two sections carry most of the weight.
 *
 * **The candidate table shows every action considered, excluded ones included.** The comparison is the
 * product, not the winner. An excluded action carries its exclusion reason, so "a retry was considered
 * and is not available on this account" is visible rather than being an absence nobody can interpret.
 *
 * **A refusal is rendered as fully as an action, and never as a warning.** Where the selection was
 * `DO_NOTHING` or `WAIT`, the server returns the recorded reason, a plain-language explanation, the
 * baseline and incremental probabilities, the net value and all three compared thresholds — and all of
 * it renders in the same visual register as a successful action. "We decided not to spend your money
 * on this customer" is the single most defensible thing this product says, and a red indicator would
 * teach an operator to read it as a fault.
 */

import { Link, useParams } from 'react-router-dom'

import {
  useAssignOwnership,
  useAuditTrail,
  useCaseDetail,
  useReleaseOwnership,
} from '../api/queries'
import { isAbsentMarker } from '../api/types'
import { Empty, Fact, Failure, Loading, Panel, StateBadge, When } from '../components/Chrome'
import { AbsentValue, Enum, Label, Money, humanise } from '../components/Figure'
import { Timeline } from '../components/Timeline'

export function CaseDetail() {
  const { caseId = '' } = useParams()
  const query = useCaseDetail(caseId)

  if (query.isPending) return <Loading what="the case" />
  if (query.isError) return <Failure error={query.error} what="the case" />

  const detail = query.data
  return (
    <div className="detail">
      {/* R26.C12. The timeline is the *first* element of the detail view, and everything R14.C3–C6
          and R14.C14 require is retained below it unchanged — the diagnosis, the full candidate
          comparison, all twelve policy checks, every execution attempt, every authoritative read,
          the recorded outcome and the refusal block.

          First taken literally, above the case header, which costs something worth naming: a reader
          landing here sees the history before they see which payment it belongs to. The header is
          immediately below and the URL carries the case id, so the cost is one scroll-height of
          ambiguity — against which the requirement's reason is that a reviewer with two minutes
          should reach the answer before they reach the ten sections that also contain it. */}
      <Timeline caseId={caseId} />
      <CaseHeader detail={detail} caseId={caseId} />
      <DiagnosisPanel detail={detail} />
      <DecisionPanel detail={detail} />
      <PolicyPanel detail={detail} />
      <ExecutionPanel detail={detail} />
      <OutcomePanel detail={detail} />
      <AuditPanel caseId={caseId} />
    </div>
  )
}

// ---------------------------------------------------------------------------
// Header, counters and ownership (R14.C11)
// ---------------------------------------------------------------------------

function CaseHeader({ detail, caseId }) {
  const summary = detail.case
  const assign = useAssignOwnership(caseId)
  const release = useReleaseOwnership(caseId)
  const owned = summary.human_owner_user_id !== null

  return (
    <Panel
      title={`Case ${summary.provider_payment_id}`}
      subtitle={`Detected ${summary.detected_at} · window closes ${summary.window_end_at}`}
      aside={
        <div className="ownership">
          {/* R14.C11. The button is not the interesting part — the sentence next to it is. Claiming a
              case suspends every automated action on it, and an operator who does not know that will
              claim one to "have a look" and silently stop the recovery. */}
          <p className="ownership__state">
            {owned ? (
              <>
                <strong>A human owns this case.</strong> Automated action is suspended until it is
                released.
              </>
            ) : (
              <>Automation is active. Claiming this case suspends it.</>
            )}
          </p>
          <button
            type="button"
            className={owned ? 'button' : 'button button--primary'}
            disabled={assign.isPending || release.isPending}
            onClick={() => {
              if (owned) release.mutate()
              else assign.mutate()
            }}
          >
            {owned ? 'Release ownership' : 'Claim ownership'}
          </button>
          {assign.isError && <p className="status status--error">{assign.error.message}</p>}
          {release.isError && <p className="status status--error">{release.error.message}</p>}
        </div>
      }
    >
      <dl className="facts">
        <Fact label="State">
          {/* The server's label (R26.C14), the same one the case list renders. Deriving it here
              with `humanise` gave this screen "Policy check" where the list said "Decision
              recorded" — two words for one state, on two pages a reader moves between. */}
          <StateBadge state={summary.state} label={summary.state_label} />
          {detail.terminal_reason !== null && (
            <span className="fact__note">{humanise(detail.terminal_reason)}</span>
          )}
        </Fact>
        <Fact label="Amount at risk">
          <Money value={summary.payment_amount} emphasis />
        </Fact>
        <Fact label="Recovered">
          <Money value={summary.recovered_amount} emphasis />
        </Fact>
        <Fact label="Classification">
          <Enum value={summary.outcome_classification} />
        </Fact>
        <Fact label="Customer">
          {/* Masked at write time. Nothing here re-masks, and nothing here decides what to hide. */}
          <code>{summary.customer_contact_masked ?? 'not recorded'}</code>
        </Fact>
        <Fact label="Consent">
          {isAbsentMarker(detail.consent) ? (
            <AbsentValue marker={detail.consent} />
          ) : (
            <span className={detail.consent.opted_out ? 'flag flag--stop' : 'flag'}>
              {detail.consent.opted_out ? 'Opted out' : 'Consent on record'}
              {detail.consent.source !== null && (
                <span className="fact__note">via {detail.consent.source}</span>
              )}
            </span>
          )}
        </Fact>
        {/* R21.C11 and R21.C9. Its own Fact, immediately beside Consent, because the two are
            different statements and this is the pair a reader is most likely to collapse: a
            suppression is an objection to *this debt*, an opt-out is a withdrawal of consent to
            be contacted at all. Rendering them adjacently is how the screen keeps them apart. A
            case with no suppression shows "None", not nothing — an omitted row reads as "we did
            not look", which on a contact control is the wrong thing for it to read as. */}
        <Fact label="Contact suppression">
          {detail.contact_suppression === null ? (
            <span className="muted">None</span>
          ) : (
            <span
              className={detail.contact_suppression.in_force ? 'flag flag--stop' : 'flag'}
            >
              {detail.contact_suppression.hard_stop_label}
              <span className="fact__note">
                {detail.contact_suppression.in_force ? 'in force since ' : 'suppressed '}
                <When iso={detail.contact_suppression.suppressed_at} />
                {detail.contact_suppression.inherited && ', from another case on this order'}
              </span>
              {detail.contact_suppression.released_at !== null && (
                <span className="fact__note">
                  released <When iso={detail.contact_suppression.released_at} /> by{' '}
                  <code>{detail.contact_suppression.released_by_user_id}</code>
                </span>
              )}
            </span>
          )}
        </Fact>
      </dl>

      {/* The bounds, shown as used-of-allowed rather than as bare counts. "1 action" means nothing;
          "1 of 3 attempts" is the fact that explains why a case did or did not get another try. */}
      <dl className="facts facts--tight">
        <Fact label="Attempts">
          {detail.counters.executed_action_count} of {detail.counters.max_recovery_attempts}
        </Fact>
        <Fact label="Customer messages">
          {detail.counters.customer_message_count} of {detail.counters.max_customer_messages}
        </Fact>
        <Fact label="Decision cycle">{detail.counters.decision_cycle_count}</Fact>
        <Fact label="Last outbound">
          {detail.counters.last_outbound_at === null ? (
            <span className="muted">never contacted</span>
          ) : (
            <When iso={detail.counters.last_outbound_at} />
          )}
        </Fact>
      </dl>
    </Panel>
  )
}

// ---------------------------------------------------------------------------
// Diagnosis and baseline (R14.C3)
// ---------------------------------------------------------------------------

function DiagnosisPanel({ detail }) {
  return (
    <Panel
      title="Why it failed"
      subtitle="Cause, confidence, method and the provider fields it was read from"
    >
      {isAbsentMarker(detail.diagnosis) ? (
        <AbsentValue marker={detail.diagnosis} />
      ) : (
        <>
          <dl className="facts">
            <Fact label="Cause">
              <strong>{humanise(detail.diagnosis.cause)}</strong>
            </Fact>
            <Fact label="Confidence">{detail.diagnosis.confidence}</Fact>
            <Fact label="Method">
              {humanise(detail.diagnosis.method)}
              {/* Stated, not implied by an absent badge. R3.C1's claim is that the deterministic path
                  needs no model at all, and a surface that only shows AI when present cannot express
                  "this decision involved none" — which is the interesting fact about this build. */}
              <span className="fact__note">
                {detail.diagnosis.ai_involved ? 'AI-assisted' : 'no AI involved'}
              </span>
            </Fact>
            <Fact label="Recorded">
              <When iso={detail.diagnosis.recorded_at} />
            </Fact>
          </dl>
          {detail.diagnosis.substituted_to_unknown && (
            <p className="notice">
              The diagnosed cause was replaced with <code>UNKNOWN</code> because confidence fell below
              the required bound. A low-confidence cause is not used to justify an action.
            </p>
          )}
          {detail.diagnosis.evidence !== null && (
            <Evidence
              title="Provider fields this was derived from"
              data={detail.diagnosis.evidence}
            />
          )}
        </>
      )}

      <h3 className="subhead">If Revora had done nothing</h3>
      {isAbsentMarker(detail.baseline) ? (
        <AbsentValue marker={detail.baseline} />
      ) : (
        <dl className="facts">
          <Fact label="Recovery probability">
            <strong>{detail.baseline.probability}</strong>
          </Fact>
          <Fact label="Interval">
            {/* An estimate without an interval is a guess wearing a decimal point. When the interval
                is absent the fact that it is absent is the finding, so it is stated. */}
            {detail.baseline.interval ?? (
              <span className="muted">no interval available for this segment</span>
            )}
          </Fact>
          <Fact label="Method">{humanise(detail.baseline.method)}</Fact>
          <Fact label="Validation">
            <span className="label label--caution">{detail.baseline.validation_status}</span>
          </Fact>
        </dl>
      )}
    </Panel>
  )
}

// ---------------------------------------------------------------------------
// The comparison (R14.C4, R14.C14)
// ---------------------------------------------------------------------------

function DecisionPanel({ detail }) {
  if (isAbsentMarker(detail.recommendation)) {
    return (
      <Panel title="What Revora considered">
        <AbsentValue marker={detail.recommendation} />
      </Panel>
    )
  }
  const recommendation = detail.recommendation
  return (
    <Panel
      title="What Revora considered"
      subtitle={`${recommendation.candidate_count} actions priced. Selected: ${humanise(
        recommendation.selected_action,
      )} — ${humanise(recommendation.selection_reason)}`}
    >
      {recommendation.refusal !== undefined && <RefusalBlock refusal={recommendation.refusal} />}

      <div className="table-scroll">
        <table className="grid grid--dense">
          <caption className="sr-only">Every candidate action considered, ranked</caption>
          <thead>
            <tr>
              <th scope="col">Rank</th>
              <th scope="col">Action</th>
              <th scope="col" className="num">
                Incremental probability
              </th>
              <th scope="col" className="num">
                Expected revenue
              </th>
              {/* Four cost figures, then their total (R31.C7). The blended "action cost" these
                  replaced could not say whether an action was excluded because links are
                  expensive or because messages are, which is the one question this column set
                  exists to answer. */}
              <th scope="col" className="num">
                Financial cost
              </th>
              <th scope="col" className="num">
                Communication cost
              </th>
              <th scope="col" className="num">
                Risk cost
              </th>
              <th scope="col" className="num">
                Customer cost
              </th>
              <th scope="col" className="num">
                Total action cost
              </th>
              <th scope="col" className="num">
                Net value
              </th>
            </tr>
          </thead>
          <tbody>
            {recommendation.candidates.map((candidate) => (
              <CandidateRow
                key={candidate.action}
                candidate={candidate}
                selected={candidate.action === recommendation.selected_action}
              />
            ))}
          </tbody>
        </table>
      </div>
      <p className="footnote">
        Every action considered is listed, including the ones that were excluded — an action that
        could not be used is different evidence from an action that was not worth using, and both are
        different from an action that never appears. Costs are summed by the server.
      </p>
      {recommendation.candidates.some((candidate) => candidate.cost_split_not_measured) && (
        <p className="notice">
          {/* R31.C10. Stated once for the table as well as beside each figure, because a reader
              scanning for the cheap row will see the columns before they see a label in one cell. */}
          Some rows below were priced before financial and communication cost were estimated
          separately. On those rows the whole recorded cost sits in <code>Financial cost</code> and
          the communication figure is a zero nothing measured, marked{' '}
          <code>COST_SPLIT_NOT_MEASURED</code> beside both.
        </p>
      )}
    </Panel>
  )
}

function CandidateRow({ candidate, selected }) {
  const classes = [selected ? 'row--selected' : '', candidate.excluded ? 'row--excluded' : '']
    .filter((part) => part !== '')
    .join(' ')
  return (
    <tr className={classes}>
      <td>{candidate.rank ?? <span className="muted">unranked</span>}</td>
      <td>
        <strong>{humanise(candidate.action)}</strong>
        {selected && <span className="pill pill--selected">selected</span>}
        {candidate.excluded && (
          <span className="pill pill--excluded" title={candidate.exclusion_reason ?? undefined}>
            excluded: {humanise(candidate.exclusion_reason ?? 'no reason recorded')}
          </span>
        )}
      </td>
      <td className="num">{candidate.incremental_probability}</td>
      <td className="num">
        <Money value={candidate.expected_incremental_revenue} />
      </td>
      {/* R31.C10. The marking sits in both cost cells rather than once per row: a reader comparing
          the communication column across rows must be able to tell a measured zero from a zero the
          migration wrote, and a label three columns away does not tell them. */}
      <td className="num">
        <Money value={candidate.financial_cost} />
        {candidate.cost_split_not_measured && <Label text="COST_SPLIT_NOT_MEASURED" />}
      </td>
      <td className="num">
        <Money value={candidate.communication_cost} />
        {candidate.cost_split_not_measured && <Label text="COST_SPLIT_NOT_MEASURED" />}
      </td>
      <td className="num">
        <Money value={candidate.risk_cost} />
      </td>
      <td className="num">
        <Money value={candidate.customer_cost} />
      </td>
      {/* Summed by the server (R14.C12). Adding `.minor` here would fail lint, by design — a
          browser-side total is free to disagree with the net value in the next column. */}
      <td className="num">
        <Money value={candidate.total_action_cost} />
      </td>
      <td className="num">
        <Money value={candidate.net_recovery_value} emphasis />
      </td>
    </tr>
  )
}

/**
 * R14.C14. The refusal, with every number that decided it.
 *
 * Styled as a finding, not a fault. All three thresholds appear even though usually one decided,
 * because a merchant asking "why not?" is asking about the whole comparison — showing only the
 * failing bound invites "so lower it", and the answer to that is the other two.
 */
function RefusalBlock({ refusal }) {
  return (
    <div className="refusal">
      <h3 className="refusal__title">Revora chose not to act</h3>
      {/* Server-composed prose. A client that worded this would eventually word it as a failure, and
          "we chose not to act" being read as "we could not act" is the misreading this product can
          least afford. */}
      <p className="refusal__explanation">{refusal.explanation}</p>
      <dl className="facts facts--tight">
        <Fact label="Recorded reason">
          <code>{refusal.reason}</code>
        </Fact>
        <Fact label="Baseline probability">
          {typeof refusal.baseline_probability === 'string' ? (
            refusal.baseline_probability
          ) : (
            <AbsentValue marker={refusal.baseline_probability} />
          )}
        </Fact>
        <Fact label="Incremental probability">
          {refusal.incremental_probability ?? <span className="muted">not applicable</span>}
        </Fact>
        <Fact label="Net value">
          {refusal.net_recovery_value === null ? (
            <span className="muted">not applicable</span>
          ) : (
            <Money value={refusal.net_recovery_value} />
          )}
        </Fact>
      </dl>
      <h4 className="subhead subhead--small">Compared against</h4>
      <dl className="facts facts--tight">
        <Fact label="Minimum net value">
          {/* Configuration, not a case figure, so it arrives as bare minor units and there is no
              formatted form to render. Shown as configured with its unit named, rather than divided
              here into something that would look like an amount while not being one. */}
          {refusal.compared_thresholds.min_net_value_threshold}
          <span className="fact__note">minor units, as configured</span>
        </Fact>
        <Fact label="Minimum incremental probability">
          {refusal.compared_thresholds.min_incremental_probability}
        </Fact>
        <Fact label="Maximum cost-to-value ratio">
          {refusal.compared_thresholds.max_cost_to_value_ratio}
        </Fact>
        <Fact label="High-baseline threshold">
          {refusal.compared_thresholds.high_baseline_threshold}
        </Fact>
      </dl>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Policy (R14.C5)
// ---------------------------------------------------------------------------

function PolicyPanel({ detail }) {
  if (isAbsentMarker(detail.policy_decisions)) {
    return (
      <Panel title="What policy decided">
        <AbsentValue marker={detail.policy_decisions} />
      </Panel>
    )
  }
  return (
    <Panel
      title="What policy decided"
      subtitle="All twelve checks, in the fixed evaluation order, for every decision cycle"
    >
      {detail.policy_decisions.map((decision) => (
        <div className="decision" key={decision.policy_decision_id}>
          <header className="decision__head">
            <span className={`verdict verdict--${decision.verdict.toLowerCase()}`}>
              {decision.verdict}
            </span>
            <span className="decision__reason">{humanise(decision.primary_reason)}</span>
            <span className="decision__meta">
              {humanise(decision.selected_action)} · cycle {decision.decision_cycle} ·{' '}
              <When iso={decision.evaluated_at} /> · rules {decision.rule_set_version}
            </span>
          </header>

          {/* Twelve, always. A record showing fewer would be indistinguishable from an evaluation
              that stopped early and approved, which is why the counts are stated and compared. */}
          {decision.recorded_check_count !== decision.expected_check_count && (
            <p className="notice notice--warn">
              {decision.recorded_check_count} of {decision.expected_check_count} checks have recorded
              results. A missing result is shown below as <code>NOT_RECORDED</code> rather than
              omitted.
            </p>
          )}

          <ol className="checks">
            {decision.checks.map((check) => (
              <CheckRow
                key={check.check_id}
                check={check}
                decided={check.check_id === decision.primary_reason}
              />
            ))}
          </ol>
        </div>
      ))}
    </Panel>
  )
}

function CheckRow({ check, decided }) {
  return (
    <li
      className={`check check--${check.outcome.toLowerCase()}${decided ? ' check--decided' : ''}`}
    >
      <span className="check__order">{check.check_order}</span>
      <span className="check__id">{humanise(check.check_id)}</span>
      <span className="check__outcome">{check.outcome}</span>
      {decided && <span className="pill pill--decided">determined the verdict</span>}
      {check.detail !== null && <span className="check__detail">{check.detail}</span>}
    </li>
  )
}

// ---------------------------------------------------------------------------
// Execution and provider reads
// ---------------------------------------------------------------------------

function ExecutionPanel({ detail }) {
  return (
    <Panel
      title="What was actually done"
      subtitle="Every execution attempt, with its idempotency key"
    >
      {isAbsentMarker(detail.executed_actions) ? (
        <AbsentValue marker={detail.executed_actions} />
      ) : (
        <ul className="attempts">
          {detail.executed_actions.map((intent) => (
            <li className="attempt" key={intent.intent_id}>
              <div className="attempt__head">
                <strong>{humanise(intent.action)}</strong>
                <span className={`intent intent--${intent.state.toLowerCase()}`}>
                  {intent.state}
                </span>
                <span className="attempt__meta">
                  attempt {intent.attempt_ordinal} · <When iso={intent.attempt_started_at} />
                </span>
              </div>
              {intent.state === 'UNCERTAIN' && (
                <p className="notice">
                  The provider call timed out after the request was sent, so whether the link exists
                  cannot be known from here. Revora records this rather than guessing; reconciliation
                  resolves it by reading, reusing this same key — so no second link can be created.
                </p>
              )}
              <dl className="facts facts--tight">
                <Fact label="Idempotency key">
                  <code>{intent.idempotency_key}</code>
                </Fact>
                <Fact label="Resolved">
                  {intent.resolved_at === null ? (
                    <span className="muted">unresolved</span>
                  ) : (
                    <When iso={intent.resolved_at} />
                  )}
                </Fact>
                {intent.provider_failure_code !== null && (
                  <Fact label="Provider failure">
                    <code>{intent.provider_failure_code}</code>
                  </Fact>
                )}
                {intent.provider_short_url !== null && (
                  <Fact label="Payment link">
                    {/* A bearer capability: whoever holds this URL can pay. Shown because this view
                        is session-authenticated, and never written to a log or an audit record. */}
                    <a
                      className="link"
                      href={intent.provider_short_url}
                      rel="noreferrer noopener"
                      target="_blank"
                    >
                      {intent.provider_short_url}
                    </a>
                  </Fact>
                )}
                {intent.is_post_payment && (
                  <Fact label="Sent after payment">
                    <span className="flag flag--stop">yes — this should not happen</span>
                  </Fact>
                )}
              </dl>
            </li>
          ))}
        </ul>
      )}
    </Panel>
  )
}

function OutcomePanel({ detail }) {
  return (
    <Panel
      title="What the provider said"
      subtitle="A recovery is declared only from an authoritative read, never from a webhook"
    >
      {isAbsentMarker(detail.authoritative_reads) ? (
        <AbsentValue marker={detail.authoritative_reads} />
      ) : (
        <div className="table-scroll">
          <table className="grid grid--dense">
            <caption className="sr-only">Authoritative provider reads</caption>
            <thead>
              <tr>
                <th scope="col">Read at</th>
                <th scope="col">Attempt</th>
                <th scope="col">Status</th>
                <th scope="col">Captured</th>
                <th scope="col" className="num">
                  Amount
                </th>
                <th scope="col" className="num">
                  Refunded
                </th>
              </tr>
            </thead>
            <tbody>
              {detail.authoritative_reads.map((read) => (
                <tr key={`${read.read_at}-${read.attempt_no}`}>
                  <td>
                    <When iso={read.read_at} />
                  </td>
                  <td>{read.attempt_no}</td>
                  <td>{read.status}</td>
                  <td>{read.captured ? 'yes' : 'no'}</td>
                  <td className="num">
                    <Money value={read.amount} />
                  </td>
                  <td className="num">
                    <Money value={read.amount_refunded} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <h3 className="subhead">Recorded outcome</h3>
      {isAbsentMarker(detail.outcome) ? (
        <AbsentValue marker={detail.outcome} />
      ) : (
        <dl className="facts">
          <Fact label="Classification">
            <strong>{detail.outcome.classification}</strong>
            {detail.outcome.classification === 'OBSERVED' && (
              <span className="label label--caution">CAUSALITY NOT ESTABLISHED</span>
            )}
          </Fact>
          <Fact label="Recovered">
            <Money value={detail.outcome.recovered_amount} emphasis />
          </Fact>
          <Fact label="Recovered at">
            <When iso={detail.outcome.recovery_timestamp} />
          </Fact>
          <Fact label="Verified by read">
            {/* The column is NOT NULL in the schema precisely so this can be shown. Surfacing the id
                is what makes the figure checkable rather than merely asserted. */}
            <code>{detail.outcome.verified_by_read_id}</code>
          </Fact>
          {detail.outcome.reconciled_from_terminal_state !== null && (
            <Fact label="Reconciled from">
              {humanise(detail.outcome.reconciled_from_terminal_state)}
              <span className="fact__note">a late capture reopened a case that had ended</span>
            </Fact>
          )}
        </dl>
      )}
    </Panel>
  )
}

// ---------------------------------------------------------------------------
// The trail (R11.C5)
// ---------------------------------------------------------------------------

function AuditPanel({ caseId }) {
  const query = useAuditTrail(caseId)
  return (
    <Panel
      title="Full history"
      subtitle="Per-case sequence, gap-free and append-only. Every record carries its correlation id."
    >
      {query.isPending && <Loading what="the audit trail" />}
      {query.isError && <Failure error={query.error} what="the audit trail" />}
      {query.isSuccess && query.data.records.length === 0 && <Empty>No records.</Empty>}
      {query.isSuccess && query.data.records.length > 0 && (
        <ol className="trail">
          {query.data.records.map((record) => (
            <li className="trail__item" key={`${record.seq}-${record.event_type}`}>
              <span className="trail__seq">{record.seq}</span>
              <div className="trail__body">
                <div className="trail__head">
                  <strong>{humanise(record.event_type)}</strong>
                  <span className="trail__actor">{record.actor}</span>
                  <When iso={record.occurred_at} />
                </div>
                {record.previous_state !== null && record.new_state !== null && (
                  <p className="trail__transition">
                    {humanise(record.previous_state)} → {humanise(record.new_state)}
                  </p>
                )}
                {record.decision !== null && <Evidence title="Decision" data={record.decision} />}
                {record.policy_result !== null && (
                  <Evidence title="Policy result" data={record.policy_result} />
                )}
                {record.evidence !== null && <Evidence title="Evidence" data={record.evidence} />}
                <p className="trail__correlation">
                  <code>{record.correlation_id}</code>
                </p>
              </div>
            </li>
          ))}
        </ol>
      )}
      <p className="footnote">
        <Link className="link" to="/cases">
          Back to cases
        </Link>
      </p>
    </Panel>
  )
}

/**
 * A recorded JSON document, shown as-is behind a disclosure.
 *
 * Deliberately not prettified into prose. These are the persisted evidence fields, already masked at
 * write time, and an operator comparing a dashboard reading against the row needs the shape the row
 * actually has. A rendering that summarised them would be a second, weaker view of the record.
 */
function Evidence({ title, data }) {
  return (
    <details className="evidence">
      <summary>{title}</summary>
      <pre>{JSON.stringify(data, null, 2)}</pre>
    </details>
  )
}
