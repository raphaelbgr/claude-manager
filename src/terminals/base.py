"""TerminalAdapter abstract base — the contract every terminal plugs into.

SOLID notes:
- SRP: each concrete adapter knows how to probe and launch exactly ONE terminal.
- OCP: adding a new terminal = subclass + @register. No changes to the launcher,
  server, or UI.
- LSP: every adapter returns {"ok": bool, "error": str|None} from launch, accepts
  the same (command, title) pair — the launcher treats them uniformly.
- ISP: small surface — id/name/os metadata, probe_shell, launch. Adapters that
  don't support a feature (e.g. title) simply ignore that param.
- DIP: launcher depends on this ABC; concrete terminals are injected via the
  registry, not imported directly.
"""
from __future__ import annotations

import abc
from typing import Callable, Awaitable


# Runner contract shared by local + remote probes. Returns (rc, stdout, stderr).
ShellRunner = Callable[[str], Awaitable[tuple[int, bytes, bytes]]]


class TerminalAdapter(abc.ABC):
    # Stable identifier exposed via /api/machines/:id/terminals and used in
    # launch requests. Must be URL/JSON safe and unique across all adapters.
    id: str
    # Human-readable label shown in the dropdown.
    name: str
    # One of 'darwin' | 'linux' | 'win32' — a probe is only attempted on the
    # matching host OS.
    os: str
    # Priority for "auto" pick (higher wins). ~100 = preferred default.
    priority: int = 0

    @abc.abstractmethod
    def probe_shell(self) -> str:
        """Return a shell one-liner that exits 0 iff this terminal is installed.

        Executed via the host's native shell (bash/zsh on Unix, pwsh on Windows).
        Keep it side-effect free — it runs during auto-discovery.
        """

    @abc.abstractmethod
    async def launch(self, command: str, *, title: str | None = None) -> dict:
        """Open a fresh terminal window on the LOCAL machine (the daemon's
        host) and run `command` inside it.

        Returning a window-title when supported is best-effort — a terminal
        that can't set a title just ignores that param. Returns:
            {"ok": True} on success, {"ok": False, "error": str} otherwise.

        Terminal-opening is fire-and-forget by convention: a long-running
        shell inside the terminal does not block this coroutine.
        """
