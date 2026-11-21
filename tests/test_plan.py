"""Run planning: cost estimate, model selection, command building, planner API."""
from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from evalkit.__main__ import select_models
from evalkit.api.app import create_app
from evalkit.config import load_config, load_tasks
from evalkit.estimate import estimate

ROOT = Path(__file__).parent.parent

CONFIG = """
run: {repetitions: 3}
models:
  - name: m
    provider: mock
    model: "{q}"
    variants: {q: [good, sloppy, broken]}
    defaults: {q: [good]}
  - name: paste
    provider: manual
judges:
  - {name: j1, provider: mock, model: judge}
  - {name: j2, provider: mock, model: judge}
"""

TASKS = """
- {id: code, category: backend_code, prompt: p, grader: {type: match, must_include: [x]}}
- id: essay
  category: general
  prompt: p
  grader: {type: rubric, criteria: [{id: c, description: d}]}
"""


@pytest.fixture
def setup(tmp_path):
    (tmp_path / "c.yaml").write_text(textwrap.dedent(CONFIG), encoding="utf-8")
    (tmp_path / "tasks").mkdir()
    (tmp_path / "tasks" / "t.yaml").write_text(textwrap.dedent(TASKS), encoding="utf-8")
    return load_config(tmp_path / "c.yaml"), load_tasks(tmp_path / "tasks"), tmp_path


# ---------------------------------------------------------------- selection

def test_select_defaults_all_family_and_names(setup):
    cfg, _, _ = setup
    names = lambda sel: [m.name for m in select_models(cfg.models, sel)]  # noqa: E731
    assert names(None) == ["m-good", "paste"]                      # plain run = defaults only
    assert names({"all"}) == ["m-good", "m-sloppy", "m-broken", "paste"]
    assert names({"m"}) == ["m-good", "m-sloppy", "m-broken"]      # family = all its variants
    assert names({"m-broken"}) == ["m-broken"]
    with pytest.raises(ValueError, match="unknown"):
        select_models(cfg.models, {"nope"})


# ---------------------------------------------------------------- estimate

def test_calls_are_exact_and_judges_bill_their_own_subscription(setup, tmp_path):
    cfg, tasks, _ = setup
    models = [m for m in cfg.models if m.name in {"m-good", "m-sloppy", "paste"}]
    est = estimate(cfg, tasks, models, results_dir=tmp_path / "none")
    subs = {s.subscription: s for s in est.subscriptions}
    # 2 mock competitors x 2 tasks x 3 reps; manual = 1 rep
    assert subs["mock"].generation_calls == 12 and subs["manual"].generation_calls == 2
    # 1 rubric task -> every answer of every competitor goes to BOTH judges (j1, j2 are mock)
    assert subs["mock"].judge_calls == (3 + 3 + 1) * 2
    assert est.total_calls == 12 + 2 + 14
    assert subs["mock"].models_without_history == 2 and any("Token estimates" in n for n in est.notes)


def test_tokens_estimated_from_earlier_runs_only(setup, tmp_path):
    cfg, tasks, _ = setup
    run = tmp_path / "results" / "old"
    run.mkdir(parents=True)
    rows = [{"model": "m-good", "input_tokens": 100, "output_tokens": 10},
            {"model": "m-good", "input_tokens": 300, "output_tokens": 30},
            {"model": "m-good", "error": "boom"}]                    # errors don't count
    (run / "generations.jsonl").write_text("\n".join(map(json.dumps, rows)) + "\n{broken", encoding="utf-8")
    est = estimate(cfg, tasks, [m for m in cfg.models if m.name in {"m-good", "m-sloppy"}],
                   repetitions=1, results_dir=tmp_path / "results")
    by = {c.name: c for c in est.models}
    assert (by["m-good"].est_input_tokens, by["m-good"].est_output_tokens) == (400, 40)  # mean 200/20 x 2 calls
    assert by["m-sloppy"].est_input_tokens is None                    # no history -> no guess


# ---------------------------------------------------------------- planner API

