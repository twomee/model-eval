import type { EstimateResponse } from '../types/api'

interface Props {
  estimate: EstimateResponse
}

const fmt = (n: number) => n.toLocaleString('en-US')

function tokens(input: number, output: number): string {
  if (!input && !output) return '–'
  const k = (n: number) => (n >= 1_000_000 ? `${(n / 1_000_000).toFixed(1)}M` : `${Math.round(n / 1000)}k`)
  return `${k(input)} in / ${k(output)} out`
}

export function CostSummary({ estimate: e }: Props) {
  return (
    <section aria-label="Estimated cost">
      <p className="summary">
        {e.n_competitors} competitor{e.n_competitors === 1 ? '' : 's'} × {e.n_tasks} tasks × {e.repetitions} reps
        {' = '}<strong>{fmt(e.total_calls)} calls</strong>
        {e.n_rubric_tasks > 0 && <span className="muted"> (incl. judging {e.n_rubric_tasks} rubric tasks)</span>}
      </p>
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th scope="col">subscription</th>
              <th scope="col">answers</th>
              <th scope="col">judging</th>
              <th scope="col">total calls</th>
              <th scope="col">est. tokens</th>
            </tr>
          </thead>
          <tbody>
            {e.subscriptions.map((s) => (
              <tr key={s.subscription}>
                <th scope="row">{s.subscription}</th>
                <td>{fmt(s.generation_calls)}</td>
                <td>{fmt(s.judge_calls)}</td>
                <td><strong>{fmt(s.total_calls)}</strong></td>
                <td>
                  {tokens(s.est_input_tokens, s.est_output_tokens)}
                  {s.models_without_history > 0 && (
                    <span className="muted"> · {s.models_without_history} without history</span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {e.notes.map((n) => <p key={n} className="muted note">{n}</p>)}
    </section>
  )
}
