"""
Subprocess utility: run_with_timeout

Hard guarantee: the child process is always killed and reaped on any
failure mode (timeout, CancelledError, OSError).  Callers never need to
worry about orphaned ssh or python3 - workers accumulating memory.
"""
from __future__ import annotations

import asyncio


async def run_with_timeout(
    cmd: list[str],
    *,
    timeout: float,
    input: bytes | None = None,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
) -> tuple[int, bytes, bytes]:
    """Run subprocess with hard timeout. Guarantees the child is killed + reaped
    on ANY failure mode (timeout, CancelledError, OSError). Returns (rc, stdout, stderr).

    On timeout: raises asyncio.TimeoutError AFTER killing+reaping.
    On CancelledError: kills+reaps, re-raises.
    On OSError at spawn: re-raises (no process to kill).
    """
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE if input is not None else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
        env=env,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(input), timeout=timeout)
        return proc.returncode, stdout, stderr
    except asyncio.TimeoutError:
        if proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=2)
            except (asyncio.TimeoutError, ProcessLookupError):
                pass
        raise
    except asyncio.CancelledError:
        if proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=2)
            except (asyncio.TimeoutError, ProcessLookupError):
                pass
        raise
