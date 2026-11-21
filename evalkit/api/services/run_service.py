"""Run button: start the planned run in the background, report progress, allow cancel.

Design decisions:
  * The run belongs to the SERVER, not the browser tab: closing the tab doesn't stop it, and
    the page reconnects to its progress. If the server stops, the run stops - and because runs
    resume, starting it again continues where it stopped.
  * One run at a time: two runs would compete for the same subscription quotas and make the
    per-subscription caps meaningless.
  * Cancel = cancel the asyncio task; procs.run kills every CLI process tree it had started.
  * Same pipeline as the CLI (pipeline.run_eval) and the same selection rules as the estimate
    (PlanService.resolve), so the button runs exactly what the planner showed.
"""
from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from ...pipeline import run_eval
from ...runner import Progress
from ..schemas import EstimateIn, JobOut, StageOut
from .plan_service import PlanService

RECENT_LINES = 40


class JobConflictError(RuntimeError):
    pass


@dataclass
class _Stage:
    total: int = 0
    done: int = 0
    failed: int = 0
    already_done: int = 0


@dataclass
class Job:
    run_name: str
    models: list[str]
    n_tasks: int
    repetitions: int
    status: str = "running"
    stage: str = "generate"
    stages: dict[str, _Stage] = field(default_factory=dict)
    recent: deque[str] = field(default_factory=lambda: deque(maxlen=RECENT_LINES))
    error: str | None = None
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None

    def out(self) -> JobOut:
        return JobOut(run_name=self.run_name, status=self.status, models=self.models, n_tasks=self.n_tasks,
                      repetitions=self.repetitions, stage=self.stage,
                      stages={k: StageOut(**v.__dict__) for k, v in self.stages.items()},
                      recent=list(self.recent), error=self.error, started_at=self.started_at,
                      finished_at=self.finished_at, results_url=f"#results/{self.run_name}")


class JobProgress(Progress):
    """Runner progress -> job state (read by the polling endpoint)."""

    def __init__(self, job: Job):
        self.job = job

    def stage(self, stage: str, total: int, already_done: int) -> None:
        self.job.stage = stage
        self.job.stages[stage] = _Stage(total=total, already_done=already_done)
        if stage != "report":
            self.job.recent.append(f"{stage}: {total} to do" + (f", {already_done} already done" if already_done else ""))

    def item(self, stage: str, model: str, task_id: str, rep: int, detail: str, failed: bool) -> None:
        s = self.job.stages.setdefault(stage, _Stage())
        s.done += 1
        s.failed += int(failed)
        line = f"{model} · {task_id} · rep{rep} · {detail}" if model else detail
        self.job.recent.append(("✗ " if failed else "") + line)

    def note(self, message: str) -> None:
        self.job.recent.append(message)


@dataclass
class RunService:
    plan: PlanService
    results_dir: Path = Path("results")
    current: Job | None = None
    _task: asyncio.Task | None = None

    def start(self, body: EstimateIn) -> Job:
        if self.current and self.current.status == "running":
            raise JobConflictError(f"a run is already in progress: {self.current.run_name}")
        cfg, tasks, models = self.plan.resolve(body)   # same validation and selection as the estimate
        job = Job(run_name=body.run_name, models=[m.name for m in models], n_tasks=len(tasks),
                  repetitions=cfg.run.repetitions)
        self.current = job
        self._task = asyncio.create_task(self._run(job, cfg, tasks, models))
        return job

    async def _run(self, job, cfg, tasks, models) -> None:
        try:
            await run_eval(cfg, tasks, models, self.results_dir / job.run_name, JobProgress(job))
            job.status, job.stage = "succeeded", "done"
        except asyncio.CancelledError:
            job.status = "cancelled"
            job.recent.append("cancelled: answers so far are kept; start the same run again to resume")
            raise
        except Exception as e:  # a crash must reach the UI, not vanish in a background task
            job.status, job.error = "failed", f"{type(e).__name__}: {e}"
            job.recent.append(f"failed: {job.error}")
        finally:
            job.finished_at = time.time()

    def cancel(self) -> Job:
        if not (self.current and self.current.status == "running" and self._task):
            raise JobConflictError("no run in progress")
        self._task.cancel()
        return self.current

    async def wait(self) -> None:
        """For tests and shutdown: wait for the current run to finish (or be cancelled)."""
        if self._task:
            try:
                await self._task
            except asyncio.CancelledError:
                pass
