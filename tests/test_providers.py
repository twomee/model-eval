"""CLI output parsing, error classification, and the `doctor` preflight."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from evalkit.config import Config, ModelSpec, RunSettings, load_config
from evalkit.doctor import format_report, run_doctor
from evalkit.providers import (CliProvider, ProviderError, Request, _error_summary, _is_retryable,
                               parse_agy_json, parse_claude_json, parse_codex_jsonl)

ROOT = Path(__file__).parent.parent


# ---------------------------------------------------------------- claude_json

def _claude(**over) -> str:
    data = {"is_error": False, "result": "hi", "total_cost_usd": 0.0123,
            "usage": {"input_tokens": 2, "cache_creation_input_tokens": 1795,
                      "cache_read_input_tokens": 649, "output_tokens": 21}}
    data.update(over)
    return json.dumps(data)


def test_claude_json_counts_cached_tokens_as_input():
    c = parse_claude_json(_claude(), 1.0)
    assert c.text == "hi"
    assert c.input_tokens == 2 + 1795 + 649  # not 2: cached tokens are still prompt tokens
    assert c.output_tokens == 21
    assert c.cost_usd == pytest.approx(0.0123)


def test_claude_json_missing_usage_is_none_not_zero():
    c = parse_claude_json(json.dumps({"result": "x"}), 1.0)
    assert c.input_tokens is None and c.cost_usd is None


@pytest.mark.parametrize("msg,retryable", [
    ("Fable 5.2 requires usage credits.", False),        # permanent: retrying wastes minutes
    ("API Error: 429 rate_limit_error", True),
    ("Overloaded, please try again", True),
])
def test_claude_json_error_retryability(msg, retryable):
    with pytest.raises(ProviderError) as ei:
        parse_claude_json(_claude(is_error=True, result=msg), 1.0)
    assert ei.value.retryable is retryable


def test_claude_json_non_json_is_provider_error():
    with pytest.raises(ProviderError, match="non-JSON"):
        parse_claude_json("Error: not logged in", 1.0)


# ---------------------------------------------------------------- stderr summarising

def test_error_summary_picks_last_error_line_over_noise():
    stderr = ("Reading additional input from stdin...\n"
              "2026-11-21T08:35Z ERROR cache: " + '{"models": ' + "x" * 5000 + "}\n"
              "model: gpt-5.5\n"
              'ERROR: {"message":"model is not supported"}\n')
    assert _error_summary(stderr) == 'ERROR: {"message":"model is not supported"}'


def test_error_summary_falls_back_to_tail():
    assert _error_summary("line one\nline two") == "line one\nline two"


def test_retryable_ignores_permanent_errors():
    assert not _is_retryable("model_not_found")
    assert _is_retryable("HTTP 429 Too Many Requests")


# ---------------------------------------------------------------- doctor

def test_doctor_mock_passes_and_warns_single_judge_vendor():
    cfg = load_config(ROOT / "config.mock.yaml")
    checks = asyncio.run(run_doctor(cfg, cfg.models, ["t1"]))
    assert all(c.ok for c in checks) and len(checks) == len(cfg.models) + len(cfg.judges)
    text, ok = format_report(cfg, checks)
    assert ok and "1 vendor" in text


def test_doctor_reports_missing_cli_and_manual_progress(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "manual" / "paste").mkdir(parents=True)
    (tmp_path / "manual" / "paste" / "t1.answer.md").write_text("an answer", encoding="utf-8")
    cfg = Config(run=RunSettings(), category_weights={}, judges=[], models=[
        ModelSpec(name="ghost", provider="cli", command=("definitely-not-a-cli-xyz", "{prompt}")),
        ModelSpec(name="paste", provider="manual"),
    ])
    checks = {c.name: c for c in asyncio.run(run_doctor(cfg, cfg.models, ["t1", "t2"]))}
    assert not checks["ghost"].ok and "not found" in checks["ghost"].detail
    assert checks["paste"].ok and "1/2" in checks["paste"].detail
    _, ok = format_report(cfg, list(checks.values()))
    assert not ok


# ---------------------------------------------------------------- codex_jsonl

def _codex(*events) -> str:
    return "\n".join(json.dumps(e) for e in events)


def test_codex_jsonl_takes_last_agent_message_and_usage():
    out = _codex({"type": "thread.started"},
                 {"type": "item.completed", "item": {"type": "error", "message": "metadata warning"}},
                 {"type": "item.completed", "item": {"type": "agent_message", "text": "draft"}},
                 {"type": "item.completed", "item": {"type": "agent_message", "text": "final"}},
                 {"type": "turn.completed", "usage": {"input_tokens": 17592, "cached_input_tokens": 13952,
                                                      "output_tokens": 5}})
    c = parse_codex_jsonl(out + "\nnot json log line", 2.0)
    assert c.text == "final" and c.input_tokens == 17592 and c.output_tokens == 5


def test_codex_jsonl_turn_failed_is_permanent_error():
    out = _codex({"type": "error", "message": "The 'x' model is not supported"},
                 {"type": "turn.failed", "error": {"message": "The 'x' model is not supported"}})
    with pytest.raises(ProviderError, match="not supported") as ei:
        parse_codex_jsonl(out, 1.0)
    assert not ei.value.retryable


# ---------------------------------------------------------------- CliProvider end-to-end with a fake CLI

def _fake_cli(tmp_path: Path, script: str) -> str:
    exe = tmp_path / "fakecli"
    exe.write_text("#!/bin/sh\n" + script, encoding="utf-8")
    exe.chmod(0o755)
    return str(exe)


def test_cli_nonzero_exit_reports_error_from_stdout(tmp_path):
    # claude -p --output-format json exits 1 with the reason on STDOUT and empty stderr
    exe = _fake_cli(tmp_path, """echo '{"is_error": true, "result": "requires usage credits"}'; exit 1""")
    spec = ModelSpec(name="c", provider="cli", command=(exe,), parse="claude_json")
    with pytest.raises(ProviderError, match="requires usage credits") as ei:
        asyncio.run(CliProvider(spec, RunSettings(max_retries=0)).complete(Request(prompt="hi")))
    assert not ei.value.retryable


def test_cli_isolated_home_sets_env_and_symlinks_login(tmp_path):
    login = tmp_path / "real" / "auth.json"
    login.parent.mkdir()
    login.write_text("{}", encoding="utf-8")
    home = tmp_path / "iso"
    exe = _fake_cli(tmp_path, 'echo "$FAKE_HOME"; ls "$FAKE_HOME"')
    spec = ModelSpec(name="c", provider="cli", command=(exe, "{prompt}"),
                     isolated_home={"env": "FAKE_HOME", "dir": str(home), "link": [str(login)]})
    out = asyncio.run(CliProvider(spec, RunSettings()).complete(Request(prompt="hi"))).text
    assert str(home) in out and "auth.json" in out
    assert (home / "auth.json").is_symlink() and (home / "auth.json").resolve() == login.resolve()


def test_cli_isolated_home_missing_login_fails_fast(tmp_path):
    spec = ModelSpec(name="c", provider="cli", command=("sh",),
                     isolated_home={"env": "X", "dir": str(tmp_path / "h"), "link": [str(tmp_path / "nope.json")]})
    with pytest.raises(ProviderError, match="missing"):
        CliProvider(spec, RunSettings())


@pytest.mark.parametrize("spec,expected", [
    (ModelSpec(name="a", provider="anthropic", model="claude-x"), "claude-x"),
    (ModelSpec(name="c", provider="cli", command=("claude", "-p", "--model", "claude-y")), "claude-y"),
    (ModelSpec(name="x", provider="cli", command=("codex", "exec", "-m", "gpt-z", "{prompt}")), "gpt-z"),
    (ModelSpec(name="u", provider="cli", command=("gemini", "-p", "{prompt}")), None),  # unpinned
    (ModelSpec(name="t", provider="cli", command=("codex", "-m")), None),               # dangling flag
])
def test_model_id(spec, expected):
    assert spec.model_id == expected


# ---------------------------------------------------------------- agy_json (Antigravity CLI / Gemini)

AGY_OK = ('{"conversation_id":"055a","status":"SUCCESS","response":"A git rebase rewrites history.\\n",'
          '"duration_seconds":7.16,"num_turns":1,"usage":{"input_tokens":10415,"output_tokens":657,'
          '"thinking_tokens":616,"cache_read_tokens":8113,"total_tokens":11072}}')  # docs example


def test_agy_json_success_uses_documented_envelope():
    c = parse_agy_json("some diagnostic line\n" + AGY_OK, 7.2)
    assert c.text.startswith("A git rebase") and c.input_tokens == 10415 and c.output_tokens == 657


@pytest.mark.parametrize("status,error,retryable", [
    ("ERROR", "model 'does-not-exist-model' not found", False),
    ("ERROR", "429 rate limit exceeded", True),
    ("CANCELED", None, False),
])
def test_agy_json_non_success_status_is_error(status, error, retryable):
    body = {"status": status, **({"error": error} if error else {})}
    with pytest.raises(ProviderError, match=status) as ei:
        parse_agy_json(json.dumps(body), 1.0)
    assert ei.value.retryable is retryable


def test_agy_json_without_envelope_is_error():
    with pytest.raises(ProviderError, match="no JSON envelope"):
        parse_agy_json("authentication required", 1.0)


def test_agy_exit_1_error_comes_from_stdout_envelope(tmp_path):
    exe = _fake_cli(tmp_path, """echo '{"status":"ERROR","error":"unknown model gemini-4-argon-low"}'; exit 1""")
    spec = ModelSpec(name="g", provider="cli", command=(exe, "-p", "{prompt}"), parse="agy_json")
    with pytest.raises(ProviderError, match="unknown model gemini-4-argon-low"):
        asyncio.run(CliProvider(spec, RunSettings(max_retries=0)).complete(Request(prompt="hi")))


# ---------------------------------------------------------------- doctor --isolation

def _probe_cfg(exe: str) -> Config:
    return Config(run=RunSettings(), category_weights={}, judges=[],
                  models=[ModelSpec(name="leaky", provider="cli", command=(exe, "{prompt}"))])


def test_isolation_probe_flags_leaked_instructions(tmp_path):
    # Answers OK to the ping, but quotes a personal instruction file when probed.
    exe = _fake_cli(tmp_path, 'case "$1" in *OK*) echo OK;; *) echo "# Who I Am";; esac')
    (check,) = asyncio.run(run_doctor(_probe_cfg(exe), _probe_cfg(exe).models, [], probe_isolation=True))
    assert not check.ok and check.isolation == "LEAK: # Who I Am"
    assert "isolation: LEAK" in format_report(_probe_cfg(exe), [check])[0]


def test_isolation_probe_clean_and_off_by_default(tmp_path):
    exe = _fake_cli(tmp_path, 'case "$1" in *OK*) echo OK;; *) echo "NONE";; esac')
    cfg = _probe_cfg(exe)
    (probed,) = asyncio.run(run_doctor(cfg, cfg.models, [], probe_isolation=True))
    assert probed.ok and probed.isolation == "clean"
    (plain,) = asyncio.run(run_doctor(cfg, cfg.models, []))
    assert plain.isolation is None  # no extra call unless asked
