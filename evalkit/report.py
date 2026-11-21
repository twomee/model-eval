"""Aggregate grades into a decision-ready report (HTML + CSV).

Two statistical habits baked in here, because small evals lie easily:
  * Overall score = weighted mean of CATEGORY means, not of all tasks.
    Otherwise whichever category you wrote the most tasks for silently dominates.
  * A bootstrap confidence interval over tasks. If two models' intervals overlap
    heavily, your eval can't tell them apart yet -> add tasks, don't crown a winner.
"""
from __future__ import annotations

import csv
import html
import random
import statistics as st
from collections import defaultdict
from pathlib import Path
from typing import Any

from .compare import Comparison, Ranking, compare
from .config import Config, Task
from .runner import read_jsonl


def _latest(records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for r in records:  # later lines win (re-runs, re-grades)
        if r.get("error") and r["key"] in out:
            continue
        out[r["key"]] = r
    return out


def _pct(xs: list[float], q: float) -> float | None:
    if not xs:
        return None
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * len(xs)))]


def _weighted_overall(task_means: dict[str, float], task_cat: dict[str, str], weights: dict[str, float]) -> float:
    by_cat: dict[str, list[float]] = defaultdict(list)
    for t, s in task_means.items():
        by_cat[task_cat[t]].append(s)
    num = sum(weights.get(c, 1.0) * st.mean(v) for c, v in by_cat.items())
    den = sum(weights.get(c, 1.0) for c in by_cat)
    return num / den if den else 0.0


def _bootstrap_ci(task_means: dict[str, float], task_cat: dict[str, str], weights: dict[str, float],
                  n: int = 2000, seed: int = 7) -> tuple[float, float]:
    rng, tasks = random.Random(seed), list(task_means)
    samples = []
    for _ in range(n):
        pick = [rng.choice(tasks) for _ in tasks]
        # keep duplicates distinct so resampling actually reweights tasks
        tm = {f"{t}#{i}": task_means[t] for i, t in enumerate(pick)}
        tc = {f"{t}#{i}": task_cat[t] for i, t in enumerate(pick)}
        samples.append(_weighted_overall(tm, tc, weights))
    samples.sort()
    return samples[int(0.05 * n)], samples[int(0.95 * n)]


def _current(records: dict[str, dict[str, Any]], tasks: list[Task] | None) -> dict[str, dict[str, Any]]:
    """Keep only results for the CURRENT version of each task. Keys are model|task|fingerprint|rep:
    an edited task has a new fingerprint, so its old answers (graded against the old prompt or
    tests) must not count, and removed tasks must disappear from the report."""
    if tasks is None:
        return records
    fingerprints = {t.id: t.fingerprint for t in tasks}
    return {k: r for k, r in records.items() if fingerprints.get(r["task_id"]) == k.split("|")[2]}


def build_summary(cfg: Config, run_dir: Path, tasks: list[Task] | None = None) -> dict[str, Any]:
    gens = _current(_latest(read_jsonl(run_dir / "generations.jsonl")), tasks)
    grades = _current(_latest(read_jsonl(run_dir / "grades.jsonl")), tasks)
    task_cat = {g["task_id"]: g["category"] for g in gens.values()}
    categories = sorted(set(task_cat.values()))
    # pair id -> {lang: task_id}, for the Hebrew-vs-English comparison
    pairs: dict[str, dict[str, str]] = defaultdict(dict)
    for t in tasks or []:
        if t.pair:
            pairs[t.pair][t.lang] = t.id

    per_model: dict[str, dict[str, Any]] = {}
    means_by_model: dict[str, dict[str, float]] = {}
    matrix: dict[str, dict[str, float]] = defaultdict(dict)   # task -> model -> mean score
    for spec in cfg.models:
        m = spec.name
        g_ok = [g for g in gens.values() if g["model"] == m and not g.get("error")]
        g_err = [g for g in gens.values() if g["model"] == m and g.get("error")]
        if not g_ok and not g_err:
            continue  # configured but not part of this run (e.g. a non-default effort)
        scores_by_task: dict[str, list[float]] = defaultdict(list)
        for r in grades.values():
            if r["model"] == m and r["score"] is not None:
                scores_by_task[r["task_id"]].append(r["score"])
        if not scores_by_task:
            per_model[m] = {"name": m, "provider": spec.provider, "empty": True, "errors": len(g_err)}
            continue
        task_means = {t: st.mean(v) for t, v in scores_by_task.items()}
        means_by_model[m] = task_means
        for t, s in task_means.items():
            matrix[t][m] = s
        cat_scores = {}
        for c in categories:
            vals = [s for t, s in task_means.items() if task_cat[t] == c]
            cat_scores[c] = st.mean(vals) if vals else None
        spreads = [st.pstdev(v) for v in scores_by_task.values() if len(v) > 1]
        lat = [g["latency_s"] for g in g_ok if g.get("latency_s")]
        costs = [g["cost_usd"] for g in g_ok if g.get("cost_usd") is not None]
        out_tok = [g["output_tokens"] for g in g_ok if g.get("output_tokens")]
        lo, hi = _bootstrap_ci(task_means, task_cat, cfg.category_weights)
        deltas = [task_means[p['he']] - task_means[p['en']] for p in pairs.values()
                  if p.get('he') in task_means and p.get('en') in task_means]
        per_model[m] = {
            "name": m, "provider": spec.provider, "empty": False,
            "overall": _weighted_overall(task_means, task_cat, cfg.category_weights),
            "ci90": (lo, hi), "categories": cat_scores,
            "n_tasks": len(task_means), "errors": len(g_err),
            "inconsistency": st.mean(spreads) if spreads else None,
            "latency_p50": _pct(lat, 0.5), "latency_p95": _pct(lat, 0.95),
            "avg_output_tokens": st.mean(out_tok) if out_tok else None,
            "total_cost": sum(costs) if costs else None,
            "he_vs_en": st.mean(deltas) if deltas else None, "n_pairs": len(deltas),
        }
    ranked = sorted((p for p in per_model.values() if not p["empty"]), key=lambda p: -p["overall"])
    ranked += [p for p in per_model.values() if p["empty"]]
    return {"models": ranked, "categories": categories, "matrix": matrix, "has_pairs": bool(pairs),
            "gens": gens, "grades": grades, "weights": cfg.category_weights,
            "task_signal": task_signal(matrix),
            "comparison": compare(means_by_model, task_cat, cfg.category_weights)}


