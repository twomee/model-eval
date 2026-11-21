import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import type { Api } from '../services/api'
import type { ComparisonResponse, Ranking, RunInfo } from '../types/api'
import { strength } from '../utils/strength'
import { ResultsPage } from './ResultsPage'

const RUNS: RunInfo[] = [
  { name: 'v2', has_report: true, n_answers: 120, updated_at: 2 },
  { name: 'v1', has_report: false, n_answers: 40, updated_at: 1 },
]

function ranking(label: string, order: string[], leaders: string[], verdict: string): Ranking {
  return {
    label, weights: label === 'for you' ? { backend_code: 2, general: 1 } : {}, verdict,
    models: order.map((name, i) => ({ name, score: 0.9 - i * 0.1, ci_low: 0.8 - i * 0.1, ci_high: 0.95 - i * 0.1,
      n_tasks: 26, leader: leaders.includes(name) })),
    head_to_head_names: order,
    head_to_head: order.map((_a, i) => order.map((_b, j) => (i === j ? null : i < j ? 0.95 : 0.05))),
  }
}

function comparisonFor(run: string): ComparisonResponse {
  return {
    run,
    // Best for you and best in general can be different models.
    for_you: ranking('for you', ['claude-opus-medium', 'gpt-sol-medium', 'glm-high'], ['claude-opus-medium'],
      `Clear winner (${run}): claude-opus-medium beats gpt-sol-medium in 95% of resamples.`),
    general: ranking('in general', ['gpt-sol-medium', 'claude-opus-medium', 'glm-high'],
      ['gpt-sol-medium', 'claude-opus-medium'], 'Statistical tie at the top: gpt-sol-medium vs claude-opus-medium.'),
    notes: run === 'v1' ? ['Fewer than 10 tasks: rankings are fragile.'] : [],
    flagged_tasks: [{ task: 'gen_capacity_estimate', verdict: 'too easy', min: 1, max: 1 }],
    report_url: run === 'v2' ? '/reports/v2/report.html' : null,
  }
}

function fakeClient(overrides: Partial<Api> = {}): Api {
  return {
    listModels: vi.fn(), runDoctor: vi.fn(), planOptions: vi.fn(), estimate: vi.fn(),
    listRuns: vi.fn(async () => RUNS),
    comparison: vi.fn(async (run: string) => comparisonFor(run)),
    ...overrides,
  }
}

describe('ResultsPage', () => {
  it('shows both rankings with their verdicts, newest run first', async () => {
    render(<ResultsPage client={fakeClient()} />)
    const forYou = await screen.findByRole('region', { name: 'Best for you' })
    const general = screen.getByRole('region', { name: 'Best in general' })
    expect(within(forYou).getByRole('status')).toHaveTextContent('Clear winner (v2)')
    expect(within(general).getByRole('status')).toHaveTextContent('Statistical tie')
    expect(within(forYou).getAllByRole('row')[1]).toHaveTextContent('claude-opus-medium')
    expect(within(general).getAllByRole('row')[1]).toHaveTextContent('gpt-sol-medium')
    expect(within(forYou).getByText(/backend_code ×2/)).toBeInTheDocument()
  })

  it('labels a single winner vs a tied lead', async () => {
    render(<ResultsPage client={fakeClient()} />)
    const forYou = await screen.findByRole('region', { name: 'Best for you' })
    expect(within(forYou).getByText('best')).toBeInTheDocument()
    expect(within(screen.getByRole('region', { name: 'Best in general' })).getAllByText('tied lead')).toHaveLength(2)
  })

  it('head-to-head matrix with readable cells', async () => {
    render(<ResultsPage client={fakeClient()} />)
    const h2h = await screen.findByRole('region', { name: 'Head-to-head' })
    expect(within(h2h).getByLabelText('claude-opus-medium beats glm-high in 95%')).toBeInTheDocument()
    expect(within(h2h).getAllByText('–')).toHaveLength(3) // diagonal
  })

  it('switching runs loads that run, never showing the other run\'s numbers', async () => {
    const client = fakeClient()
    render(<ResultsPage client={client} />)
    await screen.findByText(/Clear winner \(v2\)/)
    expect(screen.getByRole('link', { name: /Open full report/ })).toHaveAttribute('href', '/reports/v2/report.html')

    await userEvent.selectOptions(screen.getByRole('combobox', { name: /Run/ }), 'v1')
    await waitFor(() => expect(screen.getByText(/Clear winner \(v1\)/)).toBeInTheDocument())
    expect(screen.queryByText(/Clear winner \(v2\)/)).not.toBeInTheDocument()
    expect(client.comparison).toHaveBeenLastCalledWith('v1')
    expect(screen.getByText(/rankings are fragile/)).toBeInTheDocument()
    expect(screen.queryByRole('link', { name: /Open full report/ })).not.toBeInTheDocument()
  })

  it('lists tasks that do not separate the models', async () => {
    render(<ResultsPage client={fakeClient()} />)
    const signal = await screen.findByRole('region', { name: 'Task signal' })
    expect(signal).toHaveTextContent('gen_capacity_estimate: too easy')
  })

  it('no runs yet: points to the planner', async () => {
    render(<ResultsPage client={fakeClient({ listRuns: vi.fn(async () => []) })} />)
    expect(await screen.findByRole('link', { name: 'Plan run' })).toHaveAttribute('href', '#plan')
  })

  it('a comparison error is shown, not swallowed', async () => {
    render(<ResultsPage client={fakeClient({ comparison: vi.fn(async () => { throw new Error('run not found: v2') }) })} />)
    expect(await screen.findByRole('alert')).toHaveTextContent('run not found: v2')
  })
})

describe('strength', () => {
  it('uses the same thresholds as the server verdicts', () => {
    expect([0.95, 0.8, 0.5, 0.2, 0.05].map(strength)).toEqual(
      ['clearly better', 'likely better', "can't tell", 'likely worse', 'clearly worse'])
  })
})
