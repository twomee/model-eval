import { useCallback, useEffect, useState } from 'react'

export interface ResourceState<T> {
  data: T | null
  loading: boolean
  error: string | null
  reload: () => void
}

/** Load something once (and on reload()), with loading/error state and stale-response protection.
 *  `load` must be stable (a module-level function or a useCallback), or this refetches every render. */
export function useResource<T>(load: () => Promise<T>): ResourceState<T> {
  const [data, setData] = useState<T | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [version, setVersion] = useState(0)

  useEffect(() => {
    let cancelled = false // ignore a slow response that arrives after a newer reload
    load()
      .then((res) => {
        if (!cancelled) {
          setData(res)
          setError(null)
        }
      })
      .catch((e: unknown) => {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e))
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [load, version])

  // Loading is set by the event that causes the fetch, not inside the effect (avoids an extra render).
  const reload = useCallback(() => {
    setLoading(true)
    setVersion((v) => v + 1)
  }, [])
  return { data, loading, error, reload }
}
