import { afterEach, describe, expect, it, vi } from 'vitest'
import { ApiError, api } from './api'

function mockFetch(impl: () => Promise<Response>) {
  const fn = vi.fn(impl)
  vi.stubGlobal('fetch', fn)
  return fn
}

afterEach(() => vi.unstubAllGlobals())

describe('api', () => {
  it('sends doctor as JSON POST (server rejects anything else)', async () => {
    const fetchFn = mockFetch(async () => Response.json({ checks: [], all_ok: true, judge_vendors: [], warnings: [] }))
    await api.runDoctor(['a'])
    const [url, init] = fetchFn.mock.calls[0] as unknown as [string, RequestInit]
    expect(url).toBe('/api/doctor')
    expect(init.method).toBe('POST')
    expect(new Headers(init.headers).get('content-type')).toBe('application/json')
    expect(JSON.parse(init.body as string)).toEqual({ names: ['a'] })
  })

  it('surfaces the server detail message on HTTP errors', async () => {
    mockFetch(async () => Response.json({ detail: "unknown model/judge names: ['x']" }, { status: 422 }))
    await expect(api.runDoctor(['x'])).rejects.toMatchObject({ status: 422, message: expect.stringContaining("'x'") })
  })

  it('falls back to status text when the error body is not JSON', async () => {
    mockFetch(async () => new Response('<html>boom</html>', { status: 502, statusText: 'Bad Gateway' }))
    await expect(api.listModels()).rejects.toMatchObject({ status: 502, message: 'Bad Gateway' })
  })

  it('explains a network failure instead of a raw TypeError', async () => {
    mockFetch(async () => {
      throw new TypeError('Failed to fetch')
    })
    const err = await api.listModels().catch((e: unknown) => e)
    expect(err).toBeInstanceOf(ApiError)
    expect((err as ApiError).message).toMatch(/evalkit serve/)
  })
})
