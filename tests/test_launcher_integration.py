"""
Launcher integration / edge case tests for src/launcher.py.

Covers gaps not already tested in test_launcher.py:
  - launch_claude_session() with skip_permissions=True — verify flag in command
  - launch_claude_session() remote — verify SSH + -t flag (additional scenarios)
  - launch_remote_terminal() — macOS/Linux/Windows variants (mock subprocess)
  - launch_tmux_attach_remote() — verify correct mux type passed
  - launch_new_tmux_and_attach() — verify create then attach sequence (more scenarios)
  - AppleScript string escaping edge cases
  - Platform dispatch edge cases
"""
from __future__ import annotations

import asyncio
import shlex
import sys
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from src.config import FLEET_MACHINES


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_proc(returncode: int, stdout: bytes = b"", stderr: bytes = b"") -> AsyncMock:
    proc = AsyncMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    return proc


# ---------------------------------------------------------------------------
# launch_claude_session — skip_permissions
# ---------------------------------------------------------------------------

class TestLaunchClaudeSessionSkipPermissions:
    """skip_permissions=True appends --dangerously-skip-permissions to command."""

    @pytest.mark.asyncio
    async def test_skip_permissions_true_appends_flag(self):
        from src.launcher import launch_claude_session

        with patch("src.launcher.detect_local_machine", return_value="mac-mini"), \
             patch("src.launcher.launch_terminal", new=AsyncMock(return_value={"ok": True})) as mock_lt:
            await launch_claude_session("/tmp/proj", "sess-001", "mac-mini", skip_permissions=True)

        cmd = mock_lt.call_args[0][0]
        assert "--dangerously-skip-permissions" in cmd

    @pytest.mark.asyncio
    async def test_skip_permissions_false_no_flag(self):
        from src.launcher import launch_claude_session

        with patch("src.launcher.detect_local_machine", return_value="mac-mini"), \
             patch("src.launcher.launch_terminal", new=AsyncMock(return_value={"ok": True})) as mock_lt:
            await launch_claude_session("/tmp/proj", "sess-001", "mac-mini", skip_permissions=False)

        cmd = mock_lt.call_args[0][0]
        assert "--dangerously-skip-permissions" not in cmd

    @pytest.mark.asyncio
    async def test_skip_permissions_default_is_false(self):
        from src.launcher import launch_claude_session

        with patch("src.launcher.detect_local_machine", return_value="mac-mini"), \
             patch("src.launcher.launch_terminal", new=AsyncMock(return_value={"ok": True})) as mock_lt:
            await launch_claude_session("/tmp/proj", "sess-001", "mac-mini")

        cmd = mock_lt.call_args[0][0]
        assert "--dangerously-skip-permissions" not in cmd

    @pytest.mark.asyncio
    async def test_remote_skip_permissions_flag_in_ssh_command(self):
        from src.launcher import launch_claude_session

        with patch("src.launcher.detect_local_machine", return_value="mac-mini"), \
             patch("src.launcher.launch_terminal", new=AsyncMock(return_value={"ok": True})) as mock_lt:
            await launch_claude_session(
                "/remote/dir", "sess-remote", "ubuntu-desktop", skip_permissions=True
            )

        cmd = mock_lt.call_args[0][0]
        assert "--dangerously-skip-permissions" in cmd
        assert "ssh" in cmd
        assert "-t" in cmd


# ---------------------------------------------------------------------------
# launch_claude_session — remote SSH scenarios
# ---------------------------------------------------------------------------