EASY, BROKEN = 0.95, 0.05


def task_signal(matrix: dict[str, dict[str, float]], min_models: int = 2) -> list[dict[str, Any]]:
    """Which tasks actually separate the models? The README rule, enforced: "a good task is one a
    weak model fails". All models >= 0.95 -> the task adds nothing to the decision. All <= 0.05 ->
    more likely a broken task (or reference) than a hard one: check it before blaming the models."""
    out = []
    for task, by_model in sorted(matrix.items()):
        scores = list(by_model.values())
        if len(scores) < min_models:
            continue
        lo, hi = min(scores), max(scores)
        verdict = "too easy" if lo >= EASY else "nobody passes" if hi <= BROKEN else "separates"
        out.append({"task": task, "min": lo, "max": hi, "spread": hi - lo, "n_models": len(scores),
                    "verdict": verdict})
    return out


def _fmt(x: float | None, kind: str = "score") -> str:
    if x is None:
        return "–"
    return {"score": f"{x:.2f}", "s": f"{x:.1f}s", "usd": f"${x:.3f}", "int": f"{x:.0f}"}[kind]


def _cell(x: float | None) -> str:
    if x is None:
        return "<td class='na'>–</td>"
    hue = int(120 * x)  # 0 = red, 120 = green
    return f"<td style='background:hsl({hue} 60% 88%)'>{x:.2f}</td>"


def _delta_cell(d: float | None, n: int) -> str:
    if d is None:
        return "<td class='na'>–</td>"
    # within +/-0.05 is noise at this sample size; color only real gaps
    color = "#c62828" if d < -0.05 else "#2e7d32" if d > 0.05 else "#666"
    return f"<td style='color:{color}'><b>{d:+.2f}</b><br><small>{n} pairs</small></td>"


def write_csv(summary: dict[str, Any], path: Path) -> None:
    cats = summary["categories"]
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["model", "provider", "overall", "ci90_low", "ci90_high", *cats, "n_tasks", "errors",
                    "he_vs_en", "inconsistency", "latency_p50_s", "latency_p95_s", "avg_output_tokens", "total_cost_usd"])
        for p in summary["models"]:
            if p["empty"]:
                w.writerow([p["name"], p["provider"]] + [""] * (len(cats) + 11))
                continue
            w.writerow([p["name"], p["provider"], round(p["overall"], 4), round(p["ci90"][0], 4),
                        round(p["ci90"][1], 4), *[None if p["categories"][c] is None else round(p["categories"][c], 4) for c in cats],
                        p["n_tasks"], p["errors"], p["he_vs_en"], p["inconsistency"], p["latency_p50"], p["latency_p95"],
                        p["avg_output_tokens"], p["total_cost"]])


