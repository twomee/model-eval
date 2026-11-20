from __future__ import annotations

import asyncio
import json

import pytest

from evalkit.config import RunSettings, Task
from evalkit.graders import (GradingContext, _parse_judge, extract_go, extract_python, grade,
                             grade_language, grade_length, grade_match)

CTX = GradingContext(RunSettings(code_timeout_s=30), judges=[])


def run(coro):
    return asyncio.run(coro)


def make_task(graders, reference="ref"):
    return Task(id="t", category="c", prompt="p", graders=graders, reference=reference)


# ---------------------------------------------------------------- extraction

def test_extract_python_prefers_longest_block():
    ans = "```python\ndef solve():\n    return 42\n```\nUsage:\n```python\nsolve()\n```"
    assert "return 42" in extract_python(ans)


def test_extract_python_none_without_block():
    assert extract_python("just prose") is None


def test_extract_go_prefers_package_solution():
    ans = "```go\npackage main\nfunc main() { /* a long example ................................ */ }\n```\n" \
          "```go\npackage solution\nfunc F() {}\n```"
    assert "package solution" in extract_go(ans)


# ---------------------------------------------------------------- deterministic graders

def test_match_partial_credit():
    r = run(grade_match(None, "ANSWER: 694 and a race", {"must_include": ["ANSWER: 694", "mutex"]}, CTX))
    assert r.score == 0.5


def test_match_must_not_include():
    r = run(grade_match(None, "I am not sure", {"must_not_include": ["not sure"]}, CTX))
    assert r.score == 0.0


@pytest.mark.parametrize("words,score", [(50, 1.0), (100, 1.0), (150, 0.5), (250, 0.0)])
def test_length_decays_past_limit(words, score):
    r = run(grade_length(None, "w " * words, {"max_words": 100}, CTX))
    assert r.score == pytest.approx(score)


@pytest.mark.parametrize("text,ok", [
    ("We rolled back the deploy in 29 minutes.", False),
    ("היה אירוע בגלל שינוי ב-config של ה-connection pool ל-DB. עשינו rollback תוך 29 דקות.", True),
    ("התיקון:\n```python\ndef paginate(items, page): return items\n```\nזה פותר את הבעיה.", True),
])
def test_language_he(text, ok):
    r = run(grade_language(None, text, {"expected": "he"}, CTX))
    assert (r.score == 1.0) is ok


# ---------------------------------------------------------------- combination rules

def test_constraints_multiply_not_average():
    """An empty answer satisfies a length limit; it must NOT earn points for that."""
    task = make_task([{"type": "match", "must_include": ["needle"]}, {"type": "length", "max_words": 100}])
    score, _ = run(grade(task, "", CTX))
    assert score == 0.0


def test_weighted_mean_of_quality_graders():
    task = make_task([{"type": "match", "must_include": ["a"], "weight": 3},
                      {"type": "match", "must_include": ["zzz"], "weight": 1}])
    score, _ = run(grade(task, "a", CTX))
    assert score == pytest.approx(0.75)


def test_rubric_without_judges_is_excluded_not_zero():
    task = make_task([{"type": "match", "must_include": ["a"]},
                      {"type": "rubric", "criteria": [{"id": "x", "description": "d"}]}])
    score, parts = run(grade(task, "a", CTX))
    assert score == 1.0 and parts[1].score is None


def test_python_tests_partial_and_broken():
    tests = "from solution import f\ndef test_a(): assert f(1) == 2\ndef test_b(): assert f(2) == 4\n"
    task = make_task([{"type": "python_tests", "tests": tests}])
    half = "```python\ndef f(x):\n    return 2 if x == 1 else 0\n```"
    assert run(grade(task, half, CTX))[0] == pytest.approx(0.5)
    assert run(grade(task, "no code here", CTX))[0] == 0.0


# ---------------------------------------------------------------- judge parsing

def test_parse_judge_tolerates_prose_and_clamps():
    text = 'Sure! Here is my evaluation:\n' + json.dumps(
        {"criteria": [{"id": "a", "reason": "good", "score": 14}, {"id": "b", "reason": "meh", "score": 3}]}) + "\nDone."
    parsed = _parse_judge(text, ["a", "b"])
    assert parsed["a"]["score"] == 10 and parsed["b"]["score"] == 3


def test_parse_judge_rejects_missing_criterion():
    with pytest.raises(ValueError):
        _parse_judge(json.dumps({"criteria": [{"id": "a", "score": 5}]}), ["a", "b"])
