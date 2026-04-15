"""
Subprocess utility: run_with_timeout + Windows popup suppression helpers.

Hard guarantee: the child process is always killed and reaped on any
failure mode (timeout, CancelledError, OSError).  Callers never need to
worry about orphaned ssh or python3 - workers accumulating memory.
"""
from __future__ import annotations

import asyncio
import sys


# ---------------------------------------------------------------------------
# Windows console-popup suppression
# ---------------------------------------------------------------------------
# On Windows, spawning any console-subsystem binary (ssh.exe, git.exe, etc.)
# without CREATE_NO_WINDOW causes a brief black console window to flash on
# screen.  All subprocess calls in this module (and helpers that accept
# **_win32_kwargs()) pass these flags so the window never appears.
# On non-Windows the dict is empty — callers can always **_win32_kwargs().

if sys.platform == "win32":
    import subprocess as _subprocess_mod
    import ctypes

    _STARTUPINFO = _subprocess_mod.STARTUPINFO
    _STARTF_USESHOWWINDOW = 0x00000001  # STARTF_USESHOWWINDOW
    _SW_HIDE = 0                        # SW_HIDE

    def _win32_kwargs() -> dict:
        """Return creationflags + startupinfo that suppress console popups."""
        si = _STARTUPINFO()
        si.dwFlags = _STARTF_USESHOWWINDOW
        si.wShowWindow = _SW_HIDE
        return {
            "creationflags": _subprocess_mod.CREATE_NO_WINDOW,
            "startupinfo": si,
        }

    def _win32_asyncio_kwargs() -> dict:
        """Same flags for asyncio.create_subprocess_exec/shell."""
        si = _STARTUPINFO()
        si.dwFlags = _STARTF_USESHOWWINDOW
        si.wShowWindow = _SW_HIDE
        return {
            "creationflags": _subprocess_mod.CREATE_NO_WINDOW,
            "startupinfo": si,
        }
else:
    def _win32_kwargs() -> dict:
        return {}

    def _win32_asyncio_kwargs() -> dict:
        return {}


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
        **_win32_asyncio_kwargs(),
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
