import type { Check } from '../types/api'

interface Props {
  check?: Check
  pending: boolean
}

// Text + color, never color alone: status must be readable for color-blind users and screen readers.
export function StatusBadge({ check, pending }: Props) {
  if (pending) return <span className="badge badge-pending" role="status">checking…</span>
  if (!check) return <span className="badge badge-unknown">not checked</span>
  return check.ok ? (
    <span className="badge badge-ok">OK</span>
  ) : (
    <span className="badge badge-fail">failed</span>
  )
}
