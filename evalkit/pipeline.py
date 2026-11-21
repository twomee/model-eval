"""One full evaluation run: generate -> grade -> report. Shared by the CLI and the web UI,
so "Run" in the browser does exactly what `python -m evalkit run` does."""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from .config import Config, ModelSpec, Task
from .report import build_summary, write_csv, write_html
from .runner import Progress, generate, grade_all


def write_reports(cfg: Config, run_dir: Path, tasks: list[Task]) -> dict[str, Any]:
    summary = build_summary(cfg, run_dir, tasks)
    write_html(summary, run_dir / "report.html")
    write_csv(summary, run_dir / "summary.csv")
    return summary


async def run_eval(cfg: Config, tasks: list[Task], models: list[ModelSpec], run_dir: Path,
                   progress: Progress | None = None) -> dict[str, Any]:
    await generate(cfg, tasks, run_dir, models, progress)
    await grade_all(cfg, tasks, run_dir, progress=progress)
    if progress:
        progress.stage("report", 1, 0)
    # The bootstrap comparison is CPU work (about a second for many models): run it off the event
    # loop, so a web server stays responsive while it builds the report.
    summary = await asyncio.to_thread(write_reports, cfg, run_dir, tasks)
    if progress:
        progress.item("report", "", "", 0, "report written", False)
    return summary
