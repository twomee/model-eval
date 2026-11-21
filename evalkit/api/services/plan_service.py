"""Run planner: what can be run, what a selection costs, and the command that runs it.

The estimate math lives in the core (`evalkit.estimate`) so `run --dry-run` and the UI
can never disagree; this service only adapts it to the API and builds the command.
"""
from __future__ import annotations

import shlex
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

from ...config import ModelSpec, load_config, load_tasks
from ...estimate import estimate
from ..schemas import (CategoryOut, EstimateIn, EstimateOut, FamilyOut, PlanOptionsOut,
                       SubscriptionCostOut, VariantOut)
from .models_service import UnknownModelsError, _to_out


class UnknownCategoriesError(ValueError):
    pass


@dataclass(frozen=True)
class PlanService:
    config_path: Path
    tasks_dir: Path
    results_dir: Path = Path("results")

    def options(self) -> PlanOptionsOut:
        cfg = load_config(self.config_path)
        tasks = load_tasks(self.tasks_dir)
        families: OrderedDict[str, list[ModelSpec]] = OrderedDict()
        for m in cfg.models:
            families.setdefault(m.family, []).append(m)
        cats: OrderedDict[str, list] = OrderedDict()
        for t in tasks:
            cats.setdefault(t.category, []).append(t)
        return PlanOptionsOut(
            families=[FamilyOut(family=f, subscription=vs[0].subscription, provider=vs[0].provider,
                                model_id=vs[0].model_id,
                                variants=[VariantOut(name=v.name, effort=v.effort, default=v.default) for v in vs])
                      for f, vs in families.items()],
            categories=[CategoryOut(name=c, n_tasks=len(ts),
                                    n_rubric=sum(any(g["type"] == "rubric" for g in t.graders) for t in ts))
                        for c, ts in cats.items()],
            repetitions=cfg.run.repetitions,
            judges=[_to_out(j, "judge") for j in cfg.judges])

    def estimate(self, body: EstimateIn) -> EstimateOut:
        cfg = load_config(self.config_path)
        tasks = load_tasks(self.tasks_dir)
        by_name = {m.name: m for m in cfg.models}
        if unknown := set(body.models) - by_name.keys():
            raise UnknownModelsError(unknown)
        known_cats = {t.category for t in tasks}
        if bad := set(body.categories) - known_cats:
            raise UnknownCategoriesError(f"unknown categories: {sorted(bad)}")
        if body.categories:
            tasks = [t for t in tasks if t.category in body.categories]
        wanted = set(body.models)
        models = [m for m in cfg.models if m.name in wanted]  # config order, not click order
        est = estimate(cfg, tasks, models, body.repetitions, self.results_dir)
        return EstimateOut(
            n_competitors=len(models), n_tasks=est.n_tasks, n_rubric_tasks=est.n_rubric_tasks,
            repetitions=est.repetitions, total_calls=est.total_calls,
            subscriptions=[SubscriptionCostOut(**s.__dict__, total_calls=s.total_calls) for s in est.subscriptions],
            notes=est.notes,
            command=build_command(cfg.models, models, body, cfg.run.repetitions, str(self.config_path),
                                  all_categories=sorted(known_cats)))


def build_command(all_models: list[ModelSpec], selected: list[ModelSpec], body: EstimateIn,
                  default_reps: int, config_path: str, all_categories: list[str]) -> str:
    """Shortest equivalent `evalkit run` command: defaults -> no --models; a whole family -> its name."""
    chosen = {m.name for m in selected}
    args = ["python", "-m", "evalkit", "run", "--run", body.run_name]
    if config_path != "config.yaml":
        args += ["--config", config_path]
    if chosen == {m.name for m in all_models}:
        args += ["--models", "all"]
    elif chosen != {m.name for m in all_models if m.default}:
        parts = []
        for family in dict.fromkeys(m.family for m in all_models):
            members = [m.name for m in all_models if m.family == family]
            picked = [n for n in members if n in chosen]
            if picked == members and len(members) > 1:
                parts.append(family)
            else:
                parts += picked
        args += ["--models", ",".join(parts)]
    if body.categories and sorted(set(body.categories)) != all_categories:
        args += ["--category", ",".join(sorted(set(body.categories)))]
    if body.repetitions and body.repetitions != default_reps:
        args += ["--repetitions", str(body.repetitions)]
    return shlex.join(args)
