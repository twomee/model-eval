"""Graders: turn a model's answer into a score in [0, 1].

Rule of thumb for choosing a grader (cheapest + most objective first):
  1. Can you execute it?           -> python_tests   (ground truth)
  2. Is there a checkable fact?    -> match / length / language (deterministic)
  3. Only judgment can tell?       -> rubric         (LLM-as-judge; noisy, biased, but scales)
Combine them: a debugging task can be scored by tests on the fix AND a rubric on the explanation.
"""
from __future__ import annotations

import asyncio
import json
import re
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import ModelSpec, RunSettings, Task
from .providers import Provider, ProviderError, Request


@dataclass
class GradeResult:
    grader: str
    score: float | None            # None = grader could not run (excluded from the mean)
    weight: float = 1.0
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class GradingContext:
    settings: RunSettings
    judges: list[tuple[ModelSpec, Provider]]


# --------------------------------------------------------------------------- python_tests

_CODE_BLOCK = re.compile(r"```(?:python|py)?[ \t]*\n(.*?)```", re.S)


def extract_python(answer: str) -> str | None:
    blocks = _CODE_BLOCK.findall(answer)
    # Longest block heuristic: models often add a short usage example after the solution.
    return max(blocks, key=len) if blocks else None


async def grade_python_tests(task: Task, answer: str, cfg: dict, ctx: GradingContext) -> GradeResult:
    code = extract_python(answer)
    if code is None:
        return GradeResult("python_tests", 0.0, details={"error": "no python code block"})
    # SECURITY: this executes model-written code on your machine. Subprocess + timeout
    # limits accidents, not malice. For untrusted models, run evalkit inside Docker.
    with tempfile.TemporaryDirectory(prefix="evalkit-tests-") as d:
        Path(d, "solution.py").write_text(code, encoding="utf-8")
        Path(d, "test_solution.py").write_text(cfg["tests"], encoding="utf-8")
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--tb=line", "test_solution.py",
            cwd=d, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=ctx.settings.code_timeout_s)
        except asyncio.TimeoutError:
            proc.kill()
            return GradeResult("python_tests", 0.0, details={"error": "timeout (infinite loop?)"})
    text = out.decode(errors="replace")
    counts = {k: int(n) for n, k in re.findall(r"(\d+) (passed|failed|error)", text)}
    total = sum(counts.values())
    score = counts.get("passed", 0) / total if total else 0.0
    return GradeResult("python_tests", score, details={"counts": counts, "output_tail": text[-800:]})


# --------------------------------------------------------------------------- match / length

async def grade_match(task: Task, answer: str, cfg: dict, ctx: GradingContext) -> GradeResult:
    checks = [(p, True) for p in cfg.get("must_include", [])] + \
             [(p, False) for p in cfg.get("must_not_include", [])]
    results = {p: bool(re.search(p, answer, re.I | re.S)) == want for p, want in checks}
    score = sum(results.values()) / len(results) if results else None
    return GradeResult("match", score, details={"checks": results})


async def grade_length(task: Task, answer: str, cfg: dict, ctx: GradingContext) -> GradeResult:
    words = len(answer.split())
    lo, hi = cfg.get("min_words", 0), cfg.get("max_words", 10**9)
    if lo <= words <= hi:
        score = 1.0
    else:  # partial credit that decays with distance from the allowed range
        gap = (lo - words) if words < lo else (words - hi)
        score = max(0.0, 1 - gap / max(hi if words > hi else lo, 1))
    return GradeResult("length", score, details={"words": words, "range": [lo, hi]})


_SCRIPTS = {"he": re.compile(r"[א-ת]"), "en": re.compile(r"[A-Za-z]")}


async def grade_language(task: Task, answer: str, cfg: dict, ctx: GradingContext) -> GradeResult:
    """Answer must be mostly in the expected language. Technical terms in English are
    normal in Hebrew text, hence a ratio threshold rather than 'no Latin at all'."""
    expected, min_ratio = cfg["expected"], cfg.get("min_ratio", 0.6)
    prose = _CODE_BLOCK.sub("", re.sub(r"`[^`]*`", "", answer))  # code doesn't count
    # Count WORDS, not letters: Hebrew is written without vowels, so a letter count
    # under-weights Hebrew against English terms of the same "size".
    words = prose.split()
    counts = {lang: sum(1 for w in words if rx.search(w)) for lang, rx in _SCRIPTS.items()}
    total = sum(counts.values())
    ratio = counts[expected] / total if total else 0.0
    score = 1.0 if ratio >= min_ratio else ratio / min_ratio
    return GradeResult("language", score, details={"expected": expected, "ratio": round(ratio, 3)})