class TestLaunchClaudeSessionRemote:
    """Remote machine scenarios for launch_claude_session."""

    @pytest.mark.asyncio
    async def test_remote_avell_i7_uses_psmux_alias(self):
        from src.launcher import launch_claude_session

        with patch("src.launcher.detect_local_machine", return_value="mac-mini"), \
             patch("src.launcher.launch_terminal", new=AsyncMock(return_value={"ok": True})) as mock_lt:
            await launch_claude_session("/proj", "sess", "avell-i7")

        cmd = mock_lt.call_args[0][0]
        assert "avell-i7" in cmd
        assert "ssh" in cmd

    @pytest.mark.asyncio
    async def test_remote_exec_bash_at_end(self):
        from src.launcher import launch_claude_session

        with patch("src.launcher.detect_local_machine", return_value="mac-mini"), \
             patch("src.launcher.launch_terminal", new=AsyncMock(return_value={"ok": True})) as mock_lt:
            await launch_claude_session("/proj", "sess", "ubuntu-desktop")

        cmd = mock_lt.call_args[0][0]
        assert "exec bash" in cmd

    @pytest.mark.asyncio
    async def test_local_machine_does_not_use_ssh(self):
        from src.launcher import launch_claude_session

        with patch("src.launcher.detect_local_machine", return_value="ubuntu-desktop"), \
             patch("src.launcher.launch_terminal", new=AsyncMock(return_value={"ok": True})) as mock_lt:
            await launch_claude_session("/proj", "sess", "ubuntu-desktop")

        cmd = mock_lt.call_args[0][0]
        assert "ssh" not in cmd

    @pytest.mark.asyncio
    async def test_unknown_machine_falls_back_to_machine_name_as_alias(self):
        from src.launcher import launch_claude_session

        with patch("src.launcher.detect_local_machine", return_value="mac-mini"), \
             patch("src.launcher.launch_terminal", new=AsyncMock(return_value={"ok": True})) as mock_lt:
            await launch_claude_session("/proj", "sess", "mystery-host")

        cmd = mock_lt.call_args[0][0]
        assert "mystery-host" in cmd
        assert "ssh" in cmd


# ---------------------------------------------------------------------------
# launch_remote_terminal
# ---------------------------------------------------------------------------

class TestLaunchRemoteTerminal:
    """launch_remote_terminal() — opens terminal on remote machine's display."""

    @pytest.mark.asyncio
    async def test_darwin_uses_osascript(self):
        from src.launcher import launch_remote_terminal

        proc = _make_proc(0)
        with patch("src.launcher.asyncio.create_subprocess_shell", return_value=proc) as mock_shell:
            result = await launch_remote_terminal("echo hello", "mac-mini")

        assert result["ok"] is True
        cmd = mock_shell.call_args[0][0]
        assert "osascript" in cmd
        assert "mac-mini" in cmd  # ssh target

    @pytest.mark.asyncio
    async def test_linux_uses_display_and_terminal_emulator(self):
        from src.launcher import launch_remote_terminal

        proc = _make_proc(0)
        with patch("src.launcher.asyncio.create_subprocess_shell", return_value=proc) as mock_shell:
            result = await launch_remote_terminal("echo hello", "ubuntu-desktop")

        assert result["ok"] is True
        cmd = mock_shell.call_args[0][0]
        assert "DISPLAY" in cmd
        assert "ubuntu-desktop" in cmd

    @pytest.mark.asyncio
    async def test_windows_uses_powershell_start_process(self):
        from src.launcher import launch_remote_terminal

        proc = _make_proc(0)
        with patch("src.launcher.asyncio.create_subprocess_shell", return_value=proc) as mock_shell:
            result = await launch_remote_terminal("echo hello", "avell-i7")

        assert result["ok"] is True
        cmd = mock_shell.call_args[0][0]
        assert "powershell" in cmd.lower() or "Start-Process" in cmd
        assert "avell-i7" in cmd

    @pytest.mark.asyncio
    async def test_unknown_os_returns_error(self):
        from src.launcher import launch_remote_terminal

        result = await launch_remote_terminal("echo hello", "mystery-machine")
        assert result["ok"] is False
        assert "Unknown remote OS" in result["error"]

    @pytest.mark.asyncio
    async def test_timeout_is_ok_fire_and_forget(self):
        from src.launcher import launch_remote_terminal

        proc = _make_proc(0)
        with patch("src.launcher.asyncio.create_subprocess_shell", return_value=proc), \
             patch("src.launcher.asyncio.wait_for", side_effect=asyncio.TimeoutError()):
            result = await launch_remote_terminal("echo hello", "mac-mini")

        # Timeout is fire-and-forget → ok
        assert result["ok"] is True

    @pytest.mark.asyncio
    async def test_darwin_applescript_contains_terminal_app(self):
        from src.launcher import launch_remote_terminal

        proc = _make_proc(0)
        with patch("src.launcher.asyncio.create_subprocess_shell", return_value=proc) as mock_shell:
            await launch_remote_terminal("my-command", "mac-mini")

        cmd = mock_shell.call_args[0][0]
        assert "Terminal" in cmd

    @pytest.mark.asyncio
    async def test_linux_command_contains_exec_bash(self):
        from src.launcher import launch_remote_terminal

        proc = _make_proc(0)
        with patch("src.launcher.asyncio.create_subprocess_shell", return_value=proc) as mock_shell:
            await launch_remote_terminal("my-command", "ubuntu-desktop")

        cmd = mock_shell.call_args[0][0]
        assert "exec bash" in cmd

    @pytest.mark.asyncio
    async def test_exception_returns_error(self):
        from src.launcher import launch_remote_terminal

        with patch("src.launcher.asyncio.create_subprocess_shell", side_effect=OSError("fail")):
            result = await launch_remote_terminal("echo hello", "mac-mini")

        assert result["ok"] is False


