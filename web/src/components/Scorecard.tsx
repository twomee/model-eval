import { useState } from 'react'
import type { ScoreRow } from '../types/api'
import { margin, percent, type SortKey, sortRows } from '../utils/scorecard'

interface Props {
  rows: ScoreRow[]
  categories: string[]
  weights: Record<string, number>
  usableThreshold: number
}

function Bar({ value, extra }: { value: number | null; extra?: string }) {
  if (value === null) return <span className="muted">–</span>
  return (
    <span className="bar">
      <span className="bar-fill" style={{ width: `${Math.round(value * 100)}%`, background: `hsl(${Math.round(120 * value)} 55% 50% / 0.35)` }} />
      <span className="bar-text"><strong>{percent(value)}</strong>{extra && <small className="muted"> {extra}</small>}</span>
    </span>
  )
}

const COLUMNS: { key: SortKey; label: string; hint: string }[] = [
  { key: 'fit', label: 'Fit for you', hint: 'weighted by your category weights' },
  { key: 'general', label: 'General quality', hint: 'every category equal' },
  { key: 'reliability', label: 'Reliability', hint: 'usable answers / all attempts' },
]

export function Scorecard({ rows, categories, weights, usableThreshold }: Props) {
  const [sortKey, setSortKey] = useState<SortKey>('fit')
  const sorted = sortRows(rows, sortKey)
  const singleBest = rows.filter((r) => r.leader_fit).length === 1

  return (
    <section aria-label="All models">
      <h2>All models</h2>
      <p className="muted">
        <strong>Fit for you</strong>: average score on every task, weighted by your priorities ·{' '}
        <strong>General quality</strong>: the same answers, every category equal ·{' '}
        <strong>Reliability</strong>: of all attempts (errors, timeouts and refusals included), how many gave a
        usable answer (score ≥ {percent(usableThreshold)}) · <strong>±</strong>: 90% margin; overlapping ranges = too
        close to call.
      </p>
      <div className="table-wrap">
        <table className="scorecard">
          <thead>
            <tr>
              <th scope="col">#</th>
              <th scope="col" aria-sort={sortKey === 'name' ? 'ascending' : 'none'}>
                <button type="button" className="sort" onClick={() => setSortKey('name')}>model</button>
              </th>
              {COLUMNS.map((c) => (
                <th key={c.key} scope="col" aria-sort={sortKey === c.key ? 'descending' : 'none'}>
                  <button type="button" className="sort" onClick={() => setSortKey(c.key)} title={c.hint}>
                    {c.label}{sortKey === c.key ? ' ▾' : ''}
                  </button>
                </th>
              ))}
              {categories.map((c) => (
                <th key={c} scope="col" aria-label={`${c}, weight ${weights[c] ?? 1}`}>
                  <small>{c}<br />×{weights[c] ?? 1}</small>
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {sorted.map((r, i) => (
              <tr key={r.name} className={r.leader_fit ? 'leader' : undefined}>
                <td>{i + 1}</td>
                <th scope="row">
                  {r.name}
                  {r.leader_fit && (
                    <span className="badge badge-ok leader-badge">{singleBest ? 'best for you' : 'top group'}</span>
                  )}
                </th>
                <td><Bar value={r.fit} extra={margin(r.fit_margin)} /></td>
                <td><Bar value={r.general} extra={margin(r.general_margin)} /></td>
                <td>
                  <Bar value={r.reliability} />
                  {r.attempts > 0 && (
                    <div><small className="muted">{r.usable}/{r.attempts} usable{r.errors > 0 ? `, ${r.errors} errors` : ''}</small></div>
                  )}
                </td>
                {categories.map((c) => <td key={c}>{percent(r.categories[c] ?? null)}</td>)}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  )
}
