"""API contract (request/response shapes).

Kept separate from the core dataclasses on purpose: ModelSpec contains the CLI argv and
env settings, which are implementation details the browser doesn't need. The API
exposes a deliberate, stable subset, so the core can change without breaking the UI.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Role = Literal["model", "judge"]


class ModelOut(BaseModel):
    name: str
    role: Role
    provider: str
    vendor: str
    model_id: str | None = Field(description="None = the CLI chooses (unpinned)")
    effort: str | None = Field(description="Reasoning effort passed to the model; None = provider default")


class ModelsOut(BaseModel):
    config_path: str
    models: list[ModelOut]


class DoctorIn(BaseModel):
    # Empty = check everything. Names refer to models AND judges.
    names: list[str] = Field(default_factory=list, max_length=100)


class CheckOut(BaseModel):
    name: str
    role: Role
    ok: bool
    detail: str
    isolation: str | None = None


class DoctorOut(BaseModel):
    checks: list[CheckOut]
    all_ok: bool
    judge_vendors: list[str]
    warnings: list[str]


class ErrorOut(BaseModel):
    detail: str


# ---------------------------------------------------------------- run planner

RUN_NAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$"  # becomes results/<run>/: no "/", no ".."


class VariantOut(BaseModel):
    name: str
    effort: str | None
    default: bool


class FamilyOut(BaseModel):
    family: str
    subscription: str
    provider: str
    model_id: str | None = Field(description="model id of the first variant, for display")
    variants: list[VariantOut]


class CategoryOut(BaseModel):
    name: str
    n_tasks: int
    n_rubric: int


class PlanOptionsOut(BaseModel):
    families: list[FamilyOut]
    categories: list[CategoryOut]
    repetitions: int
    judges: list[ModelOut]


class EstimateIn(BaseModel):
    models: list[str] = Field(min_length=1, max_length=500)
    categories: list[str] = Field(default_factory=list, max_length=50)  # empty = all
    repetitions: int | None = Field(default=None, ge=1, le=20)
    run_name: str = Field(default="v1", pattern=RUN_NAME_PATTERN)


class SubscriptionCostOut(BaseModel):
    subscription: str
    generation_calls: int
    judge_calls: int
    total_calls: int
    est_input_tokens: int
    est_output_tokens: int
    est_cost_usd: float
    models_without_history: int


class EstimateOut(BaseModel):
    n_competitors: int
    n_tasks: int
    n_rubric_tasks: int
    repetitions: int
    total_calls: int
    subscriptions: list[SubscriptionCostOut]
    notes: list[str]
    command: str


# ---------------------------------------------------------------- results / comparison

class RunOut(BaseModel):
    name: str
    has_report: bool
    n_answers: int
    updated_at: float


class RankedOut(BaseModel):
    name: str
    score: float
    ci_low: float
    ci_high: float
    n_tasks: int
    leader: bool


class RankingOut(BaseModel):
    label: str
    weights: dict[str, float]
    verdict: str
    models: list[RankedOut]
    head_to_head_names: list[str]
    head_to_head: list[list[float | None]] = Field(description="[row][col] = P(row beats col); None on the diagonal")


class FlaggedTaskOut(BaseModel):
    task: str
    verdict: str
    min: float
    max: float


class ScoreRowOut(BaseModel):
    name: str
    fit: float = Field(description="fit for you: weighted by category_weights, 0..1")
    fit_margin: float = Field(description="half-width of the 90% interval")
    general: float = Field(description="every category weighted equally, 0..1")
    general_margin: float
    reliability: float | None = Field(description="share of all attempts with a usable answer; None = no attempts")
    usable: int
    attempts: int
    errors: int
    categories: dict[str, float | None]
    leader_fit: bool
    leader_general: bool


class ComparisonOut(BaseModel):
    run: str
    usable_threshold: float
    categories: list[str]
    scorecard: list[ScoreRowOut]
    for_you: RankingOut
    general: RankingOut
    notes: list[str]
    flagged_tasks: list[FlaggedTaskOut]
    report_url: str | None


# ---------------------------------------------------------------- run jobs (Run button)

JobStatus = Literal["running", "succeeded", "failed", "cancelled"]


class StageOut(BaseModel):
    total: int
    done: int
    failed: int
    already_done: int = Field(description="finished in an earlier run of the same name (resumed)")


class PausedOut(BaseModel):
    subscription: str
    until: float = Field(description="unix time when its calls are re-checked")
    reason: str


class JobOut(BaseModel):
    run_name: str
    paused: list[PausedOut] = Field(default_factory=list, description="subscriptions waiting for a quota reset")
    status: JobStatus
    models: list[str]
    n_tasks: int
    repetitions: int
    stage: str = Field(description="generate | grade | report | done")
    stages: dict[str, StageOut]
    recent: list[str] = Field(description="last lines of progress, newest last")
    error: str | None
    started_at: float
    finished_at: float | None
    results_url: str


class CurrentJobOut(BaseModel):
    job: JobOut | None
