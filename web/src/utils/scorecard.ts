import type { ScoreRow } from '../types/api'

export type SortKey = 'fit' | 'general' | 'reliability' | 'name'

/** Highest first for scores (unknown reliability last), A-Z for names. Never mutates its input. */
export function sortRows(rows: readonly ScoreRow[], key: SortKey): ScoreRow[] {
  const copy = [...rows]
  if (key === 'name') return copy.sort((a, b) => a.name.localeCompare(b.name))
  return copy.sort((a, b) => {
    const x = a[key] ?? -1
    const y = b[key] ?? -1
    return y - x || a.name.localeCompare(b.name)
  })
}

export const percent = (x: number | null) => (x === null ? '–' : `${Math.round(x * 100)}%`)
export const margin = (m: number) => `±${Math.round(m * 100)}`
