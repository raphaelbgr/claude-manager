"""Linux terminal adapters. Detection via `command -v` against PATH."""
from __future__ import annotations

import asyncio
import shlex

from .base import TerminalAdapter
from ..tracking import tl


async def _spawn(argv: list[str]) -> dict:
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=3)
            if proc.returncode is not None and proc.returncode != 0:
                return {"ok": False, "error": stderr.decode(errors="replace").strip() or f"rc={proc.returncode}"}
        except asyncio.TimeoutError:
            pass  # terminal emulator running — expected
        return {"ok": True}
    except FileNotFoundError:
        return {"ok": False, "error": f"{argv[0]} not found"}
    except OSError as exc:
        return {"ok": False, "error": str(exc)}


def _wrap(command: str) -> str:
    """Keep the shell alive after the command exits so the user can inspect output."""
    return f"{command}; exec bash"


class GnomeTerminalAdapter(TerminalAdapter):
    id = "gnome-terminal"
    name = "GNOME Terminal"
    os = "linux"
    priority = 100

    def probe_shell(self) -> str:
        return "command -v gnome-terminal >/dev/null 2>&1"

    async def launch(self, command: str, *, title: str | None = None) -> dict:
        argv = ["gnome-terminal"]
        if title:
            argv += ["--title", title]
        argv += ["--", "bash", "-lc", _wrap(command)]
        try:
            tl.event(
                "cm.adapter.spawn",
                adapter=self.id,
                kind="gnome-terminal",
                title_present=bool(title),
                cmd_head=(command or "")[:120],
            )
        except Exception:
            pass
        return await _spawn(argv)


class KonsoleAdapter(TerminalAdapter):
    id = "konsole"
    name = "Konsole"
    os = "linux"
    priority = 90

    def probe_shell(self) -> str:
        return "command -v konsole >/dev/null 2>&1"

    async def launch(self, command: str, *, title: str | None = None) -> dict:
        argv = ["konsole"]
        if title:
            argv += ["-p", f"tabtitle={title}"]
        argv += ["-e", "bash", "-lc", _wrap(command)]
        try:
            tl.event(
                "cm.adapter.spawn",
                adapter=self.id,
                kind="konsole",
                title_present=bool(title),
                cmd_head=(command or "")[:120],
            )
        except Exception:
            pass
        return await _spawn(argv)


class Xfce4TerminalAdapter(TerminalAdapter):
    id = "xfce4-terminal"
    name = "Xfce Terminal"
    os = "linux"
    priority = 70

    def probe_shell(self) -> str:
        return "command -v xfce4-terminal >/dev/null 2>&1"

    async def launch(self, command: str, *, title: str | None = None) -> dict:
        argv = ["xfce4-terminal"]
        if title:
            argv += ["--title", title]
        argv += ["-e", f"bash -lc {shlex.quote(_wrap(command))}"]
        try:
            tl.event(
                "cm.adapter.spawn",
                adapter=self.id,
                kind="xfce4-terminal",
                title_present=bool(title),
                cmd_head=(command or "")[:120],
            )
        except Exception:
            pass
        return await _spawn(argv)


class AlacrittyLinuxAdapter(TerminalAdapter):
    id = "alacritty"
    name = "Alacritty"
    os = "linux"
    priority = 80

    def probe_shell(self) -> str:
        return "command -v alacritty >/dev/null 2>&1"

    async def launch(self, command: str, *, title: str | None = None) -> dict:
        argv = ["alacritty"]
        if title:
            argv += ["--title", title]
        argv += ["-e", "bash", "-lc", _wrap(command)]
        try:
            tl.event(
                "cm.adapter.spawn",
                adapter=self.id,
                kind="alacritty-linux",
                title_present=bool(title),
                cmd_head=(command or "")[:120],
            )
        except Exception:
            pass
        return await _spawn(argv)


class KittyLinuxAdapter(TerminalAdapter):
    id = "kitty"
    name = "kitty"
    os = "linux"
    priority = 75

    def probe_shell(self) -> str:
        return "command -v kitty >/dev/null 2>&1"

    async def launch(self, command: str, *, title: str | None = None) -> dict:
        argv = ["kitty"]
        if title:
            argv += ["--title", title]
        argv += ["bash", "-lc", _wrap(command)]
        try:
            tl.event(
                "cm.adapter.spawn",
                adapter=self.id,
                kind="kitty-linux",
                title_present=bool(title),
                cmd_head=(command or "")[:120],
            )
        except Exception:
            pass
        return await _spawn(argv)


class XtermAdapter(TerminalAdapter):
    id = "xterm"
    name = "xterm"
    os = "linux"
    priority = 10

    def probe_shell(self) -> str:
        return "command -v xterm >/dev/null 2>&1"

    async def launch(self, command: str, *, title: str | None = None) -> dict:
        argv = ["xterm"]
        if title:
            argv += ["-T", title]
        argv += ["-e", f"bash -lc {shlex.quote(_wrap(command))}"]
        try:
            tl.event(
                "cm.adapter.spawn",
                adapter=self.id,
                kind="xterm",
                title_present=bool(title),
                cmd_head=(command or "")[:120],
            )
        except Exception:
            pass
        return await _spawn(argv)
