"""Regression tests for bugs found in the full audit (2026-11-21). Each test failed before its fix.

A  report counted answers for an edited task (stale fingerprint)
B  run.concurrency ("global cap") was never applied; no per-subscription cap
C  a timed-out CLI left its children running (only the direct child was killed, never reaped)
D  doctor started every check at once ("Check all" = 60+ CLI processes)
E  model-written code ran with the full environment (API keys/tokens readable)
"""
from __future__ import annotations

import asyncio
import os
import textwrap
import time
from dataclasses import replace
from pathlib import Path

import pytest

from evalkit import procs, providers, runner
from evalkit.config import Config, ModelSpec, RunSettings, Task, load_config, load_tasks
from evalkit.doctor import run_doctor
from evalkit.graders import GradingContext, grade_python_tests, sandbox_env
from evalkit.report import build_summary

ROOT = Path(__file__).parent.parent


# ---------------------------------------------------------------- A: stale results

def _write_task(d: Path, prompt: str, must: str) -> list[Task]:
    (d / "t.yaml").write_text(textwrap.dedent(f"""
        - {{id: t1, category: c, prompt: {prompt}, reference: x, grader: {{type: match, must_include: [{must}]}}}}
        - {{id: t2, category: c, prompt: keep, reference: x, grader: {{type: match, must_include: [x]}}}}
    """), encoding="utf-8")
    return load_tasks(d)


def test_report_ignores_answers_for_edited_or_removed_tasks(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    cfg = load_config(ROOT / "config.mock.yaml")
    cfg = replace(cfg, models=[m for m in cfg.models if m.name == "mock-good"], judges=[])
    run_dir = tmp_path / "run"
    v1 = _write_task(tasks_dir, "v1", "x")
    asyncio.run(runner.generate(cfg, v1, run_dir, cfg.models))
    asyncio.run(runner.grade_all(cfg, v1, run_dir))
    assert set(build_summary(cfg, run_dir, v1)["matrix"]) == {"t1", "t2"}

    v2 = _write_task(tasks_dir, "v2", "NEVER")          # t1 edited, not re-run yet
    matrix = build_summary(cfg, run_dir, v2)["matrix"]
    assert "t1" not in matrix and "t2" in matrix          # stale t1 must not count
    assert set(build_summary(cfg, run_dir, [t for t in v2 if t.id == "t2"])["matrix"]) == {"t2"}  # removed


# ---------------------------------------------------------------- B + D: concurrency caps

class _Probe:
    active = peak = 0

    def __init__(self, spec, settings):
        self.spec = spec

    async def complete(self, req):
        _Probe.active += 1
        _Probe.peak = max(_Probe.peak, _Probe.active)
        await asyncio.sleep(0.02)
        _Probe.active -= 1
        return providers.Completion("x", 0.02, 1, 1)


@pytest.fixture
def probe(monkeypatch):
    monkeypatch.setitem(providers._REGISTRY, "probe", _Probe)
    _Probe.active = _Probe.peak = 0
    return _Probe


def _probe_cfg(n: int, concurrency: int, sub_cap: int, base_url: str | None = None) -> Config:
    models = [ModelSpec(name=f"m{i}", provider="probe", concurrency=5, base_url=base_url) for i in range(n)]
    return Config(run=RunSettings(concurrency=concurrency, subscription_concurrency=sub_cap, repetitions=1),
                  models=models, judges=[], category_weights={})


def test_generate_respects_global_cap(probe, tmp_path):
    cfg = _probe_cfg(10, concurrency=3, sub_cap=99)
    asyncio.run(runner.generate(cfg, load_tasks(ROOT / "tasks")[:4], tmp_path, cfg.models))
    assert 1 < probe.peak <= 3


def test_cli_models_on_one_subscription_share_its_cap(probe, monkeypatch):
    from evalkit.limits import Limits
    # 6 "CLI" models logged into the same subscription (like 13 Gemini variants on one Google plan)
    specs = [ModelSpec(name=f"g{i}", provider="cli", command=("agy",)) for i in range(6)]
    limits = Limits(RunSettings(concurrency=50, subscription_concurrency=2))
    wrapped = [limits.wrap(s, _Probe(s, None)) for s in specs]

    async def go():
        await asyncio.gather(*(w.complete(providers.Request(prompt="p")) for w in wrapped for _ in range(3)))
    asyncio.run(go())
    assert probe.peak == 2


def test_doctor_respects_caps(probe):
    cfg = _probe_cfg(12, concurrency=4, sub_cap=99)
    asyncio.run(run_doctor(cfg, cfg.models, []))
    assert 1 < probe.peak <= 4


# ---------------------------------------------------------------- C: process trees

posix_only = pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX")


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # a zombie still "exists"; check it's really running
    try:
        return Path(f"/proc/{pid}/status").read_text().find("State:\tZ") == -1
    except OSError:
        return True


def _spawner(tmp_path: Path) -> tuple[list[str], Path]:
    script = tmp_path / "spawner.sh"
    script.write_text('#!/bin/sh\necho $$ > "$1.self"\nsleep 60 &\necho $! > "$1"\nsleep 60\n', encoding="utf-8")
    script.chmod(0o755)
    pidfile = tmp_path / "pid"
    return [str(script), str(pidfile)], pidfile


def _pids(pidfile: Path) -> list[int]:
    for _ in range(50):  # the script writes them asynchronously
        if pidfile.exists() and Path(f"{pidfile}.self").exists():
            break
        time.sleep(0.02)
    return [int(pidfile.read_text()), int(Path(f"{pidfile}.self").read_text())]


@posix_only
def test_timeout_kills_the_whole_process_tree(tmp_path):
    argv, pidfile = _spawner(tmp_path)
    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(procs.run(argv, timeout=0.5))
    assert not any(_alive(p) for p in _pids(pidfile))


@posix_only
def test_cancellation_kills_the_whole_process_tree(tmp_path):
    argv, pidfile = _spawner(tmp_path)

    async def cancel_soon():
        task = asyncio.create_task(procs.run(argv, timeout=60))
        await asyncio.sleep(0.5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(cancel_soon())
    assert not any(_alive(p) for p in _pids(pidfile))


@posix_only
def test_cli_provider_timeout_leaves_nothing_running(tmp_path):
    argv, pidfile = _spawner(tmp_path)
    spec = ModelSpec(name="hang", provider="cli", command=(*argv, "{prompt}"))
    with pytest.raises(providers.ProviderError, match="timeout"):
        asyncio.run(providers.CliProvider(spec, RunSettings(timeout_s=0.5, max_retries=0))
                    .complete(providers.Request(prompt="x")))
    assert not any(_alive(p) for p in _pids(pidfile))


# ---------------------------------------------------------------- E: secrets in the environment

def test_sandbox_env_drops_secrets(monkeypatch, tmp_path):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_secret")
    env = sandbox_env(str(tmp_path))
    assert "ANTHROPIC_API_KEY" not in env and "GITHUB_TOKEN" not in env
    assert env["HOME"] == str(tmp_path) and "PATH" in env


def test_model_code_cannot_read_your_environment(monkeypatch):
    monkeypatch.setenv("EVALKIT_FAKE_SECRET", "sk-test-123")
    answer = "```python\nimport os\nLEAK = os.environ.get('EVALKIT_FAKE_SECRET')\n```"
    tests = "from solution import LEAK\ndef test_no_leak():\n    assert LEAK is None\n"
    r = asyncio.run(grade_python_tests(Task(id="t", category="c", prompt="p", graders=[]), answer,
                                       {"tests": tests}, GradingContext(RunSettings(), [])))
    assert r.score == 1.0, r.details
