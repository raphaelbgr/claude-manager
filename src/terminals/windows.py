"""Windows terminal adapters.

Probes are PowerShell one-liners (that's the daemon's default shell on Win via
our policy). PowerShell itself is always present. Windows Terminal (wt.exe)
detection is existence-via-Get-Command.
"""
from __future__ import annotations

import asyncio
import base64
import shutil

from .base import TerminalAdapter
from ..subprocess_utils import _win32_asyncio_kwargs


_PWSH_PATH: str | None = None
_PWSH_PROBED = False


def _wt_shell() -> str:
    """Return the PowerShell host to run inside Windows Terminal.

    Prefers pwsh.exe (PowerShell 7+) over powershell.exe (5.1). PS 5.1 has
    stricter syntax (no `&&`/`||` chain operators, different quoting, no
    ternary) and the inner commands we hand WT — SSH-with-quoted-bash, tmux
    attach lines — routinely trip on it.
    """
    global _PWSH_PATH, _PWSH_PROBED
    if not _PWSH_PROBED:
        _PWSH_PATH = shutil.which("pwsh") or shutil.which("pwsh.exe")
        _PWSH_PROBED = True
    return "pwsh.exe" if _PWSH_PATH else "powershell.exe"


async def _spawn_shell(shell_cmd: str) -> dict:
    try:
        proc = await asyncio.create_subprocess_shell(
            shell_cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            **_win32_asyncio_kwargs(),
        )
        try:
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=5)
            if proc.returncode is not None and proc.returncode != 0:
                return {"ok": False, "error": stderr.decode(errors="replace").strip() or f"rc={proc.returncode}"}
        except asyncio.TimeoutError:
            pass
        return {"ok": True}
    except OSError as exc:
        return {"ok": False, "error": str(exc)}


def _escape_pwsh(s: str) -> str:
    """Escape a string for embedding in a PowerShell double-quoted context."""
    return s.replace('"', '`"')


class WindowsTerminalAdapter(TerminalAdapter):
    id = "wt"
    name = "Windows Terminal"
    os = "win32"
    priority = 100

    def probe_shell(self) -> str:
        return "if (Get-Command wt.exe -ErrorAction SilentlyContinue) { exit 0 } else { exit 1 }"

    async def launch(self, command: str, *, title: str | None = None) -> dict:
        # wt.exe's argument parser treats `;` as a "new tab" separator —
        # even inside a quoted -Command argument — and opens additional
        # tabs using the default profile (typically cmd.exe). For an SSH
        # command like `printf '...'; tmux attach -t name`, that means a
        # pwsh tab plus 2-3 stray cmd tabs containing fragments and
        # errors. Pass the command via -EncodedCommand (base64 UTF-16-LE,
        # PowerShell's standard remote-command transport) so wt sees no
        # `;` characters and routes the whole command to a single pwsh tab.
        encoded = base64.b64encode(command.encode("utf-16-le")).decode("ascii")
        title_arg = f'--title "{_escape_pwsh(title)}" ' if title else ""
        host = _wt_shell()
        shell = f'cmd /c start "" wt.exe {title_arg}-- {host} -NoExit -EncodedCommand {encoded}'
        return await _spawn_shell(shell)


class PowerShellAdapter(TerminalAdapter):
    id = "powershell"
    name = "PowerShell (classic window)"
    os = "win32"
    priority = 80

    def probe_shell(self) -> str:
        return "if (Get-Command powershell.exe -ErrorAction SilentlyContinue) { exit 0 } else { exit 1 }"

    async def launch(self, command: str, *, title: str | None = None) -> dict:
        escaped = _escape_pwsh(command)
        # title is embedded via $Host.UI.RawUI.WindowTitle inside the shell
        title_cmd = f"$Host.UI.RawUI.WindowTitle='{_escape_pwsh(title)}'; " if title else ""
        shell = f'cmd /c start powershell -NoExit -Command "{title_cmd}{escaped}"'
        return await _spawn_shell(shell)


class Pwsh7Adapter(TerminalAdapter):
    id = "pwsh"
    name = "PowerShell 7"
    os = "win32"
    priority = 85

    def probe_shell(self) -> str:
        return "if (Get-Command pwsh.exe -ErrorAction SilentlyContinue) { exit 0 } else { exit 1 }"

    async def launch(self, command: str, *, title: str | None = None) -> dict:
        escaped = _escape_pwsh(command)
        title_cmd = f"$Host.UI.RawUI.WindowTitle='{_escape_pwsh(title)}'; " if title else ""
        shell = f'cmd /c start pwsh -NoExit -Command "{title_cmd}{escaped}"'
        return await _spawn_shell(shell)


class CmdAdapter(TerminalAdapter):
    id = "cmd"
    name = "Command Prompt"
    os = "win32"
    priority = 30

    def probe_shell(self) -> str:
        # cmd.exe is part of every Windows install — probe via existence.
        return "if (Test-Path $env:SystemRoot\\\\System32\\\\cmd.exe) { exit 0 } else { exit 1 }"

    async def launch(self, command: str, *, title: str | None = None) -> dict:
        # Start a new cmd window that runs the command then keeps open (/k).
        t = title.replace('"', '') if title else ""
        shell = f'cmd /c start "{t}" cmd /k "{command}"'
        return await _spawn_shell(shell)


class GitBashAdapter(TerminalAdapter):
    id = "git-bash"
    name = "Git Bash"
    os = "win32"
    priority = 60

    def probe_shell(self) -> str:
        return "if (Test-Path \"$env:ProgramFiles\\Git\\bin\\bash.exe\") { exit 0 } else { exit 1 }"

    async def launch(self, command: str, *, title: str | None = None) -> dict:
        # Git Bash needs a host window — spawn via mintty if available, else
        # via cmd-start which reuses the Windows console. mintty supports titles.
        escaped = command.replace('"', '\\"')
        if title:
            return await _spawn_shell(
                f'cmd /c start "" "%ProgramFiles%\\Git\\usr\\bin\\mintty.exe" '
                f'--title "{title}" -e bash -lc "{escaped}"'
            )
        return await _spawn_shell(
            f'cmd /c start "" "%ProgramFiles%\\Git\\bin\\bash.exe" --login -i -c "{escaped}"'
        )
