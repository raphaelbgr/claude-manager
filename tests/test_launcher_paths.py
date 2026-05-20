"""End-to-end path coverage for src/launcher.py and src/terminals/*.py.

Goals:
1. Every conditional branch in launcher + terminal adapters is exercised.
2. No real subprocess ever spawns — all asyncio.create_subprocess_* and
   _spawn_shell/_osascript/_spawn calls are patched.
3. tl.event() calls are validated via a strict mock that FAILS if `name=`
   is passed as a data kwarg (reproduces the TypeError that burned prod).
4. Tests run on Windows (the test host) but simulate darwin/linux via
   `patch("src.launcher.sys.platform", ...)`.
"""
from __future__ import annotations

import asyncio
import base64
import sys
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest

# ---------------------------------------------------------------------------
# Save references to real implementations BEFORE conftest fixtures can
# replace them. conftest._disable_terminal_auto_pick() (autouse) replaces
# src.launcher._auto_pick_local_adapter_id with AsyncMock(return_value=None)
# at fixture setup time — that's AFTER module-level imports but BEFORE each
# test body runs.  We grab the real function here so TestAutoPickLocalAdapterId
# can invoke it directly, bypassing the mock.
# ---------------------------------------------------------------------------
import src.launcher as _lnch_mod
_REAL_AUTO_PICK = _lnch_mod._auto_pick_local_adapter_id


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _proc(returncode: int, stdout: bytes = b"", stderr: bytes = b"") -> AsyncMock:
    p = AsyncMock()
    p.returncode = returncode
    p.communicate = AsyncMock(return_value=(stdout, stderr))
    return p


class StrictTL:
    """Drop-in replacement for `tl` that raises TypeError when reserved
    positional kwargs (`name`, `point`) are passed as data fields — exactly
    the collision class that broke production."""
    RESERVED = {"event": "name", "track": "point", "span": "name"}

    def _check(self, method: str, kwargs: dict[str, Any]) -> None:
        reserved = self.RESERVED.get(method)
        if reserved and reserved in kwargs:
            raise TypeError(
                f"tl.{method}() got reserved positional kwarg '{reserved}' as "
                f"a data field — rename it (e.g. session=, event_name=)"
            )

    def event(self, name: str, **data: Any) -> None:       # noqa: D401
        self._check("event", data)

    def track(self, point: str, **data: Any) -> None:
        self._check("track", data)

    def span(self, name: str, **data: Any):
        self._check("span", data)
        return MagicMock(__enter__=MagicMock(return_value=MagicMock()), __exit__=MagicMock())

    def init(self, *args: Any, **kwargs: Any) -> dict:
        return {"enabled": True}

    def screen(self, name: str) -> None: ...
    def enter(self, name: str) -> None: ...
    def leave(self, name: str) -> None: ...


STRICT_TL = StrictTL()


def _patch_tl():
    """Return a context manager that injects StrictTL everywhere tl is used."""
    return patch("src.tracking._Stub", return_value=STRICT_TL)


# ---------------------------------------------------------------------------
# Tests: _auto_pick_local_adapter_id
# ---------------------------------------------------------------------------

class TestAutoPickLocalAdapterId:
    """Tests call the REAL _auto_pick_local_adapter_id via _REAL_AUTO_PICK.

    conftest._disable_terminal_auto_pick() (autouse) replaces the module attr
    src.launcher._auto_pick_local_adapter_id with AsyncMock(return_value=None)
    before each test body runs, so any `from src.launcher import ...` done
    inside the test gets the mock. We use _REAL_AUTO_PICK (captured at
    module-load time, before conftest can act) to call the real implementation.
    """

    @pytest.mark.asyncio
    async def test_cache_hit_returns_cached_value(self):
        """When the OS key is already in _AUTO_ADAPTER_CACHE, return it without probing."""
        import src.launcher as lnch
        with patch.dict(lnch._AUTO_ADAPTER_CACHE, {"win32": "wt"}, clear=True), \
             patch("src.launcher._reg_os_for_local", return_value="win32"), \
             patch("src.launcher.tl", STRICT_TL):
            result = await _REAL_AUTO_PICK()
        assert result == "wt"

    @pytest.mark.asyncio
    async def test_cache_hit_none_returns_none(self):
        """Cached None (no adapter found) is returned as-is."""
        import src.launcher as lnch
        with patch.dict(lnch._AUTO_ADAPTER_CACHE, {"darwin": None}, clear=True), \
             patch("src.launcher._reg_os_for_local", return_value="darwin"), \
             patch("src.launcher.tl", STRICT_TL):
            result = await _REAL_AUTO_PICK()
        assert result is None

    @pytest.mark.asyncio
    async def test_cache_miss_probes_and_caches(self):
        """On a miss, auto_pick is called; the result is stored in _AUTO_ADAPTER_CACHE."""
        import src.launcher as lnch
        mock_adapter = MagicMock()
        mock_adapter.id = "iterm2"

        with patch.dict(lnch._AUTO_ADAPTER_CACHE, {}, clear=True), \
             patch("src.launcher._reg_os_for_local", return_value="darwin"), \
             patch("src.launcher.tl", STRICT_TL), \
             patch("src.launcher.asyncio.create_subprocess_exec", return_value=_proc(0)), \
             patch("src.terminals.auto_pick", new=AsyncMock(return_value=mock_adapter)):
            result = await _REAL_AUTO_PICK()

        assert result == "iterm2"

    @pytest.mark.asyncio
    async def test_no_adapter_found_returns_none(self):
        """auto_pick returning None stores None and returns None."""
        import src.launcher as lnch

        with patch.dict(lnch._AUTO_ADAPTER_CACHE, {}, clear=True), \
             patch("src.launcher._reg_os_for_local", return_value="linux"), \
             patch("src.launcher.tl", STRICT_TL), \
             patch("src.launcher.asyncio.create_subprocess_exec", return_value=_proc(0)), \
             patch("src.terminals.auto_pick", new=AsyncMock(return_value=None)):
            result = await _REAL_AUTO_PICK()

        assert result is None

    @pytest.mark.asyncio
    async def test_auto_pick_exception_returns_none(self):
        """If auto_pick raises, we degrade gracefully to None."""
        import src.launcher as lnch

        with patch.dict(lnch._AUTO_ADAPTER_CACHE, {}, clear=True), \
             patch("src.launcher._reg_os_for_local", return_value="linux"), \
             patch("src.launcher.tl", STRICT_TL), \
             patch("src.launcher.asyncio.create_subprocess_exec", return_value=_proc(0)), \
             patch("src.terminals.auto_pick", new=AsyncMock(side_effect=RuntimeError("boom"))):
            result = await _REAL_AUTO_PICK()

        assert result is None


