"""Concurrency limits, shared by generate, grade (judges) and doctor.

Three caps, because three different things run out:
  * global (run.concurrency)            - your machine: CPU, open processes
  * per subscription (subscription_concurrency) - the vendor's rate limit. 13 Gemini variants
    are 13 models but ONE Google quota: a per-model cap alone would send 13 calls at once.
  * per model (spec.concurrency)        - a model that must run one-at-a-time

Every call acquires them in the same order, NARROWEST FIRST (model -> subscription -> global):
  * same order for every call -> two calls can never hold one each while waiting for the
    other's (no deadlock);
  * narrowest first -> a call only takes a global slot once it can actually run. The reverse
    order (global first) caused head-of-line blocking: jobs are queued model by model, so the
    first model's calls took every global slot and then waited on that model's own lock -
    effective parallelism 1 while every other model sat idle (found in a 1,872-call run).
"""
from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack

from .config import ModelSpec, RunSettings
from .providers import Completion, Provider, ProviderError, Request


class Limits:
    def __init__(self, settings: RunSettings):
        self.settings = settings
        self.global_sem = asyncio.Semaphore(settings.concurrency)
        self._by_subscription: dict[str, asyncio.Semaphore] = {}
        # Circuit breaker: subscription -> the quota error that tripped it. Once a subscription is
        # out of its allowance, every further call to it fails instantly for the rest of this run
        # (it resets in hours, not seconds). A new run starts closed again.
        self.exhausted: dict[str, str] = {}

    def subscription_sem(self, spec: ModelSpec) -> asyncio.Semaphore | None:
        if spec.provider == "manual":
            return None  # reading a file you pasted: no quota to protect
        # CLIs share a login (= a subscription). APIs are rate-limited per key/endpoint too.
        cap = self.settings.subscription_concurrency if spec.provider == "cli" else self.settings.concurrency
        return self._by_subscription.setdefault(spec.subscription, asyncio.Semaphore(cap))

    def wrap(self, spec: ModelSpec, provider: Provider) -> Provider:
        default = 1 if spec.provider in ("cli", "manual") else self.settings.concurrency
        model_sem = asyncio.Semaphore(spec.concurrency or default)
        sems = [s for s in (model_sem, self.subscription_sem(spec), self.global_sem) if s is not None]
        sub = None if spec.provider == "manual" else spec.subscription
        return Limited(provider, sems, self, sub)


class Limited:
    """A Provider that holds every semaphore in `sems` (in order) for the duration of a call."""

    def __init__(self, inner: Provider, sems: list[asyncio.Semaphore], limits: Limits | None = None,
                 subscription: str | None = None):
        self.inner, self.sems, self.limits, self.subscription = inner, sems, limits, subscription

    def _tripped(self) -> str | None:
        return self.limits.exhausted.get(self.subscription) if self.limits and self.subscription else None

    async def complete(self, req: Request) -> Completion:
        if reason := self._tripped():
            raise ProviderError(f"skipped: {self.subscription} is out of quota ({reason[:160]})", quota=True)
        async with AsyncExitStack() as stack:
            for sem in self.sems:
                await stack.enter_async_context(sem)
            if reason := self._tripped():          # tripped while this call was waiting for a slot
                raise ProviderError(f"skipped: {self.subscription} is out of quota ({reason[:160]})", quota=True)
            try:
                return await self.inner.complete(req)
            except ProviderError as e:
                if e.quota and self.limits and self.subscription:
                    self.limits.exhausted.setdefault(self.subscription, str(e))
                raise
