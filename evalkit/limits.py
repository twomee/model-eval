"""Concurrency limits and quota gates, shared by generate, grade (judges) and doctor.

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

Quota gates (run.on_quota):
  Subscriptions have usage windows (e.g. 5 hours). When one runs out, its gate CLOSES:
  * "wait" (default for runs): calls to that subscription pause - holding no slot, so other
    subscriptions keep going - until the reset time from the error message ("try again at
    9:15 PM"), or a re-check every quota_probe_min when the message has none. At that time ONE
    call goes through as a probe: success opens the gate for everyone; another quota error keeps
    it closed until the new reset time. No thundering herd of failing calls at the reset minute.
    After quota_max_wait_h the run gives up on that subscription (calls fail as below).
  * "fail" (doctor): every further call fails instantly for the rest of the run.
"""
from __future__ import annotations

import asyncio
import time
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Callable

from .config import ModelSpec, RunSettings
from .providers import Completion, Provider, ProviderError, Request, quota_reset_at

# (subscription, paused_until or None when resumed/given up, reason) - for progress/UI
QuotaListener = Callable[[str, "float | None", str], None]


@dataclass
class _Gate:
    open: asyncio.Event = field(default_factory=asyncio.Event)
    probe: asyncio.Lock = field(default_factory=asyncio.Lock)
    reset_at: float = 0.0
    reason: str = ""
    since: float = 0.0          # when it first closed (for quota_max_wait_h)
    gave_up: bool = False

    def __post_init__(self) -> None:
        self.open.set()


class Limits:
    def __init__(self, settings: RunSettings, on_quota_change: QuotaListener | None = None,
                 clock: Callable[[], float] = time.time):
        self.settings = settings
        self.global_sem = asyncio.Semaphore(settings.concurrency)
        self._by_subscription: dict[str, asyncio.Semaphore] = {}
        self._gates: dict[str, _Gate] = {}
        self.listener = on_quota_change
        self.clock = clock

    @property
    def exhausted(self) -> dict[str, str]:
        """Subscriptions whose gate is closed -> the quota error that closed it."""
        return {s: g.reason for s, g in self._gates.items() if not g.open.is_set()}

    def gate(self, subscription: str) -> _Gate:
        return self._gates.setdefault(subscription, _Gate())

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

    # ------------------------------------------------------------------ gate transitions

    def close(self, sub: str, reason: str) -> None:
        g = self.gate(sub)
        now = self.clock()
        if g.open.is_set():
            g.since = now
        g.open.clear()
        g.reason = reason
        parsed = quota_reset_at(reason, now)
        # +60 s: providers' clocks and "9:15 PM" rounding; a probe a minute late beats one too early.
        g.reset_at = parsed + 60 if parsed and parsed > now else now + self.settings.quota_probe_min * 60
        if self.settings.on_quota == "wait" and now - g.since >= self.settings.quota_max_wait_h * 3600:
            g.gave_up = True
        if self.listener:
            self.listener(sub, None if g.gave_up or self.settings.on_quota != "wait" else g.reset_at, reason)

    def reopen(self, sub: str) -> None:
        g = self.gate(sub)
        if not g.open.is_set():
            g.open.set()
            g.gave_up = False
            if self.listener:
                self.listener(sub, None, "")

    def _skip(self, sub: str) -> ProviderError:
        g = self.gate(sub)
        why = "gave up waiting for its reset" if g.gave_up else "out of quota"
        return ProviderError(f"skipped: {sub} is {why} ({g.reason[:160]})", quota=True)

    async def admit(self, sub: str | None) -> bool:
        """Wait until a call to `sub` may go out. Returns True if this call is the PROBE: it holds
        the probe lock and must report back through settle() (or release it if cancelled)."""
        if sub is None:
            return False
        g = self.gate(sub)
        while not g.open.is_set():
            if self.settings.on_quota != "wait" or g.gave_up:
                raise self._skip(sub)
            delay = g.reset_at - self.clock()
            if delay > 0:
                try:  # sleep until the reset time, or until another call's probe reopens the gate
                    await asyncio.wait_for(g.open.wait(), timeout=min(delay, 60))
                except asyncio.TimeoutError:
                    pass
                continue
            if not g.probe.locked():
                await g.probe.acquire()
                if g.open.is_set():          # reopened while we were acquiring
                    g.probe.release()
                    return False
                return True                  # reset time reached: this call tests the water
            try:
                await asyncio.wait_for(g.open.wait(), timeout=5)   # another call is probing
            except asyncio.TimeoutError:
                pass
        return False

    def settle(self, sub: str | None, probe: bool, quota_error: str | None) -> None:
        if sub is None:
            return
        if quota_error is not None:
            self.close(sub, quota_error)
        elif probe or not self.gate(sub).open.is_set():
            self.reopen(sub)
        if probe:
            self.gate(sub).probe.release()


class Limited:
    """A Provider that waits for its subscription's quota gate, then holds every semaphore in
    `sems` (in order) for the duration of the call."""

    def __init__(self, inner: Provider, sems: list[asyncio.Semaphore], limits: Limits | None = None,
                 subscription: str | None = None):
        self.inner, self.sems, self.limits, self.subscription = inner, sems, limits, subscription

    async def complete(self, req: Request) -> Completion:
        if self.limits is None:
            return await self._call(req)
        limits = self.limits
        while True:
            probe = await limits.admit(self.subscription)      # holds no slot while waiting
            try:
                result = await self._call(req)
            except ProviderError as e:
                if e.quota:
                    limits.settle(self.subscription, probe, str(e))
                    if limits.settings.on_quota == "wait" and not limits.gate(self.subscription).gave_up:
                        continue                                # paused until the reset, then retried
                    raise
                limits.settle(self.subscription, probe, None)
                raise
            except BaseException:
                if probe:  # cancelled mid-probe: free the lock so a later call can probe
                    limits.gate(self.subscription).probe.release()
                raise
            limits.settle(self.subscription, probe, None)
            return result

    async def _call(self, req: Request) -> Completion:
        async with AsyncExitStack() as stack:
            for sem in self.sems:
                await stack.enter_async_context(sem)
            return await self.inner.complete(req)
