import { useState } from 'react'
import type { EstimateResponse, Family, Job, StageProgress } from '../types/api'
import { describeSelection } from '../utils/selection'

interface Props {
  estimate: EstimateResponse | null   // null = nothing valid selected
  job: Job | null
  starting: boolean
  error: string | null
  onStart: () => void
  onCancel: () => void
  families: Family[]
  onUseRunSelection: (models: string[]) => void
}

const STAGE_LABELS: Record<string, string> = { generate: 'Answers', grade: 'Grading', report: 'Report' }

function StageBar({ name, s }: { name: string; s: StageProgress }) {
  const pct = s.total ? Math.round((100 * s.done) / s.total) : 100
  return (
    <div className="stage">
      <div className="stage-label">
        <strong>{STAGE_LABELS[name] ?? name}</strong>
        <span className="muted">
          {' '}{s.done}/{s.total}{s.failed > 0 && <span className="error-text"> · {s.failed} failed</span>}
          {s.already_done > 0 && ` · ${s.already_done} done earlier (resumed)`}
        </span>
      </div>
      <progress max={100} value={pct} aria-label={`${STAGE_LABELS[name] ?? name} ${pct}%`} />
    </div>
  )
}

function RunSelection({ job, families, onUse }: { job: Job; families: Family[]; onUse: (m: string[]) => void }) {
  return (
    <details className="run-selection">
      <summary>Models in this run ({job.models.length}): the dropdowns above show your next selection, not this run</summary>
      <ul>
        {describeSelection(families, job.models).map((g) => (
          <li key={g.family}><strong>{g.family}</strong>: {g.efforts.join(', ')}</li>
        ))}
      </ul>
      <button type="button" onClick={() => onUse(job.models)}>Load this selection into the dropdowns</button>
    </details>
  )
}

export function RunPanel({ estimate, job, starting, error, onStart, onCancel, families, onUseRunSelection }: Props) {
  const [confirming, setConfirming] = useState(false)
  const running = job?.status === 'running'

  return (
    <section aria-label="Run" className="run-panel">
      <h2>Run</h2>

      {running && job && (
        <div role="status" aria-live="polite">
          <p>
            <strong>Running “{job.run_name}”</strong>: {job.models.length} models × {job.n_tasks} tasks ×{' '}
            {job.repetitions} reps. You can close this tab: the run continues on the server.
          </p>
          {Object.entries(job.stages).map(([name, s]) => <StageBar key={name} name={name} s={s} />)}
          <RunSelection job={job} families={families} onUse={onUseRunSelection} />
          <button type="button" onClick={onCancel}>Cancel run</button>
          <span className="muted"> Answers so far are kept; starting the same run again resumes it.</span>
        </div>
      )}

      {!running && job && (
        <div className={`alert ${job.status === 'succeeded' ? 'alert-ok' : job.status === 'failed' ? 'alert-error' : 'alert-warn'}`}
          role="status">
          {job.status === 'succeeded' && <>Run “{job.run_name}” finished. <a href={job.results_url}>View results →</a></>}
          {job.status === 'cancelled' && <>Run “{job.run_name}” was cancelled. Start it again to resume where it stopped.</>}
          {job.status === 'failed' && <>Run “{job.run_name}” failed: {job.error}</>}
          <RunSelection job={job} families={families} onUse={onUseRunSelection} />
        </div>
      )}

      {!running && (
        estimate ? (
          confirming ? (
            <div className="confirm" role="alertdialog" aria-label="Confirm run">
              <p>
                Start “{estimate.command.match(/--run (\S+)/)?.[1] ?? 'run'}”? This makes{' '}
                <strong>{estimate.total_calls.toLocaleString('en-US')} calls</strong>:{' '}
                {estimate.subscriptions.map((s) => `${s.subscription} ${s.total_calls.toLocaleString('en-US')}`).join(' · ')}.
              </p>
              <button type="button" className="primary" disabled={starting}
                onClick={() => { setConfirming(false); onStart() }}>
                Yes, start
              </button>{' '}
              <button type="button" onClick={() => setConfirming(false)}>Back</button>
            </div>
          ) : (
            <button type="button" className="primary" onClick={() => setConfirming(true)} disabled={starting}>
              {starting ? 'Starting…' : `Run ${estimate.n_competitors} models (${estimate.total_calls.toLocaleString('en-US')} calls)`}
            </button>
          )
        ) : (
          <p className="muted">Choose an effort for at least one model to run it.</p>
        )
      )}
      {error && <div className="alert alert-error" role="alert">{error}</div>}
    </section>
  )
}
