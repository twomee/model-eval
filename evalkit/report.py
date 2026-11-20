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

from .config import Config
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


def build_summary(cfg: Config, run_dir: Path) -> dict[str, Any]:
    gens = _latest(read_jsonl(run_dir / "generations.jsonl"))
    grades = _latest(read_jsonl(run_dir / "grades.jsonl"))
    task_cat = {g["task_id"]: g["category"] for g in gens.values()}
    categories = sorted(set(task_cat.values()))

    per_model: dict[str, dict[str, Any]] = {}
    matrix: dict[str, dict[str, float]] = defaultdict(dict)   # task -> model -> mean score
    for spec in cfg.models:
        m = spec.name
        g_ok = [g for g in gens.values() if g["model"] == m and not g.get("error")]
        g_err = [g for g in gens.values() if g["model"] == m and g.get("error")]
        scores_by_task: dict[str, list[float]] = defaultdict(list)
        for r in grades.values():
            if r["model"] == m and r["score"] is not None:
                scores_by_task[r["task_id"]].append(r["score"])
        if not scores_by_task:
            per_model[m] = {"name": m, "provider": spec.provider, "empty": True, "errors": len(g_err)}
            continue
        task_means = {t: st.mean(v) for t, v in scores_by_task.items()}
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
        per_model[m] = {
            "name": m, "provider": spec.provider, "empty": False,
            "overall": _weighted_overall(task_means, task_cat, cfg.category_weights),
            "ci90": (lo, hi), "categories": cat_scores,
            "n_tasks": len(task_means), "errors": len(g_err),
            "inconsistency": st.mean(spreads) if spreads else None,
            "latency_p50": _pct(lat, 0.5), "latency_p95": _pct(lat, 0.95),
            "avg_output_tokens": st.mean(out_tok) if out_tok else None,
            "total_cost": sum(costs) if costs else None,
        }
    ranked = sorted((p for p in per_model.values() if not p["empty"]), key=lambda p: -p["overall"])
    ranked += [p for p in per_model.values() if p["empty"]]
    return {"models": ranked, "categories": categories, "matrix": matrix,
            "gens": gens, "grades": grades, "weights": cfg.category_weights}


def _fmt(x: float | None, kind: str = "score") -> str:
    if x is None:
        return "–"
    return {"score": f"{x:.2f}", "s": f"{x:.1f}s", "usd": f"${x:.3f}", "int": f"{x:.0f}"}[kind]


def _cell(x: float | None) -> str:
    if x is None:
        return "<td class='na'>–</td>"
    hue = int(120 * x)  # 0 = red, 120 = green
    return f"<td style='background:hsl({hue} 60% 88%)'>{x:.2f}</td>"


def write_csv(summary: dict[str, Any], path: Path) -> None:
    cats = summary["categories"]
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["model", "provider", "overall", "ci90_low", "ci90_high", *cats, "n_tasks", "errors",
                    "inconsistency", "latency_p50_s", "latency_p95_s", "avg_output_tokens", "total_cost_usd"])
        for p in summary["models"]:
            if p["empty"]:
                w.writerow([p["name"], p["provider"]] + [""] * (len(cats) + 10))
                continue
            w.writerow([p["name"], p["provider"], round(p["overall"], 4), round(p["ci90"][0], 4),
                        round(p["ci90"][1], 4), *[None if p["categories"][c] is None else round(p["categories"][c], 4) for c in cats],
                        p["n_tasks"], p["errors"], p["inconsistency"], p["latency_p50"], p["latency_p95"],
                        p["avg_output_tokens"], p["total_cost"]])


def write_html(summary: dict[str, Any], path: Path) -> None:
    e, cats, models = html.escape, summary["categories"], summary["models"]
    weights = summary["weights"]
    head = "".join(f"<th>{e(c)}<br><small>w={weights.get(c, 1.0)}</small></th>" for c in cats)
    rows = []
    for i, p in enumerate(models, 1):
        if p["empty"]:
            rows.append(f"<tr><td>{i}</td><td>{e(p['name'])}</td><td colspan='{len(cats) + 7}' class='na'>"
                        f"no graded results (errors: {p['errors']})</td></tr>")
            continue
        lo, hi = p["ci90"]
        rows.append(
            f"<tr><td>{i}</td><td><b>{e(p['name'])}</b><br><small>{e(p['provider'])}</small></td>"
            f"{_cell(p['overall'])}<td><small>{lo:.2f}–{hi:.2f}</small></td>"
            + "".join(_cell(p["categories"][c]) for c in cats)
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

    doc = f"""<!doctype html><html><head><meta charset="utf-8"><title>Model eval report</title>
<style>
 body{{font-family:system-ui,sans-serif;margin:2rem;max-width:1300px;color:#222}}
 table{{border-collapse:collapse;margin:1rem 0}} td,th{{border:1px solid #ddd;padding:.4rem .6rem;text-align:center}}
 th{{background:#f5f5f5}} td.na{{color:#999}} pre{{white-space:pre-wrap;background:#fafafa;padding:.8rem;border:1px solid #eee;max-height:500px;overflow:auto}}
 details{{margin:.3rem 0}} summary{{cursor:pointer}} .j{{font-size:.85em;color:#444;margin:.2rem 0}}
 .note{{background:#fff8e1;border-left:4px solid #f0b400;padding:.6rem 1rem}}
</style></head><body>
<h1>Model eval report</h1>
<p class="note"><b>How to read this:</b> overall = weighted mean of category means.
CI = 90% bootstrap interval over tasks — if two models' intervals overlap a lot, the eval can't separate them yet.
Inconsistency = mean std-dev across repetitions (lower = more predictable).</p>
<h2>Leaderboard</h2>
<table><tr><th>#</th><th>model</th><th>overall</th><th>CI 90%</th>{head}
<th>inconsistency</th><th>latency p50/p95</th><th>avg out tokens</th><th>cost</th><th>errors</th></tr>
{''.join(rows)}</table>
<h2>Per task</h2>
<table><tr><th>task</th>{''.join(f'<th>{e(n)}</th>' for n in names)}</tr>{task_rows}</table>
<h2>Answers (rep 0)</h2>{''.join(drill)}
</body></html>"""
    path.write_text(doc, encoding="utf-8")
