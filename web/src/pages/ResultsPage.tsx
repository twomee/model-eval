import { useCallback, useState } from 'react'
import { HeadToHead } from '../components/HeadToHead'
import { RankingCard } from '../components/RankingCard'
import { useResource } from '../hooks/useResource'
import type { Api } from '../services/api'
import type { RunInfo } from '../types/api'

interface Props {
  client: Api
}

export function ResultsPage({ client }: Props) {
  const runs = useResource(client.listRuns)
  if (runs.loading && !runs.data) return <p role="status">Loading runs…</p>
  if (!runs.data) {
    return (
      <div className="alert alert-error" role="alert">
        <p>Could not load runs: {runs.error}</p>
        <button type="button" onClick={runs.reload}>Retry</button>
      </div>
    )
  }
  if (runs.data.length === 0) {
    return <p className="muted">No runs yet. Plan one on the <a href="#plan">Plan run</a> page, run the command, then come back.</p>
  }
  // Mounted once the run list exists, so the selection can start at the newest run.
  return <ResultsView client={client} runs={runs.data} onReload={runs.reload} />
}

function ResultsView({ client, runs, onReload }: { client: Api; runs: RunInfo[]; onReload: () => void }) {
  const [run, setRun] = useState(runs[0].name)
  const load = useCallback(() => client.comparison(run), [client, run])
  const comparison = useResource(load)
  const data = comparison.data?.run === run ? comparison.data : null // never show another run's numbers

  const weights = data ? Object.entries(data.for_you.weights).map(([k, v]) => `${k} ×${v}`).join(', ') : ''

  return (
    <>
      <div className="toolbar">
        <label className="field-inline">
          Run{' '}
          <select value={run} onChange={(e) => setRun(e.target.value)}>
            {runs.map((r) => (
              <option key={r.name} value={r.name}>{r.name} ({r.n_answers} answers)</option>
            ))}
          </select>
        </label>
        <button type="button" onClick={onReload}>Refresh list</button>
        {data?.report_url && <a href={data.report_url} target="_blank" rel="noreferrer">Open full report ↗</a>}
      </div>

      {comparison.error && !data && <div className="alert alert-error" role="alert">{comparison.error}</div>}
      {!data && !comparison.error && <p role="status">Comparing models…</p>}
      {data && (
        <>
          {data.notes.map((n) => <div key={n} className="alert alert-warn">{n}</div>)}
          <div className="rankings">
            <RankingCard title="Best for you" subtitle={`Weighted by your category_weights: ${weights || 'none set'}`}
              ranking={data.for_you} />
            <RankingCard title="Best in general" subtitle="Every category weighted equally" ranking={data.general} />
          </div>
          <HeadToHead ranking={data.for_you} />
          {data.flagged_tasks.length > 0 && (
            <section aria-label="Task signal">
              <h2>Tasks that don't separate the models</h2>
              <p className="muted">These add little to the ranking: replace "too easy" ones with harder tasks,
                and check "nobody passes" ones for a broken test or reference.</p>
              <ul>
                {data.flagged_tasks.map((t) => (
                  <li key={t.task}><code>{t.task}</code>: {t.verdict} (scores {t.min.toFixed(2)}–{t.max.toFixed(2)})</li>
                ))}
              </ul>
            </section>
          )}
        </>
      )}
    </>
  )
}
