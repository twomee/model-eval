import { useCallback, useState } from 'react'
import type { Api } from '../services/api'
import type { Check } from '../types/api'

export interface DoctorState {
  /** Latest result per model/judge name. Kept across runs, so checking one row doesn't wipe the others. */
  results: Record<string, Check>
  /** Names with a check in flight. A Set (not a boolean) because rows can be checked independently. */
  pending: ReadonlySet<string>
  warnings: string[]
  error: string | null
  /** names = the rows being checked, so they can show a spinner. */
  run: (names: string[], opts?: { all?: boolean }) => Promise<void>
}

export function useDoctor(client: Pick<Api, 'runDoctor'>): DoctorState {
  const [results, setResults] = useState<Record<string, Check>>({})
  const [pending, setPending] = useState<ReadonlySet<string>>(new Set())
  const [warnings, setWarnings] = useState<string[]>([])
  const [error, setError] = useState<string | null>(null)

  const run = useCallback(
    async (names: string[], opts: { all?: boolean } = {}) => {
      setError(null)
      setPending((p) => new Set([...p, ...names]))
      try {
        // Always explicit names: the server treats [] as "defaults only", which isn't what "Check all" means.
        const res = await client.runDoctor(names)
        setResults((prev) => {
          const next = { ...prev }
          for (const c of res.checks) next[c.name] = c
          return next
        })
        // Vendor coverage is only computed when the whole panel was checked; keep the last known value otherwise.
        if (opts.all) setWarnings(res.warnings)
      } catch (e: unknown) {
        setError(e instanceof Error ? e.message : String(e))
      } finally {
        setPending((p) => {
          const next = new Set(p)
          for (const n of names) next.delete(n)
          return next
        })
      }
    },
    [client],
  )

  return { results, pending, warnings, error, run }
}
