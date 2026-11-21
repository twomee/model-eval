// Pure selection logic for the run planner. No React here: easy to unit-test, and the
// components stay "render what you're given".
import type { Family } from '../types/api'

export type Selection = ReadonlySet<string> // competitor (variant) names

export function defaultSelection(families: Family[]): Set<string> {
  return new Set(families.flatMap((f) => f.variants.filter((v) => v.default).map((v) => v.name)))
}

export function everything(families: Family[]): Set<string> {
  return new Set(families.flatMap((f) => f.variants.map((v) => v.name)))
}

export function toggle(sel: Selection, name: string): Set<string> {
  const next = new Set(sel)
  if (next.has(name)) next.delete(name)
  else next.add(name)
  return next
}

export function setFamily(sel: Selection, family: Family, on: boolean): Set<string> {
  const next = new Set(sel)
  for (const v of family.variants) {
    if (on) next.add(v.name)
    else next.delete(v.name)
  }
  return next
}

export type FamilyState = 'none' | 'some' | 'all'

export function familyState(sel: Selection, family: Family): FamilyState {
  const n = family.variants.filter((v) => sel.has(v.name)).length
  return n === 0 ? 'none' : n === family.variants.length ? 'all' : 'some'
}

/** What the closed dropdown shows: "—", "medium", "low, high", or "all (5)". */
export function pickerLabel(sel: Selection, family: Family): string {
  const picked = family.variants.filter((v) => sel.has(v.name))
  if (picked.length === 0) return '—'
  if (picked.length === family.variants.length && family.variants.length > 1) return `all (${picked.length})`
  return picked.map((v) => v.effort ?? 'on').join(', ')
}

/** Keep the order the server gave (config order), not the order the user clicked. */
export function orderedNames(families: Family[], sel: Selection): string[] {
  return families.flatMap((f) => f.variants.filter((v) => sel.has(v.name)).map((v) => v.name))
}
