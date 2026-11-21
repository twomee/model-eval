"""Two-stage pipeline: GENERATE (expensive, slow) then GRADE (cheap, re-runnable).

Why split them? You WILL change rubrics and tests after seeing real answers.
Storing raw outputs means re-grading costs nothing and needs no model access.
Both stages are idempotent and resumable: re-running skips work already done,
so a crash or a rate limit at task 80/100 loses nothing.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any, Iterable

from .config import Config, ModelSpec, Task
from .graders import GradingContext, grade, incomplete_grade
from .limits import Limits
from .providers import AnswerPending, Provider, ProviderError, Request, make_provider, manual_paths


class Progress:
    """Where the runner reports what it's doing. The runner doesn't know who listens:
    the CLI prints (ConsoleProgress), the web UI updates a job (api/services/run_service.py)."""

    def stage(self, stage: str, total: int, already_done: int) -> None: ...

    def item(self, stage: str, model: str, task_id: str, rep: int, detail: str, failed: bool) -> None: ...

    def note(self, message: str) -> None: ...

    def quota(self, subscription: str, paused_until: float | None, reason: str) -> None:
        """A subscription ran out of quota (paused_until = when it's re-checked) or resumed (None, "")."""


def _clock(ts: float) -> str:
    import datetime as dt
    return dt.datetime.fromtimestamp(ts).strftime("%H:%M")


class ConsoleProgress(Progress):
    def __init__(self) -> None:
        self.counter = 0

    def stage(self, stage: str, total: int, already_done: int) -> None:
        self.counter = 0
        if stage == "generate":
            print(f"generate: {total} calls to make ({already_done} already done)")
        else:
            print(f"grade: {total} answers to grade ({already_done} already graded)")

    def item(self, stage: str, model: str, task_id: str, rep: int, detail: str, failed: bool) -> None:
        self.counter += 1
        prefix = f"  [{self.counter}] " if stage == "generate" else "  "
        print(f"{prefix}{model:<18} {task_id:<28} rep{rep}  {detail}")

    def note(self, message: str) -> None:
        print(f"  {message}")

    def quota(self, subscription: str, paused_until: float | None, reason: str) -> None:
        if paused_until:
            print(f"  ⏸ {subscription} is out of quota: its calls wait until ~{_clock(paused_until)}; "
                  f"other subscriptions continue. ({reason[:120]})")
        elif reason:
            print(f"  ✗ {subscription} is out of quota and the run stopped waiting: its remaining calls fail. "
                  f"Run again after it resets to fill them in.")
        else:
            print(f"  ▶ {subscription} has quota again: resuming its calls")


def _key(model: str, task: Task, rep: int) -> str:
    return f"{model}|{task.id}|{task.fingerprint}|{rep}"


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    """Records of a JSONL file. The LAST line may be half-written (grading reads the answers file
    while generation is still appending, or a crash cut a write short): it is skipped. A broken
    line anywhere else is real corruption and raises."""
    if not path.exists():
        return []
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    out = []
    for i, line in enumerate(lines):
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            if i == len(lines) - 1:
                continue
            raise
    return out


class _Appender:
    def __init__(self, path: Path):
        self.path, self.lock = path, asyncio.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)

    async def write(self, record: dict[str, Any]) -> None:
        async with self.lock:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _reps(spec: ModelSpec, cfg: Config) -> int:
    return 1 if spec.provider == "manual" else cfg.run.repetitions


def _limited(spec: ModelSpec, cfg: Config, limits: Limits | None = None) -> Provider:
    settings = replace(cfg.run, timeout_s=spec.timeout_s) if spec.timeout_s else cfg.run
    return (limits or Limits(cfg.run)).wrap(spec, make_provider(spec, settings))


