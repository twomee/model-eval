"""Golden test: every task's reference answer must pass its own deterministic graders.

This is the most valuable test in the repo. A wrong test or a typo in a regex silently
punishes every model equally - you'd never notice from the leaderboard. If the reference
can't pass, the task is broken, not the models.
"""
from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import pytest

from evalkit.config import RunSettings, load_tasks
from evalkit.graders import GRADERS, GradingContext

TASKS = load_tasks(Path(__file__).parent.parent / "tasks")
DETERMINISTIC = {"python_tests", "go_tests", "match", "length", "language"}
CTX = GradingContext(RunSettings(code_timeout_s=60), judges=[])


@pytest.mark.parametrize("task", TASKS, ids=lambda t: t.id)
def test_reference_passes_own_graders(task):
    assert task.reference, f"{task.id}: every task needs a reference answer"
    for cfg in task.graders:
        assert cfg["type"] in GRADERS, f"{task.id}: unknown grader {cfg['type']}"
        if cfg["type"] not in DETERMINISTIC:
            continue
        if cfg["type"] == "go_tests" and shutil.which("go") is None:
            pytest.skip("go toolchain not installed")
        result = asyncio.run(GRADERS[cfg["type"]](task, task.reference, cfg, CTX))
        assert result.score == 1.0, f"{task.id}/{cfg['type']}: {result.details}"


@pytest.mark.parametrize("task", [t for t in TASKS if any(g["type"] == "rubric" for g in t.graders)],
                         ids=lambda t: t.id)
def test_rubric_criteria_are_well_formed(task):
    for cfg in (g for g in task.graders if g["type"] == "rubric"):
        ids = [c["id"] for c in cfg["criteria"]]
        assert len(ids) == len(set(ids)), f"{task.id}: duplicate criterion ids"
        for c in cfg["criteria"]:
            assert len(c["description"]) > 30, f"{task.id}/{c['id']}: description too vague for a judge"
