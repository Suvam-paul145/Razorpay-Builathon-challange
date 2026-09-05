/**
 * Experiments (R14.C8). The only thing that can license a causal claim, so the only thing that can
 * turn `incremental_recovered_revenue` into a number.
 *
 * **A lift never renders without its interval.** The bounds are shown as bounds rather than as a
 * width, because a width can be displayed without its centre and bounds cannot be displayed without
 * revealing whether zero sits inside them — and that is the single fact that decides whether a claim
 * is permitted.
 *
 * `interval_contains_zero` is computed by the server. Comparing two decimal strings in JavaScript is
 * not a job to hand to number coercion when the answer decides whether a revenue claim may be made.
 *
 * An experiment with no analysis yet is a **running** experiment, and it says so rather than looking
 * broken. The contamination and exclusion counts are shown because an experiment whose arms leaked
 * into each other is not evidence, and the count is how a reader can tell.
 */

import { useExperiments } from '../api/queries'
import { isAbsentMarker } from '../api/types'
import { Empty, Fact, Failure, Loading, Panel } from '../components/Chrome'
import { AbsentValue, Label, Rate, humanise } from '../components/Figure'

export function Experiments() {
  const query = useExperiments()
  if (query.isPending) return <Loading what="experiments" />
  if (query.isError) return <Failure error={query.error} what="experiments" />
  if (query.data.experiments.length === 0) {
    return (
      <Panel title="Experiments">
        <Empty>
          No experiments. Without one, incremental recovered revenue stays{' '}
          <code>NOT ESTABLISHED</code> — which is the honest reading, not a missing feature. A holdout
          experiment is the only thing that can distinguish revenue Revora caused from revenue it
          merely observed.
        </Empty>
      </Panel>
    )
  }
  return (
    <div className="detail">
      {query.data.experiments.map((experiment) => (
        <ExperimentCard key={experiment.experiment_id} experiment={experiment} />
      ))}
    </div>
  )
}

function ExperimentCard({ experiment }) {
  return (
    <Panel
      title={experiment.name}
      subtitle={`${humanise(experiment.state)} · ${humanise(
        experiment.primary_metric,
      )} · ${humanise(experiment.analysis_method)}`}
      aside={
        <div className="labels">
          {experiment.labels.map((label) => (
            <Label key={label} text={label} />
          ))}
        </div>
      }
    >
      <h3 className="subhead subhead--small">Design, fixed before it ran</h3>
      <dl className="facts facts--tight">
        <Fact label="Allocation">{experiment.allocation_ratio}</Fact>
        <Fact label="Assumed baseline">{experiment.assumed_baseline_rate}</Fact>
        <Fact label="Minimum detectable effect">{experiment.minimum_detectable_effect}</Fact>
        <Fact label="Significance">{experiment.significance_level}</Fact>
        <Fact label="Power">{experiment.power}</Fact>
        {/* Stated because it is what "adequately powered" means. An experiment read before it reaches
            this count cannot support a claim however good the numbers look. */}
        <Fact label="Required per group">{experiment.required_sample_size_per_group}</Fact>
      </dl>

      <h3 className="subhead">Result</h3>
      {isAbsentMarker(experiment.result) ? (
        <AbsentValue marker={experiment.result} />
      ) : (
        <>
          <div className="table-scroll">
            <table className="grid grid--dense">
              <caption className="sr-only">Per-arm results</caption>
              <thead>
                <tr>
                  <th scope="col">Arm</th>
                  <th scope="col" className="num">
                    Cases
                  </th>
                  <th scope="col" className="num">
                    Recoveries
                  </th>
                  <th scope="col" className="num">
                    Rate
                  </th>
                </tr>
              </thead>
              <tbody>
                <tr>
                  <td>Control — no Revora action</td>
                  <td className="num">{experiment.result.control.case_count}</td>
                  <td className="num">{experiment.result.control.recoveries}</td>
                  <td className="num">
                    <Rate value={experiment.result.control.rate} />
                  </td>
                </tr>
                <tr>
                  <td>Treatment — Revora acted</td>
                  <td className="num">{experiment.result.treatment.case_count}</td>
                  <td className="num">{experiment.result.treatment.recoveries}</td>
                  <td className="num">
                    <Rate value={experiment.result.treatment.rate} />
                  </td>
                </tr>
              </tbody>
            </table>
          </div>

          <dl className="facts">
            <Fact label="Lift">{experiment.result.lift ?? 'not reported'}</Fact>
            <Fact label="Interval">
              {experiment.result.lift_ci_low === null || experiment.result.lift_ci_high === null
                ? 'no interval — a lift without one supports nothing'
                : `[${experiment.result.lift_ci_low}, ${experiment.result.lift_ci_high}]`}
            </Fact>
            <Fact label="Supports a causal claim">
              {experiment.result.interval_contains_zero === null ? (
                <span className="muted">no interval to judge</span>
              ) : experiment.result.interval_contains_zero ? (
                <span className="flag flag--stop">
                  No — the interval contains zero, so the observed difference is consistent with
                  Revora having had no effect at all.
                </span>
              ) : (
                <span className="flag flag--ok">
                  Yes — the interval sits entirely above zero for this metric.
                </span>
              )}
            </Fact>
            <Fact label="Contaminated">
              {experiment.result.contaminated_count}
              <span className="fact__note">
                cases whose arms leaked; an experiment with many of these is not evidence
              </span>
            </Fact>
            <Fact label="Excluded">{experiment.result.excluded_count}</Fact>
            <Fact label="Computed">{experiment.result.computed_at}</Fact>
          </dl>

          <div className="labels">
            {experiment.result.labels.map((label) => (
              <Label key={label} text={label} />
            ))}
          </div>
        </>
      )}
    </Panel>
  )
}
