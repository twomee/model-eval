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


class ProviderError(Exception):
    def __init__(self, msg: str, retryable: bool = False):
        super().__init__(msg)
        self.retryable = retryable


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
    """

    def __init__(self, spec: ModelSpec, settings: RunSettings):
        if not spec.command:
            raise ProviderError(f"[{spec.name}] cli provider needs `command`")
        if shutil.which(spec.command[0]) is None:  # fail fast: once per model, not once per call
            raise ProviderError(f"[{spec.name}] CLI not found on PATH: {spec.command[0]}")
        self.spec, self.settings = spec, settings

    async def complete(self, req: Request) -> Completion:
        full = f"{req.system}\n\n---\n\n{req.prompt}" if req.system else req.prompt
        uses_placeholder = any("{prompt}" in a for a in self.spec.command)
        argv = [a.replace("{prompt}", full) for a in self.spec.command]
        last_err: ProviderError | None = None
        for attempt in range(self.settings.max_retries + 1):
            with tempfile.TemporaryDirectory(prefix="evalkit-cli-") as cwd:
                t0 = time.perf_counter()
                try:
                    proc = await asyncio.create_subprocess_exec(
                        *argv, cwd=cwd,
                        stdin=asyncio.subprocess.DEVNULL if uses_placeholder else asyncio.subprocess.PIPE,
                        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                except FileNotFoundError as e:
                    raise ProviderError(f"[{self.spec.name}] CLI not found: {argv[0]}") from e
                try:
                    out, err = await asyncio.wait_for(
                        proc.communicate(None if uses_placeholder else full.encode()),
                        timeout=self.settings.timeout_s)
                except asyncio.TimeoutError:
                    proc.kill()
                    last_err = ProviderError("cli timeout", retryable=True)
                else:
                    latency = time.perf_counter() - t0
                    if proc.returncode == 0:
                        return self._parse(out.decode(errors="replace"), latency)
                    msg = _error_summary(err.decode(errors="replace"))
                    last_err = ProviderError(f"exit {proc.returncode}: {msg}", _is_retryable(msg))
            if not last_err.retryable:
                break
            await asyncio.sleep(min(120, 5 * 2 ** attempt))
        raise last_err  # type: ignore[misc]

    def _parse(self, stdout: str, latency: float) -> Completion:
        if self.spec.parse == "claude_json":
            return parse_claude_json(stdout, latency)
        return Completion(stdout.strip(), latency)


_RETRYABLE = re.compile(r"rate.?limit|429|overloaded|try again|temporarily unavailable", re.I)


def _is_retryable(msg: str) -> bool:
    """Subscription CLIs signal limits in text, not status codes. Anything else (bad model id,
    no credits, auth) is permanent: retrying it just burns minutes of backoff."""
    return bool(_RETRYABLE.search(msg))


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
