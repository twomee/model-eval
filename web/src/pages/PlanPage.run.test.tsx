import { act, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { POLL_MS } from '../hooks/useJob'
import type { Api } from '../services/api'
import type { EstimateRequest, EstimateResponse, Job, PlanOptions } from '../types/api'
import { PlanPage } from './PlanPage'

const OPTIONS: PlanOptions = {
  families: [
    {
      family: 'claude-opus', subscription: 'claude', provider: 'cli', model_id: 'claude-opus-5-5',
      variants: ['low', 'medium', 'high'].map((e) => ({ name: `claude-opus-${e}`, effort: e, default: e === 'medium' })),
    },
    {
      family: 'glm', subscription: 'mistral', provider: 'cli', model_id: null,
      variants: ['low', 'high'].map((e) => ({ name: `glm-${e}`, effort: e, default: false })),   // left on "—"
    },
  ],
  categories: [{ name: 'general', n_tasks: 10, n_rubric: 0 }],
  repetitions: 1,
  judges: [],
}

function estimateFor(req: EstimateRequest): EstimateResponse {
  const calls = req.models.length * 10
  return {
    n_competitors: req.models.length, n_tasks: 10, n_rubric_tasks: 0, repetitions: 1, total_calls: calls,
    subscriptions: [{ subscription: 'claude', generation_calls: calls, judge_calls: 0, total_calls: calls,
      est_input_tokens: 0, est_output_tokens: 0, est_cost_usd: 0, models_without_history: 0 }],
    notes: [], command: `python -m evalkit run --run ${req.run_name} --models ${req.models.join(',')}`,
  }
}

function job(over: Partial<Job> = {}): Job {
  return {
    run_name: 'v1', status: 'running', paused: [], models: ['claude-opus-medium'], n_tasks: 10, repetitions: 1,
    stage: 'generate', stages: { generate: { total: 10, done: 4, failed: 1, already_done: 0 } },
    recent: [], error: null, started_at: 0, finished_at: null, results_url: '#results/v1', ...over,
  }
}

/** Behaves like the server: start / poll / cancel share ONE current run. */
function fakeClient(overrides: Partial<Api> = {}, initial: Job | null = null): Api {
  let current: Job | null = initial
  return {
    listModels: vi.fn(), runDoctor: vi.fn(), listRuns: vi.fn(), comparison: vi.fn(),
    planOptions: vi.fn(async () => OPTIONS),
    estimate: vi.fn(async (req: EstimateRequest) => estimateFor(req)),
    startRun: vi.fn(async () => (current = job())),
    currentJob: vi.fn(async () => current),
    cancelRun: vi.fn(async () => (current = job({ status: 'cancelled', finished_at: 1 }))),
    ...overrides,
  }
}

afterEach(() => vi.useRealTimers())

const runPanel = () => screen.getByRole('region', { name: 'Run' })

describe('Run button', () => {
  it('runs only the models with a chosen effort, after a confirmation', async () => {
    const client = fakeClient()
    render(<PlanPage client={client} />)
    const button = await screen.findByRole('button', { name: 'Run 1 models (10 calls)' })
    expect(screen.getByText(/models left on “—” don’t run/)).toBeInTheDocument()

    await userEvent.click(button)
    const confirm = screen.getByRole('alertdialog', { name: 'Confirm run' })
    expect(confirm).toHaveTextContent('10 calls')
    expect(client.startRun).not.toHaveBeenCalled()          // nothing spent before "Yes"

    await userEvent.click(within(confirm).getByRole('button', { name: 'Yes, start' }))
    expect(client.startRun).toHaveBeenCalledWith(
      { models: ['claude-opus-medium'], categories: ['general'], repetitions: 1, run_name: 'v1' })
    expect(await within(runPanel()).findByText(/Running “v1”/)).toBeInTheDocument()
    expect(within(runPanel()).getByText(/4\/10/)).toBeInTheDocument()
    expect(within(runPanel()).getByText(/1 failed/)).toBeInTheDocument()
  })

  it('Back cancels the confirmation without starting anything', async () => {
    const client = fakeClient()
    render(<PlanPage client={client} />)
    await userEvent.click(await screen.findByRole('button', { name: /^Run 1 models/ }))
    await userEvent.click(screen.getByRole('button', { name: 'Back' }))
    expect(client.startRun).not.toHaveBeenCalled()
    expect(screen.getByRole('button', { name: /^Run 1 models/ })).toBeInTheDocument()
  })

  it('choosing more efforts runs more models', async () => {
    const client = fakeClient()
    render(<PlanPage client={client} />)
    await screen.findByRole('button', { name: /^Run 1 models/ })
    await userEvent.click(screen.getByLabelText(/glm efforts/))
    await userEvent.click(screen.getAllByRole('checkbox', { name: 'All' }).at(-1)!)
    const button = await screen.findByRole('button', { name: 'Run 3 models (30 calls)' })
    await userEvent.click(button)
    await userEvent.click(screen.getByRole('button', { name: 'Yes, start' }))
    expect(vi.mocked(client.startRun).mock.calls[0][0].models).toEqual(['claude-opus-medium', 'glm-low', 'glm-high'])
  })

  it('nothing selected: no Run button, says why', async () => {
    render(<PlanPage client={fakeClient()} />)
    await screen.findByRole('button', { name: /^Run 1 models/ })
    await userEvent.click(screen.getByRole('button', { name: 'Clear' }))
    expect(within(runPanel()).getByText('Choose an effort for at least one model to run it.')).toBeInTheDocument()
    expect(within(runPanel()).queryByRole('button', { name: /^Run/ })).not.toBeInTheDocument()
  })

  it('reconnects to a run in progress (tab closed and reopened) and polls until it ends', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    let polls = 0
    const currentJob = vi.fn(async () => (++polls < 4 ? job() : job({ status: 'succeeded', stage: 'done', finished_at: 5 })))
    render(<PlanPage client={fakeClient({ currentJob })} />)
    expect(await screen.findByText(/Running “v1”/)).toBeInTheDocument()   // reconnected on page load

    for (let i = 0; i < 4; i++) await act(async () => { await vi.advanceTimersByTimeAsync(POLL_MS) })
    const link = await screen.findByRole('link', { name: 'View results →' })
    expect(link).toHaveAttribute('href', '#results/v1')

    const calls = currentJob.mock.calls.length
    await act(async () => { await vi.advanceTimersByTimeAsync(POLL_MS * 3) })
    expect(currentJob.mock.calls.length).toBe(calls)   // finished: polling stopped
  })

  it('cancel', async () => {
    const client = fakeClient({}, job())   // a run is already going when the page opens
    render(<PlanPage client={client} />)
    await userEvent.click(await screen.findByRole('button', { name: 'Cancel run' }))
    expect(client.cancelRun).toHaveBeenCalled()
    expect(await screen.findByText(/was cancelled. Start it again to resume/)).toBeInTheDocument()
  })

  it('a run already in progress elsewhere is reported, not hidden', async () => {
    const startRun = vi.fn(async () => { throw new Error('a run is already in progress: other') })
    render(<PlanPage client={fakeClient({ startRun })} />)
    await userEvent.click(await screen.findByRole('button', { name: /^Run 1 models/ }))
    await userEvent.click(screen.getByRole('button', { name: 'Yes, start' }))
    expect(await within(runPanel()).findByRole('alert')).toHaveTextContent('already in progress: other')
  })

  it('a failed run shows the error', async () => {
    render(<PlanPage client={fakeClient({}, job({ status: 'failed', error: 'RuntimeError: adapter bug', finished_at: 1 }))} />)
    expect(await screen.findByText(/failed: RuntimeError: adapter bug/)).toBeInTheDocument()
  })

  it('never starts with a stale estimate (selection changed mid-debounce)', async () => {
    let release!: () => void
    const estimate = vi.fn((req: EstimateRequest) => new Promise<EstimateResponse>((res) => {
      if (req.models.length > 1) release = () => res(estimateFor(req))
      else res(estimateFor(req))
    }))
    render(<PlanPage client={fakeClient({ estimate })} />)
    await screen.findByRole('button', { name: /^Run 1 models/ })
    await userEvent.click(screen.getByLabelText(/glm efforts/))
    await userEvent.click(screen.getAllByRole('checkbox', { name: 'All' }).at(-1)!)
    // The 1-model estimate is now stale: no Run button until the 3-model estimate arrives.
    await waitFor(() => expect(screen.queryByRole('button', { name: /^Run 1 models/ })).not.toBeInTheDocument())
    await waitFor(() => expect(estimate.mock.calls.some(([r]) => r.models.length === 3)).toBe(true))
    expect(screen.queryByRole('button', { name: /^Run \d+ models/ })).not.toBeInTheDocument()  // still pending
    await act(async () => { release() })
    expect(await screen.findByRole('button', { name: /^Run 3 models/ })).toBeInTheDocument()
  })
})

