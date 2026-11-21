"""Run button API: start a run in the background, poll it, cancel, resume, fail loudly."""
from __future__ import annotations

import asyncio
import textwrap
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from evalkit import providers
from evalkit.api.app import create_app

ROOT = Path(__file__).parent.parent


class _Slow:
    """A model that takes a while, so 'running' states can be observed."""

    def __init__(self, spec, settings):
        self.spec = spec

    async def complete(self, req):
        await asyncio.sleep(0.15)
        return providers.Completion(req.meta.get("reference") or "x", 0.15, 10, 5)


class _Crash:
    def __init__(self, spec, settings):
        pass

    async def complete(self, req):
        raise RuntimeError("adapter bug")  # not a ProviderError: a real bug, must reach the UI


CONFIG = """
run: {repetitions: 1, concurrency: 2}
category_weights: {general: 1}
models:
  - name: m
    provider: PROVIDER
    model: "{effort}"
    variants: {effort: [good, sloppy]}
judges: []
"""


def _app(tmp_path: Path, monkeypatch, provider: str) -> TestClient:
    monkeypatch.setitem(providers._REGISTRY, "slow", _Slow)
    monkeypatch.setitem(providers._REGISTRY, "crash", _Crash)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "c.yaml").write_text(textwrap.dedent(CONFIG.replace("PROVIDER", provider)), encoding="utf-8")
    tasks = tmp_path / "tasks"
    tasks.mkdir()
    (tasks / "t.yaml").write_text(
        "".join(f"- {{id: t{i}, category: general, prompt: p{i}, reference: x, grader: {{type: match, must_include: [x]}}}}\n"
                for i in range(6)), encoding="utf-8")
    return TestClient(create_app(tmp_path / "c.yaml", tasks, allowed_hosts=["testserver"]))


def _wait(client: TestClient, until=lambda j: j["status"] != "running", timeout: float = 15) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = client.get("/api/jobs/current").json()["job"]
        if job and until(job):
            return job
        time.sleep(0.05)
    raise AssertionError(f"job didn't reach the expected state: {job}")


def test_run_button_runs_and_results_appear(tmp_path, monkeypatch):
    with _app(tmp_path, monkeypatch, "slow") as client:
        assert client.get("/api/jobs/current").json() == {"job": None}
        r = client.post("/api/jobs", json={"models": ["m-good", "m-sloppy"], "run_name": "ui1"})
        assert r.status_code == 202 and r.json()["status"] == "running"
        assert r.json()["models"] == ["m-good", "m-sloppy"] and r.json()["results_url"] == "#results/ui1"

        job = _wait(client)
        assert job["status"] == "succeeded" and job["stage"] == "done" and job["error"] is None
        assert job["stages"]["generate"]["done"] == job["stages"]["generate"]["total"] == 12   # 2 models x 6 tasks
        assert job["stages"]["grade"]["done"] == 12 and job["stages"]["report"]["done"] == 1
        # ...and the Results tab can show it right away
        assert [x["name"] for x in client.get("/api/runs").json()] == ["ui1"]
        card = client.get("/api/runs/ui1/comparison").json()["scorecard"]
        assert {x["name"] for x in card} == {"m-good", "m-sloppy"}


def test_only_models_with_a_chosen_effort_run(tmp_path, monkeypatch):
    with _app(tmp_path, monkeypatch, "slow") as client:
        client.post("/api/jobs", json={"models": ["m-good"], "run_name": "only-good"})
        job = _wait(client)
        assert job["models"] == ["m-good"] and job["stages"]["generate"]["total"] == 6


def test_one_run_at_a_time(tmp_path, monkeypatch):
    with _app(tmp_path, monkeypatch, "slow") as client:
        assert client.post("/api/jobs", json={"models": ["m-good"], "run_name": "a"}).status_code == 202
        r = client.post("/api/jobs", json={"models": ["m-sloppy"], "run_name": "b"})
        assert r.status_code == 409 and "already in progress: a" in r.json()["detail"]
        _wait(client)
        assert client.post("/api/jobs", json={"models": ["m-sloppy"], "run_name": "b"}).status_code == 202
        _wait(client)


def test_cancel_keeps_answers_and_restart_resumes(tmp_path, monkeypatch):
    with _app(tmp_path, monkeypatch, "slow") as client:
        client.post("/api/jobs", json={"models": ["m-good", "m-sloppy"], "run_name": "c1"})
        _wait(client, until=lambda j: j["stages"].get("generate", {}).get("done", 0) >= 2)
        assert client.post("/api/jobs/current/cancel", json={}).json()["run_name"] == "c1"
        job = _wait(client)
        assert job["status"] == "cancelled" and "resume" in job["recent"][-1]
        assert client.post("/api/jobs/current/cancel", json={}).status_code == 409        # nothing running now

        client.post("/api/jobs", json={"models": ["m-good", "m-sloppy"], "run_name": "c1"})
        job = _wait(client)
        assert job["status"] == "succeeded"
        assert job["stages"]["generate"]["already_done"] >= 2                     # resumed, not redone
        assert job["stages"]["generate"]["already_done"] + job["stages"]["generate"]["total"] == 12


def test_a_crash_is_reported_not_swallowed(tmp_path, monkeypatch):
    with _app(tmp_path, monkeypatch, "crash") as client:
        client.post("/api/jobs", json={"models": ["m-good"], "run_name": "boom"})
        job = _wait(client)
        assert job["status"] == "failed" and "adapter bug" in job["error"]


@pytest.mark.parametrize("payload,status", [
    ({"models": ["nope"], "run_name": "x"}, 422),
    ({"models": ["m-good"], "run_name": "../../etc"}, 422),
    ({"models": [], "run_name": "x"}, 422),
])
def test_bad_requests_start_nothing(tmp_path, monkeypatch, payload, status):
    with _app(tmp_path, monkeypatch, "slow") as client:
        assert client.post("/api/jobs", json=payload).status_code == status
        assert client.get("/api/jobs/current").json() == {"job": None}


def test_start_requires_json_like_every_write(tmp_path, monkeypatch):
    with _app(tmp_path, monkeypatch, "slow") as client:
        r = client.post("/api/jobs", content="models=m-good", headers={"content-type": "application/x-www-form-urlencoded"})
        assert r.status_code == 415 and client.get("/api/jobs/current").json() == {"job": None}


def test_cancel_also_requires_json(tmp_path, monkeypatch):
    # A cross-site page must not be able to stop your run either.
    with _app(tmp_path, monkeypatch, "slow") as client:
        client.post("/api/jobs", json={"models": ["m-good"], "run_name": "csrf"})
        assert client.post("/api/jobs/current/cancel").status_code == 415
        assert _wait(client)["status"] == "succeeded"