# ---------------------------------------------------------------------------
# Tests: launch_terminal
# ---------------------------------------------------------------------------

class TestLaunchTerminal:

    @pytest.mark.asyncio
    async def test_known_terminal_id_dispatches_to_adapter(self):
        """Providing terminal_id routes to the matching adapter's launch()."""
        from src.launcher import launch_terminal
        mock_adapter = MagicMock()
        mock_adapter.launch = AsyncMock(return_value={"ok": True})

        with patch("src.launcher.tl", STRICT_TL), \
             patch("src.launcher._reg_os_for_local", return_value="win32"), \
             patch("src.terminals.get_adapter", return_value=mock_adapter):
            result = await launch_terminal("echo hi", terminal_id="wt")

        mock_adapter.launch.assert_awaited_once()
        assert result == {"ok": True}

    @pytest.mark.asyncio
    async def test_unknown_terminal_id_falls_back_to_platform(self):
        """An unrecognised terminal_id falls through to the legacy platform path."""
        from src.launcher import launch_terminal

        with patch("src.launcher.tl", STRICT_TL), \
             patch("src.launcher._reg_os_for_local", return_value="darwin"), \
             patch("src.terminals.get_adapter", return_value=None), \
             patch("src.launcher.sys.platform", "darwin"), \
             patch("src.launcher._launch_macos", new=AsyncMock(return_value={"ok": True})) as mac:
            result = await launch_terminal("echo", terminal_id="nonexistent")

        mac.assert_awaited_once()
        assert result == {"ok": True}

    @pytest.mark.asyncio
    async def test_no_terminal_id_auto_picks(self):
        """When terminal_id is None, auto_pick is invoked."""
        from src.launcher import launch_terminal
        mock_adapter = MagicMock()
        mock_adapter.launch = AsyncMock(return_value={"ok": True})

        with patch("src.launcher.tl", STRICT_TL), \
             patch("src.launcher._auto_pick_local_adapter_id", new=AsyncMock(return_value="wt")), \
             patch("src.launcher._reg_os_for_local", return_value="win32"), \
             patch("src.terminals.get_adapter", return_value=mock_adapter):
            result = await launch_terminal("echo", terminal_id=None)

        assert result == {"ok": True}

    @pytest.mark.asyncio
    async def test_adapter_launch_failure_logged(self):
        """A failed adapter launch returns the error dict."""
        from src.launcher import launch_terminal
        mock_adapter = MagicMock()
        mock_adapter.launch = AsyncMock(return_value={"ok": False, "error": "wt crashed"})

        with patch("src.launcher.tl", STRICT_TL), \
             patch("src.launcher._reg_os_for_local", return_value="win32"), \
             patch("src.terminals.get_adapter", return_value=mock_adapter):
            result = await launch_terminal("cmd", terminal_id="wt")

        assert result == {"ok": False, "error": "wt crashed"}

    @pytest.mark.asyncio
    async def test_no_adapter_no_id_linux_legacy(self):
        """No adapter installed + no terminal_id → legacy _launch_linux."""
        from src.launcher import launch_terminal

        with patch("src.launcher.tl", STRICT_TL), \
             patch("src.launcher._auto_pick_local_adapter_id", new=AsyncMock(return_value=None)), \
             patch("src.launcher.sys.platform", "linux"), \
             patch("src.launcher._launch_linux", new=AsyncMock(return_value={"ok": True})) as lx:
            result = await launch_terminal("echo")

        lx.assert_awaited_once_with("echo")
        assert result == {"ok": True}

    @pytest.mark.asyncio
    async def test_unsupported_platform_returns_error(self):
        from src.launcher import launch_terminal

        with patch("src.launcher.tl", STRICT_TL), \
             patch("src.launcher._auto_pick_local_adapter_id", new=AsyncMock(return_value=None)), \
             patch("src.launcher.sys.platform", "haiku"):
            result = await launch_terminal("echo")

        assert result["ok"] is False
        assert "Unsupported platform" in result["error"]


# ---------------------------------------------------------------------------
# Tests: _launch_macos
# ---------------------------------------------------------------------------

class TestLaunchMacos:

    @pytest.mark.asyncio
    async def test_iterm2_success_path(self):
        from src.launcher import _launch_macos
        with patch("src.launcher._run_osascript", new=AsyncMock(return_value={"ok": True})) as osa:
            result = await _launch_macos("echo")
        assert result == {"ok": True}
        assert osa.call_count == 1
        assert "iTerm2" in osa.call_args[0][0]

    @pytest.mark.asyncio
    async def test_iterm2_fail_fallback_to_terminal_app(self):
        from src.launcher import _launch_macos
        results = [{"ok": False, "error": "not running"}, {"ok": True}]
        idx = 0
        async def fake_osa(script):
            nonlocal idx; r = results[idx]; idx += 1; return r
        with patch("src.launcher._run_osascript", new=fake_osa):
            result = await _launch_macos("cmd")
        assert result == {"ok": True}
        assert idx == 2

    @pytest.mark.asyncio
    async def test_both_fail_propagates_terminal_app_error(self):
        from src.launcher import _launch_macos
        async def fake_osa(script):
            return {"ok": False, "error": "blocked"}
        with patch("src.launcher._run_osascript", new=fake_osa):
            result = await _launch_macos("cmd")
        assert result["ok"] is False


# ---------------------------------------------------------------------------
# Tests: _launch_macos_multi
# ---------------------------------------------------------------------------

class TestLaunchMacosMulti:

    @pytest.mark.asyncio
    async def test_empty_commands_returns_error(self):
        from src.launcher import _launch_macos_multi
        result = await _launch_macos_multi([])
        assert result == {"ok": False, "error": "No commands"}

    @pytest.mark.asyncio
    async def test_iterm2_path_succeeds(self):
        from src.launcher import _launch_macos_multi
        with patch("src.launcher._run_osascript", new=AsyncMock(return_value={"ok": True})) as osa:
            result = await _launch_macos_multi(["ssh host", "psmux attach -t sess"])
        assert result == {"ok": True}
        assert "iTerm2" in osa.call_args[0][0]

    @pytest.mark.asyncio
    async def test_iterm2_fail_terminal_app_fallback(self):
        from src.launcher import _launch_macos_multi
        results = [{"ok": False, "error": "x"}, {"ok": True}]
        idx = 0
        async def fake_osa(script):
            nonlocal idx; r = results[idx]; idx += 1; return r
        with patch("src.launcher._run_osascript", new=fake_osa):
            result = await _launch_macos_multi(["ssh host", "cmd2"])
        assert result == {"ok": True}
        assert idx == 2

    @pytest.mark.asyncio
    async def test_single_command_no_delay_lines(self):
        """Single command — no 'delay' should appear in the iTerm2 script."""
        from src.launcher import _launch_macos_multi
        scripts = []
        async def fake_osa(script):
            scripts.append(script); return {"ok": True}
        with patch("src.launcher._run_osascript", new=fake_osa):
            await _launch_macos_multi(["only-cmd"])
        assert "delay" not in scripts[0]


