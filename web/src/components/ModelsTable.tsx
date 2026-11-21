import type { Check, ModelInfo } from '../types/api'
import { StatusBadge } from './StatusBadge'

interface Props {
  title: string
  models: ModelInfo[]
  results: Record<string, Check>
  pending: ReadonlySet<string>
  onCheck: (name: string) => void
}

export function ModelsTable({ title, models, results, pending, onCheck }: Props) {
  if (models.length === 0) return null
  return (
    <section>
      <h2>{title}</h2>
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th scope="col">name</th>
              <th scope="col">vendor</th>
              <th scope="col">model</th>
              <th scope="col">effort</th>
              <th scope="col">provider</th>
              <th scope="col">status</th>
              <th scope="col">detail</th>
              <th scope="col">
                <span className="visually-hidden">actions</span>
              </th>
            </tr>
          </thead>
          <tbody>
            {models.map((m) => {
              const check = results[m.name]
              const busy = pending.has(m.name)
              return (
                <tr key={m.name}>
                  <th scope="row">{m.name}</th>
                  <td>{m.vendor}</td>
                  <td>
                    {m.model_id ??
                      (m.provider === 'manual' ? (
                        <span className="muted" title="You choose the model in the chat app">chat app</span>
                      ) : (
                        <span className="muted" title="The CLI picks the model; results can change when it updates">
                          unpinned
                        </span>
                      ))}
                  </td>
                  <td>{m.effort ?? <span className="muted">default</span>}</td>
                  <td>{m.provider}</td>
                  <td>
                    <StatusBadge check={check} pending={busy} />
                  </td>
                  <td className="detail">{check?.detail}</td>
                  <td>
                    <button type="button" onClick={() => onCheck(m.name)} disabled={busy}
                      aria-label={`Check ${m.name}`}>
                      Check
                    </button>
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
    </section>
  )
}