# ---------------------------------------------------------------------------
# launch_tmux_attach_remote
# ---------------------------------------------------------------------------

class TestLaunchTmuxAttachRemote:
    """launch_tmux_attach_remote() delegates to launch_remote_terminal with correct mux."""

    @pytest.mark.asyncio
    async def test_tmux_machine_uses_tmux_command(self):
        from src.launcher import launch_tmux_attach_remote

        with patch("src.launcher.launch_remote_terminal", new=AsyncMock(return_value={"ok": True})) as mock_rt:
            result = await launch_tmux_attach_remote("my-sess", "mac-mini")

        assert result["ok"] is True
        cmd = mock_rt.call_args[0][0]
        assert "tmux" in cmd
        assert "attach" in cmd
        assert "my-sess" in cmd

    @pytest.mark.asyncio
    async def test_psmux_machine_uses_psmux_command(self):
        from src.launcher import launch_tmux_attach_remote

        with patch("src.launcher.launch_remote_terminal", new=AsyncMock(return_value={"ok": True})) as mock_rt:
            result = await launch_tmux_attach_remote("win-sess", "avell-i7")

        cmd = mock_rt.call_args[0][0]
        assert "psmux" in cmd
        assert "attach" in cmd
        assert "win-sess" in cmd

    @pytest.mark.asyncio
    async def test_machine_passed_to_launch_remote_terminal(self):
        from src.launcher import launch_tmux_attach_remote

        with patch("src.launcher.launch_remote_terminal", new=AsyncMock(return_value={"ok": True})) as mock_rt:
            await launch_tmux_attach_remote("sess", "ubuntu-desktop")

        machine_arg = mock_rt.call_args[0][1]
        assert machine_arg == "ubuntu-desktop"

    @pytest.mark.asyncio
    async def test_session_name_shlex_quoted_in_command(self):
        from src.launcher import launch_tmux_attach_remote

        tricky_name = "sess with spaces"
        with patch("src.launcher.launch_remote_terminal", new=AsyncMock(return_value={"ok": True})) as mock_rt:
            await launch_tmux_attach_remote(tricky_name, "mac-mini")

        cmd = mock_rt.call_args[0][0]
        assert shlex.quote(tricky_name) in cmd

    @pytest.mark.asyncio
    async def test_unknown_machine_defaults_tmux(self):
        from src.launcher import launch_tmux_attach_remote

        with patch("src.launcher.launch_remote_terminal", new=AsyncMock(return_value={"ok": True})) as mock_rt:
            await launch_tmux_attach_remote("sess", "unknown-machine")

        cmd = mock_rt.call_args[0][0]
        assert "tmux" in cmd


