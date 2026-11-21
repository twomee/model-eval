"""Results API: run list, model comparison, report serving (incl. path safety)."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from evalkit.api.app import create_app
from evalkit.config import load_config, load_tasks
from evalkit.report import build_summary, write_html
from evalkit.runner import generate, grade_all

ROOT = Path(__file__).parent.parent
TASKS = {"py_token_bucket", "gen_capacity_estimate", "design_job_queue_choice", "py_sql_monthly_revenue"}


@pytest.fixture(scope="module")
def results(tmp_path_factory):
    """One real mock run (generate -> grade -> report), shared by the tests in this module."""
    root = tmp_path_factory.mktemp("cwd")
    cfg = load_config(ROOT / "config.mock.yaml")
    tasks = [t for t in load_tasks(ROOT / "tasks") if t.id in TASKS]
    run_dir = root / "results" / "mockrun"
    asyncio.run(generate(cfg, tasks, run_dir, cfg.models))
    asyncio.run(grade_all(cfg, tasks, run_dir))
    write_html(build_summary(cfg, run_dir, tasks), run_dir / "report.html")
    (root / "results" / "not-a-run").mkdir()            # a directory without results: not listed
    (root / "secret.txt").write_text("TOKEN", encoding="utf-8")
    return root


@pytest.fixture
def client(results, monkeypatch):
    monkeypatch.chdir(results)
    return TestClient(create_app(ROOT / "config.mock.yaml", ROOT / "tasks", allowed_hosts=["testserver"]))


def test_lists_only_real_runs(client):
    runs = client.get("/api/runs").json()
    assert [r["name"] for r in runs] == ["mockrun"]
    assert runs[0]["has_report"] and runs[0]["n_answers"] > 0


def test_comparison_ranks_and_explains(client):
    body = client.get("/api/runs/mockrun/comparison").json()
    for key in ("for_you", "general"):
        r = body[key]
        assert [m["name"] for m in r["models"]][0] == "mock-good"
        assert r["models"][-1]["name"] == "mock-broken"
        assert r["verdict"] and r["models"][0]["leader"] is True
        n = len(r["head_to_head_names"])
        assert len(r["head_to_head"]) == n and all(len(row) == n for row in r["head_to_head"])
        assert r["head_to_head"][0][0] is None                               # diagonal
        assert r["head_to_head"][0][-1] > 0.9                                # good beats broken
    assert body["for_you"]["weights"]["backend_code"] == 2 and body["general"]["weights"] == {}
    assert body["report_url"] == "/reports/mockrun/report.html"
    assert any("Fewer than 10 tasks" in n for n in body["notes"])


def test_report_is_served_with_the_comparison(client):
    r = client.get("/reports/mockrun/report.html")
    assert r.status_code == 200 and "Which model is best?" in r.text and "Head-to-head" in r.text


@pytest.mark.parametrize("run", ["nope", "not-a-run", "..", "%2e%2e", "..%2fsecret.txt", "mockrun%2f..%2f.."])
def test_unknown_or_escaping_runs_never_leak(client, run):
    assert client.get(f"/api/runs/{run}/comparison").status_code == 404
    r = client.get(f"/reports/{run}/report.html")
    # Names with "/" or ".." never reach the report route: they fall through to the SPA shell
    # (200, index.html), like any unknown page. What matters: no secret, no report.
    assert "TOKEN" not in r.text and "Which model is best?" not in r.text
    assert r.status_code == 404 or r.headers["content-type"].startswith("text/html")