# --------------------------------------------------------------------------- rubric (LLM-as-judge)

JUDGE_SYSTEM = """You are a strict, impartial evaluator. You grade ONE candidate answer against a rubric.
Rules:
- Judge substance, not style. Do NOT reward length; padding and repetition are flaws.
- The answer may be in Hebrew or English; judge it in its own language.
- A reference answer, if given, shows what "good" looks like; equivalent alternatives deserve full credit.
- For each criterion, write a one-sentence reason FIRST, then an integer score 0-10.
Return ONLY JSON: {"criteria": [{"id": "<id>", "reason": "<why>", "score": <0-10>}]}"""


def _judge_prompt(task: Task, answer: str, criteria: list[dict]) -> str:
    rubric = "\n".join(f"- id={c['id']}: {c['description']}" for c in criteria)
    ref = f"\n\n<reference_answer>\n{task.reference}\n</reference_answer>" if task.reference else ""
    return (f"<task>\n{task.prompt}\n</task>{ref}\n\n<rubric>\n{rubric}\n</rubric>\n\n"
            f"<candidate_answer>\n{answer}\n</candidate_answer>")


def _parse_judge(text: str, ids: list[str]) -> dict[str, dict]:
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < 0:
        raise ValueError(f"no JSON in judge output: {text[:200]}")
    items = json.loads(text[start:end + 1])["criteria"]
    parsed = {i["id"]: {"score": max(0, min(10, float(i["score"]))), "reason": i.get("reason", "")}
              for i in items if i.get("id") in ids}
    if missing := set(ids) - parsed.keys():
        raise ValueError(f"judge skipped criteria {missing}")
    return parsed


async def grade_rubric(task: Task, answer: str, cfg: dict, ctx: GradingContext) -> GradeResult:
    if not ctx.judges:
        return GradeResult("rubric", None, details={"error": "no judges configured"})
    criteria = cfg["criteria"]
    ids = [c["id"] for c in criteria]
    weights = {c["id"]: c.get("weight", 1.0) for c in criteria}
    req = Request(prompt=_judge_prompt(task, answer, criteria), system=JUDGE_SYSTEM,
                  max_tokens=2000, temperature=0.0,
                  meta={"criteria": ids, "candidate": answer, "reference": task.reference})

    async def one(spec: ModelSpec, provider: Provider) -> tuple[str, dict | str]:
        for _ in range(2):  # one retry on malformed JSON
            try:
                comp = await provider.complete(req)
                return spec.name, _parse_judge(comp.text, ids)
            except (ValueError, KeyError, json.JSONDecodeError, ProviderError) as e:
                err = repr(e)
        return spec.name, err

    per_judge, judge_scores = {}, []
    for name, res in await asyncio.gather(*(one(s, p) for s, p in ctx.judges)):
        per_judge[name] = res
        if isinstance(res, dict):
            total_w = sum(weights.values())
            judge_scores.append(sum(res[i]["score"] * weights[i] for i in ids) / (10 * total_w))
    if not judge_scores:
        return GradeResult("rubric", None, details={"judges": per_judge, "error": "all judges failed"})
    score = sum(judge_scores) / len(judge_scores)
    # Disagreement between judges is a signal: high spread = vague rubric, fix the rubric.
    spread = max(judge_scores) - min(judge_scores)
    return GradeResult("rubric", score, details={"judges": per_judge, "judge_spread": round(spread, 3)})


CONSTRAINTS = {"length", "language"}

GRADERS = {
    "python_tests": grade_python_tests,
    "match": grade_match,
    "length": grade_length,
    "language": grade_language,
    "rubric": grade_rubric,
}


async def grade(task: Task, answer: str, ctx: GradingContext) -> tuple[float | None, list[GradeResult]]:
    results = []
    for cfg in task.graders:
        fn = GRADERS.get(cfg["type"])
        if fn is None:
            raise ValueError(f"task {task.id}: unknown grader {cfg['type']!r}")
        r = await fn(task, answer, cfg, ctx)
        r.weight = cfg.get("weight", 1.0)
        results.append(r)
    # Quality graders are averaged; CONSTRAINTS (length) multiply. Otherwise an empty
    # answer "passes" the length check and collects free points.
    quality = [r for r in results if r.score is not None and r.grader not in CONSTRAINTS]
    constraints = [r for r in results if r.score is not None and r.grader in CONSTRAINTS]
    if not quality:
        return None, results
    total_w = sum(r.weight for r in quality)
    score = sum(r.score * r.weight for r in quality) / total_w
    for c in constraints:
        score *= c.score
    return score, results