# ---------------------------------------------------------------------------
# launch_new_tmux_and_attach — additional scenarios
# ---------------------------------------------------------------------------

class TestLaunchNewTmuxAndAttachExtra:
    """Extra scenarios for launch_new_tmux_and_attach."""

    @pytest.mark.asyncio
    async def test_no_cwd_no_command(self):
        from src.launcher import launch_new_tmux_and_attach

        mock_create = AsyncMock(return_value={"ok": True})
        mock_attach = AsyncMock(return_value={"ok": True})

        with patch("src.tmux_manager.create_tmux_session", mock_create), \
             patch("src.launcher.launch_tmux_attach", mock_attach):
            result = await launch_new_tmux_and_attach("sess", "mac-mini")

        mock_create.assert_awaited_once_with("mac-mini", "sess", cwd=None, command=None)
        assert result["ok"] is True

    @pytest.mark.asyncio
    async def test_with_cwd_and_command(self):
        from src.launcher import launch_new_tmux_and_attach

        mock_create = AsyncMock(return_value={"ok": True})
        mock_attach = AsyncMock(return_value={"ok": True})

        with patch("src.tmux_manager.create_tmux_session", mock_create), \
             patch("src.launcher.launch_tmux_attach", mock_attach):
            await launch_new_tmux_and_attach(
                "sess", "mac-mini", cwd="/home/user", command="python app.py"
            )

        create_kwargs = mock_create.call_args[1]
        assert create_kwargs["cwd"] == "/home/user"
        assert create_kwargs["command"] == "python app.py"

    @pytest.mark.asyncio
    async def test_attach_uses_correct_machine(self):
        from src.launcher import launch_new_tmux_and_attach

        mock_create = AsyncMock(return_value={"ok": True})
        mock_attach = AsyncMock(return_value={"ok": True})

        with patch("src.tmux_manager.create_tmux_session", mock_create), \
             patch("src.launcher.launch_tmux_attach", mock_attach):
            await launch_new_tmux_and_attach("my-sess", "ubuntu-desktop")

        attach_args = mock_attach.call_args[0]
        assert attach_args[0] == "my-sess"
        assert attach_args[1] == "ubuntu-desktop"

    @pytest.mark.asyncio
    async def test_create_failure_propagated_without_attach(self):
        from src.launcher import launch_new_tmux_and_attach

        mock_create = AsyncMock(return_value={"ok": False, "error": "duplicate name"})
        mock_attach = AsyncMock(return_value={"ok": True})

        with patch("src.tmux_manager.create_tmux_session", mock_create), \
             patch("src.launcher.launch_tmux_attach", mock_attach):
            result = await launch_new_tmux_and_attach("bad-sess", "mac-mini")

        mock_attach.assert_not_awaited()
        assert result["ok"] is False
        assert "duplicate name" in result["error"]

    @pytest.mark.asyncio
    async def test_attach_error_propagated(self):
        from src.launcher import launch_new_tmux_and_attach

        mock_create = AsyncMock(return_value={"ok": True})
        mock_attach = AsyncMock(return_value={"ok": False, "error": "no display"})

        with patch("src.tmux_manager.create_tmux_session", mock_create), \
             patch("src.launcher.launch_tmux_attach", mock_attach):
            result = await launch_new_tmux_and_attach("sess", "mac-mini")

        assert result["ok"] is False
        assert "no display" in result["error"]


# ---------------------------------------------------------------------------
# applescript_string — additional edge cases
# ---------------------------------------------------------------------------