def write_html(summary: dict[str, Any], path: Path) -> None:
    e, cats, models = html.escape, summary["categories"], summary["models"]
    weights = summary["weights"]
    head = "".join(f"<th>{e(c)}<br><small>w={weights.get(c, 1.0)}</small></th>" for c in cats)
    he_head = "<th>HE vs EN<br><small>Δ score</small></th>" if summary["has_pairs"] else ""
    rows = []
    # Computed outside the f-string: nested same-type quotes are a SyntaxError before Python 3.12.
    empty_colspan = len(cats) + 7 + int(summary["has_pairs"])
    for i, p in enumerate(models, 1):
        if p["empty"]:
            rows.append(f"<tr><td>{i}</td><td>{e(p['name'])}</td><td colspan='{empty_colspan}' class='na'>"
                        f"no graded results (errors: {p['errors']})</td></tr>")
            continue
        lo, hi = p["ci90"]
        rows.append(
            f"<tr><td>{i}</td><td><b>{e(p['name'])}</b><br><small>{e(p['provider'])}</small></td>"
            f"{_cell(p['overall'])}<td><small>{lo:.2f}–{hi:.2f}</small></td>"
            + "".join(_cell(p["categories"][c]) for c in cats)
            + (_delta_cell(p["he_vs_en"], p["n_pairs"]) if summary["has_pairs"] else "")
            + f"<td>{_fmt(p['inconsistency'])}</td><td>{_fmt(p['latency_p50'], 's')} / {_fmt(p['latency_p95'], 's')}</td>"
              f"<td>{_fmt(p['avg_output_tokens'], 'int')}</td><td>{_fmt(p['total_cost'], 'usd')}</td>"
              f"<td>{p['errors']}</td></tr>")

    names = [p["name"] for p in models]
    task_rows = "".join(
        f"<tr><td>{e(t)}</td>" + "".join(_cell(summary["matrix"][t].get(n)) for n in names) + "</tr>"
        for t in sorted(summary["matrix"]))

    # Drill-down: first rep of every (task, model) with its grade breakdown.
    grades_by = {(g["model"], g["task_id"], g["rep"]): g for g in summary["grades"].values()}
    drill = []
    for t in sorted(summary["matrix"]):
        items = []
        for n in names:
            gen = next((g for g in summary["gens"].values()
                        if g["model"] == n and g["task_id"] == t and g["rep"] == 0 and not g.get("error")), None)
            if not gen:
                continue
            gr = grades_by.get((n, t, 0), {})
            parts = "; ".join(f"{p['grader']}={_fmt(p['score'])}" for p in gr.get("parts", []))
            judge_notes = ""
            for p in gr.get("parts", []):
                for jn, jr in (p.get("details", {}).get("judges") or {}).items():
                    if isinstance(jr, dict):
                        judge_notes += f"<div class='j'><b>{e(jn)}</b>: " + " | ".join(
                            f"{e(cid)} {v['score']:.0f}/10 – {e(v['reason'])}" for cid, v in jr.items()) + "</div>"
            items.append(f"<details><summary>{e(n)} — {_fmt(gr.get('score'))} <small>({e(parts)})</small></summary>"
                         f"{judge_notes}<pre dir='auto'>{e(gen['output'])}</pre></details>")
        drill.append(f"<h3>{e(t)}</h3>{''.join(items)}")

    comparison_html = _comparison_html(summary["comparison"]) if summary.get("comparison") else ""
    signal = summary.get("task_signal") or []
    flagged = [s for s in signal if s["verdict"] != "separates"]
    signal_html = ""
    if signal:
        rows_s = "".join(
            f"<tr><td>{e(s['task'])}</td><td>{s['min']:.2f}</td><td>{s['max']:.2f}</td><td>{s['n_models']}</td>"
            f"<td class='{'na' if s['verdict'] == 'separates' else 'warn'}'>{s['verdict']}</td></tr>"
            for s in sorted(signal, key=lambda s: (s["verdict"] == "separates", s["spread"])))
        signal_html = (f"<h2>Task signal</h2><p class='note'>{len(flagged)} of {len(signal)} tasks don't separate "
                       "the models. <b>too easy</b> = every model scored &ge; 0.95: it adds nothing to the decision, "
                       "replace it with a harder one. <b>nobody passes</b> = every model &le; 0.05: check the task and "
                       "reference before blaming the models.</p><table><tr><th>task</th><th>min</th><th>max</th>"
                       f"<th>models</th><th>verdict</th></tr>{rows_s}</table>")

    doc = f"""<!doctype html><html><head><meta charset="utf-8"><title>Model eval report</title>
<style>
 body{{font-family:system-ui,sans-serif;margin:2rem;max-width:1300px;color:#222}}
 table{{border-collapse:collapse;margin:1rem 0}} td,th{{border:1px solid #ddd;padding:.4rem .6rem;text-align:center}}
 th{{background:#f5f5f5}} td.na{{color:#999}} pre{{white-space:pre-wrap;background:#fafafa;padding:.8rem;border:1px solid #eee;max-height:500px;overflow:auto}}
 details{{margin:.3rem 0}} summary{{cursor:pointer}} .j{{font-size:.85em;color:#444;margin:.2rem 0}}
 .note{{background:#fff8e1;border-left:4px solid #f0b400;padding:.6rem 1rem}} td.warn{{color:#b26a00;font-weight:600}}
 .verdict{{font-size:1.05rem;padding:.7rem 1rem;border-left:4px solid #2e7d32;background:#f1f8f2;margin:.5rem 0}}
 .verdict.tie{{border-color:#f0b400;background:#fff8e1}} .cols{{display:flex;gap:2rem;flex-wrap:wrap;align-items:flex-start}}
 .lead td:first-child{{font-weight:700}}
</style></head><body>
<h1>Model eval report</h1>
<p class="note"><b>How to read this:</b> overall = weighted mean of category means.
CI = 90% bootstrap interval over tasks — if two models' intervals overlap a lot, the eval can't separate them yet.
Inconsistency = mean std-dev across repetitions (lower = more predictable). HE vs EN = mean score change on the same task in Hebrew vs English (negative = the model gets worse in Hebrew).</p>
{comparison_html}
<h2>Leaderboard (for you)</h2>
<table><tr><th>#</th><th>model</th><th>overall</th><th>CI 90%</th>{head}
{he_head}<th>inconsistency</th><th>latency p50/p95</th><th>avg out tokens</th><th>cost</th><th>errors</th></tr>
{''.join(rows)}</table>
<h2>Per task</h2>
<table><tr><th>task</th>{''.join(f'<th>{e(n)}</th>' for n in names)}</tr>{task_rows}</table>
{signal_html}
<h2>Answers (rep 0)</h2>{''.join(drill)}
</body></html>"""
    path.write_text(doc, encoding="utf-8")


