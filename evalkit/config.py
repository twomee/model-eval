"""Configuration and task loading.

Design note: config (WHO we test) and tasks (WHAT we test) are separate files.
They change at different rates and for different reasons — you'll add tasks
weekly, but swap models only when a new release lands.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class ModelSpec:
    name: str                      # your label, e.g. "claude-opus"
    provider: str                  # "anthropic" | "openai_compat" | "cli" | "manual" | "mock"
    model: str = ""                # provider's model id (API providers)
    command: tuple[str, ...] = ()  # cli provider: argv; prompt is sent on stdin
    api_key_env: str | None = None
    base_url: str | None = None
    price_in: float | None = None   # USD per 1M input tokens
    price_out: float | None = None  # USD per 1M output tokens
    params: dict[str, Any] = field(default_factory=dict)  # merged into request body
    concurrency: int | None = None  # per-model cap (subscriptions rate-limit hard)
    parse: str = "text"             # cli output: "text" | "claude_json"

    def cost(self, tokens_in: int, tokens_out: int) -> float | None:
        if self.price_in is None or self.price_out is None:
            return None
        return (tokens_in * self.price_in + tokens_out * self.price_out) / 1_000_000


@dataclass(frozen=True)
class RunSettings:
    repetitions: int = 3
    concurrency: int = 4
    temperature: float | None = 0.0
    max_tokens: int = 4096
    timeout_s: float = 180
    max_retries: int = 4
    code_timeout_s: float = 30


@dataclass(frozen=True)
class Config:
    run: RunSettings
    models: list[ModelSpec]
    judges: list[ModelSpec]
    category_weights: dict[str, float]


@dataclass(frozen=True)
class Task:
    id: str
    category: str
    prompt: str
    graders: list[dict[str, Any]]
    system: str | None = None
    reference: str | None = None   # ideal answer: shown to the judge, used by mock

    @property
    def fingerprint(self) -> str:
        """Changes whenever the task changes -> stale results get re-run, not silently reused."""
        blob = json.dumps([self.prompt, self.system, self.graders, self.reference], sort_keys=True)
        return hashlib.sha256(blob.encode()).hexdigest()[:12]


def _model_from_dict(d: dict[str, Any]) -> ModelSpec:
    price = d.get("price") or {}
    return ModelSpec(
        name=d["name"],
        provider=d["provider"],
        model=d.get("model", ""),
        command=tuple(d.get("command") or ()),
        api_key_env=d.get("api_key_env"),
        base_url=d.get("base_url"),
        price_in=price.get("input"),
        price_out=price.get("output"),
        params=d.get("params") or {},
        concurrency=d.get("concurrency"),
        parse=d.get("parse", "text"),
    )


def load_config(path: Path) -> Config:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    models = [_model_from_dict(m) for m in raw["models"]]
    judges = [_model_from_dict(j) for j in raw.get("judges", [])]
    names = [m.name for m in models]
    if len(names) != len(set(names)):
        raise ValueError(f"Duplicate model names in {path}: {names}")
    return Config(
        run=RunSettings(**(raw.get("run") or {})),
        models=models,
        judges=judges,
        category_weights=raw.get("category_weights") or {},
    )


def load_tasks(tasks_dir: Path) -> list[Task]:
    tasks: list[Task] = []
    for f in sorted(tasks_dir.glob("*.yaml")):
        docs = yaml.safe_load(f.read_text(encoding="utf-8"))
        for d in docs if isinstance(docs, list) else [docs]:
            graders = d.get("graders") or [d["grader"]]
            tasks.append(Task(
                id=d["id"], category=d["category"], prompt=d["prompt"],
                graders=graders, system=d.get("system"), reference=d.get("reference"),
            ))
    ids = [t.id for t in tasks]
    dupes = {i for i in ids if ids.count(i) > 1}
    if dupes:
        raise ValueError(f"Duplicate task ids: {dupes}")
    return tasks
