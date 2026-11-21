"""Results: which runs exist, and the model comparison for one run.

The comparison is computed from the same build_summary() the CLI report uses, so the UI,
the terminal and report.html always agree.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from ...compare import Ranking
from ...config import load_config, load_tasks
from ...report import build_summary
from ..schemas import (RUN_NAME_PATTERN, ComparisonOut, FlaggedTaskOut, RankedOut, RankingOut, RunOut)

HEAD_TO_HEAD_TOP = 10


class RunNotFoundError(LookupError):
    pass


@dataclass(frozen=True)
class ResultsService:
    config_path: Path
    tasks_dir: Path
    results_dir: Path = Path("results")

    def run_dir(self, run: str) -> Path:
        """Resolve a run name to its directory, refusing anything that could escape results/."""
        if not re.fullmatch(RUN_NAME_PATTERN, run):
            raise RunNotFoundError(run)
        root = self.results_dir.resolve()
        d = (root / run).resolve()
        if d.parent != root or not (d / "generations.jsonl").is_file():
            raise RunNotFoundError(run)
        return d

    def list_runs(self) -> list[RunOut]:
        if not self.results_dir.is_dir():
            return []
        runs = []
        for d in self.results_dir.iterdir():
            gens = d / "generations.jsonl"
            if d.is_dir() and gens.is_file() and re.fullmatch(RUN_NAME_PATTERN, d.name):
                n = sum(1 for line in gens.read_text(encoding="utf-8").splitlines() if line.strip())
                runs.append(RunOut(name=d.name, has_report=(d / "report.html").is_file(), n_answers=n,
                                   updated_at=gens.stat().st_mtime))
        return sorted(runs, key=lambda r: -r.updated_at)

    def comparison(self, run: str) -> ComparisonOut:
        d = self.run_dir(run)
        summary = build_summary(load_config(self.config_path), d, load_tasks(self.tasks_dir))
        c = summary["comparison"]
        return ComparisonOut(
            run=run, for_you=_ranking_out(c.for_you), general=_ranking_out(c.general), notes=c.notes,
            flagged_tasks=[FlaggedTaskOut(task=s["task"], verdict=s["verdict"], min=s["min"], max=s["max"])
                           for s in summary["task_signal"] if s["verdict"] != "separates"],
            report_url=f"/reports/{run}/report.html" if (d / "report.html").is_file() else None)

    def report_path(self, run: str) -> Path:
        path = self.run_dir(run) / "report.html"
        if not path.is_file():
            raise RunNotFoundError(run)
        return path


def _ranking_out(r: Ranking) -> RankingOut:
    leaders = set(r.leaders)
    names = [m.name for m in r.models[:HEAD_TO_HEAD_TOP]]
    return RankingOut(
        label=r.label, weights=r.weights, verdict=r.verdict,
        models=[RankedOut(name=m.name, score=m.score, ci_low=m.ci90[0], ci_high=m.ci90[1], n_tasks=m.n_tasks,
                          leader=m.name in leaders) for m in r.models],
        head_to_head_names=names,
        head_to_head=[[None if a == b else r.p_beats.get((a, b)) for b in names] for a in names])