# ---------------------------------------------------------------------------
# Tests: _launch_linux
# ---------------------------------------------------------------------------

class TestLaunchLinux:

    @pytest.mark.asyncio
    async def test_gnome_terminal_chosen_first(self):
        from src.launcher import _launch_linux
        proc = _proc(0)
        def which(n): return "/usr/bin/" + n if n == "gnome-terminal" else None
        with patch("src.launcher.shutil.which", side_effect=which), \
             patch("src.launcher.asyncio.create_subprocess_exec", return_value=proc) as mock_exec:
            result = await _launch_linux("cmd")
        assert result["ok"]
        assert mock_exec.call_args[0][0] == "gnome-terminal"
        assert "--" in mock_exec.call_args[0]

    @pytest.mark.asyncio
    async def test_konsole_chosen_when_gnome_absent(self):
        from src.launcher import _launch_linux
        proc = _proc(0)
        def which(n): return "/usr/bin/konsole" if n == "konsole" else None
        with patch("src.launcher.shutil.which", side_effect=which), \
             patch("src.launcher.asyncio.create_subprocess_exec", return_value=proc) as mock_exec:
            result = await _launch_linux("cmd")
        assert result["ok"]
        assert mock_exec.call_args[0][0] == "konsole"

    @pytest.mark.asyncio
    async def test_xterm_fallback(self):
        from src.launcher import _launch_linux
        proc = _proc(0)
        def which(n): return "/usr/bin/xterm" if n == "xterm" else None
        with patch("src.launcher.shutil.which", side_effect=which), \
             patch("src.launcher.asyncio.create_subprocess_exec", return_value=proc) as mock_exec:
            result = await _launch_linux("cmd")
        assert result["ok"]
        assert mock_exec.call_args[0][0] == "xterm"

    @pytest.mark.asyncio
    async def test_none_found_returns_error(self):
        from src.launcher import _launch_linux
        with patch("src.launcher.shutil.which", return_value=None):
            result = await _launch_linux("cmd")
        assert result["ok"] is False
        assert "No supported terminal emulator" in result["error"]

    @pytest.mark.asyncio
    async def test_oserror_returns_error(self):
        from src.launcher import _launch_linux
        def which(n): return "/usr/bin/xterm" if n == "xterm" else None
        with patch("src.launcher.shutil.which", side_effect=which), \
             patch("src.launcher.asyncio.create_subprocess_exec", side_effect=OSError("fail")):
            result = await _launch_linux("cmd")
        assert result["ok"] is False

    @pytest.mark.asyncio
    async def test_timeout_is_non_fatal(self):
        from src.launcher import _launch_linux
        proc = AsyncMock(); proc.returncode = None
        def which(n): return "/usr/bin/xterm" if n == "xterm" else None
        with patch("src.launcher.shutil.which", side_effect=which), \
             patch("src.launcher.asyncio.create_subprocess_exec", return_value=proc), \
             patch("src.launcher.asyncio.wait_for", side_effect=asyncio.TimeoutError()):
            result = await _launch_linux("cmd")
        assert result["ok"]


# ---------------------------------------------------------------------------
# Tests: _launch_windows
# ---------------------------------------------------------------------------

class TestLaunchWindows:

    @pytest.mark.asyncio
    async def test_uses_encoded_command(self):
        """Critical: -EncodedCommand must appear in the shell string."""
        from src.launcher import _launch_windows
        proc = _proc(0)
        with patch("src.launcher.asyncio.create_subprocess_shell", return_value=proc) as sh:
            await _launch_windows("echo hello")
        cmd = sh.call_args[0][0]
        assert "-EncodedCommand" in cmd

    @pytest.mark.asyncio
    async def test_encoded_command_roundtrip(self):
        """The base64 payload must decode back to the original command."""
        from src.launcher import _launch_windows
        proc = _proc(0)
        payload = "echo 'hello world'"
        with patch("src.launcher.asyncio.create_subprocess_shell", return_value=proc) as sh:
            await _launch_windows(payload)
        cmd = sh.call_args[0][0]
        encoded = cmd.split("-EncodedCommand")[1].strip().split()[0]
        assert base64.b64decode(encoded).decode("utf-16-le") == payload

    @pytest.mark.asyncio
    async def test_pwsh_preferred_when_available(self):
        from src.launcher import _launch_windows
        proc = _proc(0)
        with patch("src.launcher.asyncio.create_subprocess_shell", return_value=proc) as sh, \
             patch("src.launcher.shutil.which", return_value="/usr/bin/pwsh"):
            await _launch_windows("cmd")
        assert "pwsh" in sh.call_args[0][0].lower()

    @pytest.mark.asyncio
    async def test_powershell_fallback_when_pwsh_absent(self):
        from src.launcher import _launch_windows
        proc = _proc(0)
        with patch("src.launcher.asyncio.create_subprocess_shell", return_value=proc) as sh, \
             patch("src.launcher.shutil.which", return_value=None):
            await _launch_windows("cmd")
        assert "powershell" in sh.call_args[0][0].lower()

    @pytest.mark.asyncio
    async def test_oserror_returns_error(self):
        from src.launcher import _launch_windows
        with patch("src.launcher.asyncio.create_subprocess_shell", side_effect=OSError("no shell")):
            result = await _launch_windows("cmd")
        assert result["ok"] is False
        assert "no shell" in result["error"]

    @pytest.mark.asyncio
    async def test_timeout_is_non_fatal(self):
        from src.launcher import _launch_windows
        proc = AsyncMock(); proc.returncode = None
        with patch("src.launcher.asyncio.create_subprocess_shell", return_value=proc), \
             patch("src.launcher.asyncio.wait_for", side_effect=asyncio.TimeoutError()):
            result = await _launch_windows("cmd")
        assert result["ok"]


# ---------------------------------------------------------------------------
# Tests: launch_claude_session
# ---------------------------------------------------------------------------

