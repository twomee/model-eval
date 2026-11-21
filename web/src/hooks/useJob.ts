import { useCallback, useEffect, useState } from 'react'
import type { Api } from '../services/api'
import type { EstimateRequest, Job } from '../types/api'

export const POLL_MS = 2000

export interface JobState {
  job: Job | null
  error: string | null
  starting: boolean
  start: (body: EstimateRequest) => Promise<void>
  cancel: () => Promise<void>
}

/** The server's current run. Fetched on mount (so a closed-and-reopened tab reconnects to a run
 *  in progress) and polled every 2 s while it runs. Polling stops when the run ends. */
export function useJob(client: Pick<Api, 'startRun' | 'currentJob' | 'cancelRun'>): JobState {
  const [job, setJob] = useState<Job | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [starting, setStarting] = useState(false)
  const running = job?.status === 'running'

  useEffect(() => {
    let cancelled = false
    const poll = () =>
      client.currentJob()
        .then((j) => { if (!cancelled) setJob(j) })
        .catch((e: unknown) => { if (!cancelled) setError(e instanceof Error ? e.message : String(e)) })
    void poll()
    const timer = running ? setInterval(() => void poll(), POLL_MS) : undefined
    return () => {
      cancelled = true
      if (timer) clearInterval(timer)
    }
  }, [client, running])

  const start = useCallback(async (body: EstimateRequest) => {
    setError(null)
    setStarting(true)
    try {
      setJob(await client.startRun(body))
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setStarting(false)
    }
  }, [client])

  const cancel = useCallback(async () => {
    try {
      setJob(await client.cancelRun())
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }, [client])

  return { job, error, starting, start, cancel }
}
