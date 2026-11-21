import { useMemo, useState } from 'react'
import { CommandBox } from '../components/CommandBox'
import { CostSummary } from '../components/CostSummary'
import { RunPanel } from '../components/RunPanel'
import { EffortPicker } from '../components/EffortPicker'
import { useEstimate } from '../hooks/useEstimate'
import { useJob } from '../hooks/useJob'
import { useResource } from '../hooks/useResource'
import type { Api } from '../services/api'
import type { EstimateRequest, PlanOptions } from '../types/api'
import { defaultSelection, everything, orderedNames, setFamily, toggle } from '../utils/selection'

// Same rule as the server (schemas.RUN_NAME_PATTERN): the name becomes results/<run>/.
const RUN_NAME = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$/

interface Props {
  client: Api
}

export function PlanPage({ client }: Props) {
  const options = useResource(client.planOptions)
  if (options.loading && !options.data) return <p role="status">Loading options…</p>
  if (!options.data) {
    return (
      <div className="alert alert-error" role="alert">
        <p>Could not load run options: {options.error}</p>
        <button type="button" onClick={options.reload}>Retry</button>
      </div>
    )
  }
  // Mounted only once options exist, so the form's state can be initialised from them directly.
  return <PlanForm client={client} options={options.data} />
}

function PlanForm({ client, options }: { client: Api; options: PlanOptions }) {
  const [selection, setSelection] = useState(() => defaultSelection(options.families))
  const [categories, setCategories] = useState(() => new Set(options.categories.map((c) => c.name)))
  const [repetitions, setRepetitions] = useState(options.repetitions)
  const [runName, setRunName] = useState('v1')

  const runNameValid = RUN_NAME.test(runName)
  const repsValid = Number.isInteger(repetitions) && repetitions >= 1 && repetitions <= 20
  const request = useMemo<EstimateRequest | null>(() => {
    if (selection.size === 0 || categories.size === 0 || !runNameValid || !repsValid) return null
    return { models: orderedNames(options.families, selection), categories: [...categories].sort(),
      repetitions, run_name: runName }
  }, [selection, categories, repetitions, runName, runNameValid, repsValid, options.families])
  const estimate = useEstimate(client, request)
  const job = useJob(client)

  return (
    <>
      <div className="toolbar">
        <button type="button" onClick={() => setSelection(defaultSelection(options.families))}>Defaults</button>
        <button type="button" onClick={() => setSelection(everything(options.families))}>All efforts, all models</button>
        <button type="button" onClick={() => setSelection(new Set())}>Clear</button>
        <span className="muted">{selection.size} competitor{selection.size === 1 ? '' : 's'} selected</span>
      </div>

      <section>
        <h2>Models and efforts</h2>
        <p className="muted">Pick an effort for each model you want to run; models left on “—” don’t run.</p>
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th scope="col">model</th>
                <th scope="col">model id</th>
                <th scope="col">subscription</th>
                <th scope="col">efforts</th>
              </tr>
            </thead>
            <tbody>
              {options.families.map((f) => (
                <tr key={f.family}>
                  <th scope="row">{f.family}</th>
                  <td className="muted">{f.model_id ?? (f.provider === 'manual' ? 'chat app' : '–')}</td>
                  <td>{f.subscription}</td>
                  <td>
                    <EffortPicker family={f} selection={selection}
                      onToggle={(n) => setSelection((s) => toggle(s, n))}
                      onSetFamily={(on) => setSelection((s) => setFamily(s, f, on))} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>

      <section className="plan-options">
        <fieldset>
          <legend>Task categories</legend>
          {options.categories.map((c) => (
            <label key={c.name} className="check">
              <input type="checkbox" checked={categories.has(c.name)}
                onChange={() => setCategories((s) => toggle(s, c.name))} />
              {c.name} <span className="muted">({c.n_tasks})</span>
            </label>
          ))}
        </fieldset>
        <label className="field">
          Repetitions
          <input type="number" min={1} max={20} value={repetitions}
            onChange={(e) => setRepetitions(e.target.valueAsNumber)} aria-invalid={!repsValid} />
          <span className="muted">1 = cheap first pass; 3 = measures consistency</span>
        </label>
        <label className="field">
          Run name
          <input value={runName} onChange={(e) => setRunName(e.target.value)} aria-invalid={!runNameValid} />
          <span className={runNameValid ? 'muted' : 'error-text'}>
            {runNameValid ? `results/${runName}/` : 'letters, digits, . _ - only'}
          </span>
        </label>
      </section>

      {selection.size === 0 && <p className="muted">Select at least one model.</p>}
      {categories.size === 0 && <p className="muted">Select at least one category.</p>}
      {estimate.error && <div className="alert alert-error" role="alert">{estimate.error}</div>}
      {request && estimate.data && (
        <div aria-busy={estimate.loading}>
          <CostSummary estimate={estimate.data} />
        </div>
      )}
      {/* Always rendered: a run in progress stays visible (and cancellable) whatever is selected now. */}
      <RunPanel estimate={request && estimate.fresh ? estimate.data : null} job={job.job} starting={job.starting}
        error={job.error} onStart={() => request && void job.start(request)} onCancel={() => void job.cancel()}
        families={options.families}
        onUseRunSelection={(models) => {
          setSelection(new Set(models))
          if (job.job) {
            setRunName(job.job.run_name)
            setRepetitions(job.job.repetitions)
          }
        }} />
      {request && estimate.data && (
        <details className="cli-alt">
          <summary>Or run it from a terminal</summary>
          <CommandBox command={estimate.data.command} />
        </details>
      )}
    </>
  )
}
