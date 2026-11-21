"""Compare every model against every other: who is best for YOU, who is best in general,
and how sure can we be?

Two rankings from the same answers:
  * for you    - category means weighted by your `category_weights`
  * in general - every category weighted equally (a neutral view; closer to a public benchmark)

"Is A better than B?" is answered with a PAIRED bootstrap: each round draws one resample of
the tasks and scores EVERY model on that same resample. P(A beats B) = share of rounds where
A's score is higher. Pairing matters: two models can have overlapping confidence intervals
while A beats B on almost every task. Comparing intervals would call that a tie; pairing doesn't.

Verdict thresholds (one-sided probability that #1 beats a rival):
  >= 0.90 clear lead · >= 0.75 likely lead · below that: a statistical tie -> decide on cost/latency.
"""
from __future__ import annotations

import random
from collections import defaultdict
from dataclasses import dataclass, field
from itertools import combinations

CLEAR, LIKELY = 0.90, 0.75


@dataclass
class RankedModel:
    name: str
    score: float
    ci90: tuple[float, float]
    n_tasks: int


@dataclass
class Ranking:
    label: str                      # "for you" | "in general"
    weights: dict[str, float]
    models: list[RankedModel]
    p_beats: dict[tuple[str, str], float]   # (a, b) -> P(a scores higher than b)
    verdict: str
    leaders: list[str]              # #1 plus every model it doesn't clearly beat


@dataclass
class Comparison:
    for_you: Ranking
    general: Ranking
    notes: list[str] = field(default_factory=list)


def weighted_score(task_scores: dict[str, float], task_cat: dict[str, str], weights: dict[str, float]) -> float:
    by_cat: dict[str, list[float]] = defaultdict(list)
    for t, s in task_scores.items():
        by_cat[task_cat[t]].append(s)
    num = sum(weights.get(c, 1.0) * sum(v) / len(v) for c, v in by_cat.items())
    den = sum(weights.get(c, 1.0) for c in by_cat)
    return num / den if den else 0.0


def _resampled_scores(means: dict[str, dict[str, float]], task_cat: dict[str, str], weights: dict[str, float],
                      rounds: int, seed: int) -> dict[str, list[float | None]]:
    """For each round, one shared resample of the task universe; every model is scored on it
    (on the drawn tasks it has). None = the model had none of the drawn tasks this round."""
    rng = random.Random(seed)
    universe = sorted({t for m in means.values() for t in m})
    out: dict[str, list[float | None]] = {m: [] for m in means}
    for _ in range(rounds):
        picks = [rng.choice(universe) for _ in universe]
        for model, scores in means.items():
            sums: dict[str, float] = defaultdict(float)
            counts: dict[str, int] = defaultdict(int)
            for t in picks:
                if t in scores:
                    sums[task_cat[t]] += scores[t]
                    counts[task_cat[t]] += 1
            if not counts:
                out[model].append(None)
                continue
            num = sum(weights.get(c, 1.0) * sums[c] / counts[c] for c in counts)
            out[model].append(num / sum(weights.get(c, 1.0) for c in counts))
    return out


def _ranking(label: str, means: dict[str, dict[str, float]], task_cat: dict[str, str],
             weights: dict[str, float], rounds: int, seed: int) -> Ranking:
    samples = _resampled_scores(means, task_cat, weights, rounds, seed)
    ranked = []
    for model, scores in means.items():
        vals = sorted(v for v in samples[model] if v is not None)
        ci = (vals[int(0.05 * len(vals))], vals[min(len(vals) - 1, int(0.95 * len(vals)))]) if vals else (0.0, 0.0)
        ranked.append(RankedModel(model, weighted_score(scores, task_cat, weights), ci, len(scores)))
    ranked.sort(key=lambda r: (-r.score, r.name))

    p_beats: dict[tuple[str, str], float] = {}
    for a, b in combinations([r.name for r in ranked], 2):
        both = [(x, y) for x, y in zip(samples[a], samples[b]) if x is not None and y is not None]
        if not both:
            continue
        wins = sum(1.0 if x > y else 0.5 if x == y else 0.0 for x, y in both) / len(both)
        p_beats[(a, b)], p_beats[(b, a)] = wins, 1.0 - wins

    verdict, leaders = _verdict(ranked, p_beats)
    return Ranking(label, dict(weights), ranked, p_beats, verdict, leaders)


def _verdict(ranked: list[RankedModel], p: dict[tuple[str, str], float]) -> tuple[str, list[str]]:
    if not ranked:
        return "No graded results yet.", []
    top = ranked[0]
    if len(ranked) == 1:
        return f"Only {top.name} has results: nothing to compare against.", [top.name]
    # The leading group = #1 plus every model #1 doesn't beat with at least LIKELY confidence.
    leaders = [top.name] + [r.name for r in ranked[1:] if p.get((top.name, r.name), 0.5) < LIKELY]
    runner_up = ranked[1]
    p2 = p.get((top.name, runner_up.name), 0.5)
    if len(leaders) == 1:
        strength = "Clear" if p2 >= CLEAR else "Likely"
        return (f"{strength} winner: {top.name} ({top.score:.2f}) beats the runner-up {runner_up.name} "
                f"({runner_up.score:.2f}) in {p2:.0%} of resamples."), leaders
    others = ", ".join(leaders[1:])
    return (f"Statistical tie at the top: {top.name} ({top.score:.2f}) can't be separated from {others} "
            f"(beats {runner_up.name} in only {p2:.0%} of resamples). Decide between them on cost, latency and "
            "consistency, or add tasks where they differ."), leaders


def compare(means: dict[str, dict[str, float]], task_cat: dict[str, str], weights: dict[str, float],
            rounds: int = 1000, seed: int = 7) -> Comparison:
    """means: model -> {task_id: mean score over reps}. task_cat: task_id -> category."""
    means = {m: s for m, s in means.items() if s}
    notes = []
    task_sets = {frozenset(s) for s in means.values()}
    if len(task_sets) > 1:
        notes.append("Not every model has a score on every task (errors, skipped graders, partial runs). Each is "
                     "ranked on the tasks it has; for a strict comparison, run the same tasks for all of them.")
    if means and len({t for s in means.values() for t in s}) < 10:
        notes.append("Fewer than 10 tasks: rankings are fragile. Treat this as a smoke test, not a decision.")
    return Comparison(
        for_you=_ranking("for you", means, task_cat, weights, rounds, seed),
        general=_ranking("in general", means, task_cat, {}, rounds, seed + 1),
        notes=notes)
