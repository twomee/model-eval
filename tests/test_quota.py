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


def test_cli_quota_error_is_not_backed_off_by_the_provider(tmp_path):
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


def test_fail_mode_skips_the_exhausted_subscription_only():
    limits = Limits(RunSettings(on_quota="fail"))
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
    # (deferral is the on_quota="fail" path, or what happens after waiting gave up)
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


# ---------------------------------------------------------------- wait for the reset (default)

class _Window:
    """A subscription that is out of quota for its first `blocked` calls, then resets."""

    def __init__(self, blocked: int, message: str = "5-hour limit reached"):
        self.blocked, self.message, self.calls, self.ok = blocked, message, 0, 0

    async def complete(self, req):
        self.calls += 1
        if self.calls <= self.blocked:
            raise ProviderError(self.message)
        self.ok += 1
        return Completion("ok", 0.0)


FAST = RunSettings(quota_probe_min=0.002)   # re-check after 0.12 s instead of 15 min


def test_calls_wait_for_the_reset_then_all_succeed():
    events = []
    limits = Limits(FAST, on_quota_change=lambda sub, until, why: events.append((sub, until is not None)))
    window = _Window(blocked=2)
    w = limits.wrap(ModelSpec(name="opus", provider="cli", command=("claude",)), window)

    async def go():
        return await asyncio.gather(*(w.complete(Request(prompt=str(i))) for i in range(6)))
    out = asyncio.run(go())
    assert [c.text for c in out] == ["ok"] * 6          # every call answered, none failed
    assert window.ok == 6 and window.calls == 8           # 2 blocked probes, no flood of failing calls
    assert events[0] == ("claude", True) and events[-1] == ("claude", False)   # paused -> resumed


def test_other_subscriptions_keep_running_while_one_waits():
    limits = Limits(RunSettings(quota_probe_min=0.02))    # claude waits ~1.2 s
    claude, gpt = _Window(blocked=1), _Window(blocked=0)
    w_claude = limits.wrap(ModelSpec(name="opus", provider="cli", command=("claude",)), claude)
    w_gpt = limits.wrap(ModelSpec(name="sol", provider="cli", command=("codex",)), gpt)
    finished = []

    async def call(w, tag):
        await w.complete(Request(prompt="p"))
        finished.append(tag)

    async def go():
        await asyncio.gather(call(w_claude, "claude"), *(call(w_gpt, "gpt") for _ in range(3)))
    asyncio.run(go())
    assert finished[:3] == ["gpt"] * 3 and finished[-1] == "claude"   # gpt didn't wait for claude


def test_waiting_holds_no_concurrency_slot():
    limits = Limits(RunSettings(concurrency=1, quota_probe_min=0.02))
    blocked, free = _Window(blocked=1), _Window(blocked=0)
    w_b = limits.wrap(ModelSpec(name="opus", provider="cli", command=("claude",)), blocked)
    w_f = limits.wrap(ModelSpec(name="sol", provider="cli", command=("codex",)), free)

    async def go():
        t = asyncio.create_task(w_b.complete(Request(prompt="p")))
        await asyncio.sleep(0.05)                       # claude is now paused, waiting for its reset
        await asyncio.wait_for(w_f.complete(Request(prompt="p")), timeout=0.5)  # the ONE global slot is free
        await t
    asyncio.run(go())


def test_reset_time_from_the_message_is_used(monkeypatch):
    now = [1_000_000.0]
    seen = []
    limits = Limits(RunSettings(), on_quota_change=lambda s, until, why: seen.append(until), clock=lambda: now[0])
    limits.close("openai", "You've hit your usage limit ... try again in 2h 30m")
    assert seen[0] == pytest.approx(now[0] + 2.5 * 3600 + 60)     # +60 s safety margin


def test_gives_up_after_quota_max_wait():
    limits = Limits(RunSettings(quota_probe_min=0.002, quota_max_wait_h=0.00005))   # ~0.18 s budget
    window = _Window(blocked=10_000)
    w = limits.wrap(ModelSpec(name="opus", provider="cli", command=("claude",)), window)
    with pytest.raises(ProviderError) as ei:
        asyncio.run(asyncio.wait_for(w.complete(Request(prompt="p")), timeout=5))
    assert ei.value.quota and window.calls < 50    # stopped, didn't hammer the vendor


def test_cancel_while_waiting_or_probing_leaves_no_stuck_lock():
    limits = Limits(RunSettings(quota_probe_min=10))   # would wait 10 minutes
    w = limits.wrap(ModelSpec(name="opus", provider="cli", command=("claude",)), _Window(blocked=1))

    async def go():
        t = asyncio.create_task(w.complete(Request(prompt="p")))
        await asyncio.sleep(0.05)
        t.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t
        assert not limits.gate("claude").probe.locked()
    asyncio.run(go())


def test_bad_on_quota_value_is_rejected():
    with pytest.raises(ValueError, match="on_quota"):
        RunSettings(on_quota="maybe")
