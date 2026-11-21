import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import type { Api } from '../services/api'
import type { EstimateRequest, EstimateResponse, PlanOptions } from '../types/api'
import { PlanPage } from './PlanPage'

const OPTIONS: PlanOptions = {
  families: [
    {
      family: 'claude-opus', subscription: 'claude', provider: 'cli', model_id: 'claude-opus-5-5',
      variants: ['low', 'medium', 'high'].map((e) => ({ name: `claude-opus-${e}`, effort: e, default: e === 'medium' })),
    },
    {
      family: 'gpt-oss', subscription: 'google', provider: 'cli', model_id: 'gpt-oss-120b-medium',
      variants: [{ name: 'gpt-oss-medium', effort: 'medium', default: true }],
    },
  ],
  categories: [{ name: 'backend_code', n_tasks: 6, n_rubric: 2 }, { name: 'general', n_tasks: 5, n_rubric: 3 }],
  repetitions: 3,
  judges: [],
}

function estimateFor(req: EstimateRequest): EstimateResponse {
  const calls = req.models.length * 11 * req.repetitions
  return {
    n_competitors: req.models.length, n_tasks: 11, n_rubric_tasks: 5, repetitions: req.repetitions,
    total_calls: calls,
    subscriptions: [{ subscription: 'claude', generation_calls: calls, judge_calls: 0, total_calls: calls,
      est_input_tokens: 0, est_output_tokens: 0, est_cost_usd: 1.25 * req.models.length, models_without_history: 1 }],
    notes: ['Token estimates only cover competitors with an earlier run; calls are exact.'],
    command: `python -m evalkit run --run ${req.run_name} --models ${req.models.join(',')}`,
  }
}

function fakeClient(overrides: Partial<Api> = {}): Api {
  return {
    listModels: vi.fn(),
    runDoctor: vi.fn(),
    planOptions: vi.fn(async () => OPTIONS),
    estimate: vi.fn(async (req: EstimateRequest) => estimateFor(req)),
    ...overrides,
  }
}

const lastRequest = (client: Api) =>
  (client.estimate as ReturnType<typeof vi.fn>).mock.lastCall?.[0] as EstimateRequest

describe('PlanPage', () => {
  it('starts with the defaults and shows their cost and command', async () => {
    const client = fakeClient()
    render(<PlanPage client={client} />)
    expect(await screen.findByText(/66 calls/)).toBeInTheDocument() // 2 models x 11 tasks x 3 reps
    expect(lastRequest(client).models).toEqual(['claude-opus-medium', 'gpt-oss-medium'])
    expect(screen.getByText(/--models claude-opus-medium,gpt-oss-medium/)).toBeInTheDocument()
    expect(screen.getByText('$2.50')).toBeInTheDocument() // est. cost column (2 models x 1.25)
  })

  it('effort dropdown: "All" selects every effort of that model and re-estimates', async () => {
    const client = fakeClient()
    render(<PlanPage client={client} />)
    const picker = await screen.findByLabelText(/claude-opus efforts: medium/)
    await userEvent.click(picker)
    await userEvent.click(screen.getByRole('checkbox', { name: 'All' }))

    await waitFor(() => expect(lastRequest(client).models).toEqual(
      ['claude-opus-low', 'claude-opus-medium', 'claude-opus-high', 'gpt-oss-medium']))
    expect(screen.getByLabelText(/claude-opus efforts: all \(3\)/)).toBeInTheDocument()
    expect(await screen.findByText(/132 calls/)).toBeInTheDocument()
  })

  it('"All efforts, all models" and "Clear"', async () => {
    const client = fakeClient()
    render(<PlanPage client={client} />)
    await userEvent.click(await screen.findByRole('button', { name: 'All efforts, all models' }))
    await waitFor(() => expect(lastRequest(client).models).toHaveLength(4))

    await userEvent.click(screen.getByRole('button', { name: 'Clear' }))
    expect(await screen.findByText('Select at least one model.')).toBeInTheDocument()
    expect(screen.queryByText(/calls/)).not.toBeInTheDocument() // no stale estimate
  })

  it('categories and repetitions flow into the request', async () => {
    const client = fakeClient()
    render(<PlanPage client={client} />)
    const general = await screen.findByRole('checkbox', { name: /general/ })
    await userEvent.click(general)
    const reps = screen.getByRole('spinbutton', { name: /Repetitions/ })
    await userEvent.clear(reps)
    await userEvent.type(reps, '1')
    await waitFor(() => expect(lastRequest(client)).toMatchObject({ categories: ['backend_code'], repetitions: 1 }))
  })

  it('an invalid run name blocks the request and says why', async () => {
    const client = fakeClient()
    render(<PlanPage client={client} />)
    const name = await screen.findByRole('textbox', { name: /Run name/ })
    await waitFor(() => expect(client.estimate).toHaveBeenCalled())
    const before = (client.estimate as ReturnType<typeof vi.fn>).mock.calls.length
    await userEvent.clear(name)
    await userEvent.type(name, '../etc')
    expect(screen.getByText('letters, digits, . _ - only')).toBeInTheDocument()
    expect(name).toHaveAttribute('aria-invalid', 'true')
    await new Promise((r) => setTimeout(r, 300)) // past the debounce
    expect((client.estimate as ReturnType<typeof vi.fn>).mock.calls.length).toBe(before)
  })

  it('debounces: several quick changes send one request', async () => {
    const client = fakeClient()
    render(<PlanPage client={client} />)
    await screen.findByText(/66 calls/)
    const before = (client.estimate as ReturnType<typeof vi.fn>).mock.calls.length
    await userEvent.click(screen.getByLabelText(/claude-opus efforts/))
    const menu = screen.getByRole('checkbox', { name: 'All' }).closest('.picker-menu') as HTMLElement
    await userEvent.click(within(menu).getByRole('checkbox', { name: /low/ }))
    await userEvent.click(within(menu).getByRole('checkbox', { name: /high/ }))
    await waitFor(() => expect(lastRequest(client).models).toHaveLength(4))
    expect((client.estimate as ReturnType<typeof vi.fn>).mock.calls.length - before).toBe(1)
  })

  it('shows estimate errors instead of a stale number', async () => {
    const client = fakeClient({ estimate: vi.fn(async () => { throw new Error('unknown categories') }) })
    render(<PlanPage client={client} />)
    expect(await screen.findByRole('alert')).toHaveTextContent('unknown categories')
  })

  it('copy button reports success', async () => {
    const writeText = vi.fn(async () => undefined)
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true })
    render(<PlanPage client={fakeClient()} />)
    await userEvent.click(await screen.findByRole('button', { name: 'Copy' }))
    expect(writeText).toHaveBeenCalledWith(expect.stringContaining('python -m evalkit run'))
    expect(screen.getByText(/Copied/)).toBeInTheDocument()
  })
})
