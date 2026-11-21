"""Graders: turn a model's answer into a score in [0, 1].

Rule of thumb for choosing a grader (cheapest + most objective first):
  1. Can you execute it?           -> python_tests / go_tests   (ground truth)
  2. Is there a checkable fact?    -> match / length / language (deterministic)
  3. Only judgment can tell?       -> rubric         (LLM-as-judge; noisy, biased, but scales)
Combine them: a debugging task can be scored by tests on the fix AND a rubric on the explanation.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import procs
from .config import ModelSpec, RunSettings, Task
from .providers import Provider, ProviderError, Request, is_quota_message


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


# --------------------------------------------------------------------------- sandbox env

# What model-written code may see. Your shell environment holds API keys and tokens; code a
# model wrote must not be able to read them (and print them into an answer that gets sent on).
_PASSTHROUGH = ("PATH", "LANG", "LC_ALL", "TZ", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT")


def sandbox_env(workdir: str) -> dict[str, str]:
    env = {k: os.environ[k] for k in _PASSTHROUGH if k in os.environ}
    env.update(HOME=workdir, TMPDIR=workdir, TEMP=workdir, TMP=workdir, PYTHONDONTWRITEBYTECODE="1")
    return env


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
        try:
            _, out, _ = await procs.run(
                [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--tb=line", "test_solution.py"],
                cwd=d, env=sandbox_env(d), stderr=asyncio.subprocess.STDOUT, timeout=ctx.settings.code_timeout_s)
        except asyncio.TimeoutError:  # procs.run killed pytest and anything the code spawned
            return GradeResult("python_tests", 0.0, details={"error": "timeout (infinite loop?)"})
    text = out.decode(errors="replace")
    counts = _pytest_counts(text)
    total = sum(counts.values())
    score = counts.get("passed", 0) / total if total else 0.0
    return GradeResult("python_tests", score, details={"counts": counts, "output_tail": text[-800:]})


def _pytest_counts(text: str) -> dict[str, int]:
    return {k: int(n) for n, k in re.findall(r"(\d+) (passed|failed|error)", text)}


# --------------------------------------------------------------------------- answer_tests

async def grade_answer_tests(task: Task, answer: str, cfg: dict, ctx: GradingContext) -> GradeResult:
    """pytest tests over the RAW answer text (answer.md in the test's working dir).

    For checks that aren't "run the code": structured output ("exactly this JSON"), format
    rules, required facts. Tests read it with: ANSWER = Path("answer.md").read_text()
    """
    with tempfile.TemporaryDirectory(prefix="evalkit-answer-") as d:
        Path(d, "answer.md").write_text(answer, encoding="utf-8")
        Path(d, "test_answer.py").write_text(cfg["tests"], encoding="utf-8")
        try:
            _, out, _ = await procs.run(
                [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--tb=line", "test_answer.py"],
                cwd=d, env=sandbox_env(d), stderr=asyncio.subprocess.STDOUT, timeout=ctx.settings.code_timeout_s)
        except asyncio.TimeoutError:
            return GradeResult("answer_tests", 0.0, details={"error": "timeout"})
    text = out.decode(errors="replace")
    counts = _pytest_counts(text)
    total = sum(counts.values())
    return GradeResult("answer_tests", counts.get("passed", 0) / total if total else 0.0,
                       details={"counts": counts, "output_tail": text[-800:]})


# --------------------------------------------------------------------------- node_tests

_TS_BLOCK = re.compile(r"```(?:ts|typescript|js|javascript|tsx)?[ \t]*\n(.*?)```", re.S)


def extract_ts(answer: str) -> str | None:
    # Prefer a block that exports something (the module), else the longest block.
    blocks = [b for b in _TS_BLOCK.findall(answer) if b.strip()]
    exporting = [b for b in blocks if re.search(r"^\s*export\b", b, re.M)]
    return max(exporting or blocks, key=len) if blocks else None


async def grade_node_tests(task: Task, answer: str, cfg: dict, ctx: GradingContext) -> GradeResult:
    """TypeScript/JavaScript via Node's built-in runner (`node --test`; Node >= 22.6 strips types)."""
    if shutil.which("node") is None:
        return GradeResult("node_tests", None, details={"error": "node not installed"})  # not the model's fault
    code = extract_ts(answer)
    if code is None:
        return GradeResult("node_tests", 0.0, details={"error": "no ts/js code block"})
    with tempfile.TemporaryDirectory(prefix="evalkit-node-") as d:
        Path(d, "solution.ts").write_text(code, encoding="utf-8")
        Path(d, "solution.test.ts").write_text(cfg["tests"], encoding="utf-8")
        Path(d, "package.json").write_text('{"type": "module"}', encoding="utf-8")
        try:
            _, out, _ = await procs.run(["node", "--test", "solution.test.ts"], cwd=d, env=sandbox_env(d),
                                        stderr=asyncio.subprocess.STDOUT, timeout=ctx.settings.code_timeout_s)
        except asyncio.TimeoutError:
            return GradeResult("node_tests", 0.0, details={"error": "timeout (infinite loop?)"})
    text = out.decode(errors="replace")
    passed = int(m.group(1)) if (m := re.search(r"^# pass (\d+)", text, re.M)) else 0
    failed = int(m.group(1)) if (m := re.search(r"^# fail (\d+)", text, re.M)) else 0
    total = passed + failed
    if total == 0:  # syntax error in the module: the test file never got to run a test
        return GradeResult("node_tests", 0.0, details={"error": "no tests ran", "output_tail": text[-800:]})
    return GradeResult("node_tests", passed / total,
                       details={"counts": {"passed": passed, "failed": failed}, "output_tail": text[-800:]})


# --------------------------------------------------------------------------- go_tests

_GO_BLOCK = re.compile(r"```go[ \t]*\n(.*?)```", re.S)


def extract_go(answer: str) -> str | None:
    # Prefer the block that declares the package we compile; fall back to the longest.
    blocks = _GO_BLOCK.findall(answer)
    pkg = [b for b in blocks if re.search(r"^\s*package\s+solution\b", b, re.M)]
    return max(pkg or blocks, key=len) if blocks else None


async def grade_go_tests(task: Task, answer: str, cfg: dict, ctx: GradingContext) -> GradeResult:
    if shutil.which("go") is None:
        # Not the model's fault -> excluded from the mean, not scored 0.
        return GradeResult("go_tests", None, details={"error": "go toolchain not installed"})
    code = extract_go(answer)
    if code is None:
        return GradeResult("go_tests", 0.0, details={"error": "no go code block"})
    with tempfile.TemporaryDirectory(prefix="evalkit-go-") as d:
        Path(d, "go.mod").write_text("module solution\n\ngo 1.22\n", encoding="utf-8")
        Path(d, "solution.go").write_text(code, encoding="utf-8")
        Path(d, "solution_test.go").write_text(cfg["tests"], encoding="utf-8")
        args = ["go", "test", "-v", "-count=1", "-timeout", f"{int(ctx.settings.code_timeout_s)}s"]
        if cfg.get("race"):  # concurrency tasks: the race detector IS the test
            args.append("-race")
        env = {**sandbox_env(d), "GOFLAGS": "-mod=mod", "GOTOOLCHAIN": "local",
               # keep the build cache (speed) - paths only, no secrets
               "GOCACHE": os.environ.get("GOCACHE", str(Path.home() / ".cache" / "go-build")),
               "GOPATH": os.environ.get("GOPATH", str(Path.home() / "go")),
               **({"GOROOT": os.environ["GOROOT"]} if "GOROOT" in os.environ else {})}
        try:
            # compile time is real time: give go a bit more than the test timeout
            _, out, _ = await procs.run([*args, "."], cwd=d, env=env, stderr=asyncio.subprocess.STDOUT,
                                        timeout=ctx.settings.code_timeout_s + 60)
        except asyncio.TimeoutError:
            return GradeResult("go_tests", 0.0, details={"error": "timeout"})
    text = out.decode(errors="replace")
    # count top-level tests only (subtests are indented)
    passed = len(re.findall(r"^--- PASS", text, re.M))
    failed = len(re.findall(r"^--- FAIL", text, re.M))
    # Distinguish failure modes: "build failed" vs "deadlock" lead you to very different fixes.
    if "WARNING: DATA RACE" in text:
        error = "data race"
    elif "panic: test timed out" in text:
        error = "test timeout (deadlock or infinite loop?)"
    elif re.search(r"\[build failed\]|\[setup failed\]", text):
        error = "build failed"
    elif re.search(r"^panic:", text, re.M):
        error = "panic"
    else:
        error = None
    if error == "data race":  # a race is disqualifying even if assertions happened to pass
        return GradeResult("go_tests", 0.0, details={"error": error, "output_tail": text[-800:]})
    if error:  # the crashing test never reports --- FAIL; count it as one failure
        failed += 1
    total = passed + failed
    if total == 0:
        return GradeResult("go_tests", 0.0, details={"error": error or "no tests ran", "output_tail": text[-800:]})
    return GradeResult("go_tests", passed / total,
                       details={"counts": {"passed": passed, "failed": failed}, "error": error,
                                "output_tail": text[-800:]})


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
            except ProviderError as e:
                if e.quota:  # out of quota: retrying now is pointless; see `deferred` below
                    return spec.name, f"QUOTA: {e}"
                err = repr(e)
            except (ValueError, KeyError, json.JSONDecodeError) as e:
                err = repr(e)
        return spec.name, err

    results = await asyncio.gather(*(one(s, p) for s, p in ctx.judges))
    if out_of_quota := [n for n, r in results if isinstance(r, str) and r.startswith("QUOTA:")]:
        # Never grade with HALF the panel: one judge alone carries its family's self-preference,
        # and the answer would be scored differently from its neighbours. Leave it ungraded; a
        # later run (after the subscription resets) grades it with every judge.
        return GradeResult("rubric", None, details={"judges": dict(results),
                                                    "deferred": f"judge out of quota: {', '.join(out_of_quota)}"})
    per_judge, judge_scores = {}, []
    for name, res in results:
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


def incomplete_grade(record: dict[str, Any]) -> bool:
    """A stored grade whose judge panel was cut short by a quota error (the old behaviour scored
    it with the remaining judges). It must be regraded with the full panel, not counted."""
    for part in record.get("parts") or []:
        for result in ((part.get("details") or {}).get("judges") or {}).values():
            if isinstance(result, str) and (result.startswith("QUOTA:") or is_quota_message(result)):
                return True
    return False


CONSTRAINTS = {"length", "language"}

GRADERS = {
    "python_tests": grade_python_tests,
    "answer_tests": grade_answer_tests,
    "node_tests": grade_node_tests,
    "go_tests": grade_go_tests,
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