describe('the running run is shown with its own selection', () => {
  it("lists the run's models and efforts (the dropdowns show defaults) and can load them", async () => {
    const running = job({ run_name: 'v1', repetitions: 3, models: ['claude-opus-high', 'glm-low', 'glm-high'] })
    const client = fakeClient({}, running)
    render(<PlanPage client={client} />)
    const panel = await screen.findByRole('region', { name: 'Run' })
    await userEvent.click(within(panel).getByText(/Models in this run \(3\)/))
    expect(within(panel).getByText('claude-opus', { exact: false }).closest('li')).toHaveTextContent('claude-opus: high')
    expect(within(panel).getByText('glm', { exact: false, selector: 'strong' }).closest('li')).toHaveTextContent('glm: low, high')

    // the dropdowns start at the config defaults (claude-opus: medium), not the running selection
    expect(screen.getByLabelText(/claude-opus efforts: medium/)).toBeInTheDocument()
    await userEvent.click(within(panel).getByRole('button', { name: 'Load this selection into the dropdowns' }))
    expect(screen.getByLabelText(/claude-opus efforts: high/)).toBeInTheDocument()
    expect(screen.getByLabelText(/glm efforts: all \(2\)/)).toBeInTheDocument()
    expect(screen.getByRole('spinbutton', { name: /Repetitions/ })).toHaveValue(3)
  })
})


describe('quota pauses', () => {
  it('shows a subscription waiting for its reset, while the run keeps going', async () => {
    const until = new Date(2026, 10, 21, 21, 16).getTime() / 1000
    const running = job({ paused: [{ subscription: 'openai', until, reason: "You've hit your usage limit ... try again at 9:15 PM." }] })
    render(<PlanPage client={fakeClient({}, running)} />)
    const pause = await screen.findByText(/is out of quota/)
    expect(pause.closest('.paused')).toHaveTextContent('openai')
    expect(pause.closest('.paused')).toHaveTextContent('21:16')
    expect(pause.closest('.paused')).toHaveTextContent('Other subscriptions keep running')
    expect(screen.getByRole('button', { name: 'Cancel run' })).toBeInTheDocument()   // still a running run
  })
})
