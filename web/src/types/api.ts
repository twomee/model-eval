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

// ---------------------------------------------------------------- run planner

export interface Variant {
  name: string
  effort: string | null
  default: boolean
}

export interface Family {
  family: string
  subscription: string
  provider: string
  model_id: string | null
  variants: Variant[]
}

export interface Category {
  name: string
  n_tasks: number
  n_rubric: number
}

export interface PlanOptions {
  families: Family[]
  categories: Category[]
  repetitions: number
  judges: ModelInfo[]
}

export interface EstimateRequest {
  models: string[]
  categories: string[]
  repetitions: number
  run_name: string
}

export interface SubscriptionCost {
  subscription: string
  generation_calls: number
  judge_calls: number
  total_calls: number
  est_input_tokens: number
  est_output_tokens: number
  est_cost_usd: number
  models_without_history: number
}

export interface EstimateResponse {
  n_competitors: number
  n_tasks: number
  n_rubric_tasks: number
  repetitions: number
  total_calls: number
  subscriptions: SubscriptionCost[]
  notes: string[]
  command: string
}
