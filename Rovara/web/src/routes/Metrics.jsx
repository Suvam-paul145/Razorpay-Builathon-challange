/**
 * The metrics summary (R14.C1, C7, C8, C9, R12.C13) — the screen the product is judged on.
 *
 * Two figures sit next to each other and they are the entire point of Revora:
 *
 * * **Observed recovered revenue.** Money that arrived on cases Revora acted on. A fact, labelled
 *   `CAUSALITY_NOT_ESTABLISHED` right beside it, because a meaningful share of those customers would
 *   have paid anyway.
 *
 * * **Incremental recovered revenue.** Money that arrived *because* Revora acted. A causal claim, and
 *   it renders `NOT ESTABLISHED` — with the reasons the server gave — unless a completed, adequately
 *   powered experiment reports a lift interval entirely above zero.
 *
 * `NOT ESTABLISHED` is rendered large and plainly, in the same visual weight as a real figure, rather
 * than tucked away as an error state. That is deliberate: a competitor's dashboard would print the
 * observed number in this slot, and the honest answer deserves the same prominence as the dishonest
 * one it replaces.
 */

import { useMetrics } from '../api/queries'
import { Fact, Failure, Loading, Panel } from '../components/Chrome'
import { AbsentValue, Label, Money, Rate } from '../components/Figure'

export function Metrics() {
  const query = useMetrics()
  if (query.isPending) return <Loading what="metrics" />
  if (query.isError) return <Failure error={query.error} what="metrics" />

  const report = query.data.report
  return (
    <div className="detail">
      <Panel
        title="Recovery performance"
        subtitle={`${report.reporting_period.start} to ${report.reporting_period.end} · computed ${report.computed_at}`}
        aside={
          <div className="labels">
            {report.labels.map((label) => (
              <Label key={label} text={label} />
            ))}
            {report.is_synthetic && <Label text="SYNTHETIC" />}
          </div>
        }
      >
        <div className="headline">
          <div className="headline__figure">
            <span className="headline__label">Observed recovered revenue</span>
            <Money value={report.observed_recovered_revenue} emphasis />
            <p className="headline__caveat">
              Money that arrived after Revora acted. This is not a claim that it arrived{' '}
              <em>because</em> Revora acted.
            </p>
            <Label text="CAUSALITY_NOT_ESTABLISHED" />
          </div>

          <div className="headline__figure headline__figure--primary">
            <span className="headline__label">Incremental recovered revenue</span>
            <IncrementalFigure incremental={report.incremental_recovered_revenue} />
          </div>
        </div>
      </Panel>

      <Panel
        title="The money"
        subtitle="Every amount formatted by the server; every one carries its labels"
      >
        <dl className="metrics">
          <Metric
            label="Revenue at risk"
            hint="Total value of failed payments detected in this period"
          >
            <Money value={report.revenue_at_risk} />
          </Metric>
          <Metric
            label="Natural recovered revenue"
            hint="Recovered with no Revora action at all — the baseline this product measures itself against"
          >
            <Money value={report.natural_recovered_revenue} />
          </Metric>
          <Metric label="Total recovery cost" hint="What the actions cost to take">
            <Money value={report.total_recovery_cost} />
          </Metric>
          <Metric
            label="Net recovered revenue"
            hint="Observed recovery less cost. Still not a causal claim."
          >
            <Money value={report.net_recovered_revenue} />
          </Metric>
          <Metric label="Unresolved revenue" hint="Detected, not recovered, and not yet ended">
            <Money value={report.unresolved_revenue} />
          </Metric>
        </dl>
      </Panel>

      <Panel title="The rates" subtitle="A rate with no denominator reads UNDEFINED rather than zero">
        <dl className="metrics">
          <Metric label="Recovery rate">
            <Rate value={report.recovery_rate} />
          </Metric>
          <Metric label="Intervention rate" hint="Share of cases Revora acted on at all">
            <Rate value={report.intervention_rate} />
          </Metric>
          <Metric label="Action success rate">
            <Rate value={report.action_success_rate} />
          </Metric>
          <Metric label="Escalation rate">
            <Rate value={report.escalation_rate} />
          </Metric>
          <Metric label="Average hours to recovery">
            <Rate value={report.average_hours_to_recovery} />
          </Metric>
        </dl>
      </Panel>

      <Panel title="The counts" subtitle="Including the two nobody else reports">
        <dl className="metrics">
          <Metric label="Cases">{report.case_count}</Metric>
          <Metric label="Recovered">{report.recovered_case_count}</Metric>
          <Metric label="Intervened">{report.intervened_case_count}</Metric>
          <Metric label="Confirmed actions">{report.confirmed_action_count}</Metric>
          <Metric label="Successful actions">{report.successful_action_count}</Metric>
          <Metric
            label="Unnecessary actions"
            hint="Revora acted on a payment that recovered anyway. Money and a customer's patience spent for nothing — reported because it is the cost of being wrong."
          >
            {report.unnecessary_action_count}
          </Metric>
          <Metric
            label="Cycles without action"
            hint="Revora evaluated and deliberately did nothing. A working outcome, not a gap."
          >
            {report.cycles_without_action_count}
          </Metric>
          <Metric label="Blocked">{report.blocked_case_count}</Metric>
          <Metric label="Escalated">{report.escalated_case_count}</Metric>
        </dl>
      </Panel>

      <Segment report={report} />
    </div>
  )
}

