"""Model comparison: rankings for you vs in general, paired head-to-head, verdicts."""
from __future__ import annotations

import pytest

from evalkit.compare import compare, weighted_score

CAT = {f"code{i}": "backend_code" for i in range(10)} | {f"he{i}": "hebrew_text" for i in range(10)}


def test_for_you_and_in_general_can_disagree():
    # A is great at code (weight 3 for this user), B is great at Hebrew.
    means = {
        "A": {**{f"code{i}": 0.9 for i in range(10)}, **{f"he{i}": 0.3 for i in range(10)}},
        "B": {**{f"code{i}": 0.5 for i in range(10)}, **{f"he{i}": 0.9 for i in range(10)}},
    }
    c = compare(means, CAT, {"backend_code": 3, "hebrew_text": 1})
    assert c.for_you.models[0].name == "A"     # 0.75 vs 0.6
    assert c.general.models[0].name == "B"     # 0.7 vs 0.6
    assert c.for_you.models[0].score == pytest.approx(weighted_score(means["A"], CAT, {"backend_code": 3, "hebrew_text": 1}))


def test_paired_comparison_sees_a_consistent_small_lead():
    # A is 0.03 better on EVERY task, but tasks vary a lot: the intervals overlap heavily,
    # yet A wins every paired resample. Comparing intervals would wrongly call this a tie.
    base = {t: (i % 10) / 10 for i, t in enumerate(CAT)}
    means = {"A": {t: s + 0.03 for t, s in base.items()}, "B": dict(base)}
    r = compare(means, CAT, {}).for_you
    a, b = r.models
    assert a.ci90[0] < b.ci90[1]                       # intervals overlap...
    assert r.p_beats[("A", "B")] == 1.0                # ...but A always wins head-to-head
    assert r.verdict.startswith("Clear winner: A") and r.leaders == ["A"]


def test_equal_models_are_a_statistical_tie():
    base = {t: (i % 7) / 7 for i, t in enumerate(CAT)}
    noisy = {t: s + (0.05 if i % 2 else -0.05) for i, (t, s) in enumerate(base.items())}
    r = compare({"A": base, "B": noisy, "C": {t: 0.1 for t in CAT}}, CAT, {}).for_you
    assert r.verdict.startswith("Statistical tie")
    assert set(r.leaders) == {"A", "B"} and "C" not in r.leaders   # C is clearly worse
    assert r.p_beats[("A", "B")] == pytest.approx(1 - r.p_beats[("B", "A")])


def test_partial_coverage_and_small_evals_are_flagged():
    c = compare({"A": {"code0": 1.0, "code1": 0.5}, "B": {"code0": 0.5}}, CAT, {})
    assert any("Not every model" in n for n in c.notes)
    assert any("Fewer than 10 tasks" in n for n in c.notes)


def test_degenerate_inputs():
    assert compare({}, CAT, {}).for_you.verdict == "No graded results yet."
    one = compare({"A": {"code0": 1.0}}, CAT, {}).for_you
    assert one.leaders == ["A"] and "nothing to compare" in one.verdict


def test_deterministic():
    means = {"A": {t: 0.6 for t in CAT}, "B": {t: (i % 3) / 3 for i, t in enumerate(CAT)}}
    assert compare(means, CAT, {}).for_you.p_beats == compare(means, CAT, {}).for_you.p_beats