@pytest.fixture
def client(setup, monkeypatch):
    _, _, tmp = setup
    monkeypatch.chdir(tmp)  # results/ history is read relative to cwd
    return TestClient(create_app(tmp / "c.yaml", tmp / "tasks", allowed_hosts=["testserver"]))


def test_options_group_variants_by_family(client):
    body = client.get("/api/plan/options").json()
    fam = {f["family"]: f for f in body["families"]}
    assert [(v["name"], v["default"]) for v in fam["m"]["variants"]] == \
        [("m-good", True), ("m-sloppy", False), ("m-broken", False)]
    assert {c["name"]: (c["n_tasks"], c["n_rubric"]) for c in body["categories"]} == \
        {"backend_code": (1, 0), "general": (1, 1)}
    assert body["repetitions"] == 3 and [j["name"] for j in body["judges"]] == ["j1", "j2"]


@pytest.mark.parametrize("models,extra,command_tail", [
    (["m-good", "paste"], {}, "--run v1"),                                         # = defaults
    (["m-good", "m-sloppy", "m-broken", "paste"], {}, "--run v1 --models all"),
    (["m-good", "m-sloppy", "m-broken"], {}, "--run v1 --models m"),               # whole family
    (["m-broken"], {"categories": ["general"], "repetitions": 1, "run_name": "try-2"},
     "--run try-2 --models m-broken --category general --repetitions 1"),
])
def test_estimate_builds_shortest_equivalent_command(client, setup, models, extra, command_tail):
    r = client.post("/api/plan/estimate", json={"models": models, **extra})
    assert r.status_code == 200, r.text
    cmd = r.json()["command"]
    # A non-default config must be in the command, or it would run against config.yaml instead.
    config_arg = f" --config {setup[2] / 'c.yaml'}"
    assert config_arg in cmd
    assert cmd.replace(config_arg, "") == f"python -m evalkit run {command_tail}"


def test_estimate_command_round_trips_to_same_selection(client, setup):
    cfg, _, _ = setup
    pick = ["m-sloppy", "m-broken"]
    cmd = client.post("/api/plan/estimate", json={"models": pick}).json()["command"]
    models_arg = cmd.split("--models ")[1].split()[0]
    assert [m.name for m in select_models(cfg.models, set(models_arg.split(",")))] == pick


@pytest.mark.parametrize("payload", [
    {"models": ["nope"]},
    {"models": ["m-good"], "categories": ["nope"]},
    {"models": []},
    {"models": ["m-good"], "repetitions": 0},
    {"models": ["m-good"], "run_name": "../../etc"},     # becomes results/<run>/: path traversal
    {"models": ["m-good"], "run_name": "a/b"},
])
def test_estimate_rejects_bad_input(client, payload):
    assert client.post("/api/plan/estimate", json=payload).status_code == 422


def test_cost_estimated_from_cost_the_cli_reported(setup, tmp_path):
    cfg, tasks, _ = setup
    run = tmp_path / "results" / "old"
    run.mkdir(parents=True)
    rows = [{"model": "m-good", "input_tokens": 1, "output_tokens": 1, "cost_usd": 0.02},
            {"model": "m-good", "input_tokens": 1, "output_tokens": 1, "cost_usd": 0.04},
            {"model": "m-sloppy", "input_tokens": 1, "output_tokens": 1}]          # tokens but no cost
    (run / "generations.jsonl").write_text("\n".join(map(json.dumps, rows)), encoding="utf-8")
    est = estimate(cfg, tasks, [m for m in cfg.models if m.name in {"m-good", "m-sloppy"}], repetitions=1,
                   results_dir=tmp_path / "results")
    by = {c.name: c for c in est.models}
    assert by["m-good"].est_cost_usd == pytest.approx(0.03 * 2)    # mean cost x 2 calls
    assert by["m-sloppy"].est_cost_usd is None                      # no reported cost -> no guess
    assert est.subscriptions[0].est_cost_usd == pytest.approx(0.06)
    assert any("list-price" in n for n in est.notes)