/**
 * The incremental figure, in all three of its arms.
 *
 * `NOT_ESTABLISHED` shows the refusal codes the server supplied, because "why not?" is the immediate
 * next question and the codes are the answer — `NO_COMPLETED_EXPERIMENT` and
 * `INTERVAL_CONTAINS_ZERO` are different situations with different remedies.
 *
 * @param {{ incremental: import('../api/types').Incremental }} props
 */
function IncrementalFigure({ incremental }) {
  if (incremental.status === 'DATA_UNAVAILABLE') {
    return (
      <>
        <AbsentValue marker={incremental} />
        <p className="headline__caveat">
          This one figure timed out. Every other figure on this page is current — and the causality
          caveat stays on, because a figure that could not be computed is certainly not a causal claim
          that was established.
        </p>
      </>
    )
  }
  if (incremental.status === 'NOT_ESTABLISHED') {
    return (
      <>
        <span className="not-established">NOT ESTABLISHED</span>
        <p className="headline__caveat">{incremental.detail}</p>
        <ul className="refusal-codes">
          {incremental.refusal_codes.map((code) => (
            <li key={code}>
              <code>{code}</code>
            </li>
          ))}
        </ul>
      </>
    )
  }
  const armsReported =
    incremental.control_case_count !== null && incremental.treatment_case_count !== null
  return (
    <>
      <Money value={incremental.amount} emphasis />
      <p className="headline__caveat">
        Supported by a completed experiment whose lift interval sits entirely above zero.
      </p>
      <dl className="facts facts--tight">
        <Fact label="Lift">{incremental.lift ?? 'not reported'}</Fact>
        <Fact label="Interval">{incremental.lift_interval ?? 'not reported'}</Fact>
        <Fact label="Control / treatment">
          {/* Not `?? 0` — the lint rule catches that, and it is right: a null arm count means the
              analysis did not report one, and rendering it as zero would claim an empty control arm,
              making an established causal claim look unsupported by its own numbers. */}
          {armsReported ? (
            `${incremental.control_case_count} / ${incremental.treatment_case_count}`
          ) : (
            <span className="muted">arm counts not reported</span>
          )}
        </Fact>
      </dl>
    </>
  )
}

/** @param {{ label: string, hint?: string, children: import('react').ReactNode }} props */
function Metric({ label, hint, children }) {
  return (
    <div className="metric">
      <dt>
        {label}
        {hint !== undefined && <span className="metric__hint">{hint}</span>}
      </dt>
      <dd>{children}</dd>
    </div>
  )
}

function Segment({ report }) {
  const entries = Object.entries(report.segment ?? {}).filter(([, value]) => value !== null)
  if (entries.length === 0) return null
  return (
    <Panel title="Segment" subtitle="The filter these figures were computed under">
      <dl className="facts facts--tight">
        {entries.map(([key, value]) => (
          <Fact key={key} label={key.replaceAll('_', ' ')}>
            {String(value)}
          </Fact>
        ))}
      </dl>
    </Panel>
  )
}
