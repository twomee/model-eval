/** A head-to-head probability as words (never colour alone). Same thresholds as evalkit/compare.py. */
export function strength(p: number): string {
  if (p >= 0.9) return 'clearly better'
  if (p >= 0.75) return 'likely better'
  if (p > 0.25) return "can't tell"
  if (p > 0.1) return 'likely worse'
  return 'clearly worse'
}
