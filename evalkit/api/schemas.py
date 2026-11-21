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


class DoctorOut(BaseModel):
    checks: list[CheckOut]
    all_ok: bool
    judge_vendors: list[str]
    warnings: list[str]


class ErrorOut(BaseModel):
    detail: str
