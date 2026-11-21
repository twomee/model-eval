import type { Ranking } from '../types/api'

interface Props {
  title: string
  subtitle: string
  ranking: Ranking
}

export function RankingCard({ title, subtitle, ranking }: Props) {
  const tie = ranking.models.filter((m) => m.leader).length > 1
  return (
    <section className="ranking" aria-label={title}>
      <h2>{title}</h2>
      <p className="muted">{subtitle}</p>
      <p className={`verdict${tie ? ' verdict-tie' : ''}`} role="status">{ranking.verdict}</p>
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th scope="col">#</th>
              <th scope="col">model</th>
              <th scope="col">score</th>
              <th scope="col">CI 90%</th>
              <th scope="col">tasks</th>
            </tr>
          </thead>
          <tbody>
            {ranking.models.map((m, i) => (
              <tr key={m.name} className={m.leader ? 'leader' : undefined}>
                <td>{i + 1}</td>
                <th scope="row">
                  {m.name}
                  {m.leader && <span className="badge badge-ok leader-badge">{tie ? 'tied lead' : 'best'}</span>}
                </th>
                <td><strong>{m.score.toFixed(3)}</strong></td>
                <td className="muted">{m.ci_low.toFixed(2)}–{m.ci_high.toFixed(2)}</td>
                <td>{m.n_tasks}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  )
}
