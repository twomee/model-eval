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
from .graders import GradingContext, grade
from .limits import Limits
from .providers import AnswerPending, Provider, ProviderError, Request, make_provider, manual_paths


def _key(model: str, task: Task, rep: int) -> str:
    return f"{model}|{task.id}|{task.fingerprint}|{rep}"


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


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


async def generate(cfg: Config, tasks: list[Task], run_dir: Path, models: Iterable[ModelSpec]) -> None:
    out = _Appender(run_dir / "generations.jsonl")
    done = {r["key"] for r in read_jsonl(out.path) if not r.get("error")}
    jobs, pending = [], []
    limits = Limits(cfg.run)  # one registry: caps are shared by every model on a subscription

    for spec in models:
        try:
            provider = _limited(spec, cfg, limits)
        except ProviderError as e:
            print(f"  ! skipping {spec.name}: {e}")
            continue
        for task in tasks:
            for rep in range(_reps(spec, cfg)):
                key = _key(spec.name, task, rep)
                if key not in done:
                    jobs.append((spec, provider, task, rep, key))

    total = len(jobs)
    print(f"generate: {total} calls to make ({len(done)} already done)")
    counter = 0

    async def run_one(spec: ModelSpec, provider: Provider, task: Task, rep: int, key: str) -> None:
        nonlocal counter
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
            return
        except ProviderError as e:
            record["error"] = str(e)
        await out.write(record)
        counter += 1
        status = "ERR " + record["error"][:80] if "error" in record else f"{record['latency_s']:.1f}s"
        print(f"  [{counter}/{total}] {spec.name:<18} {task.id:<28} rep{rep}  {status}")

    await asyncio.gather(*(run_one(*j) for j in jobs))
    if pending:
        print(f"  … {len(pending)} manual answers still missing (run `evalkit export` to see prompts)")


async def grade_all(cfg: Config, tasks: list[Task], run_dir: Path, regrade: bool = False) -> None:
    gens = {r["key"]: r for r in read_jsonl(run_dir / "generations.jsonl") if not r.get("error")}
    grades_path = run_dir / "grades.jsonl"
    if regrade and grades_path.exists():
        grades_path.unlink()
    out = _Appender(grades_path)
    graded = {r["key"] for r in read_jsonl(grades_path) if r.get("score") is not None}
    by_id = {t.id: t for t in tasks}

    judges = []
    limits = Limits(cfg.run)
    for spec in cfg.judges:
        try:
            judges.append((spec, _limited(spec, cfg, limits)))
        except ProviderError as e:
            print(f"  ! judge {spec.name} unavailable: {e}")
    ctx = GradingContext(cfg.run, judges)

    todo = [g for k, g in gens.items() if k not in graded and g["task_id"] in by_id
            and k.split("|")[2] == by_id[g["task_id"]].fingerprint]  # ignore outputs for edited tasks
    print(f"grade: {len(todo)} answers to grade ({len(graded)} already graded)")
    sem = asyncio.Semaphore(cfg.run.concurrency)

    async def run_one(g: dict[str, Any]) -> None:
        async with sem:
            score, parts = await grade(by_id[g["task_id"]], g["output"], ctx)
        await out.write({"key": g["key"], "model": g["model"], "task_id": g["task_id"],
                         "category": g["category"], "rep": g["rep"], "score": score,
                         "parts": [asdict(p) for p in parts]})
        s = "n/a" if score is None else f"{score:.2f}"
        print(f"  {g['model']:<18} {g['task_id']:<28} rep{g['rep']}  score={s}")

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
