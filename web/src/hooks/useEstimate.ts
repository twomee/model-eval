import { useEffect, useState } from 'react'
import type { Api } from '../services/api'
import type { EstimateRequest, EstimateResponse } from '../types/api'

export interface EstimateState {
  data: EstimateResponse | null
  loading: boolean
  error: string | null
  /** true only when `data` was computed for the CURRENT request (not the previous one, mid-debounce).
   *  Anything that acts on the estimate (the Run button) must check this. */
  fresh: boolean
}

interface Internal {
  data: EstimateResponse | null
  loading: boolean
  error: string | null
  forKey: string | null
}

const DEBOUNCE_MS = 200

/** Re-estimate whenever the request changes. Debounced (clicking through 10 checkboxes = 1
 *  request), and the previous request is aborted so a slow old answer can't overwrite a new one. */
export function useEstimate(client: Pick<Api, 'estimate'>, request: EstimateRequest | null): EstimateState {
  const [state, setState] = useState<Internal>({ data: null, loading: false, error: null, forKey: null })
  // Serialized so a new-but-equal object (re-render) doesn't trigger a refetch.
  const key = request ? JSON.stringify(request) : null

  useEffect(() => {
    if (key === null) return
    const body = JSON.parse(key) as EstimateRequest
    const ctrl = new AbortController()
    const timer = setTimeout(() => {
      setState((s) => ({ ...s, loading: true }))
      client
        .estimate(body, ctrl.signal)
        .then((data) => setState({ data, loading: false, error: null, forKey: key }))
        .catch((e: unknown) => {
          if (ctrl.signal.aborted) return // superseded by a newer selection
          setState((s) => ({ ...s, loading: false, error: e instanceof Error ? e.message : String(e) }))
        })
    }, DEBOUNCE_MS)
    return () => {
      clearTimeout(timer)
      ctrl.abort()
    }
  }, [client, key])

  // Nothing selected: show no (stale) estimate. Derived during render, not stored in state.
  if (key === null) return { data: null, loading: false, error: null, fresh: false }
  return { data: state.data, loading: state.loading, error: state.error, fresh: state.forKey === key && !state.loading }
}