class TestApplescriptStringEdgeCases:
    """Additional edge cases for applescript_string()."""

    def _fn(self):
        from src.launcher import applescript_string
        return applescript_string

    def test_newline_not_escaped(self):
        """Newlines in command should be handled without crashing."""
        fn = self._fn()
        result = fn("line1\nline2")
        assert isinstance(result, str)
        assert result.startswith('"')
        assert result.endswith('"')

    def test_tab_not_escaped(self):
        fn = self._fn()
        result = fn("a\tb")
        assert isinstance(result, str)

    def test_single_backslash_doubled(self):
        fn = self._fn()
        result = fn("path\\to\\file")
        assert "\\\\" in result

    def test_multiple_double_quotes(self):
        fn = self._fn()
        result = fn('"first" "second"')
        inner = result[1:-1]
        # No raw unescaped double quotes remain
        assert '"' not in inner.replace('\\"', "")

    def test_unicode_string(self):
        fn = self._fn()
        result = fn("café résumé")
        assert "café résumé" in result

    def test_backslash_at_end_of_string(self):
        fn = self._fn()
        result = fn("trailing\\")
        assert "\\\\" in result
        assert result.startswith('"')
        assert result.endswith('"')

    def test_only_backslash(self):
        fn = self._fn()
        result = fn("\\")
        assert result == '"\\\\"'

    def test_injection_attempt_neutralized(self):
        """Shell injection via AppleScript string must be neutralized."""
        fn = self._fn()
        malicious = 'end tell\ntell application "Terminal" to do malicious script "rm -rf /"'
        result = fn(malicious)
        # The raw double-quote that closes the string must be escaped
        inner = result[1:-1]
        assert '"' not in inner.replace('\\"', "")


# ---------------------------------------------------------------------------
# Platform-specific edge cases
# ---------------------------------------------------------------------------

class TestPlatformSpecificEdgeCases:
    """Edge cases in platform detection and command building."""

    @pytest.mark.asyncio
    async def test_darwin_platform_returns_ok_result_type(self):
        from src.launcher import launch_terminal

        with patch("src.launcher.sys") as mock_sys, \
             patch("src.launcher._launch_macos", new=AsyncMock(return_value={"ok": True})):
            mock_sys.platform = "darwin"
            result = await launch_terminal("echo hi")

        assert isinstance(result, dict)
        assert "ok" in result

    @pytest.mark.asyncio
    async def test_linux_startswith_check(self):
        """sys.platform == 'linux' triggers _launch_linux."""
        from src.launcher import launch_terminal

        with patch("src.launcher.sys") as mock_sys, \
             patch("src.launcher._launch_linux", new=AsyncMock(return_value={"ok": True})) as mock_linux:
            mock_sys.platform = "linux"
            await launch_terminal("echo hi")

        mock_linux.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_launch_tmux_attach_local_does_not_use_ssh(self):
        from src.launcher import launch_tmux_attach

        with patch("src.launcher.detect_local_machine", return_value="ubuntu-desktop"), \
             patch("src.launcher.launch_terminal", new=AsyncMock(return_value={"ok": True})) as mock_lt:
            await launch_tmux_attach("my-sess", "ubuntu-desktop")

        cmd = mock_lt.call_args[0][0]
        assert "ssh" not in cmd
        assert "tmux" in cmd

    @pytest.mark.asyncio
    async def test_cwd_with_spaces_shlex_quoted_in_remote_cmd(self):
        from src.launcher import launch_claude_session

        cwd_with_spaces = "/home/user/my project folder"
        with patch("src.launcher.detect_local_machine", return_value="mac-mini"), \
             patch("src.launcher.launch_terminal", new=AsyncMock(return_value={"ok": True})) as mock_lt:
            await launch_claude_session(cwd_with_spaces, "sess-001", "ubuntu-desktop")

        cmd = mock_lt.call_args[0][0]
        # The path should appear quoted
        assert shlex.quote(cwd_with_spaces) in cmd
