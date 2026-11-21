import type {
  ComparisonResponse, DoctorResponse, EstimateRequest, EstimateResponse, Job, ModelsResponse, PlanOptions, RunInfo,
} from '../types/api'

/** An HTTP error with the server's `detail` message, so the UI can show *why*. */
export class ApiError extends Error {
  readonly status: number

  constructor(status: number, message: string) {
    super(message)
    this.name = 'ApiError'
    this.status = status
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response
  try {
    res = await fetch(path, init)
  } catch (e) {
    // A cancelled request (newer one superseded it) is not a server outage: let callers ignore it.
    if (e instanceof DOMException && e.name === 'AbortError') throw e
    throw new ApiError(0, 'Cannot reach the evalkit server. Is `python -m evalkit serve` running?')
  }
  if (!res.ok) {
    let detail = res.statusText || `HTTP ${res.status}`
    try {
      const body: unknown = await res.json()
      if (body && typeof body === 'object' && 'detail' in body) {
        const d = (body as { detail: unknown }).detail
        detail = typeof d === 'string' ? d : JSON.stringify(d)
      }
    } catch {
      // the body wasn't JSON; keep the status text
    }
    throw new ApiError(res.status, detail)
  }
  return (await res.json()) as T
}

export const api = {
  listModels: () => request<ModelsResponse>('/api/models'),

  // The server rejects non-JSON POSTs (CSRF protection), so the header is required, not decorative.
  runDoctor: (names: string[] = []) =>
    request<DoctorResponse>('/api/doctor', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ names }),
    }),

  planOptions: () => request<PlanOptions>('/api/plan/options'),

  // Pure computation on the server: calls no model, spends no quota.
  estimate: (body: EstimateRequest, signal?: AbortSignal) =>
    request<EstimateResponse>('/api/plan/estimate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
      signal,
    }),

  listRuns: () => request<RunInfo[]>('/api/runs'),

  comparison: (run: string) => request<ComparisonResponse>(`/api/runs/${encodeURIComponent(run)}/comparison`),

  // Starts a REAL run: spends subscription quota. The server runs it in the background.
  startRun: (body: EstimateRequest) =>
    request<Job>('/api/jobs', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }),

  currentJob: () => request<{ job: Job | null }>('/api/jobs/current').then((r) => r.job),

  // JSON even without a body: the server requires it on every write (CSRF protection).
  cancelRun: () =>
    request<Job>('/api/jobs/current/cancel', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: '{}',
    }),
}

export type Api = typeof api
