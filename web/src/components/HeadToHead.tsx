import type { Ranking } from '../types/api'
import { strength } from '../utils/strength'

interface Props {
  ranking: Ranking
}

export function HeadToHead({ ranking }: Props) {
  const names = ranking.head_to_head_names
  if (names.length < 2) return null
  return (
    <section aria-label="Head-to-head">
      <h2>Head-to-head ({ranking.label})</h2>
      <p className="muted">
        Each cell: how often the <strong>row</strong> model scores higher than the <strong>column</strong> model
        on the same resampled tasks. ≥ 90% = clearly better · 75–90% = likely · near 50% = can't tell them apart.
      </p>
      <div className="table-wrap">
        <table className="h2h">
          <thead>
            <tr>
              <th scope="col"><span className="visually-hidden">row beats column</span></th>
              {names.map((n) => <th key={n} scope="col"><small>{n}</small></th>)}
            </tr>
          </thead>
          <tbody>
            {names.map((a, i) => (
              <tr key={a}>
                <th scope="row"><small>{a}</small></th>
                {names.map((b, j) => {
                  const p = ranking.head_to_head[i][j]
                  if (p === null) return <td key={b} className="na">–</td>
                  return (
                    <td key={b} style={{ background: `hsl(${Math.round(120 * p)} 55% 50% / 0.25)` }}
                      title={`${a} vs ${b}: ${strength(p)}`} aria-label={`${a} beats ${b} in ${Math.round(p * 100)}%`}>
                      {Math.round(p * 100)}%
                    </td>
                  )
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  )
}
