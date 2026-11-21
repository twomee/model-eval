"""Config/task loading, resumability, and report statistics."""
from __future__ import annotations

import asyncio
import os
import textwrap
from pathlib import Path

import pytest

from evalkit.config import load_config, load_tasks
from evalkit.report import _bootstrap_ci, _weighted_overall, build_summary
from evalkit.runner import generate, grade_all, read_jsonl

ROOT = Path(__file__).parent.parent


# ---------------------------------------------------------------- tasks

def write(tmp: Path, name: str, body: str) -> None:
    (tmp / name).write_text(textwrap.dedent(body), encoding="utf-8")


def test_fingerprint_changes_with_prompt(tmp_path):
    write(tmp_path, "a.yaml", """
        - id: t1
          category: c
          prompt: one
          grader: {type: match, must_include: [x]}
    """)
    fp1 = load_tasks(tmp_path)[0].fingerprint
    write(tmp_path, "a.yaml", """
        - id: t1
          category: c
          prompt: two
          grader: {type: match, must_include: [x]}
    """)
    assert load_tasks(tmp_path)[0].fingerprint != fp1


def test_twin_inherits_graders_and_links_pair(tmp_path):
    write(tmp_path, "a.yaml", """
        - id: orig
          category: c
          prompt: english
          grader: {type: match, must_include: [x]}
        - id: orig_he
          twin_of: orig
          prompt: עברית
          extra_graders: [{type: language, expected: he}]
    """)
    tasks = {t.id: t for t in load_tasks(tmp_path)}
    assert tasks["orig"].pair == tasks["orig_he"].pair == "orig"
    assert tasks["orig_he"].category == "c" and tasks["orig_he"].lang == "he"
    assert [g["type"] for g in tasks["orig_he"].graders] == ["match", "language"]


@pytest.mark.parametrize("body,err", [
    ("- {id: a, category: c, prompt: p, grader: {type: match}}\n- {id: a, category: c, prompt: p, grader: {type: match}}",
     "Duplicate"),
    ("- {id: b, twin_of: nope, prompt: p}", "unknown task"),
])
def test_task_loading_errors(tmp_path, body, err):
    write(tmp_path, "a.yaml", body)
    with pytest.raises(ValueError, match=err):
        load_tasks(tmp_path)


def test_repo_tasks_load():
    tasks = load_tasks(ROOT / "tasks")
    assert len(tasks) >= 20


# ---------------------------------------------------------------- statistics

def test_overall_is_mean_of_category_means():
    # category a has 3 perfect tasks, b has 1 zero task: task-mean would be 0.75, category-mean is 0.5
    tm = {"a1": 1.0, "a2": 1.0, "a3": 1.0, "b1": 0.0}
    cat = {"a1": "a", "a2": "a", "a3": "a", "b1": "b"}
    assert _weighted_overall(tm, cat, {}) == pytest.approx(0.5)
    assert _weighted_overall(tm, cat, {"a": 3}) == pytest.approx(0.75)


def test_bootstrap_ci_brackets_point_estimate():
    tm = {f"t{i}": (i % 5) / 4 for i in range(20)}
    cat = {t: "c" for t in tm}
    point = _weighted_overall(tm, cat, {})
    lo, hi = _bootstrap_ci(tm, cat, {})
    assert lo <= point <= hi and hi - lo > 0


# ---------------------------------------------------------------- end-to-end with mock models

def test_mock_pipeline_ranks_and_resumes(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    cfg = load_config(ROOT / "config.mock.yaml")
    tasks = [t for t in load_tasks(ROOT / "tasks") if t.id in
             {"py_token_bucket", "gen_capacity_estimate", "gen_capacity_estimate_he", "design_job_queue_choice"}]
    run_dir = tmp_path / "results" / "r"

    asyncio.run(generate(cfg, tasks, run_dir, cfg.models))
    asyncio.run(grade_all(cfg, tasks, run_dir))
    n_gen = len(read_jsonl(run_dir / "generations.jsonl"))
    assert n_gen == len(cfg.models) * len(tasks) * cfg.run.repetitions

    # resume: nothing left to do, nothing appended
    asyncio.run(generate(cfg, tasks, run_dir, cfg.models))
    asyncio.run(grade_all(cfg, tasks, run_dir))
    assert len(read_jsonl(run_dir / "generations.jsonl")) == n_gen
    assert "0 calls to make" in capsys.readouterr().out

    summary = build_summary(cfg, run_dir, tasks)
    ranking = [m["name"] for m in summary["models"]]
    assert ranking[0] == "mock-good" and ranking[-1] == "mock-broken"
    assert summary["has_pairs"] and summary["models"][0]["n_pairs"] == 1


def test_task_signal_flags_tasks_that_dont_separate_models():
    from evalkit.report import task_signal
    matrix = {
        "easy": {"a": 1.0, "b": 0.97, "c": 0.96},
        "hard_good": {"a": 1.0, "b": 0.4, "c": 0.0},
        "broken": {"a": 0.0, "b": 0.02, "c": 0.0},
        "single": {"a": 0.5},                        # one model: can't judge separation
    }
    verdicts = {s["task"]: s["verdict"] for s in task_signal(matrix)}
    assert verdicts == {"easy": "too easy", "hard_good": "separates", "broken": "nobody passes"}
