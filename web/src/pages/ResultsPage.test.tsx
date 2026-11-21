import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import type { Api } from '../services/api'
import type { ComparisonResponse, Ranking, RunInfo, ScoreRow } from '../types/api'
import { strength } from '../utils/strength'
import { ResultsPage } from './ResultsPage'

const RUNS: RunInfo[] = [
  { name: 'v2', has_report: true, n_answers: 120, updated_at: 2 },
  { name: 'v1', has_report: false, n_answers: 40, updated_at: 1 },
]

function row(name: string, fit: number, general: number, reliability: number | null, extra: Partial<ScoreRow> = {}): ScoreRow {
  return {
    name, fit, fit_margin: 0.04, general, general_margin: 0.05, reliability,
    usable: reliability === null ? 0 : Math.round(reliability * 26), attempts: reliability === null ? 0 : 26, errors: 0,
    categories: { backend_code: fit, hebrew_text: general }, leader_fit: false, leader_general: false, ...extra,
  }
}

function ranking(label: string, names: string[], verdict: string): Ranking {
  return {
    label, weights: label === 'for you' ? { backend_code: 2, hebrew_text: 1 } : {}, verdict,
    models: names.map((name, i) => ({ name, score: 0.9 - i * 0.1, ci_low: 0, ci_high: 1, n_tasks: 26, leader: i === 0 })),
    head_to_head_names: names,
    head_to_head: names.map((_a, i) => names.map((_b, j) => (i === j ? null : i < j ? 0.95 : 0.05))),
  }
}

function comparisonFor(run: string): ComparisonResponse {
  const names = ['claude-opus-medium', 'gpt-sol-medium', 'glm-flash-high']
  return {
    run,
    usable_threshold: 0.8,
    categories: ['backend_code', 'hebrew_text'],
    scorecard: [
      row('claude-opus-medium', 0.91, 0.84, 0.92, { leader_fit: true }),
      row('gpt-sol-medium', 0.86, 0.88, 0.96, { leader_general: true }),   // better in general, more reliable
      row('glm-flash-high', 0.62, 0.6, 0.58, { errors: 4, usable: 15 }),
    ],
    for_you: ranking('for you', names, `Clear winner (${run}): claude-opus-medium.`),
    general: ranking('in general', ['gpt-sol-medium', ...names.filter((n) => n !== 'gpt-sol-medium')],
      'Likely winner: gpt-sol-medium.'),
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

const bodyRows = (table: HTMLElement) => within(table).getAllByRole('row').slice(1)
const names = (table: HTMLElement) => bodyRows(table).map((r) => within(r).getByRole('rowheader').textContent ?? '')

describe('ResultsPage scorecard', () => {
  it('lists EVERY model with fit, general and reliability percentages', async () => {
    render(<ResultsPage client={fakeClient()} />)
    const card = await screen.findByRole('region', { name: 'All models' })
    const table = within(card).getByRole('table')
    expect(bodyRows(table)).toHaveLength(3)
    const opus = bodyRows(table)[0]
    expect(opus).toHaveTextContent('claude-opus-medium')
    expect(opus).toHaveTextContent('91%')       // fit for you
    expect(opus).toHaveTextContent('84%')       // general quality
    expect(opus).toHaveTextContent('92%')       // reliability
    expect(opus).toHaveTextContent('±4')        // margin
    expect(within(opus).getByText('best for you')).toBeInTheDocument()
    expect(bodyRows(table)[2]).toHaveTextContent('15/26 usable, 4 errors')
  })

  it('sorts by any column', async () => {
    render(<ResultsPage client={fakeClient()} />)
    const table = within(await screen.findByRole('region', { name: 'All models' })).getByRole('table')
    expect(names(table)[0]).toMatch(/^claude-opus-medium/)                       // default: fit for you
    await userEvent.click(within(table).getByRole('button', { name: /General quality/ }))
    expect(names(table)[0]).toMatch(/^gpt-sol-medium/)
    await userEvent.click(within(table).getByRole('button', { name: /Reliability/ }))
    expect(names(table).map((n) => n.split(/best|top/)[0])).toEqual(['gpt-sol-medium', 'claude-opus-medium', 'glm-flash-high'])
    expect(within(table).getByRole('columnheader', { name: /Reliability/ })).toHaveAttribute('aria-sort', 'descending')
  })

  it('shows both verdicts and per-category columns with their weights', async () => {
    render(<ResultsPage client={fakeClient()} />)
    expect(await screen.findByText(/Clear winner \(v2\)/)).toBeInTheDocument()
    expect(screen.getByText(/Likely winner: gpt-sol-medium/)).toBeInTheDocument()
    expect(screen.getByRole('columnheader', { name: 'backend_code, weight 2' })).toBeInTheDocument()
  })

  it('head-to-head is available but secondary', async () => {
    render(<ResultsPage client={fakeClient()} />)
    await screen.findByRole('region', { name: 'All models' })
    const summary = screen.getByText(/Head-to-head: how often/)
    expect(summary.closest('details')).not.toHaveAttribute('open')
    await userEvent.click(summary)
    expect(screen.getByLabelText('claude-opus-medium beats glm-flash-high in 95%')).toBeInTheDocument()
  })

  it("switching runs loads that run, never showing the other run's numbers", async () => {
    const client = fakeClient()
    render(<ResultsPage client={client} />)
    await screen.findByText(/Clear winner \(v2\)/)
    expect(screen.getByRole('link', { name: /Open full report/ })).toHaveAttribute('href', '/reports/v2/report.html')
    await userEvent.selectOptions(screen.getByRole('combobox', { name: /Run/ }), 'v1')
    await waitFor(() => expect(screen.getByText(/Clear winner \(v1\)/)).toBeInTheDocument())
    expect(screen.queryByText(/Clear winner \(v2\)/)).not.toBeInTheDocument()
    expect(screen.getByText(/rankings are fragile/)).toBeInTheDocument()
    expect(screen.queryByRole('link', { name: /Open full report/ })).not.toBeInTheDocument()
  })

  it('lists tasks that do not separate the models', async () => {
    render(<ResultsPage client={fakeClient()} />)
    expect(await screen.findByRole('region', { name: 'Task signal' })).toHaveTextContent('gen_capacity_estimate: too easy')
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