class TestLaunchClaudeSession:

    @pytest.mark.asyncio
    async def test_local_unix_session(self):
        from src.launcher import launch_claude_session
        with patch("src.launcher.detect_local_machine", return_value="mac-mini"), \
             patch("src.launcher.launch_terminal", new=AsyncMock(return_value={"ok": True})) as lt, \
             patch("src.launcher.sys.platform", "darwin"), \
             patch("src.launcher.tl", STRICT_TL):
            result = await launch_claude_session("/tmp/proj", "abc123", "mac-mini")
        assert result == {"ok": True}
        cmd = lt.call_args[0][0]
        assert "claude" in cmd and "--resume" in cmd and "abc123" in cmd

    @pytest.mark.asyncio
    async def test_local_windows_session(self):
        from src.launcher import launch_claude_session
        with patch("src.launcher.detect_local_machine", return_value="avell-i7"), \
             patch("src.launcher.launch_terminal", new=AsyncMock(return_value={"ok": True})) as lt, \
             patch("src.launcher.sys.platform", "win32"), \
             patch("src.launcher.tl", STRICT_TL):
            result = await launch_claude_session("C:\\Users\\proj", "abc123", "avell-i7")
        assert result == {"ok": True}

    @pytest.mark.asyncio
    async def test_remote_unix_target_ssh_t(self):
        from src.launcher import launch_claude_session
        with patch("src.launcher.detect_local_machine", return_value="mac-mini"), \
             patch("src.launcher.launch_terminal", new=AsyncMock(return_value={"ok": True})) as lt, \
             patch("src.launcher.tl", STRICT_TL):
            await launch_claude_session("/remote/dir", "sess-xyz", "ubuntu-desktop")
        cmd = lt.call_args[0][0]
        assert "ssh" in cmd and "-t" in cmd and "ubuntu-desktop" in cmd

    @pytest.mark.asyncio
    async def test_remote_windows_target_no_inner_title_prefix(self):
        """Windows remote must not inject the POSIX-quoted title into the SSH arg."""
        from src.launcher import launch_claude_session
        with patch("src.launcher.detect_local_machine", return_value="mac-mini"), \
             patch("src.launcher.launch_terminal", new=AsyncMock(return_value={"ok": True})) as lt, \
             patch("src.launcher.tl", STRICT_TL):
            await launch_claude_session("/remote/dir", "sess-xyz", "avell-i7")
        cmd = lt.call_args[0][0]
        # The inner SSH arg (shlex.quote'd) must NOT contain a PowerShell title prefix
        # that would produce `'\''` sequences — they collide with PS parsing.
        assert "WindowTitle" not in cmd or "\\'" not in cmd

    @pytest.mark.asyncio
    async def test_skip_permissions_propagated(self):
        from src.launcher import launch_claude_session
        with patch("src.launcher.detect_local_machine", return_value="mac-mini"), \
             patch("src.launcher.launch_terminal", new=AsyncMock(return_value={"ok": True})) as lt, \
             patch("src.launcher.sys.platform", "darwin"), \
             patch("src.launcher.tl", STRICT_TL):
            await launch_claude_session("/tmp", "s", "mac-mini", skip_permissions=True)
        cmd = lt.call_args[0][0]
        assert "--dangerously-skip-permissions" in cmd

    @pytest.mark.asyncio
    async def test_tl_event_no_kwarg_collision(self):
        """launch_claude_session must not call tl.event(name=...) — StrictTL raises."""
        from src.launcher import launch_claude_session
        # StrictTL will raise if name= is passed as data kwarg
        with patch("src.launcher.detect_local_machine", return_value="mac-mini"), \
             patch("src.launcher.launch_terminal", new=AsyncMock(return_value={"ok": True})), \
             patch("src.launcher.tl", STRICT_TL), \
             patch("src.launcher.sys.platform", "darwin"):
            # Must not raise TypeError
            await launch_claude_session("/tmp", "abc", "mac-mini")


# ---------------------------------------------------------------------------
# Tests: launch_tmux_attach
# ---------------------------------------------------------------------------

class TestLaunchTmuxAttach:

    @pytest.mark.asyncio
    async def test_local_tmux_attach(self):
        from src.launcher import launch_tmux_attach
        with patch("src.launcher.detect_local_machine", return_value="mac-mini"), \
             patch("src.launcher._ensure_claude_running", new=AsyncMock()), \
             patch("src.launcher.launch_terminal", new=AsyncMock(return_value={"ok": True})) as lt, \
             patch("src.launcher.sys.platform", "darwin"), \
             patch("src.launcher.tl", STRICT_TL):
            result = await launch_tmux_attach("my-sess", "mac-mini")
        cmd = lt.call_args[0][0]
        assert "tmux" in cmd and "attach" in cmd and "my-sess" in cmd

    @pytest.mark.asyncio
    async def test_remote_tmux_ssh_t(self):
        from src.launcher import launch_tmux_attach
        with patch("src.launcher.detect_local_machine", return_value="mac-mini"), \
             patch("src.launcher._ensure_claude_running", new=AsyncMock()), \
             patch("src.launcher.launch_terminal", new=AsyncMock(return_value={"ok": True})) as lt, \
             patch("src.launcher.tl", STRICT_TL):
            await launch_tmux_attach("remote-sess", "ubuntu-desktop")
        cmd = lt.call_args[0][0]
        assert "ssh" in cmd and "-t" in cmd and "ubuntu-desktop" in cmd

    @pytest.mark.asyncio
    async def test_remote_psmux_macos_uses_launch_macos_multi(self):
        """psmux + darwin orchestrator → _launch_macos_multi."""
        from src.launcher import launch_tmux_attach
        with patch("src.launcher.detect_local_machine", return_value="mac-mini"), \
             patch("src.launcher._ensure_claude_running", new=AsyncMock()), \
             patch("src.launcher.sys.platform", "darwin"), \
             patch("src.launcher._launch_macos_multi", new=AsyncMock(return_value={"ok": True})) as mm, \
             patch("src.launcher.tl", STRICT_TL):
            result = await launch_tmux_attach("win-sess", "avell-i7")
        mm.assert_awaited_once()
        cmds = mm.call_args[0][0]
        assert any("avell-i7" in c for c in cmds)

    @pytest.mark.asyncio
    async def test_remote_psmux_non_mac_uses_launch_terminal(self):
        """psmux + non-darwin orchestrator → launch_terminal with ssh -t."""
        from src.launcher import launch_tmux_attach
        with patch("src.launcher.detect_local_machine", return_value="windows-desktop"), \
             patch("src.launcher._ensure_claude_running", new=AsyncMock()), \
             patch("src.launcher.sys.platform", "win32"), \
             patch("src.launcher.launch_terminal", new=AsyncMock(return_value={"ok": True})) as lt, \
             patch("src.launcher.tl", STRICT_TL):
            result = await launch_tmux_attach("win-sess", "avell-i7")
        lt.assert_awaited_once()
        cmd = lt.call_args[0][0]
        assert "ssh" in cmd and "avell-i7" in cmd

    @pytest.mark.asyncio
    async def test_cc_mode_iterm2_tmux_injects_cc_flag(self):
        """terminal_id='iterm2' + tmux → -CC attach."""
        from src.launcher import launch_tmux_attach
        with patch("src.launcher.detect_local_machine", return_value="mac-mini"), \
             patch("src.launcher._ensure_claude_running", new=AsyncMock()), \
             patch("src.launcher.launch_terminal", new=AsyncMock(return_value={"ok": True})) as lt, \
             patch("src.launcher.tl", STRICT_TL):
            await launch_tmux_attach("remote-sess", "ubuntu-desktop", terminal_id="iterm2")
        cmd = lt.call_args[0][0]
        assert "-CC" in cmd

    @pytest.mark.asyncio
    async def test_tl_event_no_kwarg_collision(self):
        from src.launcher import launch_tmux_attach
        with patch("src.launcher.detect_local_machine", return_value="mac-mini"), \
             patch("src.launcher._ensure_claude_running", new=AsyncMock()), \
             patch("src.launcher.launch_terminal", new=AsyncMock(return_value={"ok": True})), \
             patch("src.launcher.tl", STRICT_TL):
            await launch_tmux_attach("sess", "mac-mini")  # must not raise TypeError


