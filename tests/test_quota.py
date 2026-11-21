"""Out-of-quota subscriptions (found in a real run: ChatGPT hit its usage limit mid-grading).

Before: the quota message matched the retry pattern ("try again at 9:15 PM"), so every judge call
slept through 4 backoff retries (~2.5 min), grading crawled, and the answer was then scored by
the REMAINING judge alone - a different, self-biased panel than its neighbours got.
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

from evalkit import providers, runner
from evalkit.config import Config, ModelSpec, RunSettings, Task
from evalkit.graders import GradingContext, grade_rubric, incomplete_grade
from evalkit.limits import Limits
from evalkit.providers import CliProvider, Completion, ProviderError, Request

CODEX_QUOTA = ("You’ve hit your usage limit. Upgrade to Pro (https://chatgpt.com/explore/pro), visit "
               "https://chatgpt.com/settings/usage to purchase more credits or try again at 9:15 PM.")


@pytest.mark.parametrize("msg", [CODEX_QUOTA, "Fable 5.2 requires usage credits.", "RESOURCE_EXHAUSTED: quota exceeded"])
def test_quota_messages_are_never_retried(msg):
    e = ProviderError(msg, retryable=True)   # callers may say retryable ("try again"): quota wins
    assert e.quota and not e.retryable


def test_ordinary_rate_limits_are_still_retried():
    e = ProviderError("HTTP 429 rate limit, try again in 2s", retryable=True)
    assert e.retryable and not e.quota


def test_cli_quota_error_fails_fast_instead_of_backing_off(tmp_path):
    exe = tmp_path / "codexish"
    exe.write_text(f"#!/bin/sh\necho 'ERROR: {CODEX_QUOTA}' >&2\nexit 1\n", encoding="utf-8")
    exe.chmod(0o755)
    spec = ModelSpec(name="g", provider="cli", command=(str(exe), "{prompt}"))
    t0 = time.perf_counter()
    with pytest.raises(ProviderError) as ei:
        asyncio.run(CliProvider(spec, RunSettings(max_retries=4)).complete(Request(prompt="x")))
    assert ei.value.quota and time.perf_counter() - t0 < 3     # was ~2.5 min of backoff


class _Counting:
    def __init__(self, fail_quota: bool):
        self.calls, self.fail_quota = 0, fail_quota

    async def complete(self, req):
        self.calls += 1
        if self.fail_quota:
            raise ProviderError(CODEX_QUOTA)
        return Completion("ok", 0.0)


def test_circuit_breaker_skips_the_exhausted_subscription_only():
    limits = Limits(RunSettings())
    gpt1, gpt2, claude = _Counting(True), _Counting(True), _Counting(False)
    w_gpt1 = limits.wrap(ModelSpec(name="sol", provider="cli", command=("codex",)), gpt1)
    w_gpt2 = limits.wrap(ModelSpec(name="luna", provider="cli", command=("codex",)), gpt2)
    w_claude = limits.wrap(ModelSpec(name="opus", provider="cli", command=("claude",)), claude)

    async def go():
        for w in (w_gpt1, w_gpt2, w_gpt2):
            with pytest.raises(ProviderError) as ei:
                await w.complete(Request(prompt="p"))
            assert ei.value.quota
        assert (await w_claude.complete(Request(prompt="p"))).text == "ok"
    asyncio.run(go())
    assert gpt1.calls == 1 and gpt2.calls == 0       # one real call trips it; the rest are skipped
    assert claude.calls == 1 and "openai" in limits.exhausted and "claude" not in limits.exhausted


# ---------------------------------------------------------------- grading: never a half panel

class _Judge:
    def __init__(self, quota: bool):
        self.quota = quota

    async def complete(self, req):
        if self.quota:
            raise ProviderError(CODEX_QUOTA)
        return Completion(json.dumps({"criteria": [{"id": "c", "reason": "r", "score": 9}]}), 0.0)


TASK = Task(id="t", category="c", prompt="p", reference="ref",
            graders=[{"type": "rubric", "criteria": [{"id": "c", "description": "d" * 40}]}])


def _ctx(gpt_quota: bool) -> GradingContext:
    return GradingContext(RunSettings(), [(ModelSpec(name="judge-claude", provider="mock"), _Judge(False)),
                                         (ModelSpec(name="judge-gpt", provider="mock"), _Judge(gpt_quota))])


def test_rubric_is_deferred_not_graded_by_half_the_panel():
    r = asyncio.run(grade_rubric(TASK, "answer", TASK.graders[0], _ctx(gpt_quota=True)))
    assert r.score is None and "judge-gpt" in r.details["deferred"]
    ok = asyncio.run(grade_rubric(TASK, "answer", TASK.graders[0], _ctx(gpt_quota=False)))
    assert ok.score == pytest.approx(0.9) and "deferred" not in ok.details


def test_grade_all_writes_nothing_when_deferred_then_grades_after_reset(tmp_path, monkeypatch):
    run = tmp_path / "r"
    run.mkdir()
    (run / "generations.jsonl").write_text(json.dumps(
        {"key": f"m|t|{TASK.fingerprint}|0", "model": "m", "task_id": "t", "category": "c", "rep": 0,
         "output": "answer"}) + "\n", encoding="utf-8")
    cfg = Config(run=RunSettings(), models=[], judges=[ModelSpec(name="j", provider="mock")], category_weights={})

    for quota, expected in ((True, 0), (False, 1)):   # out of quota -> nothing; reset -> graded
        monkeypatch.setattr(runner, "GradingContext", lambda settings, judges, q=quota: _ctx(q))
        asyncio.run(runner.grade_all(cfg, [TASK], run))
        grades = runner.read_jsonl(run / "grades.jsonl")
        assert len(grades) == expected
    assert grades[0]["score"] == pytest.approx(0.9)


def test_old_half_panel_grades_are_regraded_and_not_reported():
    old = {"key": "k", "score": 0.8, "parts": [{"grader": "rubric", "details": {"judges": {
        "judge-claude": {"c": {"score": 8, "reason": "r"}},
        "judge-gpt": f"ProviderError('exit 1: codex error: {CODEX_QUOTA}')"}}}]}
    fine = {"key": "k2", "score": 0.8, "parts": [{"grader": "rubric", "details": {"judges": {
        "judge-claude": {"c": {"score": 8, "reason": "r"}}, "judge-gpt": "ValueError('no JSON in judge output')"}}}]}
    assert incomplete_grade(old) and not incomplete_grade(fine)   # only quota cuts are regraded
