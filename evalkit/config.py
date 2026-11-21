"""Configuration and task loading.

Design note: config (WHO we test) and tasks (WHAT we test) are separate files.
They change at different rates and for different reasons — you'll add tasks
weekly, but swap models only when a new release lands.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import yaml

from .variants import effort_error, expand_variants


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
    parse: str = "text"             # cli output: "text" | "claude_json" | "codex_jsonl"
    # cli: run with a clean home dir {env, dir, link: [files to symlink in]} - see CliProvider
    isolated_home: dict[str, Any] | None = None
    timeout_s: float | None = None  # per-model override of run.timeout_s (high effort thinks longer)
    variant: dict[str, str] = field(default_factory=dict)  # e.g. {"effort": "high"} - see variants.py

    @property
    def model_id(self) -> str | None:
        """The model that actually runs: `model` for APIs, the --model/-m value for CLIs.
        None = the CLI picks (unpinned: results can change when the CLI updates)."""
        if self.model:
            return self.model
        for flag in ("--model", "-m"):
            if flag in self.command[:-1]:
                return self.command[self.command.index(flag) + 1]
        return None

    @property
    def effort(self) -> str | None:
        """Reasoning effort actually passed to the CLI, read from the final command so it is
        right whether it came from a variant or was written into the command directly."""
        cmd = self.command
        for i, arg in enumerate(cmd[:-1]):
            if arg == "--effort":
                return cmd[i + 1]
            if arg == "-c" and (m := re.fullmatch(r'model_reasoning_effort\s*=\s*"?([\w-]+)"?', cmd[i + 1])):
                return m.group(1)
        return self.variant.get("effort")

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
    # Same task in two languages shares a `pair` id; the report then shows how much
    # each model loses when you work in Hebrew instead of English.
    pair: str | None = None
    lang: str = "en"

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
        isolated_home=d.get("isolated_home"),
        timeout_s=d.get("timeout_s"),
        variant=d.get("variant") or {},
    )


def _codex_cache(spec: ModelSpec) -> Path:
    home = (spec.isolated_home or {}).get("dir", "~/.codex")
    return Path(home).expanduser() / "models_cache.json"


def _load_models(entries: list[dict[str, Any]]) -> list[ModelSpec]:
    specs = [_model_from_dict(e) for entry in entries for e in expand_variants(entry)]
    errors = [err for s in specs if (err := effort_error(s.name, s.command, s.model_id, s.effort, _codex_cache(s)))]
    if errors:
        raise ValueError("invalid effort settings:\n  " + "\n  ".join(errors))
    return specs


def load_config(path: Path) -> Config:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    models = _load_models(raw["models"])
    judges = _load_models(raw.get("judges", []))
    names = [m.name for m in models]
    if dupes := sorted({n for n in names if names.count(n) > 1}):
        raise ValueError(f"Duplicate model names in {path}: {dupes}")
    return Config(
        run=RunSettings(**(raw.get("run") or {})),
        models=models,
        judges=judges,
        category_weights=raw.get("category_weights") or {},
    )


def load_tasks(tasks_dir: Path) -> list[Task]:
    """Load tasks. A doc with `twin_of: <id>` is a translation of another task: it INHERITS
    the original's category and graders (single source of truth - fix a test once, both
    sides get it) and may add `extra_graders` (e.g. a language constraint)."""
    raw: list[dict[str, Any]] = []
    for f in sorted(tasks_dir.glob("*.yaml")):
        docs = yaml.safe_load(f.read_text(encoding="utf-8"))
        raw.extend(docs if isinstance(docs, list) else [docs])

    ids = [d["id"] for d in raw]
    if dupes := {i for i in ids if ids.count(i) > 1}:
        raise ValueError(f"Duplicate task ids: {dupes}")

    by_id: dict[str, Task] = {}
    for d in (d for d in raw if "twin_of" not in d):
        by_id[d["id"]] = Task(
            id=d["id"], category=d["category"], prompt=d["prompt"],
            graders=d.get("graders") or [d["grader"]], system=d.get("system"),
            reference=d.get("reference"), lang=d.get("lang", "en"))

    for d in (d for d in raw if "twin_of" in d):
        orig = by_id.get(d["twin_of"])
        if orig is None:
            raise ValueError(f"task {d['id']}: twin_of unknown task {d['twin_of']!r}")
        if orig.pair:
            raise ValueError(f"task {orig.id} already has a twin")
        lang = d.get("lang", "he")
        if lang == orig.lang:
            raise ValueError(f"task {d['id']}: twin must be in a different language than {orig.id}")
        by_id[orig.id] = replace(orig, pair=orig.id)
        by_id[d["id"]] = Task(
            id=d["id"], category=orig.category, prompt=d["prompt"],
            graders=orig.graders + (d.get("extra_graders") or []), system=d.get("system", orig.system),
            reference=d.get("reference"), pair=orig.id, lang=lang)

    return [by_id[i] for i in ids]
