import { ModelsTable } from '../components/ModelsTable'
import { useDoctor } from '../hooks/useDoctor'
import { useModels } from '../hooks/useModels'
import type { Api } from '../services/api'

interface Props {
  client: Api // injected, so tests pass a fake instead of mocking fetch globally
}

export function ModelsPage({ client }: Props) {
  const models = useModels(client)
  const doctor = useDoctor(client)

  if (models.loading && !models.data) return <p role="status">Loading models…</p>
  if (models.error && !models.data) {
    return (
      <div className="alert alert-error" role="alert">
        <p>Could not load models: {models.error}</p>
        <button type="button" onClick={models.reload}>Retry</button>
      </div>
    )
  }

  const all = models.data?.models ?? []
  const competitors = all.filter((m) => m.role === 'model')
  const judges = all.filter((m) => m.role === 'judge')
  const anyPending = doctor.pending.size > 0
  const checked = all.filter((m) => doctor.results[m.name])
  const failed = checked.filter((m) => !doctor.results[m.name].ok)

  return (
    <>
      <div className="toolbar">
        <button type="button" className="primary" disabled={anyPending}
          onClick={() => doctor.run(all.map((m) => m.name), { all: true })}>
          {anyPending ? 'Checking…' : `Check all (${all.length} calls)`}
        </button>
        <button type="button" onClick={models.reload} disabled={models.loading}>
          Reload config
        </button>
        <span className="muted">
          {models.data?.config_path} · each check sends one short prompt (uses subscription quota)
        </span>
      </div>

      {checked.length > 0 && !anyPending && (
        <p className="summary" role="status">
          {failed.length === 0
            ? `${checked.length} checked, all OK`
            : `${failed.length} of ${checked.length} failed: ${failed.map((m) => m.name).join(', ')}`}
        </p>
      )}
      {doctor.error && <div className="alert alert-error" role="alert">{doctor.error}</div>}
      {doctor.warnings.map((w) => (
        <div key={w} className="alert alert-warn" role="alert">{w}</div>
      ))}

      <ModelsTable title="Competitors" models={competitors} results={doctor.results}
        pending={doctor.pending} onCheck={(n) => doctor.run([n])} />
      <ModelsTable title="Judges" models={judges} results={doctor.results}
        pending={doctor.pending} onCheck={(n) => doctor.run([n])} />
    </>
  )
}