async def generate(cfg: Config, tasks: list[Task], run_dir: Path, models: Iterable[ModelSpec],
                   progress: Progress | None = None, limits: Limits | None = None) -> None:
    progress = progress or ConsoleProgress()
    out = _Appender(run_dir / "generations.jsonl")
    done = {r["key"] for r in read_jsonl(out.path) if not r.get("error")}
    jobs, pending = [], []
    limits = limits or Limits(cfg.run)  # one registry: caps are shared by every model on a subscription

    for spec in models:
        try:
            provider = _limited(spec, cfg, limits)
        except ProviderError as e:
            progress.note(f"! skipping {spec.name}: {e}")
            continue
        for task in tasks:
            for rep in range(_reps(spec, cfg)):
                key = _key(spec.name, task, rep)
                if key not in done:
                    jobs.append((spec, provider, task, rep, key))

    total = len(jobs)
    progress.stage("generate", total, len(done))

    async def run_one(spec: ModelSpec, provider: Provider, task: Task, rep: int, key: str) -> None:
        req = Request(prompt=task.prompt, system=task.system, max_tokens=cfg.run.max_tokens,
                      temperature=cfg.run.temperature,
                      meta={"task_id": task.id, "reference": task.reference})
        record: dict[str, Any] = {"key": key, "model": spec.name, "task_id": task.id,
                                  "category": task.category, "rep": rep}
        try:
            comp = await provider.complete(req)
            record.update(output=comp.text, latency_s=round(comp.latency_s, 3),
                          input_tokens=comp.input_tokens, output_tokens=comp.output_tokens,
                          cost_usd=comp.cost_usd if comp.cost_usd is not None
                          else spec.cost(comp.input_tokens or 0, comp.output_tokens or 0))
        except AnswerPending as e:
            pending.append(str(e))
            # Counted, so progress reaches 100%: nothing to call, the human hasn't pasted it yet.
            progress.item("generate", spec.name, task.id, rep, "manual: answer not pasted yet", False)
            return
        except ProviderError as e:
            record["error"] = str(e)
        await out.write(record)
        status = "ERR " + record["error"][:80] if "error" in record else f"{record['latency_s']:.1f}s"
        progress.item("generate", spec.name, task.id, rep, status, "error" in record)

    await asyncio.gather(*(run_one(*j) for j in jobs))
    if pending:
        progress.note(f"… {len(pending)} manual answers still missing (run `evalkit export` to see prompts)")


async def grade_all(cfg: Config, tasks: list[Task], run_dir: Path, regrade: bool = False,
                    progress: Progress | None = None, limits: Limits | None = None) -> None:
    progress = progress or ConsoleProgress()
    gens = {r["key"]: r for r in read_jsonl(run_dir / "generations.jsonl") if not r.get("error")}
    grades_path = run_dir / "grades.jsonl"
    if regrade and grades_path.exists():
        grades_path.unlink()
    out = _Appender(grades_path)
    latest: dict[str, dict[str, Any]] = {}
    for r in read_jsonl(grades_path):
        latest[r["key"]] = r                    # a regrade appends; the last record counts
    # Graded = has a score AND a complete judge panel (a quota-cut panel is regraded).
    graded = {k for k, r in latest.items() if r.get("score") is not None and not incomplete_grade(r)}
    by_id = {t.id: t for t in tasks}

    judges = []
    limits = limits or Limits(cfg.run)  # shared with generation when run together (see pipeline)
    for spec in cfg.judges:
        try:
            judges.append((spec, _limited(spec, cfg, limits)))
        except ProviderError as e:
            progress.note(f"! judge {spec.name} unavailable: {e}")
    ctx = GradingContext(cfg.run, judges)

    todo = [g for k, g in gens.items() if k not in graded and g["task_id"] in by_id
            and k.split("|")[2] == by_id[g["task_id"]].fingerprint]  # ignore outputs for edited tasks
    progress.stage("grade", len(todo), len(graded))
    sem = asyncio.Semaphore(cfg.run.concurrency)

    async def run_one(g: dict[str, Any]) -> None:
        async with sem:
            score, parts = await grade(by_id[g["task_id"]], g["output"], ctx)
        if deferred := next((p.details["deferred"] for p in parts if p.details.get("deferred")), None):
            # Not written: stays ungraded, so the next run grades it with the full judge panel.
            progress.item("grade", g["model"], g["task_id"], g["rep"], f"deferred ({deferred})", False)
            return
        await out.write({"key": g["key"], "model": g["model"], "task_id": g["task_id"],
                         "category": g["category"], "rep": g["rep"], "score": score,
                         "parts": [asdict(p) for p in parts]})
        s = "n/a" if score is None else f"{score:.2f}"
        # score None = the grader couldn't run (e.g. Go not installed): left out of the score, and
        # not the model's failure, so it isn't reported as one.
        detail = f"score={s}" if score is not None else "not scored (grader couldn't run, e.g. toolchain missing)"
        progress.item("grade", g["model"], g["task_id"], g["rep"], detail, False)

    await asyncio.gather(*(run_one(g) for g in todo))


def export_manual(cfg: Config, tasks: list[Task]) -> list[Path]:
    """Write one prompt file per (manual model, task); you paste replies into *.answer.md."""
    written = []
    for spec in (m for m in cfg.models if m.provider == "manual"):
        for task in tasks:
            prompt_path, answer_path = manual_paths(spec.name, task.id)
            prompt_path.parent.mkdir(parents=True, exist_ok=True)
            body = (f"{task.system}\n\n---\n\n{task.prompt}" if task.system else task.prompt)
            prompt_path.write_text(body, encoding="utf-8")
            if not answer_path.exists():
                answer_path.write_text("", encoding="utf-8")
            written.append(prompt_path)
    return written
