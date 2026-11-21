"""Provider adapters.

The rest of the system depends on ONE narrow interface: `Provider.complete(Request) -> Completion`.
Everything vendor-specific (auth headers, payload shape, how usage is reported,
whether it's an HTTP API, a CLI on your subscription, or you pasting answers by hand)
lives behind it. That's why switching from "no API key" to "API key" is a
one-line config change, not a refactor.
"""
from __future__ import annotations

import asyncio
import difflib
import json
import os
import random
import re
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import httpx

from . import procs
from .config import ModelSpec, RunSettings


@dataclass
class Request:
    prompt: str
    system: str | None = None
    max_tokens: int = 4096
    temperature: float | None = 0.0
    # Hints that real providers ignore. Used by `manual` (task id) and `mock` (reference).
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class Completion:
    text: str
    latency_s: float
    input_tokens: int | None = None
    output_tokens: int | None = None
    # Cost reported by the provider itself (e.g. Claude Code's API-list-price estimate).
    # When None, the runner falls back to the price table in config.
    cost_usd: float | None = None


# A subscription that ran out of its usage allowance. Not a hiccup: it resets in hours, so retrying
# (codex says "try again at 9:15 PM", which also matches the retry pattern) just burns minutes of
# backoff per call. Seen in a real run: grading crawled at ~1 answer per 2.5 min this way.
_QUOTA = re.compile(r"usage limit|hit your (usage )?limit|purchase more credits|requires usage credits|"
                    r"out of (credits|quota)|quota (exceeded|exhausted)|insufficient (credits|quota)|"
                    r"resource.?exhausted", re.I)


def is_quota_message(msg: str) -> bool:
    return bool(_QUOTA.search(msg))


class ProviderError(Exception):
    def __init__(self, msg: str, retryable: bool = False, quota: bool | None = None):
        super().__init__(msg)
        self.quota = is_quota_message(msg) if quota is None else quota
        self.retryable = retryable and not self.quota   # a quota error is never retried


class AnswerPending(Exception):
    """Manual provider: the human hasn't pasted this answer yet. Not a failure."""


class Provider(Protocol):
    async def complete(self, req: Request) -> Completion: ...


# --------------------------------------------------------------------------- HTTP APIs

