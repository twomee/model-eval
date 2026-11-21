import type { Family } from '../types/api'
import { familyState, pickerLabel, type Selection } from '../utils/selection'

interface Props {
  family: Family
  selection: Selection
  onToggle: (name: string) => void
  onSetFamily: (on: boolean) => void
}

// <details>/<summary> is a native disclosure widget: keyboard (Enter/Space), focus and screen
// reader semantics come for free, with no click-outside or focus-trap code to get wrong.
export function EffortPicker({ family, selection, onToggle, onSetFamily }: Props) {
  const state = familyState(selection, family)
  const single = family.variants.length === 1

  if (single) {
    const v = family.variants[0]
    return (
      <label className="check">
        <input type="checkbox" checked={selection.has(v.name)} onChange={() => onToggle(v.name)}
          aria-label={`Include ${family.family}`} />
        {v.effort ?? 'include'}
      </label>
    )
  }

  return (
    <details className="picker">
      <summary aria-label={`${family.family} efforts: ${pickerLabel(selection, family)}`}>
        {pickerLabel(selection, family)}
      </summary>
      <div className="picker-menu">
        <label className="check picker-all">
          <input type="checkbox" checked={state === 'all'}
            ref={(el) => { if (el) el.indeterminate = state === 'some' }}
            onChange={() => onSetFamily(state !== 'all')} />
          All
        </label>
        {family.variants.map((v) => (
          <label key={v.name} className="check">
            <input type="checkbox" checked={selection.has(v.name)} onChange={() => onToggle(v.name)} />
            {v.effort}
            {v.default && <span className="muted"> (default)</span>}
          </label>
        ))}
      </div>
    </details>
  )
}
