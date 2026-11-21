"""One full evaluation run: generate -> grade -> report. Shared by the CLI and the web UI,
so "Run" in the browser does exactly what `python -m evalkit run` does.

Generation and grading OVERLAP: answers that are already in get graded while slower models are
still answering. Judges run on other subscriptions (claude, openai) that are often idle by then,
so grading's hour no longer waits for the slowest model's last answer. Both stages share ONE
Limits registry, so per-subscription caps hold across them (a judge call and an answer call on
the same subscription count against the same cap).
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from .config import Config, ModelSpec, Task
from .limits import Limits
from .report import build_summary, write_csv, write_html
from .runner import Progress, generate, grade_all, read_jsonl

GRADE_EVERY_S = 20.0


def write_reports(cfg: Config, run_dir: Path, tasks: list[Task]) -> dict[str, Any]:
    summary = build_summary(cfg, run_dir, tasks)
    write_html(summary, run_dir / "report.html")
    write_csv(summary, run_dir / "summary.csv")
    return summary


def _answer_count(run_dir: Path) -> int:
    return sum(1 for r in read_jsonl(run_dir / "generations.jsonl") if not r.get("error"))


async def run_eval(cfg: Config, tasks: list[Task], models: list[ModelSpec], run_dir: Path,
                   progress: Progress | None = None, grade_every_s: float = GRADE_EVERY_S) -> dict[str, Any]:
    limits = Limits(cfg.run)
    gen = asyncio.create_task(generate(cfg, tasks, run_dir, models, progress, limits))
    graded_at = -1   # answer count at the last grading pass
    try:
        while not gen.done():
            await asyncio.wait({gen}, timeout=grade_every_s)
            if gen.done():
                break
            if (n := _answer_count(run_dir)) > graded_at:     # only when there's something new
                graded_at = n
                await grade_all(cfg, tasks, run_dir, progress=progress, limits=limits)
        await gen  # re-raise a generation error
        await grade_all(cfg, tasks, run_dir, progress=progress, limits=limits)  # the last answers
    finally:
        if not gen.done():  # cancelled (Ctrl+C / Cancel button) while grading: stop answering too
            gen.cancel()
            await asyncio.gather(gen, return_exceptions=True)
    if progress:
        progress.stage("report", 1, 0)
    # The bootstrap comparison is CPU work (about a second for many models): run it off the event
    # loop, so a web server stays responsive while it builds the report.
    summary = await asyncio.to_thread(write_reports, cfg, run_dir, tasks)
    if progress:
        progress.item("report", "", "", 0, "report written", False)
    return summary