class _HttpProvider:
    def __init__(self, spec: ModelSpec, settings: RunSettings):
        self.spec, self.settings = spec, settings
        key_env = spec.api_key_env
        self.api_key = os.environ.get(key_env or "", "")
        if not self.api_key:
            raise ProviderError(f"[{spec.name}] env var {key_env!r} is not set")
        self.client = httpx.AsyncClient(timeout=settings.timeout_s)

    def _body(self, base: dict[str, Any]) -> dict[str, Any]:
        # Per-model overrides win; a value of null removes the key
        # (e.g. reasoning models that reject `temperature`).
        merged = {**base, **self.spec.params}
        return {k: v for k, v in merged.items() if v is not None}

    async def _post(self, url: str, headers: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
        for attempt in range(self.settings.max_retries + 1):
            try:
                r = await self.client.post(url, headers=headers, json=body)
            except (httpx.TimeoutException, httpx.TransportError) as e:
                err = ProviderError(f"network: {e!r}", retryable=True)
            else:
                if r.status_code == 200:
                    return r.json()
                retryable = r.status_code == 429 or r.status_code >= 500
                err = ProviderError(f"HTTP {r.status_code}: {r.text[:500]}", retryable)
                if retryable and (ra := r.headers.get("retry-after", "")).replace(".", "").isdigit():
                    await asyncio.sleep(float(ra))
                    continue
            if not err.retryable or attempt == self.settings.max_retries:
                raise err
            # Exponential backoff + jitter: avoid synchronized retry storms.
            await asyncio.sleep(min(60, 2 ** attempt) + random.random())
        raise AssertionError("unreachable")


class AnthropicProvider(_HttpProvider):
    URL = "https://api.anthropic.com/v1/messages"

    async def complete(self, req: Request) -> Completion:
        body = self._body({
            "model": self.spec.model,
            "max_tokens": req.max_tokens,
            "temperature": req.temperature,
            "system": req.system,
            "messages": [{"role": "user", "content": req.prompt}],
        })
        headers = {"x-api-key": self.api_key, "anthropic-version": "2023-06-01"}
        t0 = time.perf_counter()
        data = await self._post(self.spec.base_url or self.URL, headers, body)
        text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
        usage = data.get("usage", {})
        return Completion(text, time.perf_counter() - t0, usage.get("input_tokens"), usage.get("output_tokens"))


class OpenAICompatProvider(_HttpProvider):
    """OpenAI, Gemini, Mistral and GLM (Z.ai) all speak /chat/completions."""

    async def complete(self, req: Request) -> Completion:
        if not self.spec.base_url:
            raise ProviderError(f"[{self.spec.name}] openai_compat needs base_url")
        messages = ([{"role": "system", "content": req.system}] if req.system else []) + [
            {"role": "user", "content": req.prompt}]
        body = self._body({
            "model": self.spec.model,
            "messages": messages,
            "max_tokens": req.max_tokens,
            "temperature": req.temperature,
        })
        headers = {"Authorization": f"Bearer {self.api_key}"}
        t0 = time.perf_counter()
        data = await self._post(self.spec.base_url.rstrip("/") + "/chat/completions", headers, body)
        try:
            text = data["choices"][0]["message"].get("content") or ""
        except (KeyError, IndexError) as e:
            raise ProviderError(f"unexpected response shape: {str(data)[:300]}") from e
        usage = data.get("usage") or {}
        return Completion(text, time.perf_counter() - t0, usage.get("prompt_tokens"), usage.get("completion_tokens"))


# --------------------------------------------------------------------------- Subscription CLIs

class CliProvider:
    """Runs an official CLI (claude / codex / gemini) in headless mode, on your subscription.

    `command` is argv (no shell -> no injection). If an arg contains "{prompt}" it is
    substituted; otherwise the prompt goes to stdin. Runs in an empty temp dir so an
    agentic CLI can't touch your files.

    `isolated_home` points the CLI's home dir (e.g. CODEX_HOME) at a clean directory that
    contains only symlinks to the files it needs to log in. Some CLIs always load global
    instructions from their home (codex: ~/.codex/AGENTS.md) and offer no flag to skip it.
    """

    def __init__(self, spec: ModelSpec, settings: RunSettings):
        if not spec.command:
            raise ProviderError(f"[{spec.name}] cli provider needs `command`")
        if shutil.which(spec.command[0]) is None:  # fail fast: once per model, not once per call
            raise ProviderError(f"[{spec.name}] CLI not found on PATH: {spec.command[0]}")
        self.spec, self.settings = spec, settings
        self.env = {**os.environ, **_prepare_isolated_home(spec)}

    async def complete(self, req: Request) -> Completion:
        full = f"{req.system}\n\n---\n\n{req.prompt}" if req.system else req.prompt
        uses_placeholder = any("{prompt}" in a for a in self.spec.command)
        argv = [a.replace("{prompt}", full) for a in self.spec.command]
        last_err: ProviderError | None = None
        for attempt in range(self.settings.max_retries + 1):
            with tempfile.TemporaryDirectory(prefix="evalkit-cli-") as cwd:
                t0 = time.perf_counter()
                try:
                    code, out, err = await procs.run(
                        argv, cwd=cwd, env=self.env, timeout=self.settings.timeout_s,
                        input_bytes=None if uses_placeholder else full.encode())
                except FileNotFoundError as e:
                    raise ProviderError(f"[{self.spec.name}] CLI not found: {argv[0]}") from e
                except asyncio.TimeoutError:
                    # procs.run already killed the CLI and everything it spawned.
                    last_err = ProviderError(f"cli timeout after {self.settings.timeout_s:.0f}s", retryable=True)
                else:
                    latency = time.perf_counter() - t0
                    stdout = out.decode(errors="replace")
                    # Parsed inside the `with`: some CLIs (vibe) write usage/cost files into cwd.
                    if code == 0:
                        return self._parse(stdout, latency, Path(cwd))
                    last_err = self._failure(code, stdout, err.decode(errors="replace"), Path(cwd))
            if not last_err.retryable:
                break
            await asyncio.sleep(min(120, 5 * 2 ** attempt))
        raise last_err  # type: ignore[misc]

    def _parse(self, stdout: str, latency: float, workdir: Path | None = None) -> Completion:
        parser = _PARSERS.get(self.spec.parse)
        if parser is None:
            return Completion(stdout.strip(), latency)
        return parser(stdout, latency, workdir)

    def _failure(self, code: int, stdout: str, stderr: str, workdir: Path | None = None) -> ProviderError:
        # Structured-output CLIs exit non-zero with the error on STDOUT (claude, codex, agy) or
        # on STDERR with empty stdout (vibe). Ask the parser first, and add stderr's error line
        # when it has one, so neither source is lost.
        err_line = _last_error_line(stderr)
        if self.spec.parse in _PARSERS:
            try:
                self._parse(stdout, 0.0, workdir)
            except ProviderError as e:
                msg = f"{e} | {err_line}" if err_line and err_line not in str(e) else str(e)
                return ProviderError(f"exit {code}: {msg}", e.retryable or _is_retryable(err_line))
        msg = _error_summary(stderr) or _error_summary(stdout)
        return ProviderError(f"exit {code}: {msg}", _is_retryable(msg))


def _prepare_isolated_home(spec: ModelSpec) -> dict[str, str]:
    """{env, dir, link: [files to symlink in], files: {name: content to write}}.
    `files` generates config the CLI only reads from its home (vibe: thinking level per model)."""
    iso = spec.isolated_home
    if not iso:
        return {}
    home = Path(iso["dir"]).expanduser()
    home.mkdir(parents=True, exist_ok=True)
    for name, content in (iso.get("files") or {}).items():
        if Path(name).name != name or name in (".", ".."):
            raise ProviderError(f"[{spec.name}] isolated_home.files: {name!r} must be a plain file name")
        target = home / name
        if not target.exists() or target.read_text(encoding="utf-8") != content:  # rewrite only on change
            target.write_text(content, encoding="utf-8")
    for src in map(lambda x: Path(x).expanduser(), iso.get("link", [])):
        if not src.exists():
            raise ProviderError(f"[{spec.name}] isolated_home: {src} missing (logged in to the CLI?)")
        dst = home / src.name
        # Symlink, not copy: the CLI refreshes tokens in place; a copy would go stale.
        if not (dst.is_symlink() and dst.resolve() == src.resolve()):
            dst.unlink(missing_ok=True)
            dst.symlink_to(src)
    return {iso["env"]: str(home)}


_RETRYABLE = re.compile(r"rate.?limit|429|overloaded|try again|temporarily unavailable", re.I)


def _is_retryable(msg: str) -> bool:
    """Subscription CLIs signal limits in text, not status codes. Anything else (bad model id,
    no credits, auth) is permanent: retrying it just burns minutes of backoff."""
    return bool(_RETRYABLE.search(msg))


def _last_error_line(stderr: str, limit: int = 500) -> str:
    """The last line that looks like an error, or "" (unlike _error_summary, no tail fallback:
    informational stderr like vibe's 'Reading API keys from the keyring' must not become the error)."""
    lines = [ln.strip() for ln in stderr.splitlines() if ln.strip()]
    errors = [ln for ln in lines if re.match(r"(error|fatal)\b", ln, re.I) or ln.startswith("ERROR:")]
    return errors[-1][-limit:] if errors else ""


def _error_summary(stderr: str, limit: int = 500) -> str:
    """CLIs log a lot to stderr (codex dumps whole JSON payloads). The useful part is the
    LAST line that looks like an error; fall back to the tail when nothing does."""
    lines = [ln.strip() for ln in stderr.splitlines() if ln.strip()]
    errors = [ln for ln in lines if re.match(r"(error|fatal)\b", ln, re.I) or ln.startswith("ERROR:")]
    return (errors[-1] if errors else "\n".join(lines))[-limit:]


def parse_claude_json(stdout: str, latency: float) -> Completion:
    """`claude -p --output-format json` -> Completion.

    Token accounting: `usage.input_tokens` only counts UNCACHED tokens (often single digits);
    the real prompt size is input + cache_creation + cache_read. Reporting only the first
    would make Claude look ~1000x cheaper on input than an API provider.
    """
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError as e:
        raise ProviderError(f"claude: non-JSON output: {stdout[:300]!r}") from e
    if data.get("is_error"):
        msg = str(data.get("result"))
        raise ProviderError(f"claude error: {msg}", retryable=_is_retryable(msg))
    usage = data.get("usage") or {}
    parts = [usage.get(k) for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")]
    tokens_in = sum(p for p in parts if p) if any(p is not None for p in parts) else None
    return Completion(data.get("result", ""), latency, tokens_in, usage.get("output_tokens"),
                      cost_usd=data.get("total_cost_usd"))


def parse_codex_jsonl(stdout: str, latency: float) -> Completion:
    """`codex exec --json` -> Completion. One JSON event per line; the answer is the last
    `agent_message`, usage arrives in `turn.completed`, failures as `error` / `turn.failed`.
    OpenAI's input_tokens already INCLUDES cached tokens (unlike Anthropic's)."""
    text, usage, errors = None, {}, []
    for line in stdout.splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue  # codex may interleave non-JSON log lines
        kind = ev.get("type")
        item = ev.get("item") or {}
        if kind == "item.completed" and item.get("type") == "agent_message":
            text = item.get("text", "")
        elif kind == "turn.completed":
            usage = ev.get("usage") or {}
        elif kind == "turn.failed":
            errors.append((ev.get("error") or {}).get("message", "turn failed"))
        elif kind == "error":
            errors.append(ev.get("message", "error"))
    if errors and text is None:
        msg = errors[-1]
        raise ProviderError(f"codex error: {msg}", retryable=_is_retryable(msg))
    if text is None:
        raise ProviderError(f"codex: no agent_message in output: {stdout[-300:]!r}")
    return Completion(text, latency, usage.get("input_tokens"), usage.get("output_tokens"))


def parse_agy_json(stdout: str, latency: float) -> Completion:
    """`agy -p ... --output-format json` (Antigravity CLI, Gemini) -> Completion.

    One JSON envelope. Success is `status == "SUCCESS"` (there is no is_error flag); other
    statuses (ERROR, CANCELED, INTERRUPTED, INVALID, ...) are failures with an `error` string.
    `usage.input_tokens` already includes cached tokens (input + output == total_tokens).
    """
    # agy may print diagnostics before the envelope; the envelope is the last JSON line.
    data = None
    for line in reversed([ln for ln in stdout.splitlines() if ln.strip()]):
        try:
            data = json.loads(line)
            break
        except json.JSONDecodeError:
            continue
    if not isinstance(data, dict) or "status" not in data:
        raise ProviderError(f"agy: no JSON envelope in output: {stdout[-300:]!r}")
    if data["status"] != "SUCCESS":
        msg = str(data.get("error") or data["status"])
        raise ProviderError(f"agy {data['status']}: {msg}", retryable=_is_retryable(msg))
    usage = data.get("usage") or {}
    return Completion(data.get("response", ""), latency, usage.get("input_tokens"), usage.get("output_tokens"))


def parse_vibe_json(stdout: str, latency: float, workdir: Path | None = None) -> Completion:
    """`vibe -p --output json --output-dir <dir>` (Mistral Vibe) -> Completion.

    stdout = the session's entries (user / reasoning / assistant messages); the answer is the
    last completed assistant message. Usage, cost and failures are in <dir>/export.json, which
    the CLI writes into the run's working directory. input_tokens already includes cached.
    """
    export = _find_vibe_export(workdir)
    if export and export.get("error"):
        msg = str(export["error"])
        raise ProviderError(f"vibe {export.get('outcome', 'error')}: {msg}", retryable=_is_retryable(msg))
    # A limit (token_limit, turn_limit, time limit) leaves PARTIAL assistant messages in stdout
    # ("I'll implement it, then test..."). Only outcome == finished means the last one is the answer.
    if export and export.get("outcome") not in (None, "finished"):
        raise ProviderError(f"vibe stopped before answering: outcome={export['outcome']}, "
                            f"stop_reason={export.get('stop_reason')} (raise --max-tokens/--time-limit?)")
    try:
        entries = json.loads(stdout) if stdout.strip() else []
    except json.JSONDecodeError as e:
        raise ProviderError(f"vibe: non-JSON output: {stdout[:300]!r}") from e
    answers = [e for e in entries if isinstance(e, dict) and e.get("type") == "message"
               and e.get("role") == "assistant" and e.get("generationStatus", "completed") == "completed"]
    if not answers:
        outcome = (export or {}).get("outcome", "no export")
        raise ProviderError(f"vibe: no assistant message (outcome: {outcome})")
    text = "".join(c.get("text", "") for c in answers[-1].get("content", []) if c.get("type") == "text")
    usage = (export or {}).get("usage") or {}
    return Completion(text, latency, usage.get("input_tokens"), usage.get("output_tokens"),
                      cost_usd=(export or {}).get("cost_usd"))


def _find_vibe_export(workdir: Path | None) -> dict | None:
    if workdir is None:
        return None
    for f in [workdir / "export.json", *workdir.glob("*/export.json")]:
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
    return None


# Parsers get the run's working directory; only those that need files from it use it.
_PARSERS = {
    "claude_json": lambda out, lat, _wd: parse_claude_json(out, lat),
    "codex_jsonl": lambda out, lat, _wd: parse_codex_jsonl(out, lat),
    "agy_json": lambda out, lat, _wd: parse_agy_json(out, lat),
    "vibe_json": parse_vibe_json,
}


# --------------------------------------------------------------------------- Manual (copy/paste)

MANUAL_DIR = Path("manual")


def manual_paths(model_name: str, task_id: str) -> tuple[Path, Path]:
    d = MANUAL_DIR / model_name
    return d / f"{task_id}.prompt.md", d / f"{task_id}.answer.md"


class ManualProvider:
    """You paste the prompt into the chat UI and save the reply to <task>.answer.md."""

    def __init__(self, spec: ModelSpec, settings: RunSettings):
        self.spec = spec

    async def complete(self, req: Request) -> Completion:
        _, answer = manual_paths(self.spec.name, req.meta["task_id"])
        if not answer.exists() or not answer.read_text(encoding="utf-8").strip():
            raise AnswerPending(str(answer))
        return Completion(answer.read_text(encoding="utf-8"), latency_s=0.0)


# --------------------------------------------------------------------------- Mock (dry runs)

class MockProvider:
    """Lets you test the whole pipeline with zero cost. `model` = good | sloppy | broken."""

    def __init__(self, spec: ModelSpec, settings: RunSettings):
        self.spec = spec

    async def complete(self, req: Request) -> Completion:
        await asyncio.sleep(random.uniform(0.01, 0.05))
        if "criteria" in req.meta:  # acting as a judge
            sim = difflib.SequenceMatcher(None, req.meta["candidate"], req.meta.get("reference") or "").ratio()
            scores = [{"id": c, "score": round(10 * sim), "reason": "mock"} for c in req.meta["criteria"]]
            return Completion(json.dumps({"criteria": scores}), 0.02, 500, 80)
        ref = req.meta.get("reference") or "no reference"
        text = {
            "good": ref,
            "sloppy": ref[: len(ref) // 2] if random.random() < 0.5 else ref,
            "broken": "I'm not sure. Here is some code:\n```python\ndef f(): pass\n```",
            "judge": ref,  # a mock judge asked a non-judge question (e.g. `doctor` ping)
        }[self.spec.model]
        return Completion(text, random.uniform(0.5, 3), len(req.prompt) // 4, len(text) // 4)


_REGISTRY = {
    "anthropic": AnthropicProvider,
    "openai_compat": OpenAICompatProvider,
    "cli": CliProvider,
    "manual": ManualProvider,
    "mock": MockProvider,
}


def make_provider(spec: ModelSpec, settings: RunSettings) -> Provider:
    try:
        return _REGISTRY[spec.provider](spec, settings)
    except KeyError:
        raise ProviderError(f"unknown provider {spec.provider!r}; options: {sorted(_REGISTRY)}") from None