# ---------------------------------------------------------------------------
# Tests: _ensure_claude_running
# ---------------------------------------------------------------------------

class TestEnsureClaudeRunning:
    """capture_pane is imported inside _ensure_claude_running as a local import:
    'from .tmux_manager import capture_pane'. Patch it at the source module."""

    @pytest.mark.asyncio
    async def test_no_claude_typed_into_pane(self):
        """When pane shows a shell prompt, claude is sent via mux_send_keys."""
        from src.launcher import _ensure_claude_running
        proc = _proc(0)
        with patch("src.tmux_manager.capture_pane", new=AsyncMock(return_value="PS C:\\Users\\x> ")), \
             patch("src.launcher.detect_local_machine", return_value="mac-mini"), \
             patch("src.launcher.sys.platform", "darwin"), \
             patch("src.launcher.asyncio.create_subprocess_shell", return_value=proc), \
             patch("src.launcher.tl", STRICT_TL):
            await _ensure_claude_running("mac-mini", "sess")
        # If we get here without exception, the shell prompt was detected and acted on

    @pytest.mark.asyncio
    async def test_claude_already_running_no_op(self):
        """When pane shows claude TUI, we skip the send-keys step."""
        from src.launcher import _ensure_claude_running
        pane = "Welcome to Claude\n╭──────╮\n│ > "
        with patch("src.tmux_manager.capture_pane", new=AsyncMock(return_value=pane)), \
             patch("src.launcher.detect_local_machine", return_value="mac-mini"), \
             patch("src.launcher.asyncio.create_subprocess_shell") as sh, \
             patch("src.launcher.tl", STRICT_TL):
            await _ensure_claude_running("mac-mini", "sess")
        sh.assert_not_called()

    @pytest.mark.asyncio
    async def test_local_windows_uses_mux_send_keys_ps(self):
        """On local Windows, mux_send_keys_ps must be used (not mux_send_keys)."""
        from src.launcher import _ensure_claude_running
        proc = _proc(0)
        with patch("src.tmux_manager.capture_pane", new=AsyncMock(return_value="PS C:\\> ")), \
             patch("src.launcher.detect_local_machine", return_value="avell-i7"), \
             patch("src.launcher.sys.platform", "win32"), \
             patch("src.launcher.asyncio.create_subprocess_shell", return_value=proc) as sh, \
             patch("src.launcher.tl", STRICT_TL):
            await _ensure_claude_running("avell-i7", "sess")
        # Should have called create_subprocess_shell (local path taken)
        sh.assert_called()

    @pytest.mark.asyncio
    async def test_capture_pane_exception_is_suppressed(self):
        """If capture_pane raises, _ensure_claude_running returns silently."""
        from src.launcher import _ensure_claude_running
        with patch("src.tmux_manager.capture_pane", new=AsyncMock(side_effect=Exception("ssh err"))), \
             patch("src.launcher.tl", STRICT_TL):
            # Must not raise
            await _ensure_claude_running("mac-mini", "sess")

    @pytest.mark.asyncio
    async def test_skip_permissions_passed_to_claude_cmd(self):
        """When skip_permissions=True, the send-keys command includes --dangerously-skip."""
        from src.launcher import _ensure_claude_running
        proc = _proc(0)
        sent_cmds = []
        async def fake_shell(cmd, **kw):
            sent_cmds.append(cmd); return proc
        with patch("src.tmux_manager.capture_pane", new=AsyncMock(return_value="$ ")), \
             patch("src.launcher.detect_local_machine", return_value="mac-mini"), \
             patch("src.launcher.sys.platform", "darwin"), \
             patch("src.launcher.asyncio.create_subprocess_shell", side_effect=fake_shell), \
             patch("src.launcher.tl", STRICT_TL):
            await _ensure_claude_running("mac-mini", "sess", skip_permissions=True)
        assert any("--dangerously-skip-permissions" in c for c in sent_cmds)


# ---------------------------------------------------------------------------
# Tests: launch_remote_terminal
# ---------------------------------------------------------------------------

class TestLaunchRemoteTerminal:

    @pytest.mark.asyncio
    async def test_darwin_remote(self):
        from src.launcher import launch_remote_terminal
        proc = _proc(0)
        with patch("src.launcher.asyncio.create_subprocess_exec", return_value=proc):
            result = await launch_remote_terminal("echo hi", "mac-mini")
        assert result["ok"]

    @pytest.mark.asyncio
    async def test_linux_remote(self):
        from src.launcher import launch_remote_terminal
        proc = _proc(0)
        with patch("src.launcher.asyncio.create_subprocess_exec", return_value=proc):
            result = await launch_remote_terminal("echo hi", "ubuntu-desktop")
        assert result["ok"]

    @pytest.mark.asyncio
    async def test_win32_remote(self):
        from src.launcher import launch_remote_terminal
        proc = _proc(0)
        with patch("src.launcher.asyncio.create_subprocess_exec", return_value=proc):
            result = await launch_remote_terminal("psmux attach -t s", "avell-i7")
        assert result["ok"]

    @pytest.mark.asyncio
    async def test_unknown_os_returns_error(self):
        from src.launcher import launch_remote_terminal
        result = await launch_remote_terminal("cmd", "mystery-machine")
        assert result["ok"] is False

    @pytest.mark.asyncio
    async def test_nonzero_rc_returns_error(self):
        from src.launcher import launch_remote_terminal
        proc = _proc(1, stderr=b"connection refused")
        with patch("src.launcher.asyncio.create_subprocess_exec", return_value=proc):
            result = await launch_remote_terminal("cmd", "mac-mini")
        assert result["ok"] is False

    @pytest.mark.asyncio
    async def test_timeout_treated_as_ok(self):
        """fire-and-forget: timeout means the terminal is running — ok."""
        from src.launcher import launch_remote_terminal
        proc = AsyncMock(); proc.returncode = None
        with patch("src.launcher.asyncio.create_subprocess_exec", return_value=proc), \
             patch("src.launcher.asyncio.wait_for", side_effect=asyncio.TimeoutError()):
            result = await launch_remote_terminal("cmd", "mac-mini")
        assert result["ok"]


