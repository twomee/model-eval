import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import type { Api } from '../services/api'
import type { DoctorResponse, ModelsResponse } from '../types/api'
import { ModelsPage } from './ModelsPage'

const MODELS: ModelsResponse = {
  config_path: 'config.yaml',
  models: [
    { name: 'claude-opus', role: 'model', provider: 'cli', vendor: 'claude', model_id: 'claude-opus-5-5' },
    { name: 'gpt-sol', role: 'model', provider: 'cli', vendor: 'openai', model_id: null },
    { name: 'judge-claude', role: 'judge', provider: 'cli', vendor: 'claude', model_id: 'claude-opus-5-5' },
  ],
}

function fakeClient(overrides: Partial<Api> = {}): Api {
  return {
    listModels: vi.fn(async () => MODELS),
    runDoctor: vi.fn(async (names: string[] = []): Promise<DoctorResponse> => {
      const picked = names.length ? MODELS.models.filter((m) => names.includes(m.name)) : MODELS.models
      return {
        checks: picked.map((m) => ({ name: m.name, role: m.role, ok: m.name !== 'gpt-sol',
          detail: m.name === 'gpt-sol' ? 'exit 1: model not supported' : '3.1s' })),
        all_ok: false,
        judge_vendors: ['claude'],
        warnings: names.length ? [] : ['Working judges cover 1 vendor(s).'],
      }
    }),
    ...overrides,
  }
}

// Find a row by its exact row header: other cells may contain the same text (a judge's model id).
function row(name: string): HTMLElement {
  const tr = screen.getByRole('rowheader', { name }).closest('tr')
  if (!tr) throw new Error(`no row for ${name}`)
  return tr
}

describe('ModelsPage', () => {
  it('lists competitors and judges separately and marks unpinned models', async () => {
    render(<ModelsPage client={fakeClient()} />)
    const competitors = await screen.findByRole('heading', { name: 'Competitors' })
    expect(competitors).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Judges' })).toBeInTheDocument()
    expect(within(row('gpt-sol')).getByText('unpinned')).toBeInTheDocument()
    expect(within(row('claude-opus')).getByText('not checked')).toBeInTheDocument()
  })

  it('check all: shows per-row status, a failure summary, and the vendor warning', async () => {
    const client = fakeClient()
    render(<ModelsPage client={client} />)
    await userEvent.click(await screen.findByRole('button', { name: 'Check all' }))

    expect(client.runDoctor).toHaveBeenCalledWith([]) // server decides what "all" is
    await waitFor(() => expect(within(row('gpt-sol')).getByText('failed')).toBeInTheDocument())
    expect(within(row('gpt-sol')).getByText(/model not supported/)).toBeInTheDocument()
    expect(within(row('claude-opus')).getByText('OK')).toBeInTheDocument()
    expect(screen.getByText('1 of 3 failed: gpt-sol')).toBeInTheDocument()
    expect(screen.getByText(/1 vendor/)).toBeInTheDocument()
  })

  it('checking one row leaves the other rows untouched', async () => {
    const client = fakeClient()
    render(<ModelsPage client={client} />)
    await userEvent.click(await screen.findByRole('button', { name: 'Check claude-opus' }))

    expect(client.runDoctor).toHaveBeenCalledWith(['claude-opus'])
    await waitFor(() => expect(within(row('claude-opus')).getByText('OK')).toBeInTheDocument())
    expect(within(row('gpt-sol')).getByText('not checked')).toBeInTheDocument()
  })

  it('shows the row as checking while the request is in flight', async () => {
    let resolve!: (r: DoctorResponse) => void
    const client = fakeClient({ runDoctor: vi.fn(() => new Promise<DoctorResponse>((r) => { resolve = r })) })
    render(<ModelsPage client={client} />)
    const button = await screen.findByRole('button', { name: 'Check claude-opus' })
    await userEvent.click(button)

    expect(within(row('claude-opus')).getByText('checking…')).toBeInTheDocument()
    expect(button).toBeDisabled()
    resolve({ checks: [], all_ok: true, judge_vendors: [], warnings: [] })
    await waitFor(() => expect(button).toBeEnabled())
  })

  it('shows a doctor request failure instead of failing silently', async () => {
    const client = fakeClient({ runDoctor: vi.fn(async () => { throw new Error('Cannot reach the evalkit server') }) })
    render(<ModelsPage client={client} />)
    await userEvent.click(await screen.findByRole('button', { name: 'Check all' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Cannot reach the evalkit server')
  })

  it('model list failure offers a retry that recovers', async () => {
    const listModels = vi.fn<Api['listModels']>()
      .mockRejectedValueOnce(new Error('boom'))
      .mockResolvedValueOnce(MODELS)
    render(<ModelsPage client={fakeClient({ listModels })} />)
    expect(await screen.findByRole('alert')).toHaveTextContent('Could not load models: boom')
    await userEvent.click(screen.getByRole('button', { name: 'Retry' }))
    expect(await screen.findByRole('heading', { name: 'Competitors' })).toBeInTheDocument()
  })
})
