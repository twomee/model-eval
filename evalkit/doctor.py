"""Preflight: can every competitor and judge actually answer right now?

Why a separate command: a real run costs minutes and subscription quota per model.
Finding out at call 40/120 that a CLI is outdated, a model id was retired, or a judge
is missing is the expensive way to learn it. `doctor` sends ONE trivial prompt per
model with retries disabled, so a broken entry fails in seconds with its real error.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace

from .config import Config, ModelSpec
from .providers import ProviderError, Request, make_provider, manual_paths

PING = "Reply with exactly the word OK and nothing else."


@dataclass
class Check:
    name: str
    role: str          # "model" | "judge"
    ok: bool
    detail: str


async def _check(spec: ModelSpec, role: str, cfg: Config, task_ids: list[str]) -> Check:
    if spec.provider == "manual":
        # Nothing to call; report how many pasted answers exist instead.
        have = sum(1 for t in task_ids if (p := manual_paths(spec.name, t)[1]).exists() and p.read_text().strip())
        return Check(spec.name, role, True, f"manual: {have}/{len(task_ids)} answers saved")
    settings = replace(cfg.run, max_retries=0, timeout_s=min(cfg.run.timeout_s, 120))
    try:
        provider = make_provider(spec, settings)
        comp = await provider.complete(Request(prompt=PING, max_tokens=50, temperature=0.0,
                                               meta={"task_id": "doctor", "reference": "OK"}))
    except ProviderError as e:
        return Check(spec.name, role, False, str(e)[:300])
    except Exception as e:  # a bug in an adapter must show up as a failed check, not kill the report
        return Check(spec.name, role, False, f"internal error {type(e).__name__}: {e}"[:300])
    text = comp.text.strip()
    if not text:
        return Check(spec.name, role, False, "empty answer (parser mismatch? check `parse:`)")
    note = "" if text.rstrip(".").upper() == "OK" else f" (answered {text[:40]!r})"
    return Check(spec.name, role, True, f"{comp.latency_s:.1f}s, in={comp.input_tokens} out={comp.output_tokens}{note}")


async def run_doctor(cfg: Config, models: list[ModelSpec], task_ids: list[str]) -> list[Check]:
    # Different vendors in parallel (independent quotas); still one call each.
    jobs = [(m, "model") for m in models] + [(j, "judge") for j in cfg.judges]
    return list(await asyncio.gather(*(_check(s, r, cfg, task_ids) for s, r in jobs)))


def judge_vendors(cfg: Config, checks: list[Check]) -> set[str]:
    """Vendors with a WORKING judge. Self-preference bias is per family, so what matters is
    how many distinct families can actually judge, not how many judges are configured."""
    ok = {c.name for c in checks if c.role == "judge" and c.ok}
    return {vendor_of(j) for j in cfg.judges if j.name in ok}


def vendor_of(spec: ModelSpec) -> str:
    if spec.provider == "cli" and spec.command:
        return {"codex": "openai"}.get(spec.command[0], spec.command[0])
    if spec.provider == "anthropic":
        return "anthropic"
    return spec.base_url or spec.provider


def format_report(cfg: Config, checks: list[Check]) -> tuple[str, bool]:
    lines = [f"{'':2}{'role':<6} {'name':<20} result"]
    for c in checks:
        lines.append(f"{'OK' if c.ok else 'XX':<2} {c.role:<6} {c.name:<20} {c.detail}")
    vendors = judge_vendors(cfg, checks)
    if len(vendors) < 2:
        lines.append(f"\nWARNING: working judges cover {len(vendors)} vendor(s) {sorted(vendors)}. "
                     "Rubric scores will carry that family's self-preference bias; add a judge from another vendor.")
    all_ok = all(c.ok for c in checks)
    lines.append("\nall checks passed" if all_ok else
                 f"\n{sum(not c.ok for c in checks)} check(s) failed: those models will be skipped or error during `run`.")
    return "\n".join(lines), all_ok