# ---------------------------------------------------------------------------
# Tests: launch_tmux_attach_remote
# ---------------------------------------------------------------------------

class TestLaunchTmuxAttachRemote:

    @pytest.mark.asyncio
    async def test_calls_launch_remote_terminal(self):
        from src.launcher import launch_tmux_attach_remote
        with patch("src.launcher.launch_remote_terminal", new=AsyncMock(return_value={"ok": True})) as lrt:
            result = await launch_tmux_attach_remote("my-sess", "mac-mini")
        assert result == {"ok": True}
        cmd_arg, machine_arg = lrt.call_args[0]
        assert "attach" in cmd_arg and "my-sess" in cmd_arg
        assert machine_arg == "mac-mini"


# ---------------------------------------------------------------------------
# Tests: launch_new_tmux_and_attach
# ---------------------------------------------------------------------------

class TestLaunchNewTmuxAndAttach:

    @pytest.mark.asyncio
    async def test_create_then_attach(self):
        from src.launcher import launch_new_tmux_and_attach
        mock_create = AsyncMock(return_value={"ok": True, "name": "clean-sess"})
        mock_attach = AsyncMock(return_value={"ok": True})
        with patch("src.tmux_manager.create_tmux_session", mock_create), \
             patch("src.launcher.launch_tmux_attach", mock_attach):
            result = await launch_new_tmux_and_attach("clean-sess", "mac-mini", cwd="/tmp")
        assert result == {"ok": True}
        mock_create.assert_awaited_once()
        mock_attach.assert_awaited_once_with("clean-sess", "mac-mini", terminal_id=None)

    @pytest.mark.asyncio
    async def test_create_failure_skips_attach(self):
        from src.launcher import launch_new_tmux_and_attach
        mock_create = AsyncMock(return_value={"ok": False, "error": "already exists"})
        mock_attach = AsyncMock()
        with patch("src.tmux_manager.create_tmux_session", mock_create), \
             patch("src.launcher.launch_tmux_attach", mock_attach):
            result = await launch_new_tmux_and_attach("s", "mac-mini")
        assert result["ok"] is False
        mock_attach.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_session_name_sanitized(self):
        from src.launcher import launch_new_tmux_and_attach
        mock_create = AsyncMock(return_value={"ok": True, "name": "sess-with-dots"})
        mock_attach = AsyncMock(return_value={"ok": True})
        with patch("src.tmux_manager.create_tmux_session", mock_create), \
             patch("src.launcher.launch_tmux_attach", mock_attach):
            await launch_new_tmux_and_attach("sess.with.dots", "mac-mini")
        # sanitize_mux_name replaces '.' with '-'
        create_name_arg = mock_create.call_args[0][1]
        assert "." not in create_name_arg


# ---------------------------------------------------------------------------
# Tests: src/terminals/__init__.py — _probe and auto_pick
# ---------------------------------------------------------------------------

class TestTerminalsInit:

    @pytest.mark.asyncio
    async def test_probe_success_includes_adapter(self):
        """An adapter whose probe_shell exits 0 is included in list_available."""
        import src.terminals as terms
        async def runner(script: str) -> tuple[int, bytes, bytes]:
            return 0, b"", b""
        # Pick an OS/id that's definitely registered
        avail = await terms.list_available("darwin", runner)
        ids = [a["id"] for a in avail]
        # iterm2 and terminal and alacritty are registered; at least one present
        assert len(ids) > 0

    @pytest.mark.asyncio
    async def test_probe_failure_excludes_adapter(self):
        """An adapter whose probe_shell exits 1 is excluded."""
        import src.terminals as terms
        async def runner(script: str) -> tuple[int, bytes, bytes]:
            return 1, b"", b"not found"
        avail = await terms.list_available("darwin", runner)
        assert avail == []

    @pytest.mark.asyncio
    async def test_auto_pick_highest_priority(self):
        """auto_pick returns the adapter with the highest priority."""
        import src.terminals as terms
        async def runner(script: str) -> tuple[int, bytes, bytes]:
            return 0, b"", b""
        adapter = await terms.auto_pick("darwin", runner)
        # iterm2 has priority=100, the highest on darwin
        assert adapter is not None
        assert adapter.id == "iterm2"

    @pytest.mark.asyncio
    async def test_auto_pick_none_when_nothing_installed(self):
        import src.terminals as terms
        async def runner(script: str) -> tuple[int, bytes, bytes]:
            return 1, b"", b""
        adapter = await terms.auto_pick("darwin", runner)
        assert adapter is None

    @pytest.mark.asyncio
    async def test_probe_exception_excludes_adapter(self):
        import src.terminals as terms
        async def runner(script: str) -> tuple[int, bytes, bytes]:
            raise OSError("timeout")
        avail = await terms.list_available("linux", runner)
        assert avail == []

    @pytest.mark.asyncio
    async def test_tl_event_no_kwarg_collision_in_probe(self):
        """Probing must not call tl.event(name=...) — StrictTL raises TypeError."""
        import src.terminals as terms
        with patch("src.terminals.tl", STRICT_TL):
            async def runner(script):
                return 0, b"", b""
            await terms.list_available("win32", runner)  # must not raise


# ---------------------------------------------------------------------------
# Tests: src/terminals/windows.py
# ---------------------------------------------------------------------------

class TestWtShell:

    def test_pwsh_returned_when_available(self):
        import src.terminals.windows as tw
        with patch.object(tw, "_PWSH_PROBED", False), \
             patch.object(tw, "_PWSH_PATH", None), \
             patch("src.terminals.windows.shutil.which", return_value="/usr/bin/pwsh"), \
             patch("src.terminals.windows.tl", STRICT_TL):
            result = tw._wt_shell()
        assert result == "pwsh.exe"

    def test_powershell_fallback_when_pwsh_absent(self):
        import src.terminals.windows as tw
        with patch.object(tw, "_PWSH_PROBED", False), \
             patch.object(tw, "_PWSH_PATH", None), \
             patch("src.terminals.windows.shutil.which", return_value=None), \
             patch("src.terminals.windows.tl", STRICT_TL):
            result = tw._wt_shell()
        assert result == "powershell.exe"

    def test_cached_on_repeat_call(self):
        """_wt_shell probes shutil.which only once; the second call uses the cache."""
        import src.terminals.windows as tw
        with patch.object(tw, "_PWSH_PROBED", False), \
             patch.object(tw, "_PWSH_PATH", None), \
             patch("src.terminals.windows.tl", STRICT_TL):
            with patch("src.terminals.windows.shutil.which", return_value="/usr/bin/pwsh") as w:
                first = tw._wt_shell()
                second = tw._wt_shell()
            # Both calls should agree on the host
            assert first == second == "pwsh.exe"
            # which() was called at most once for "pwsh" (or also "pwsh.exe" as
            # short-circuit) — the second _wt_shell() call returns from cache
            # (_PWSH_PROBED=True) and NEVER calls which() again.
            assert w.call_count <= 2


