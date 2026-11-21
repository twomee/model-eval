import { describe, expect, it } from 'vitest'
import type { Family } from '../types/api'
import { defaultSelection, everything, familyState, orderedNames, pickerLabel, setFamily, toggle } from './selection'

const opus: Family = {
  family: 'claude-opus', subscription: 'claude', provider: 'cli', model_id: 'claude-opus-5-5',
  variants: ['low', 'medium', 'high'].map((e) => ({ name: `claude-opus-${e}`, effort: e, default: e === 'medium' })),
}
const oss: Family = {
  family: 'gpt-oss', subscription: 'google', provider: 'cli', model_id: 'gpt-oss-120b-medium',
  variants: [{ name: 'gpt-oss-medium', effort: 'medium', default: true }],
}
const fams = [opus, oss]

describe('selection', () => {
  it('starts from the config defaults', () => {
    expect([...defaultSelection(fams)]).toEqual(['claude-opus-medium', 'gpt-oss-medium'])
    expect(everything(fams).size).toBe(4)
  })

  it('toggles one effort and a whole family', () => {
    let s = toggle(new Set(), 'claude-opus-high')
    expect(familyState(s, opus)).toBe('some')
    s = setFamily(s, opus, true)
    expect(familyState(s, opus)).toBe('all')
    s = setFamily(s, opus, false)
    expect(familyState(s, opus)).toBe('none')
  })

  it('labels the closed dropdown', () => {
    expect(pickerLabel(new Set(), opus)).toBe('—')
    expect(pickerLabel(new Set(['claude-opus-low', 'claude-opus-high']), opus)).toBe('low, high')
    expect(pickerLabel(everything([opus]), opus)).toBe('all (3)')
    expect(pickerLabel(new Set(['gpt-oss-medium']), oss)).toBe('medium') // single variant: no "all (1)"
  })

  it('keeps config order, not click order', () => {
    const s = new Set(['gpt-oss-medium', 'claude-opus-high', 'claude-opus-low'])
    expect(orderedNames(fams, s)).toEqual(['claude-opus-low', 'claude-opus-high', 'gpt-oss-medium'])
  })

  it('never mutates its input (React state must be replaced, not edited)', () => {
    const s = new Set(['claude-opus-low'])
    toggle(s, 'claude-opus-high')
    setFamily(s, opus, true)
    expect([...s]).toEqual(['claude-opus-low'])
  })
})
