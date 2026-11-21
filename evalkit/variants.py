"""Model variants (e.g. reasoning effort) and their validation.

One config entry can stand for several competitors:

    - name: gpt-sol
      command: [..., "-c", 'model_reasoning_effort="{effort}"', "{prompt}"]
      variants: {effort: [low, high]}

expands to `gpt-sol-low` and `gpt-sol-high`, each with {effort} substituted in command,
model and params. Several keys expand to their cartesian product.

`variants` lists what EXISTS; optional `defaults: {effort: [medium]}` lists what a plain
`run` uses. Keeping the two apart lets the UI offer every effort without a bare `run`
quietly spending quota on all of them. Without `defaults`, every variant is a default.

Design choices:
  * Every variant gets a suffix, even with a single value. Results are keyed by name, so
    turning [medium] into [medium, high] later must not rename the medium results.
  * Effort values are validated HERE, because the CLIs don't do it reliably:
    `claude --effort bogus` prints a warning and runs at the default effort, and codex
    silently accepts an effort the model doesn't support (e.g. ultra on gpt-6-luna).
    Without this check, a result labelled "max" could really be the default.
"""
from __future__ import annotations

import itertools
import json
import re
from pathlib import Path
from typing import Any

# From `claude --help` (2.1.330). Update when the CLI adds levels.
CLAUDE_EFFORTS = ("low", "medium", "high", "xhigh", "max")

_PLACEHOLDER = re.compile(r"\{(\w+)\}")
_RESERVED = {"prompt"}  # substituted per call by CliProvider, not at load time


def _substitute(value: Any, subs: dict[str, str]) -> Any:
    if isinstance(value, str):
        return _PLACEHOLDER.sub(lambda m: subs.get(m.group(1), m.group(0)), value)
    if isinstance(value, list):
        return [_substitute(v, subs) for v in value]
    if isinstance(value, dict):
        return {k: _substitute(v, subs) for k, v in value.items()}
    return value


def _placeholders(value: Any) -> set[str]:
    if isinstance(value, str):
        return set(_PLACEHOLDER.findall(value))
    if isinstance(value, list):
        return set().union(*map(_placeholders, value)) if value else set()
    if isinstance(value, dict):
        return set().union(*map(_placeholders, value.values())) if value else set()
    return set()


_SUBSTITUTED_FIELDS = ("command", "model", "params")


def expand_variants(entry: dict[str, Any]) -> list[dict[str, Any]]:
    """One raw config entry -> one entry per variant combination (or itself, unchanged)."""
    variants = entry.get("variants")
    used = set().union(*(_placeholders(entry.get(f)) for f in _SUBSTITUTED_FIELDS)) - _RESERVED
    name = entry.get("name", "?")
    if not variants:
        if used:
            raise ValueError(f"model {name}: placeholders {sorted(used)} but no `variants` to fill them")
        if "defaults" in entry:
            raise ValueError(f"model {name}: `defaults` without `variants`")
        return [{**entry, "family": name, "default": True}]
    if not isinstance(variants, dict) or not all(isinstance(v, list) and v for v in variants.values()):
        raise ValueError(f"model {name}: `variants` must map each key to a non-empty list")
    if unused := set(variants) - used:
        # Would create identical competitors under different names.
        raise ValueError(f"model {name}: variant keys {sorted(unused)} are not used as {{placeholders}}")
    if missing := used - set(variants):
        raise ValueError(f"model {name}: placeholders {sorted(missing)} have no values in `variants`")

    defaults = entry.get("defaults") or {}
    if not isinstance(defaults, dict):
        raise ValueError(f"model {name}: `defaults` must map variant keys to lists")
    for k, vals in defaults.items():
        if k not in variants:
            raise ValueError(f"model {name}: defaults key {k!r} is not a variant key")
        if bad := [v for v in vals if v not in variants[k]]:
            raise ValueError(f"model {name}: defaults {k}={bad} not in variants {variants[k]}")

    keys = list(variants)
    out = []
    for combo in itertools.product(*(variants[k] for k in keys)):
        subs = dict(zip(keys, map(str, combo)))
        e = {k: v for k, v in entry.items() if k not in ("variants", "defaults")}
        e["family"] = name
        e["default"] = all(subs[k] in map(str, defaults[k]) for k in defaults)
        for f in _SUBSTITUTED_FIELDS:
            if f in e:
                e[f] = _substitute(e[f], subs)
        e["name"] = "-".join([name, *subs.values()])
        e["variant"] = subs
        out.append(e)
    return out


def effort_error(name: str, command: tuple[str, ...], model_id: str | None, effort: str | None,
                 codex_models_cache: Path | None) -> str | None:
    """Return why this effort is invalid for this CLI/model, or None if it's fine or unknowable."""
    if effort is None or not command:
        return None
    cli = Path(command[0]).name
    if cli == "claude" and effort not in CLAUDE_EFFORTS:
        return f"model {name}: claude effort {effort!r} not in {CLAUDE_EFFORTS} (claude would silently use its default)"
    if cli == "codex" and codex_models_cache and codex_models_cache.exists() and model_id:
        try:
            models = json.loads(codex_models_cache.read_text(encoding="utf-8")).get("models", [])
        except (OSError, json.JSONDecodeError):
            return None  # unreadable cache: can't validate, don't block the run
        info = next((m for m in models if m.get("slug") == model_id), None)
        if info is None:
            return f"model {name}: {model_id!r} is not in codex's model list ({codex_models_cache})"
        levels = [lvl.get("effort") for lvl in info.get("supported_reasoning_levels", [])]
        if levels and effort not in levels:
            return f"model {name}: {model_id} supports efforts {levels}, not {effort!r} (codex would not refuse it)"
    return None
