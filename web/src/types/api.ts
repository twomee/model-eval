// Mirrors evalkit/api/schemas.py. Hand-written for now; once there are more endpoints,
// generate it from /api/openapi.json (openapi-typescript) so the two can't drift.

export type Role = 'model' | 'judge'

export interface ModelInfo {
  name: string
  role: Role
  provider: string
  vendor: string
  model_id: string | null
  effort: string | null
}

export interface ModelsResponse {
  config_path: string
  models: ModelInfo[]
}

export interface Check {
  name: string
  role: Role
  ok: boolean
  detail: string
}

export interface DoctorResponse {
  checks: Check[]
  all_ok: boolean
  judge_vendors: string[]
  warnings: string[]
}
