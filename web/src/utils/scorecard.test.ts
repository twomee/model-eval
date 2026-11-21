import { describe, expect, it } from 'vitest'
import type { ScoreRow } from '../types/api'
import { margin, percent, sortRows } from './scorecard'

const r = (name: string, fit: number, general: number, reliability: number | null): ScoreRow => ({
  name, fit, fit_margin: 0, general, general_margin: 0, reliability, usable: 0, attempts: 0, errors: 0,
  categories: {}, leader_fit: false, leader_general: false,
})

describe('sortRows', () => {
  const rows = [r('b', 0.7, 0.9, null), r('a', 0.9, 0.6, 0.5), r('c', 0.7, 0.8, 0.9)]

  it('highest first, ties broken by name, unknown reliability last', () => {
    expect(sortRows(rows, 'fit').map((x) => x.name)).toEqual(['a', 'b', 'c'])
    expect(sortRows(rows, 'general').map((x) => x.name)).toEqual(['b', 'c', 'a'])
    expect(sortRows(rows, 'reliability').map((x) => x.name)).toEqual(['c', 'a', 'b'])
    expect(sortRows(rows, 'name').map((x) => x.name)).toEqual(['a', 'b', 'c'])
  })

  it('does not mutate its input', () => {
    sortRows(rows, 'fit')
    expect(rows.map((x) => x.name)).toEqual(['b', 'a', 'c'])
  })
})

describe('formatting', () => {
  it('percent and margin', () => {
    expect(percent(0.876)).toBe('88%')
    expect(percent(null)).toBe('–')
    expect(margin(0.043)).toBe('±4')
  })
})
