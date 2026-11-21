"""CLI.

  python -m evalkit run      --config config.yaml [--models a,b] [--tasks id1,id2] [--run NAME]
  python -m evalkit grade    --config config.yaml --run NAME [--regrade]
  python -m evalkit report   --config config.yaml --run NAME
  python -m evalkit export   --config config.yaml          # prompts for `manual` models
  python -m evalkit validate --config config.yaml          # check config + tasks, no model calls
  python -m evalkit doctor   --config config.yaml          # one trivial call per model/judge: is it reachable?
  python -m evalkit serve    --config config.yaml          # web UI on http://127.0.0.1:8765
"""
from __future__ import annotations

import argparse
import asyncio
import shutil
import sys
from pathlib import Path

from .config import load_config, load_tasks
from .doctor import format_report, run_doctor
from .report import build_summary, write_csv, write_html
from .runner import export_manual, generate, grade_all


def _csv(s: str | None) -> set[str] | None:
    return {x.strip() for x in s.split(",") if x.strip()} if s else None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="evalkit")
    ap.add_argument("command", choices=["run", "grade", "report", "export", "validate", "doctor", "serve"])
    ap.add_argument("--config", default="config.yaml", type=Path)
    ap.add_argument("--tasks-dir", default="tasks", type=Path)
    ap.add_argument("--run", default="default", help="run name; results go to results/<run>/")
    ap.add_argument("--models", help="comma-separated subset of model names")
    ap.add_argument("--tasks", help="comma-separated subset of task ids")
    ap.add_argument("--category", help="comma-separated subset of categories")
    ap.add_argument("--regrade", action="store_true", help="discard existing grades and re-grade")
    ap.add_argument("--host", default="127.0.0.1", help="serve: bind address (default: this machine only)")
    ap.add_argument("--port", default=8765, type=int, help="serve: port")
    args = ap.parse_args(argv)

    if args.command == "serve":
        return _serve(args)

    cfg = load_config(args.config)
    tasks = load_tasks(args.tasks_dir)
    if (ids := _csv(args.tasks)):
        tasks = [t for t in tasks if t.id in ids]
    if (cats := _csv(args.category)):
        tasks = [t for t in tasks if t.category in cats]
    models = cfg.models
    if (names := _csv(args.models)):
        unknown = names - {m.name for m in models}
        if unknown:
            ap.error(f"unknown models: {unknown}")
        models = [m for m in models if m.name in names]
    run_dir = Path("results") / args.run

    if args.command == "validate":
        print(f"config OK: {len(cfg.models)} models, {len(cfg.judges)} judges")
        by_cat: dict[str, int] = {}
        for t in tasks:
            by_cat[t.category] = by_cat.get(t.category, 0) + 1
        print(f"tasks OK: {len(tasks)} -> {by_cat}")
        if not cfg.judges and any(g["type"] == "rubric" for t in tasks for g in t.graders):
            print("WARNING: rubric graders present but no judges configured")
        if shutil.which("go") is None and any(g["type"] == "go_tests" for t in tasks for g in t.graders):
            print("WARNING: go toolchain not found -> go_tests graders will be skipped (install Go 1.22+)")
        n_pairs = len({t.pair for t in tasks if t.pair})
        if n_pairs:
            print(f"pairs: {n_pairs} EN/HE task pairs -> 'HE vs EN' column in the report")
        def ident(s):  # what actually runs, not the label
            return (s.provider, s.model, s.command)
        competitors = {ident(m): m.name for m in cfg.models}
        for j in cfg.judges:
            if ident(j) in competitors:
                print(f"NOTE: judge {j.name} is the same model as competitor {competitors[ident(j)]} "
                      f"-> self-preference bias. Mitigation: a multi-vendor judge panel.")
        return 0
    if args.command == "doctor":
        checks = asyncio.run(run_doctor(cfg, models, [t.id for t in tasks]))
        text, all_ok = format_report(cfg, checks)
        print(text)
        return 0 if all_ok else 1
    if args.command == "export":
        paths = export_manual(cfg, tasks)
        print(f"wrote {len(paths)} prompt files under manual/ — paste each reply into the matching .answer.md")
        return 0
    if args.command == "run":
        asyncio.run(generate(cfg, tasks, run_dir, models))
        asyncio.run(grade_all(cfg, tasks, run_dir))
    elif args.command == "grade":
        asyncio.run(grade_all(cfg, tasks, run_dir, regrade=args.regrade))

    summary = build_summary(cfg, run_dir, tasks)
    write_html(summary, run_dir / "report.html")
    write_csv(summary, run_dir / "summary.csv")
    print(f"\nreport: {run_dir / 'report.html'}")
    for i, p in enumerate(summary["models"], 1):
        if not p["empty"]:
            lo, hi = p["ci90"]
            he = f"  HE vs EN {p['he_vs_en']:+.2f}" if p.get("he_vs_en") is not None else ""
            print(f"  {i}. {p['name']:<20} {p['overall']:.3f}  (90% CI {lo:.2f}–{hi:.2f}){he}")
    return 0


def _serve(args: argparse.Namespace) -> int:
    try:
        import uvicorn

        from .api.app import LOCAL_HOSTS, create_app
    except ImportError as e:  # web deps are optional for CLI-only use
        print(f"serve needs the web extras: pip install -r requirements.txt ({e})", file=sys.stderr)
        return 1
    load_config(args.config)  # fail fast on a broken config, before the browser opens
    hosts = LOCAL_HOSTS
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print(f"WARNING: binding {args.host} exposes this UI to your network. Anyone who can reach it "
              "can spend your subscription quota. There is no authentication.", file=sys.stderr)
        hosts = [*LOCAL_HOSTS, args.host]
    app = create_app(args.config, args.tasks_dir, allowed_hosts=hosts)
    print(f"evalkit UI: http://{'localhost' if args.host == '127.0.0.1' else args.host}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
