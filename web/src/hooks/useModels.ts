import { useCallback, useEffect, useState } from 'react'
import type { Api } from '../services/api'
import type { ModelsResponse } from '../types/api'

export interface ModelsState {
  data: ModelsResponse | null
  loading: boolean
  error: string | null
  reload: () => void
}

export function useModels(client: Pick<Api, 'listModels'>): ModelsState {
  const [data, setData] = useState<ModelsResponse | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [version, setVersion] = useState(0)

  useEffect(() => {
    let cancelled = false // ignore a slow response that arrives after a newer reload
    client
      .listModels()
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
  }, [client, version])

  // Loading is set by the event that causes the fetch, not inside the effect (avoids an extra render).
  const reload = useCallback(() => {
    setLoading(true)
    setVersion((v) => v + 1)
  }, [])
  return { data, loading, error, reload }
}
