"""
Subprocess utility: run_with_timeout + Windows popup suppression helpers.

Hard guarantee: the child process is always killed and reaped on any
failure mode (timeout, CancelledError, OSError).  Callers never need to
worry about orphaned ssh or python3 - workers accumulating memory.
"""
from __future__ import annotations

import asyncio
import logging
import sys

log = logging.getLogger("claude_manager.subprocess_utils")


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

    _SESSION_ID: int | None = None

    def _win32_is_session_zero() -> bool:
        """True when the process runs in Session 0 (background/service context).

        Background processes (WMI-spawned, services, SSH sessions) run in
        Session 0 where UI windows are invisible to the logged-in user.
        """
        global _SESSION_ID
        if _SESSION_ID is None:
            try:
                pid = ctypes.windll.kernel32.GetCurrentProcessId()
                sid = ctypes.c_ulong()
                ctypes.windll.kernel32.ProcessIdToSessionId(pid, ctypes.byref(sid))
                _SESSION_ID = sid.value
            except Exception:
                _SESSION_ID = -1
            log.info("win32 session id: %d (session_zero=%s)", _SESSION_ID, _SESSION_ID == 0)
        return _SESSION_ID == 0

    async def _win32_spawn_in_user_session(shell_cmd: str) -> dict:
        """Run shell_cmd in the interactive desktop session via a scheduled task.

        Session 0 processes cannot create visible windows.  This writes the
        command to a temp .cmd file and launches it through schtasks, which
        targets the logged-in user's interactive desktop.
        """
        import os
        import tempfile
        import uuid

        task_id = uuid.uuid4().hex[:8]
        task_name = f"cm-{task_id}"
        bat = os.path.join(tempfile.gettempdir(), f"{task_name}.cmd")

        with open(bat, "w", encoding="utf-8") as f:
            f.write(f"@echo off\r\n{shell_cmd}\r\n")

        try:
            log.info("session_zero: creating schtask %s -> %s", task_name, bat)
            proc = await asyncio.create_subprocess_shell(
                f'schtasks /Create /TN "{task_name}" /TR "{bat}" /SC ONCE /ST 00:00 /F',
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
                **_win32_asyncio_kwargs(),
            )
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=5)
            if proc.returncode != 0:
                err = f"schtasks create: {stderr.decode(errors='replace').strip()}"
                log.error("session_zero: %s", err)
                return {"ok": False, "error": err}

            log.info("session_zero: running schtask %s", task_name)
            proc = await asyncio.create_subprocess_shell(
                f'schtasks /Run /TN "{task_name}"',
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
                **_win32_asyncio_kwargs(),
            )
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=5)
            if proc.returncode != 0:
                err = f"schtasks run: {stderr.decode(errors='replace').strip()}"
                log.error("session_zero: %s", err)
                return {"ok": False, "error": err}

            log.info("session_zero: schtask %s launched successfully", task_name)
            asyncio.ensure_future(_schtask_cleanup(task_name, bat))
            return {"ok": True}
        except Exception as exc:
            try:
                os.unlink(bat)
            except Exception:
                pass
            return {"ok": False, "error": str(exc)}

    async def _schtask_cleanup(task_name: str, bat_path: str) -> None:
        import os
        await asyncio.sleep(5)
        try:
            proc = await asyncio.create_subprocess_shell(
                f'schtasks /Delete /TN "{task_name}" /F',
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                **_win32_asyncio_kwargs(),
            )
            await asyncio.wait_for(proc.communicate(), timeout=5)
        except Exception:
            pass
        try:
            os.unlink(bat_path)
        except Exception:
            pass

else:
    def _win32_kwargs() -> dict:
        return {}

    def _win32_asyncio_kwargs() -> dict:
        return {}

    def _win32_is_session_zero() -> bool:
        return False

    async def _win32_spawn_in_user_session(shell_cmd: str) -> dict:
        return {"ok": False, "error": "Not Windows"}


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