def _ranking_table(r: Ranking, leaders: set[str]) -> str:
    e = html.escape
    rows = "".join(
        f"<tr class='{'lead' if m.name in leaders else ''}'><td>{i}</td><td>{e(m.name)}</td>{_cell(m.score)}"
        f"<td><small>{m.ci90[0]:.2f}–{m.ci90[1]:.2f}</small></td><td>{m.n_tasks}</td></tr>"
        for i, m in enumerate(r.models, 1))
    return (f"<table><tr><th>#</th><th>model</th><th>score</th><th>CI 90%</th><th>tasks</th></tr>{rows}</table>")


def _head_to_head(r: Ranking, top: int = 10) -> str:
    e = html.escape
    names = [m.name for m in r.models[:top]]
    if len(names) < 2:
        return ""

    def cell(a: str, b: str) -> str:
        if a == b:
            return "<td class='na'>–</td>"
        p = r.p_beats.get((a, b))
        if p is None:
            return "<td class='na'>?</td>"
        hue = int(120 * p)  # red = row usually loses, green = row usually wins
        return f"<td style='background:hsl({hue} 60% 88%)'>{p:.0%}</td>"

    head = "".join(f"<th><small>{e(n)}</small></th>" for n in names)
    body = "".join(f"<tr><th><small>{e(a)}</small></th>{''.join(cell(a, b) for b in names)}</tr>" for a in names)
    return (f"<h3>Head-to-head ({e(r.label)}, top {len(names)})</h3><p class='note'>Cell = how often the ROW model "
            f"scores higher than the COLUMN model on the same resampled tasks. &ge; 90% = clear, 75–90% = likely, "
            f"closer to 50% = can't tell them apart.</p><table><tr><th></th>{head}</tr>{body}</table>")


def _comparison_html(c: Comparison) -> str:
    e = html.escape
    weights = ", ".join(f"{k} {v:g}" for k, v in c.for_you.weights.items()) or "none set"

    def block(r: Ranking, subtitle: str) -> str:
        tie = " tie" if len(r.leaders) > 1 else ""
        return (f"<div><h3>Best {e(r.label)}</h3><p class='muted'><small>{e(subtitle)}</small></p>"
                f"<p class='verdict{tie}'>{e(r.verdict)}</p>{_ranking_table(r, set(r.leaders))}</div>")

    notes = "".join(f"<p class='note'>{e(n)}</p>" for n in c.notes)
    return ("<h2>Which model is best?</h2>" + notes + "<div class='cols'>"
            + block(c.for_you, f"weighted by your category_weights: {weights}")
            + block(c.general, "every category weighted equally")
            + "</div>" + _head_to_head(c.for_you))