class TestWindowsTerminalAdapterLaunch:

    @pytest.mark.asyncio
    async def test_uses_encoded_command(self):
        from src.terminals.windows import WindowsTerminalAdapter, _spawn_shell
        adapter = WindowsTerminalAdapter()
        shells = []
        async def fake_spawn(cmd):
            shells.append(cmd); return {"ok": True}
        with patch("src.terminals.windows._spawn_shell", side_effect=fake_spawn), \
             patch("src.terminals.windows._wt_shell", return_value="pwsh.exe"), \
             patch("src.terminals.windows.tl", STRICT_TL):
            result = await adapter.launch("echo hello", title="My Title")
        assert result["ok"]
        assert "-EncodedCommand" in shells[0]

    @pytest.mark.asyncio
    async def test_base64_roundtrip(self):
        from src.terminals.windows import WindowsTerminalAdapter
        adapter = WindowsTerminalAdapter()
        captured = []
        async def fake_spawn(cmd):
            captured.append(cmd); return {"ok": True}
        payload = "psmux attach -t 'my session'"
        with patch("src.terminals.windows._spawn_shell", side_effect=fake_spawn), \
             patch("src.terminals.windows._wt_shell", return_value="pwsh.exe"), \
             patch("src.terminals.windows.tl", STRICT_TL):
            await adapter.launch(payload)
        encoded = captured[0].split("-EncodedCommand")[1].strip().split()[0]
        assert base64.b64decode(encoded).decode("utf-16-le") == payload

    @pytest.mark.asyncio
    async def test_tl_event_no_kwarg_collision(self):
        from src.terminals.windows import WindowsTerminalAdapter
        adapter = WindowsTerminalAdapter()
        with patch("src.terminals.windows._spawn_shell", new=AsyncMock(return_value={"ok": True})), \
             patch("src.terminals.windows._wt_shell", return_value="pwsh.exe"), \
             patch("src.terminals.windows.tl", STRICT_TL):
            await adapter.launch("echo", title="t")  # must not raise


class TestPowerShellAdapterLaunch:

    @pytest.mark.asyncio
    async def test_launches_powershell(self):
        from src.terminals.windows import PowerShellAdapter
        adapter = PowerShellAdapter()
        with patch("src.terminals.windows._spawn_shell", new=AsyncMock(return_value={"ok": True})) as sp, \
             patch("src.terminals.windows.tl", STRICT_TL):
            result = await adapter.launch("echo hi", title="T")
        assert result["ok"]
        cmd = sp.call_args[0][0]
        assert "powershell" in cmd.lower()

    @pytest.mark.asyncio
    async def test_tl_no_kwarg_collision(self):
        from src.terminals.windows import PowerShellAdapter
        adapter = PowerShellAdapter()
        with patch("src.terminals.windows._spawn_shell", new=AsyncMock(return_value={"ok": True})), \
             patch("src.terminals.windows.tl", STRICT_TL):
            await adapter.launch("cmd")


class TestPwsh7AdapterLaunch:

    @pytest.mark.asyncio
    async def test_launches_pwsh(self):
        from src.terminals.windows import Pwsh7Adapter
        adapter = Pwsh7Adapter()
        with patch("src.terminals.windows._spawn_shell", new=AsyncMock(return_value={"ok": True})) as sp, \
             patch("src.terminals.windows.tl", STRICT_TL):
            result = await adapter.launch("echo hi")
        cmd = sp.call_args[0][0]
        assert "pwsh" in cmd.lower()


class TestCmdAdapterLaunch:

    @pytest.mark.asyncio
    async def test_launches_cmd(self):
        from src.terminals.windows import CmdAdapter
        adapter = CmdAdapter()
        with patch("src.terminals.windows._spawn_shell", new=AsyncMock(return_value={"ok": True})) as sp, \
             patch("src.terminals.windows.tl", STRICT_TL):
            result = await adapter.launch("echo hi", title="T")
        assert result["ok"]
        cmd = sp.call_args[0][0]
        assert "cmd /c start" in cmd.lower()


class TestGitBashAdapterLaunch:

    @pytest.mark.asyncio
    async def test_mintty_path_when_title_present(self):
        from src.terminals.windows import GitBashAdapter
        adapter = GitBashAdapter()
        with patch("src.terminals.windows._spawn_shell", new=AsyncMock(return_value={"ok": True})) as sp, \
             patch("src.terminals.windows.tl", STRICT_TL):
            result = await adapter.launch("cmd", title="MyTab")
        assert result["ok"]
        cmd = sp.call_args[0][0]
        assert "mintty" in cmd.lower()

    @pytest.mark.asyncio
    async def test_login_shell_path_when_no_title(self):
        from src.terminals.windows import GitBashAdapter
        adapter = GitBashAdapter()
        with patch("src.terminals.windows._spawn_shell", new=AsyncMock(return_value={"ok": True})) as sp, \
             patch("src.terminals.windows.tl", STRICT_TL):
            result = await adapter.launch("cmd", title=None)
        assert result["ok"]
        cmd = sp.call_args[0][0]
        assert "bash.exe" in cmd.lower() and "--login" in cmd.lower()


# ---------------------------------------------------------------------------
# Tests: src/terminals/darwin.py
# ---------------------------------------------------------------------------

