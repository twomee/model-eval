"""Variant expansion (model x effort) and effort validation."""
from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

from evalkit.config import ModelSpec, load_config
from evalkit.variants import effort_error, expand_variants

ROOT = Path(__file__).parent.parent


def entry(**over):
    e = {"name": "gpt", "provider": "cli", "command": ["codex", "-c", 'model_reasoning_effort="{effort}"', "{prompt}"]}
    e.update(over)
    return e


def test_single_variant_still_gets_suffix():
    # [medium] -> [medium, high] later must not rename the medium results.
    (only,) = expand_variants(entry(variants={"effort": ["medium"]}))
    assert only["name"] == "gpt-medium"
    assert only["command"] == ["codex", "-c", 'model_reasoning_effort="medium"', "{prompt}"]  # {prompt} kept


def test_cartesian_product_of_keys():
    e = entry(command=["x", "-m", "{model}", "--effort", "{effort}"],
              variants={"model": ["a", "b"], "effort": ["low", "high"]})
    names = [v["name"] for v in expand_variants(e)]
    assert names == ["gpt-a-low", "gpt-a-high", "gpt-b-low", "gpt-b-high"]


def test_substitutes_in_params_for_api_providers():
    e = {"name": "api", "provider": "openai_compat", "model": "m",
         "params": {"reasoning_effort": "{effort}"}, "variants": {"effort": ["high"]}}
    assert expand_variants(e)[0]["params"] == {"reasoning_effort": "high"}


def test_no_variants_is_its_own_family_and_a_default():
    e = {"name": "plain", "provider": "cli", "command": ["claude", "-p"]}
    assert expand_variants(e) == [{**e, "family": "plain", "default": True}]


def test_defaults_mark_which_variants_a_plain_run_uses():
    out = expand_variants(entry(variants={"effort": ["low", "medium", "high"]}, defaults={"effort": ["medium"]}))
    assert {v["name"]: v["default"] for v in out} == {"gpt-low": False, "gpt-medium": True, "gpt-high": False}
    assert {v["family"] for v in out} == {"gpt"} and all("defaults" not in v for v in out)


def test_without_defaults_every_variant_is_default():
    assert all(v["default"] for v in expand_variants(entry(variants={"effort": ["low", "high"]})))


@pytest.mark.parametrize("defaults,msg", [
    ({"effort": ["ultra"]}, "not in variants"),
    ({"temp": [1]}, "not a variant key"),
])
def test_defaults_errors(defaults, msg):
    with pytest.raises(ValueError, match=msg):
        expand_variants(entry(variants={"effort": ["low"]}, defaults=defaults))


@pytest.mark.parametrize("bad,msg", [
    (entry(), "no `variants`"),                                                  # placeholder, no values
    (entry(variants={"effort": ["low"], "temp": [0, 1]}), "not used"),          # key would make duplicates
    (entry(command=["x", "{effort}", "{seed}"], variants={"effort": ["low"]}), "no values"),
    (entry(variants={"effort": []}), "non-empty list"),
])
def test_expansion_errors(bad, msg):
    with pytest.raises(ValueError, match=msg):
        expand_variants(bad)


# ---------------------------------------------------------------- effort parsing + validation

@pytest.mark.parametrize("command,expected", [
    (("claude", "-p", "--effort", "high"), "high"),
    (("codex", "exec", "-c", 'model_reasoning_effort="xhigh"', "{prompt}"), "xhigh"),
    (("codex", "exec", "-c", "model_reasoning_effort=low", "{prompt}"), "low"),
    (("codex", "exec", "-c", 'sandbox="read-only"', "{prompt}"), None),
])
def test_effort_read_from_final_command(command, expected):
    assert ModelSpec(name="m", provider="cli", command=command).effort == expected


def test_claude_effort_typo_is_rejected():
    # claude itself only warns and silently runs at its default effort
    assert "silently" in effort_error("c", ("claude", "-p"), "claude-opus-5-5", "maximum", None)
    assert effort_error("c", ("claude", "-p"), "claude-opus-5-5", "xhigh", None) is None


def _cache(tmp_path: Path) -> Path:
    p = tmp_path / "models_cache.json"
    p.write_text(json.dumps({"models": [
        {"slug": "gpt-6-luna", "supported_reasoning_levels": [{"effort": e} for e in ["low", "medium", "high", "max"]]},
    ]}), encoding="utf-8")
    return p


def test_codex_effort_checked_against_model_support(tmp_path):
    cache = _cache(tmp_path)
    cmd = ("codex", "exec")
    assert effort_error("l", cmd, "gpt-6-luna", "max", cache) is None
    assert "not 'ultra'" in effort_error("l", cmd, "gpt-6-luna", "ultra", cache)  # codex would accept it
    assert "not in codex's model list" in effort_error("l", cmd, "gpt-9-typo", "low", cache)
    assert effort_error("l", cmd, "gpt-9-typo", "low", tmp_path / "absent.json") is None  # can't know: allow


def test_load_config_reports_invalid_effort(tmp_path):
    cfg = tmp_path / "c.yaml"
    cfg.write_text(textwrap.dedent("""
        models:
          - name: opus
            provider: cli
            command: [claude, -p, --effort, "{effort}"]
            variants: {effort: [medium, maxx]}
    """), encoding="utf-8")
    with pytest.raises(ValueError, match="opus-maxx"):
        load_config(cfg)


def test_repo_config_pins_effort_for_every_cli_model():
    cfg = load_config(ROOT / "config.yaml")
    unpinned = [m.name for m in (*cfg.models, *cfg.judges) if m.provider == "cli" and m.effort is None]
    assert unpinned == [], "unpinned effort = codex runs with reasoning 'none' in an isolated home"


@pytest.mark.parametrize("model_timeout,expected", [(900, 900), (None, 300)])
def test_per_model_timeout_overrides_run_setting(monkeypatch, model_timeout, expected):
    from evalkit import runner
    from evalkit.config import Config, RunSettings
    seen = []
    monkeypatch.setattr(runner, "make_provider", lambda spec, settings: seen.append(settings.timeout_s))
    cfg = Config(run=RunSettings(timeout_s=300), models=[], judges=[], category_weights={})
    runner._limited(ModelSpec(name="m", provider="mock", model="good", timeout_s=model_timeout), cfg)
    assert seen == [expected]
