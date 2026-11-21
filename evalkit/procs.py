"""Subprocess helpers: run with a timeout, and on timeout kill the WHOLE process tree.

Why not just proc.kill()? Agentic CLIs (codex, agy, vibe) and test runners spawn children.
proc.kill() only kills the direct child; its children keep running - and a CLI that keeps
running can keep spending quota. So each subprocess starts its own session (process group)
and we kill the group. Also covers cancellation (Ctrl+C), not just timeouts.
"""
from __future__ import annotations

import asyncio
import os
import signal
from typing import Any

POSIX = os.name == "posix"


async def run(argv: list[str], *, input_bytes: bytes | None = None, timeout: float,
              **kwargs: Any) -> tuple[int, bytes, bytes]:
    """Run argv; return (returncode, stdout, stderr). Raises asyncio.TimeoutError after killing the tree."""
    proc = await asyncio.create_subprocess_exec(
        *argv, start_new_session=POSIX,
        stdin=asyncio.subprocess.PIPE if input_bytes is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=kwargs.pop("stderr", asyncio.subprocess.PIPE), **kwargs)
    try:
        out, err = await asyncio.wait_for(proc.communicate(input_bytes), timeout=timeout)
    except BaseException:  # TimeoutError, CancelledError (Ctrl+C), anything: never leave the tree running
        await kill_tree(proc)
        raise
    return proc.returncode or 0, out or b"", err or b""


async def kill_tree(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return
    try:
        if POSIX:
            os.killpg(proc.pid, signal.SIGKILL)  # pid == pgid because of start_new_session
        else:
            proc.kill()  # Windows: no process groups here; best effort
    except ProcessLookupError:
        pass
    await proc.wait()  # reap it: no zombie, no "unclosed transport" warning