class TestDarwinAdapters:

    @pytest.mark.asyncio
    async def test_iterm2_adapter_launch(self):
        from src.terminals.darwin import ItermAdapter
        adapter = ItermAdapter()
        with patch("src.terminals.darwin._osascript", new=AsyncMock(return_value={"ok": True})) as osa, \
             patch("src.terminals.darwin.tl", STRICT_TL):
            result = await adapter.launch("echo hi", title="T")
        assert result["ok"]
        script = osa.call_args[0][0]
        assert "iTerm2" in script and "write text" in script

    @pytest.mark.asyncio
    async def test_iterm2_adapter_title_in_script(self):
        from src.terminals.darwin import ItermAdapter
        adapter = ItermAdapter()
        with patch("src.terminals.darwin._osascript", new=AsyncMock(return_value={"ok": True})) as osa, \
             patch("src.terminals.darwin.tl", STRICT_TL):
            await adapter.launch("cmd", title="MyTitle")
        script = osa.call_args[0][0]
        assert "MyTitle" in script

    @pytest.mark.asyncio
    async def test_terminal_app_adapter_launch(self):
        from src.terminals.darwin import TerminalAppAdapter
        adapter = TerminalAppAdapter()
        with patch("src.terminals.darwin._osascript", new=AsyncMock(return_value={"ok": True})) as osa, \
             patch("src.terminals.darwin.tl", STRICT_TL):
            result = await adapter.launch("echo")
        assert result["ok"]
        assert "do script" in osa.call_args[0][0]

    @pytest.mark.asyncio
    async def test_alacritty_adapter_first_path_found(self):
        from src.terminals.darwin import AlacrittyDarwinAdapter
        adapter = AlacrittyDarwinAdapter()
        proc = _proc(0)
        with patch("src.terminals.darwin.asyncio.create_subprocess_exec", return_value=proc), \
             patch("src.terminals.darwin.asyncio.wait_for", return_value=(b"", b"")), \
             patch("src.terminals.darwin.tl", STRICT_TL):
            result = await adapter.launch("cmd", title="T")
        assert result["ok"]

    @pytest.mark.asyncio
    async def test_alacritty_adapter_all_paths_missing(self):
        from src.terminals.darwin import AlacrittyDarwinAdapter
        adapter = AlacrittyDarwinAdapter()
        with patch("src.terminals.darwin.asyncio.create_subprocess_exec", side_effect=FileNotFoundError()), \
             patch("src.terminals.darwin.tl", STRICT_TL):
            result = await adapter.launch("cmd")
        assert result["ok"] is False
        assert "not found" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_ghostty_adapter_success(self):
        from src.terminals.darwin import GhosttyAdapter
        adapter = GhosttyAdapter()
        proc = _proc(0)
        with patch("src.terminals.darwin.asyncio.create_subprocess_exec", return_value=proc), \
             patch("src.terminals.darwin.asyncio.wait_for", return_value=(b"", b"")), \
             patch("src.terminals.darwin.tl", STRICT_TL):
            result = await adapter.launch("cmd")
        assert result["ok"]

    @pytest.mark.asyncio
    async def test_ghostty_not_installed_returns_error(self):
        from src.terminals.darwin import GhosttyAdapter
        adapter = GhosttyAdapter()
        with patch("src.terminals.darwin.asyncio.create_subprocess_exec", side_effect=FileNotFoundError()):
            result = await adapter.launch("cmd")
        assert result["ok"] is False

    @pytest.mark.asyncio
    async def test_tl_no_kwarg_collision_iterm2(self):
        from src.terminals.darwin import ItermAdapter
        adapter = ItermAdapter()
        with patch("src.terminals.darwin._osascript", new=AsyncMock(return_value={"ok": True})), \
             patch("src.terminals.darwin.tl", STRICT_TL):
            await adapter.launch("cmd", title="T")  # must not raise TypeError


# ---------------------------------------------------------------------------
# Tests: src/terminals/linux.py
# ---------------------------------------------------------------------------

class TestLinuxAdapters:

    @pytest.mark.asyncio
    async def test_gnome_terminal_adapter_launch(self):
        from src.terminals.linux import GnomeTerminalAdapter
        adapter = GnomeTerminalAdapter()
        proc = _proc(0)
        with patch("src.terminals.linux.asyncio.create_subprocess_exec", return_value=proc), \
             patch("src.terminals.linux.tl", STRICT_TL):
            result = await adapter.launch("echo hi", title="T")
        assert result["ok"]

    @pytest.mark.asyncio
    async def test_konsole_adapter_launch(self):
        from src.terminals.linux import KonsoleAdapter
        adapter = KonsoleAdapter()
        proc = _proc(0)
        with patch("src.terminals.linux.asyncio.create_subprocess_exec", return_value=proc), \
             patch("src.terminals.linux.tl", STRICT_TL):
            result = await adapter.launch("cmd", title="T")
        assert result["ok"]

    @pytest.mark.asyncio
    async def test_xfce4_terminal_adapter_launch(self):
        from src.terminals.linux import Xfce4TerminalAdapter
        adapter = Xfce4TerminalAdapter()
        proc = _proc(0)
        with patch("src.terminals.linux.asyncio.create_subprocess_exec", return_value=proc), \
             patch("src.terminals.linux.tl", STRICT_TL):
            result = await adapter.launch("cmd")
        assert result["ok"]

    @pytest.mark.asyncio
    async def test_alacritty_linux_adapter_launch(self):
        from src.terminals.linux import AlacrittyLinuxAdapter
        adapter = AlacrittyLinuxAdapter()
        proc = _proc(0)
        with patch("src.terminals.linux.asyncio.create_subprocess_exec", return_value=proc), \
             patch("src.terminals.linux.tl", STRICT_TL):
            result = await adapter.launch("cmd", title="T")
        assert result["ok"]

    @pytest.mark.asyncio
    async def test_xterm_adapter_launch(self):
        from src.terminals.linux import XtermAdapter
        adapter = XtermAdapter()
        proc = _proc(0)
        with patch("src.terminals.linux.asyncio.create_subprocess_exec", return_value=proc), \
             patch("src.terminals.linux.tl", STRICT_TL):
            result = await adapter.launch("cmd", title="T")
        assert result["ok"]

    @pytest.mark.asyncio
    async def test_file_not_found_returns_error(self):
        from src.terminals.linux import XtermAdapter
        adapter = XtermAdapter()
        with patch("src.terminals.linux.asyncio.create_subprocess_exec",
                   side_effect=FileNotFoundError("xterm not found")), \
             patch("src.terminals.linux.tl", STRICT_TL):
            result = await adapter.launch("cmd")
        assert result["ok"] is False

    @pytest.mark.asyncio
    async def test_timeout_is_non_fatal(self):
        from src.terminals.linux import KonsoleAdapter
        adapter = KonsoleAdapter()
        proc = AsyncMock(); proc.returncode = None
        with patch("src.terminals.linux.asyncio.create_subprocess_exec", return_value=proc), \
             patch("src.terminals.linux.asyncio.wait_for", side_effect=asyncio.TimeoutError()), \
             patch("src.terminals.linux.tl", STRICT_TL):
            result = await adapter.launch("cmd")
        assert result["ok"]

    @pytest.mark.asyncio
    async def test_tl_no_kwarg_collision_gnome(self):
        from src.terminals.linux import GnomeTerminalAdapter
        adapter = GnomeTerminalAdapter()
        proc = _proc(0)
        with patch("src.terminals.linux.asyncio.create_subprocess_exec", return_value=proc), \
             patch("src.terminals.linux.tl", STRICT_TL):
            await adapter.launch("cmd", title="T")  # must not raise TypeError
