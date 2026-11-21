"""What will a run cost BEFORE you start it?

Subscriptions are rate-limited by MESSAGES per time window, not by tokens, and none of
the CLIs report how much quota is left. So the exact, decision-relevant number is
CALLS per subscription. Tokens are a secondary estimate, averaged from earlier runs
of the same competitor (results/*/generations.jsonl); no history -> None, not a guess.

Judge calls are charged to the JUDGE's subscription: grading 10 Gemini answers with a
Claude judge spends Claude quota, which is easy to forget.
"""
from __future__ import annotations

import json
import statistics as st
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config, ModelSpec, Task


@dataclass
class ModelCost:
    name: str
    family: str
    subscription: str
    calls: int
    est_input_tokens: int | None     # None = no earlier run of this exact competitor
    est_output_tokens: int | None


@dataclass
class SubscriptionCost:
    subscription: str
    generation_calls: int = 0
    judge_calls: int = 0
    est_input_tokens: int = 0
    est_output_tokens: int = 0
    models_without_history: int = 0

    @property
    def total_calls(self) -> int:
        return self.generation_calls + self.judge_calls


@dataclass
class Estimate:
    n_tasks: int
    n_rubric_tasks: int
    repetitions: int
    models: list[ModelCost]
    subscriptions: list[SubscriptionCost]
    notes: list[str] = field(default_factory=list)

    @property
    def total_calls(self) -> int:
        return sum(s.total_calls for s in self.subscriptions)


def token_history(results_dir: Path) -> dict[str, tuple[float, float, int]]:
    """competitor name -> (mean input tokens, mean output tokens, n calls) over earlier runs."""
    ins: dict[str, list[int]] = defaultdict(list)
    outs: dict[str, list[int]] = defaultdict(list)
    for f in results_dir.glob("*/generations.jsonl"):
        for line in f.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue  # a crashed run can leave a half-written last line
            if r.get("error") or r.get("input_tokens") is None or r.get("output_tokens") is None:
                continue
            ins[r["model"]].append(r["input_tokens"])
            outs[r["model"]].append(r["output_tokens"])
    return {m: (st.mean(ins[m]), st.mean(outs[m]), len(ins[m])) for m in ins}


def _reps(spec: ModelSpec, repetitions: int) -> int:
    return 1 if spec.provider == "manual" else repetitions


def estimate(cfg: Config, tasks: list[Task], models: list[ModelSpec], repetitions: int | None = None,
             results_dir: Path = Path("results")) -> Estimate:
    reps = repetitions or cfg.run.repetitions
    history = token_history(results_dir)
    n_rubric = sum(1 for t in tasks if any(g["type"] == "rubric" for g in t.graders))
    subs: dict[str, SubscriptionCost] = {}

    def sub(name: str) -> SubscriptionCost:
        return subs.setdefault(name, SubscriptionCost(name))

    costs = []
    for spec in models:
        calls = len(tasks) * _reps(spec, reps)
        h = history.get(spec.name)
        cost = ModelCost(spec.name, spec.family, spec.subscription, calls,
                         round(h[0] * calls) if h else None, round(h[1] * calls) if h else None)
        costs.append(cost)
        s = sub(spec.subscription)
        s.generation_calls += calls
        if h:
            s.est_input_tokens += cost.est_input_tokens or 0
            s.est_output_tokens += cost.est_output_tokens or 0
        elif spec.provider != "manual":
            s.models_without_history += 1
        # Every rubric-graded answer is sent to every judge.
        for judge in cfg.judges:
            sub(judge.subscription).judge_calls += n_rubric * _reps(spec, reps)

    notes = []
    if any(s.models_without_history for s in subs.values()):
        notes.append("Token estimates only cover competitors with an earlier run; calls are exact. "
                     "Higher effort uses more tokens per call than medium, so expect more.")
    if "manual" in subs:
        notes.append("manual = answers you paste yourself: no quota, but your time.")
    order = sorted((s for s in subs.values() if s.total_calls), key=lambda s: -s.total_calls)
    return Estimate(len(tasks), n_rubric, reps, costs, order, notes)
