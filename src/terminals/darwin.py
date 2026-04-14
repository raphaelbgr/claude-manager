"""macOS terminal adapters.

Detection is file-existence based (more reliable than `command -v` for .app
bundles that register via LaunchServices rather than $PATH). Each adapter has
a deterministic id the UI can round-trip through the launch API.
"""
from __future__ import annotations

import asyncio
import shlex

from .base import TerminalAdapter
from ..subprocess_utils import run_with_timeout


def _applescript_string(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


async def _osascript(script: str) -> dict:
    try:
        proc = await asyncio.create_subprocess_exec(
            "osascript", "-e", script,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
        if proc.returncode != 0:
            return {"ok": False, "error": stderr.decode().strip()}
        return {"ok": True}
    except asyncio.TimeoutError:
        return {"ok": False, "error": "osascript timed out"}
    except OSError as exc:
        return {"ok": False, "error": str(exc)}


class ItermAdapter(TerminalAdapter):
    id = "iterm2"
    name = "iTerm2"
    os = "darwin"
    priority = 100

    def probe_shell(self) -> str:
        return 'test -d /Applications/iTerm.app'

    async def launch(self, command: str, *, title: str | None = None) -> dict:
        cmd_esc = _applescript_string(command)
        script = (
            'tell application "iTerm2"\n'
            '    activate\n'
            '    set newWindow to (create window with default profile)\n'
            '    tell current session of newWindow\n'
            f'        write text {cmd_esc}\n'
            + (f'        set name to {_applescript_string(title)}\n' if title else '')
            + '    end tell\n'
            'end tell'
        )
        return await _osascript(script)


class TerminalAppAdapter(TerminalAdapter):
    id = "terminal"
    name = "Terminal.app"
    os = "darwin"
    priority = 50

    def probe_shell(self) -> str:
        return 'test -d /System/Applications/Utilities/Terminal.app || test -d /Applications/Utilities/Terminal.app'

    async def launch(self, command: str, *, title: str | None = None) -> dict:
        cmd_esc = _applescript_string(command)
        # Terminal.app honors the OSC 0 sequence for titles — it's already
        # injected upstream by launcher.title_prefix_for when requested, so we
        # don't need a separate 'set custom title' here.
        script = (
            'tell application "Terminal"\n'
            '    activate\n'
            f'    do script {cmd_esc}\n'
            'end tell'
        )
        return await _osascript(script)


class AlacrittyDarwinAdapter(TerminalAdapter):
    id = "alacritty"
    name = "Alacritty"
    os = "darwin"
    priority = 70

    def probe_shell(self) -> str:
        return 'command -v alacritty >/dev/null 2>&1 || test -d /Applications/Alacritty.app'

    async def launch(self, command: str, *, title: str | None = None) -> dict:
        bin_paths = ["/opt/homebrew/bin/alacritty", "/usr/local/bin/alacritty", "/Applications/Alacritty.app/Contents/MacOS/alacritty"]
        args = ["--title", title] if title else []
        args += ["-e", "bash", "-lc", f"{command}; exec bash"]
        for path in bin_paths:
            try:
                proc = await asyncio.create_subprocess_exec(
                    path, *args,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE,
                )
                try:
                    _, _ = await asyncio.wait_for(proc.communicate(), timeout=3)
                except asyncio.TimeoutError:
                    pass  # expected — GUI app keeps running
                return {"ok": True}
            except FileNotFoundError:
                continue
        return {"ok": False, "error": "alacritty binary not found in expected paths"}


class KittyDarwinAdapter(TerminalAdapter):
    id = "kitty"
    name = "kitty"
    os = "darwin"
    priority = 60

    def probe_shell(self) -> str:
        return 'command -v kitty >/dev/null 2>&1 || test -d /Applications/kitty.app'

    async def launch(self, command: str, *, title: str | None = None) -> dict:
        bin_paths = ["/opt/homebrew/bin/kitty", "/usr/local/bin/kitty", "/Applications/kitty.app/Contents/MacOS/kitty"]
        args = ["--title", title] if title else []
        args += ["bash", "-lc", f"{command}; exec bash"]
        for path in bin_paths:
            try:
                proc = await asyncio.create_subprocess_exec(
                    path, *args,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE,
                )
                try:
                    _, _ = await asyncio.wait_for(proc.communicate(), timeout=3)
                except asyncio.TimeoutError:
                    pass
                return {"ok": True}
            except FileNotFoundError:
                continue
        return {"ok": False, "error": "kitty binary not found"}


class GhosttyAdapter(TerminalAdapter):
    id = "ghostty"
    name = "Ghostty"
    os = "darwin"
    priority = 55

    def probe_shell(self) -> str:
        return 'test -d /Applications/Ghostty.app'

    async def launch(self, command: str, *, title: str | None = None) -> dict:
        bin_path = "/Applications/Ghostty.app/Contents/MacOS/ghostty"
        args = [bin_path, "-e", f"bash -lc {shlex.quote(command + '; exec bash')}"]
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                _, _ = await asyncio.wait_for(proc.communicate(), timeout=3)
            except asyncio.TimeoutError:
                pass
            return {"ok": True}
        except FileNotFoundError:
            return {"ok": False, "error": "Ghostty not installed"}
        except OSError as exc:
            return {"ok": False, "error": str(exc)}
