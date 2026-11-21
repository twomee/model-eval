"""API integration tests: real app, real routing and middleware, mock models."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from evalkit.api import app as app_module
from evalkit.api.app import create_app

ROOT = Path(__file__).parent.parent


@pytest.fixture
def client() -> TestClient:
    app = create_app(ROOT / "config.mock.yaml", ROOT / "tasks", allowed_hosts=["testserver"])
    return TestClient(app)


def test_health(client):
    assert client.get("/api/health").json() == {"status": "ok"}


def test_list_models_includes_judges_and_hides_internals(client):
    body = client.get("/api/models").json()
    roles = {m["name"]: m["role"] for m in body["models"]}
    assert roles["mock-good"] == "model" and roles["judge-mock-a"] == "judge"
    good = next(m for m in body["models"] if m["name"] == "mock-good")
    assert good["model_id"] == "good" and good["vendor"] == "mock" and good["effort"] is None
    assert "command" not in good and "isolated_home" not in good  # argv/env are not API surface


def test_doctor_all(client):
    body = client.post("/api/doctor", json={}).json()
    assert body["all_ok"] is True and len(body["checks"]) == 5
    assert body["warnings"], "two mock judges = one vendor -> must warn"


def test_doctor_subset_skips_vendor_warning(client):
    body = client.post("/api/doctor", json={"names": ["mock-good"]}).json()
    assert [c["name"] for c in body["checks"]] == ["mock-good"]
    assert body["warnings"] == []  # judge panel not checked -> can't judge its coverage


def test_doctor_unknown_name_is_422(client):
    r = client.post("/api/doctor", json={"names": ["nope"]})
    assert r.status_code == 422 and "nope" in r.json()["detail"]


# ---------------------------------------------------------------- local-only security

def test_post_without_json_content_type_is_rejected(client):
    # What a cross-origin <form> or no-preflight fetch could send: must not trigger model calls.
    r = client.post("/api/doctor", content="names=x", headers={"content-type": "application/x-www-form-urlencoded"})
    assert r.status_code == 415


def test_foreign_host_header_is_rejected():
    # DNS rebinding: request arrives at 127.0.0.1 but carries Host: evil.example
    client = TestClient(create_app(ROOT / "config.mock.yaml", ROOT / "tasks"))
    r = client.get("/api/models", headers={"host": "evil.example"})
    assert r.status_code == 400


# ---------------------------------------------------------------- frontend serving

def test_unbuilt_ui_explains_how_to_build(client, monkeypatch, tmp_path):
    monkeypatch.setattr(app_module, "WEB_DIST", tmp_path / "missing")
    c = TestClient(create_app(ROOT / "config.mock.yaml", ROOT / "tasks", allowed_hosts=["testserver"]))
    r = c.get("/")
    assert r.status_code == 503 and "npm run build" in r.json()["detail"]


def test_built_ui_serves_spa_but_api_typos_stay_404(monkeypatch, tmp_path):
    (tmp_path / "assets").mkdir()
    (tmp_path / "index.html").write_text("<div id=root></div>", encoding="utf-8")
    monkeypatch.setattr(app_module, "WEB_DIST", tmp_path)
    c = TestClient(create_app(ROOT / "config.mock.yaml", ROOT / "tasks", allowed_hosts=["testserver"]))
    assert "root" in c.get("/models").text          # client-side route -> SPA shell
    r = c.get("/api/modles")
    assert r.status_code == 404 and r.headers["content-type"].startswith("application/json")


def test_built_ui_serves_root_files_without_path_traversal(monkeypatch, tmp_path):
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<div id=root></div>", encoding="utf-8")
    (dist / "favicon.svg").write_text("<svg/>", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("TOKEN", encoding="utf-8")
    monkeypatch.setattr(app_module, "WEB_DIST", dist)
    c = TestClient(create_app(ROOT / "config.mock.yaml", ROOT / "tasks", allowed_hosts=["testserver"]))
    assert c.get("/favicon.svg").text == "<svg/>"
    for probe in ("/..%2fsecret.txt", "/%2e%2e/secret.txt", "/assets/..%2f..%2fsecret.txt"):
        assert "TOKEN" not in c.get(probe).text, probe
