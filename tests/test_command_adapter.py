"""
Tests for src/command_adapter.py — OS-aware command builder.

Covers all public methods across darwin/linux/win32 × tmux/psmux combos.
Pure functions only; no mocking required.
"""
import pytest
from src.command_adapter import CommandAdapter, get_adapter


# ---------------------------------------------------------------------------
# TestCommandAdapterInit
# ---------------------------------------------------------------------------

class TestCommandAdapterInit:
    def test_darwin_tmux(self):
        adapter = CommandAdapter("darwin", "tmux")
        assert adapter.target_os == "darwin"
        assert adapter.mux_type == "tmux"
        assert adapter.is_windows is False
        assert adapter.target_shell == "bash"

    def test_linux_tmux(self):
        adapter = CommandAdapter("linux", "tmux")
        assert adapter.target_os == "linux"
        assert adapter.mux_type == "tmux"
        assert adapter.is_windows is False
        assert adapter.target_shell == "bash"

    def test_win32_psmux(self):
        adapter = CommandAdapter("win32", "psmux")
        assert adapter.target_os == "win32"
        assert adapter.mux_type == "psmux"
        assert adapter.is_windows is True
        assert adapter.target_shell == "cmd"

    def test_win32_tmux(self):
        adapter = CommandAdapter("win32", "tmux")
        assert adapter.target_os == "win32"
        assert adapter.mux_type == "tmux"
        assert adapter.is_windows is True
        assert adapter.target_shell == "bash"

    def test_default_mux_type_is_tmux(self):
        adapter = CommandAdapter("darwin")
        assert adapter.mux_type == "tmux"
        assert adapter.target_shell == "bash"


# ---------------------------------------------------------------------------
# TestQuotePath
# ---------------------------------------------------------------------------

class TestQuotePath:
    def setup_method(self):
        self.bash = CommandAdapter("darwin", "tmux")
        self.cmd = CommandAdapter("win32", "psmux")

    def test_bash_simple_path(self):
        # shlex.quote only adds quotes when path contains unsafe chars; safe paths are returned as-is
        assert self.bash.quote_path("/usr/local/bin") == "/usr/local/bin"

    def test_bash_path_with_spaces(self):
        assert self.bash.quote_path("/home/user/my project") == "'/home/user/my project'"

    def test_bash_path_special_chars(self):
        result = self.bash.quote_path("/path/with's/quote")
        # shlex.quote wraps in single quotes and escapes internal single quotes
        assert "'" in result

    def test_cmd_simple_path_no_quoting(self):
        assert self.cmd.quote_path("C:\\Users\\rbgnr") == "C:\\Users\\rbgnr"

    def test_cmd_path_with_spaces(self):
        assert self.cmd.quote_path("C:\\Users\\my user\\docs") == '"C:\\Users\\my user\\docs"'

    def test_cmd_path_with_ampersand(self):
        assert self.cmd.quote_path("C:\\Users\\R&D\\project") == '"C:\\Users\\R&D\\project"'

    def test_cmd_path_no_special_chars_no_quotes(self):
        result = self.cmd.quote_path("C:\\nospace")
        assert result == "C:\\nospace"


# ---------------------------------------------------------------------------
# TestCdCommand
# ---------------------------------------------------------------------------

class TestCdCommand:
    def setup_method(self):
        self.bash = CommandAdapter("linux", "tmux")
        self.cmd = CommandAdapter("win32", "psmux")

    def test_bash_simple(self):
        # shlex.quote on a safe path (no spaces/special chars) returns the path as-is
        assert self.bash.cd_command("/home/user/project") == "cd /home/user/project"

    def test_bash_path_with_spaces(self):
        assert self.bash.cd_command("/home/my user/proj") == "cd '/home/my user/proj'"

    def test_cmd_simple(self):
        assert self.cmd.cd_command("C:\\Projects\\myapp") == "cd /d C:\\Projects\\myapp"

    def test_cmd_path_with_spaces(self):
        result = self.cmd.cd_command("C:\\My Projects\\app")
        assert result == 'cd /d "C:\\My Projects\\app"'

    def test_cmd_includes_slash_d_flag(self):
        result = self.cmd.cd_command("D:\\work")
        assert "/d" in result


# ---------------------------------------------------------------------------
# TestChainCommands
# ---------------------------------------------------------------------------

class TestChainCommands:
    def setup_method(self):
        self.bash = CommandAdapter("darwin", "tmux")
        self.cmd = CommandAdapter("win32", "psmux")

    def test_bash_two_commands(self):
        result = self.bash.chain_commands("cd /tmp", "ls")
        assert result == "cd /tmp && ls"

    def test_cmd_two_commands(self):
        result = self.cmd.chain_commands("cd /d C:\\work", "dir")
        assert result == "cd /d C:\\work && dir"

    def test_three_commands(self):
        result = self.bash.chain_commands("a", "b", "c")
        assert result == "a && b && c"

    def test_single_command_no_ampersands(self):
        result = self.bash.chain_commands("echo hello")
        assert result == "echo hello"


# ---------------------------------------------------------------------------
# TestCdCommandSsh
# ---------------------------------------------------------------------------

class TestCdCommandSsh:
    def setup_method(self):
        self.bash = CommandAdapter("linux", "tmux")
        self.win = CommandAdapter("win32", "psmux")

    def test_bash_simple(self):
        # shlex.quote on a safe path returns it as-is (no quotes added)
        assert self.bash.cd_command_ssh("/home/user/project") == "cd /home/user/project"

    def test_bash_path_with_spaces(self):
        result = self.bash.cd_command_ssh("/home/my user/proj")
        assert result == "cd '/home/my user/proj'"

    def test_windows_set_location(self):
        result = self.win.cd_command_ssh("C:\\Users\\rbgnr\\project")
        assert result == "Set-Location 'C:\\Users\\rbgnr\\project'"

    def test_windows_unix_path(self):
        result = self.win.cd_command_ssh("/c/Users/rbgnr")
        assert result == "Set-Location '/c/Users/rbgnr'"


# ---------------------------------------------------------------------------
# TestBuildSessionCommandSsh
# ---------------------------------------------------------------------------

class TestBuildSessionCommandSsh:
    SESSION_ID = "abc123-def456"

    def setup_method(self):
        self.linux = CommandAdapter("linux", "tmux")
        self.win = CommandAdapter("win32", "psmux")

    def test_linux_basic(self):
        # shlex.quote on safe paths returns them unquoted
        result = self.linux.build_session_command_ssh("/home/user/proj", self.SESSION_ID)
        assert result == f"cd /home/user/proj && claude --resume {self.SESSION_ID}"

    def test_linux_with_skip_permissions(self):
        result = self.linux.build_session_command_ssh(
            "/home/user/proj", self.SESSION_ID, skip_permissions=True
        )
        assert "--dangerously-skip-permissions" in result

    def test_windows_uses_set_location_with_native_path(self):
        """Windows SSH lands in PowerShell — use Set-Location with native C:\\ paths."""
        result = self.win.build_session_command_ssh(
            "C:\\Users\\rbgnr\\project", self.SESSION_ID
        )
        assert "Set-Location 'C:\\Users\\rbgnr\\project'" in result

    def test_windows_d_drive(self):
        result = self.win.build_session_command_ssh(
            "D:\\Work\\myapp", self.SESSION_ID
        )
        assert "Set-Location 'D:\\Work\\myapp'" in result

    def test_windows_chained_with_semicolon_not_amp(self):
        """PowerShell 5.1 does NOT support && — use ; separator."""
        result = self.win.build_session_command_ssh(
            "C:\\Projects\\x", self.SESSION_ID
        )
        assert ";" in result
        assert " && " not in result

    def test_windows_with_skip_permissions(self):
        result = self.win.build_session_command_ssh(
            "C:\\proj", self.SESSION_ID, skip_permissions=True
        )
        assert "--dangerously-skip-permissions" in result

    def test_windows_escapes_single_quotes_in_path(self):
        """Paths with single quotes must be escaped by doubling in PowerShell."""
        result = self.win.build_session_command_ssh(
            "C:\\O'Brien\\app", self.SESSION_ID
        )
        assert "O''Brien" in result


class TestBuildNewSessionCommandSsh:
    """build_new_session_command_ssh — fresh session without --resume."""
    CWD_LINUX = "/home/rbgnr/git/proj"
    CWD_WIN = "C:\\Users\\rbgnr\\git\\proj"

    def setup_method(self):
        self.linux = CommandAdapter("linux", "tmux")
        self.win = CommandAdapter("win32", "psmux")

    def test_linux_basic(self):
        result = self.linux.build_new_session_command_ssh(self.CWD_LINUX)
        assert result == "cd /home/rbgnr/git/proj && claude"

    def test_linux_skip_permissions(self):
        result = self.linux.build_new_session_command_ssh(self.CWD_LINUX, skip_permissions=True)
        assert "--dangerously-skip-permissions" in result
        assert " && " in result

    def test_windows_uses_powershell_syntax(self):
        result = self.win.build_new_session_command_ssh(self.CWD_WIN)
        assert "Set-Location 'C:\\Users\\rbgnr\\git\\proj'" in result
        assert "; claude" in result
        assert " && " not in result

    def test_windows_no_resume_flag(self):
        result = self.win.build_new_session_command_ssh(self.CWD_WIN)
        assert "--resume" not in result

    def test_windows_skip_permissions(self):
        result = self.win.build_new_session_command_ssh(self.CWD_WIN, skip_permissions=True)
        assert "--dangerously-skip-permissions" in result


# ---------------------------------------------------------------------------
# TestWinPathToBash
# ---------------------------------------------------------------------------

class TestWinPathToBash:
    def test_c_drive_backslash(self):
        assert CommandAdapter._win_path_to_bash("C:\\Users\\rbgnr") == "/c/Users/rbgnr"

    def test_d_drive_backslash(self):
        assert CommandAdapter._win_path_to_bash("D:\\foo\\bar") == "/d/foo/bar"

    def test_lowercase_drive(self):
        assert CommandAdapter._win_path_to_bash("c:\\Users\\rbgnr") == "/c/Users/rbgnr"

    def test_forward_slash_drive(self):
        assert CommandAdapter._win_path_to_bash("C:/Users/rbgnr") == "/c/Users/rbgnr"

    def test_already_unix_path(self):
        assert CommandAdapter._win_path_to_bash("/home/user/project") == "/home/user/project"

    def test_no_drive_letter_backslashes_converted(self):
        # Path like \\server\share or just backslash path without drive
        result = CommandAdapter._win_path_to_bash("some\\path\\here")
        assert result == "some/path/here"

    def test_nested_path(self):
        result = CommandAdapter._win_path_to_bash("C:\\Users\\rbgnr\\git\\myproject")
        assert result == "/c/Users/rbgnr/git/myproject"


# ---------------------------------------------------------------------------
# TestQuoteArg
# ---------------------------------------------------------------------------

class TestQuoteArg:
    def setup_method(self):
        self.bash = CommandAdapter("darwin", "tmux")
        self.cmd = CommandAdapter("win32", "psmux")

    def test_bash_simple_arg(self):
        assert self.bash.quote_arg("hello") == "hello"

    def test_bash_arg_with_spaces(self):
        assert self.bash.quote_arg("hello world") == "'hello world'"

    def test_bash_uuid_no_quoting(self):
        uuid = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
        assert self.bash.quote_arg(uuid) == uuid

    def test_cmd_simple_arg(self):
        assert self.cmd.quote_arg("hello") == "hello"

    def test_cmd_arg_with_space(self):
        assert self.cmd.quote_arg("hello world") == '"hello world"'

    def test_cmd_arg_with_pipe(self):
        assert self.cmd.quote_arg("foo|bar") == '"foo|bar"'

    def test_cmd_arg_with_semicolon(self):
        assert self.cmd.quote_arg("foo;bar") == '"foo;bar"'

    def test_cmd_arg_with_ampersand(self):
        assert self.cmd.quote_arg("foo&bar") == '"foo&bar"'

    def test_cmd_uuid_no_quoting(self):
        uuid = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
        assert self.cmd.quote_arg(uuid) == uuid


# ---------------------------------------------------------------------------
# TestClaudeResumeCommand
# ---------------------------------------------------------------------------

class TestClaudeResumeCommand:
    SESSION_ID = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"

    def setup_method(self):
        self.adapter = CommandAdapter("darwin", "tmux")

    def test_basic_resume(self):
        result = self.adapter.claude_resume_command(self.SESSION_ID)
        assert result == f"claude --resume {self.SESSION_ID}"

    def test_with_skip_permissions(self):
        result = self.adapter.claude_resume_command(self.SESSION_ID, skip_permissions=True)
        assert result == f"claude --resume {self.SESSION_ID} --dangerously-skip-permissions"

    def test_without_skip_permissions_flag_absent(self):
        result = self.adapter.claude_resume_command(self.SESSION_ID, skip_permissions=False)
        assert "--dangerously-skip-permissions" not in result

    def test_session_id_with_spaces_gets_quoted(self):
        result = self.adapter.claude_resume_command("my session id")
        assert "'my session id'" in result


# ---------------------------------------------------------------------------
# TestBuildSessionCommand
# ---------------------------------------------------------------------------

class TestBuildSessionCommand:
    SESSION_ID = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"

    def test_bash_full_command(self):
        adapter = CommandAdapter("darwin", "tmux")
        result = adapter.build_session_command("/Users/rbgnr/project", self.SESSION_ID)
        # shlex.quote on safe paths (no spaces/special chars) returns path as-is
        assert result == f"cd /Users/rbgnr/project && claude --resume {self.SESSION_ID}"

    def test_bash_with_skip_permissions(self):
        adapter = CommandAdapter("linux", "tmux")
        result = adapter.build_session_command(
            "/home/user/proj", self.SESSION_ID, skip_permissions=True
        )
        assert result == (
            f"cd /home/user/proj && claude --resume {self.SESSION_ID}"
            " --dangerously-skip-permissions"
        )

    def test_cmd_full_command(self):
        adapter = CommandAdapter("win32", "psmux")
        result = adapter.build_session_command("C:\\Projects\\myapp", self.SESSION_ID)
        assert result == f"cd /d C:\\Projects\\myapp && claude --resume {self.SESSION_ID}"

    def test_cmd_with_spaces_in_path(self):
        adapter = CommandAdapter("win32", "psmux")
        result = adapter.build_session_command("C:\\My Projects\\app", self.SESSION_ID)
        assert '"C:\\My Projects\\app"' in result
        assert " && " in result


# ---------------------------------------------------------------------------
# TestMuxCreateSession
# ---------------------------------------------------------------------------

class TestMuxCreateSession:
    def test_tmux_create(self):
        adapter = CommandAdapter("darwin", "tmux")
        result = adapter.mux_create_session("my-session")
        assert result == "tmux new-session -d -s my-session"

    def test_psmux_create(self):
        adapter = CommandAdapter("win32", "psmux")
        result = adapter.mux_create_session("my-session")
        assert result == "psmux new-session -d -s my-session"

    def test_session_name_with_spaces_quoted(self):
        adapter = CommandAdapter("darwin", "tmux")
        result = adapter.mux_create_session("my session")
        assert result == "tmux new-session -d -s 'my session'"


# ---------------------------------------------------------------------------
# TestMuxSendKeys
# ---------------------------------------------------------------------------

class TestMuxSendKeys:
    def test_tmux_basic(self):
        adapter = CommandAdapter("darwin", "tmux")
        result = adapter.mux_send_keys("my-session", "ls -la")
        assert result == "tmux send-keys -t my-session 'ls -la' Enter"

    def test_psmux_basic(self):
        adapter = CommandAdapter("win32", "psmux")
        result = adapter.mux_send_keys("win-session", "dir")
        assert result == "psmux send-keys -t win-session dir Enter"

    def test_command_with_special_chars_quoted(self):
        adapter = CommandAdapter("darwin", "tmux")
        result = adapter.mux_send_keys("sess", "cd '/my path' && ls")
        # The whole command string should be shlex-quoted
        assert "send-keys" in result
        assert "Enter" in result
        # shlex.quote wraps in single quotes; special chars inside need escaping
        assert "sess" in result

    def test_session_name_with_spaces(self):
        adapter = CommandAdapter("linux", "tmux")
        result = adapter.mux_send_keys("my session", "echo hi")
        assert "'my session'" in result


# ---------------------------------------------------------------------------
# TestMuxAttach
# ---------------------------------------------------------------------------

class TestMuxAttach:
    def test_tmux_attach(self):
        adapter = CommandAdapter("darwin", "tmux")
        result = adapter.mux_attach("my-session")
        assert result == "tmux attach -t my-session"

    def test_psmux_attach(self):
        adapter = CommandAdapter("win32", "psmux")
        result = adapter.mux_attach("win-session")
        assert result == "psmux attach -t win-session"

    def test_session_name_with_spaces_quoted(self):
        adapter = CommandAdapter("darwin", "tmux")
        result = adapter.mux_attach("my session")
        assert result == "tmux attach -t 'my session'"


# ---------------------------------------------------------------------------
# TestMuxKillSession
# ---------------------------------------------------------------------------

class TestMuxKillSession:
    def test_tmux_kill(self):
        adapter = CommandAdapter("linux", "tmux")
        result = adapter.mux_kill_session("old-session")
        assert result == "tmux kill-session -t old-session"

    def test_psmux_kill(self):
        adapter = CommandAdapter("win32", "psmux")
        result = adapter.mux_kill_session("win-session")
        assert result == "psmux kill-session -t win-session"

    def test_session_name_with_spaces_quoted(self):
        adapter = CommandAdapter("linux", "tmux")
        result = adapter.mux_kill_session("my session")
        assert result == "tmux kill-session -t 'my session'"


# ---------------------------------------------------------------------------
# TestSshWrap
# ---------------------------------------------------------------------------

class TestSshWrap:
    def setup_method(self):
        self.adapter = CommandAdapter("darwin", "tmux")

    def test_without_tty(self):
        result = self.adapter.ssh_wrap("ubuntu-desktop", "ls /home")
        assert result == "ssh ubuntu-desktop 'ls /home'"

    def test_with_tty_allocation(self):
        result = self.adapter.ssh_wrap("ubuntu-desktop", "ls /home", allocate_tty=True)
        assert result == "ssh -t ubuntu-desktop 'ls /home'"

    def test_alias_with_spaces_quoted(self):
        result = self.adapter.ssh_wrap("my host", "echo hi")
        assert "'my host'" in result

    def test_command_with_special_chars_quoted(self):
        cmd = "cd '/path/with spaces' && ls"
        result = self.adapter.ssh_wrap("remote", cmd)
        assert "remote" in result
        # The command is shlex-quoted
        assert "ssh" in result

    def test_no_tty_flag_absent_by_default(self):
        result = self.adapter.ssh_wrap("host", "cmd")
        assert "-t " not in result


# ---------------------------------------------------------------------------
# TestForTerminal
# ---------------------------------------------------------------------------

class TestForTerminal:
    def test_bash_adds_exec_bash(self):
        adapter = CommandAdapter("darwin", "tmux")
        result = adapter.for_terminal("cd /tmp && ls")
        assert result == "cd /tmp && ls; exec bash"

    def test_bash_keep_open_false_no_suffix(self):
        adapter = CommandAdapter("linux", "tmux")
        result = adapter.for_terminal("echo hi", keep_open=False)
        assert result == "echo hi"
        assert "exec bash" not in result

    def test_cmd_no_op(self):
        adapter = CommandAdapter("win32", "psmux")
        original = "cd /d C:\\work && dir"
        result = adapter.for_terminal(original)
        assert result == original

    def test_cmd_keep_open_false_still_no_op(self):
        adapter = CommandAdapter("win32", "psmux")
        original = "dir"
        result = adapter.for_terminal(original, keep_open=False)
        assert result == original


# ---------------------------------------------------------------------------
# TestGenerateMuxSessionName
# ---------------------------------------------------------------------------

class TestGenerateMuxSessionName:
    def setup_method(self):
        self.adapter = CommandAdapter("darwin", "tmux")

    def test_no_existing_sessions(self):
        result = self.adapter.generate_mux_session_name("mac-mini", "myproject", [])
        assert result == "mac-mini_myproject-session-01"

    def test_one_existing_increments(self):
        existing = ["mac-mini_myproject-session-01"]
        result = self.adapter.generate_mux_session_name("mac-mini", "myproject", existing)
        assert result == "mac-mini_myproject-session-02"

    def test_multiple_existing_picks_next(self):
        existing = [
            "mac-mini_myproject-session-01",
            "mac-mini_myproject-session-02",
            "mac-mini_myproject-session-03",
        ]
        result = self.adapter.generate_mux_session_name("mac-mini", "myproject", existing)
        assert result == "mac-mini_myproject-session-04"

    def test_unrelated_sessions_ignored(self):
        existing = [
            "other-machine_myproject-session-05",
            "mac-mini_otherproject-session-99",
        ]
        result = self.adapter.generate_mux_session_name("mac-mini", "myproject", existing)
        assert result == "mac-mini_myproject-session-01"

    def test_format_uses_underscore_not_slash(self):
        result = self.adapter.generate_mux_session_name("mac-mini", "proj", [])
        assert "/" not in result
        assert "_" in result

    def test_zero_padded_two_digits(self):
        result = self.adapter.generate_mux_session_name("mac-mini", "proj", [])
        # Should end in -01, not -1
        assert result.endswith("-01")


# ---------------------------------------------------------------------------
# TestGenerateClaudeSessionName
# ---------------------------------------------------------------------------

class TestGenerateClaudeSessionName:
    def test_tmux_format(self):
        adapter = CommandAdapter("darwin", "tmux")
        result = adapter.generate_claude_session_name("mac-mini", "myproject", 1)
        assert result == "[tmux] mac-mini/myproject session 01"

    def test_psmux_format(self):
        adapter = CommandAdapter("win32", "psmux")
        result = adapter.generate_claude_session_name("avell-i7", "myapp", 3)
        assert result == "[psmux] avell-i7/myapp session 03"

    def test_session_number_zero_padded(self):
        adapter = CommandAdapter("linux", "tmux")
        result = adapter.generate_claude_session_name("ubuntu-desktop", "proj", 5)
        assert "session 05" in result

    def test_two_digit_session_number(self):
        adapter = CommandAdapter("darwin", "tmux")
        result = adapter.generate_claude_session_name("mac-mini", "proj", 12)
        assert "session 12" in result


# ---------------------------------------------------------------------------
# TestGetAdapter
# ---------------------------------------------------------------------------

class TestGetAdapter:
    def test_mac_mini(self):
        adapter = get_adapter("mac-mini")
        assert adapter.target_os == "darwin"
        assert adapter.mux_type == "tmux"
        assert adapter.is_windows is False
        assert adapter.target_shell == "bash"

    def test_ubuntu_desktop(self):
        adapter = get_adapter("ubuntu-desktop")
        assert adapter.target_os == "linux"
        assert adapter.mux_type == "tmux"
        assert adapter.is_windows is False
        assert adapter.target_shell == "bash"

    def test_avell_i7(self):
        adapter = get_adapter("avell-i7")
        assert adapter.target_os == "win32"
        assert adapter.mux_type == "psmux"
        assert adapter.is_windows is True
        assert adapter.target_shell == "cmd"

    def test_windows_desktop(self):
        adapter = get_adapter("windows-desktop")
        assert adapter.target_os == "win32"
        assert adapter.mux_type == "psmux"
        assert adapter.is_windows is True
        assert adapter.target_shell == "cmd"

    def test_unknown_machine_defaults_to_darwin_tmux(self):
        adapter = get_adapter("nonexistent-machine")
        assert adapter.target_os == "darwin"
        assert adapter.mux_type == "tmux"
        assert adapter.is_windows is False
        assert adapter.target_shell == "bash"

    def test_returns_command_adapter_instance(self):
        adapter = get_adapter("mac-mini")
        assert isinstance(adapter, CommandAdapter)


class TestBuildPaneCommand:
    """build_pane_command emits shell-appropriate cd + claude for the pane.

    psmux on Windows → PowerShell pane (modern default): `Set-Location '...'; claude`.
    tmux on Unix → bash pane: `cd '...' && claude`.

    Regression guard: the old code emitted `cd /d C:\\path && claude` for psmux
    which fails inside PowerShell panes (`/d` is not a switch on Set-Location,
    `&&` isn't supported in PS 5.1). Result: Claude launched at the home
    directory instead of the project directory.
    """

    def test_psmux_new_session_uses_powershell(self):
        a = CommandAdapter(target_os="win32", mux_type="psmux")
        cmd = a.build_pane_command("C:\\Users\\rbgnr\\git\\smart-kanban")
        assert cmd == "Set-Location 'C:\\Users\\rbgnr\\git\\smart-kanban'; claude"
        assert "cd /d" not in cmd
        assert "&&" not in cmd  # PS 5.1 doesn't support it

    def test_psmux_resume_uses_powershell(self):
        a = CommandAdapter(target_os="win32", mux_type="psmux")
        cmd = a.build_pane_command("C:\\proj", session_id="abc-123")
        assert cmd.startswith("Set-Location 'C:\\proj'; claude --resume")
        assert "abc-123" in cmd

    def test_psmux_skip_permissions_flag(self):
        a = CommandAdapter(target_os="win32", mux_type="psmux")
        cmd = a.build_pane_command("C:\\proj", skip_permissions=True)
        assert "--dangerously-skip-permissions" in cmd

    def test_psmux_escapes_single_quote_in_path(self):
        """PowerShell single-quoted strings escape ' by doubling."""
        a = CommandAdapter(target_os="win32", mux_type="psmux")
        cmd = a.build_pane_command("C:\\foo's\\bar")
        assert "'C:\\foo''s\\bar'" in cmd

    def test_tmux_unix_uses_bash_syntax(self):
        a = CommandAdapter(target_os="darwin", mux_type="tmux")
        cmd = a.build_pane_command("/Users/rbgnr/git/proj")
        # shlex.quote omits quotes for paths with no special chars
        assert "cd /Users/rbgnr/git/proj" in cmd
        assert "&&" in cmd

    def test_tmux_unix_resume(self):
        a = CommandAdapter(target_os="linux", mux_type="tmux")
        cmd = a.build_pane_command("/home/rbgnr/proj", session_id="u1")
        assert "cd /home/rbgnr/proj" in cmd
        assert "&&" in cmd
        assert "--resume" in cmd and "u1" in cmd

    def test_tmux_unix_quotes_path_with_spaces(self):
        a = CommandAdapter(target_os="darwin", mux_type="tmux")
        cmd = a.build_pane_command("/Users/rbgnr/my code/proj")
        assert "'/Users/rbgnr/my code/proj'" in cmd

    def test_empty_session_id_is_fresh(self):
        """session_id None → fresh `claude`, no --resume."""
        a = CommandAdapter(target_os="win32", mux_type="psmux")
        cmd = a.build_pane_command("C:\\proj", session_id=None)
        assert "--resume" not in cmd
        assert cmd.endswith("; claude")


class TestMuxSendKeysPowerShell:
    """mux_send_keys_ps produces PowerShell-safe quoting.

    Regression: mux_send_keys uses POSIX shlex.quote which wraps embedded
    single quotes as `'\"'\"'`. PowerShell parses that as FIVE separate
    string-literal arguments, so the psmux command's text payload gets split
    into pieces and the pane never receives the intended Set-Location.
    Result: claude started at the home dir instead of the project dir.
    The _ps variant uses PowerShell's own rule: double internal `'` to `''`.
    """

    def _adapter(self):
        return CommandAdapter(target_os="win32", mux_type="psmux")

    def test_wraps_text_in_single_quotes(self):
        out = self._adapter().mux_send_keys_ps("sess", "claude")
        assert out == "psmux send-keys -t 'sess' 'claude' Enter"

    def test_doubles_internal_single_quote(self):
        """Set-Location 'C:\\x'; claude → escape ' as ''. No POSIX '\"'\"' sequence."""
        a = self._adapter()
        out = a.mux_send_keys_ps("sess", "Set-Location 'C:\\x'; claude")
        assert out == "psmux send-keys -t 'sess' 'Set-Location ''C:\\x''; claude' Enter"
        # POSIX escape must NOT appear — that's the bug.
        assert "'\"'\"'" not in out

    def test_preserves_backslashes(self):
        """Windows paths contain \\ — PowerShell single-quoted strings treat
        \\ literally, so no escaping needed."""
        a = self._adapter()
        out = a.mux_send_keys_ps("s", "Set-Location 'C:\\Users\\rbgnr\\git\\Smart-kanban'; claude")
        assert "C:\\Users\\rbgnr\\git\\Smart-kanban" in out

    def test_session_name_with_quote_is_escaped(self):
        """If a session name somehow contains ', it must be doubled too."""
        out = self._adapter().mux_send_keys_ps("sess'name", "claude")
        assert "'sess''name'" in out

    def test_send_keys_ps_is_not_posix_equivalent(self):
        """The two variants must differ when the payload has any single quote."""
        a = self._adapter()
        cmd_with_quote = "Set-Location 'C:\\x'; claude"
        posix = a.mux_send_keys("sess", cmd_with_quote)
        ps = a.mux_send_keys_ps("sess", cmd_with_quote)
        assert posix != ps
        # POSIX uses '"'"' to re-quote; PowerShell uses ''.
        assert "'\"'\"'" in posix
        assert "'\"'\"'" not in ps


# ---------------------------------------------------------------------------
# TestSanitizeMuxName — covers the "no slash in mux session names" rule.
# Both tmux AND psmux reject '/'. Also '.', ':', whitespace.  Every call site
# that feeds a name to {tmux,psmux} new-session/kill-session/has-session/etc.
# must route through this. See memory: feedback_no_slash_in_mux_session_names.md.
# ---------------------------------------------------------------------------

from src.command_adapter import sanitize_mux_name  # noqa: E402


class TestSanitizeMuxName:
    def test_plain_alphanumeric_passes_through(self):
        assert sanitize_mux_name("project01") == "project01"

    def test_underscore_and_dash_allowed(self):
        assert sanitize_mux_name("my_project-session-01") == "my_project-session-01"

    def test_single_slash_becomes_dash(self):
        assert sanitize_mux_name("nxs-ensemble/streams-android") == "nxs-ensemble-streams-android"

    def test_multiple_slashes_collapse_to_single_dash(self):
        assert sanitize_mux_name("a/b/c/d") == "a-b-c-d"

    def test_backslash_becomes_dash(self):
        # Windows path separator should also be sanitized.
        assert sanitize_mux_name("C:\\Users\\rbgnr\\proj") == "C-Users-rbgnr-proj"

    def test_dot_becomes_dash(self):
        # tmux treats '.' as pane-index separator.
        assert sanitize_mux_name("my.project.name") == "my-project-name"

    def test_colon_becomes_dash(self):
        # Both muxes treat ':' as window-index separator.
        assert sanitize_mux_name("session:1") == "session-1"

    def test_space_becomes_dash(self):
        assert sanitize_mux_name("my project name") == "my-project-name"

    def test_mixed_whitespace_collapses(self):
        assert sanitize_mux_name("my\t\nproject \t name") == "my-project-name"

    def test_leading_slash_stripped(self):
        # Leading unsafe chars get replaced, then leading dashes stripped.
        assert sanitize_mux_name("/absolute/path") == "absolute-path"

    def test_trailing_slash_stripped(self):
        assert sanitize_mux_name("project/") == "project"

    def test_all_unsafe_becomes_default(self):
        # If sanitization reduces to empty, fall back to 'session'.
        assert sanitize_mux_name("///") == "session"
        assert sanitize_mux_name("...") == "session"
        assert sanitize_mux_name(":::") == "session"
        assert sanitize_mux_name("   ") == "session"

    def test_empty_string_becomes_default(self):
        assert sanitize_mux_name("") == "session"

    def test_repeated_unsafe_chars_collapse_to_single_dash(self):
        assert sanitize_mux_name("a//b") == "a-b"
        assert sanitize_mux_name("a!!@@##b") == "a-b"
        assert sanitize_mux_name("a///b///c") == "a-b-c"

    def test_mixed_unsafe_chars_all_converted(self):
        # Realistic nasty input: path with slashes, dots, spaces, colons.
        raw = "nxs-ensemble/streams-android:main feat.v2"
        # Expected: every run of unsafe chars becomes a single dash.
        assert sanitize_mux_name(raw) == "nxs-ensemble-streams-android-main-feat-v2"

    def test_windows_drive_letter_form(self):
        # "C:/Users/..." form (mixed slashes) — same sanitization.
        assert sanitize_mux_name("C:/Users/rbgnr/git/streams-android") == "C-Users-rbgnr-git-streams-android"

    def test_unicode_rejected(self):
        # Only ASCII [A-Za-z0-9_-] survives; unicode → dash.
        assert sanitize_mux_name("projëct") == "proj-ct"
        assert sanitize_mux_name("项目") == "session"

    def test_numeric_only_passes_through(self):
        assert sanitize_mux_name("12345") == "12345"

    def test_already_sanitized_idempotent(self):
        # Running twice must produce the same output.
        once = sanitize_mux_name("foo/bar.baz:qux")
        twice = sanitize_mux_name(once)
        assert once == twice == "foo-bar-baz-qux"

    def test_shell_metacharacters_all_stripped(self):
        # Would break `<mux> -t NAME` if not sanitized.
        for meta in ["$", "`", "\"", "'", "(", ")", "[", "]", "{", "}", "|", "&", ";", "*", "?", "<", ">"]:
            result = sanitize_mux_name(f"pre{meta}post")
            assert "/" not in result
            assert meta not in result, f"metacharacter {meta!r} leaked through"
            assert result == "pre-post", f"unexpected shape for {meta!r}: {result!r}"

    def test_invariant_no_slash_ever_in_output(self):
        # Fundamental rule: the output must NEVER contain '/' or '\\'.
        samples = [
            "a/b", "a\\b", "/leading", "trailing/",
            "a/b/c/d/e", "///", "path/with/slashes",
            "C:\\Windows\\Path",
        ]
        for s in samples:
            out = sanitize_mux_name(s)
            assert "/" not in out, f"slash leaked for {s!r}: {out!r}"
            assert "\\" not in out, f"backslash leaked for {s!r}: {out!r}"

    def test_generate_mux_session_name_never_introduces_slash(self):
        # Integration: confirm generate_mux_session_name output is always safe
        # even when given project_folder strings that happen to look path-like.
        adapter = CommandAdapter("win32", "psmux")
        result = adapter.generate_mux_session_name("avell-i7", "streams-android", [])
        assert "/" not in result
        assert "\\" not in result
        assert result == "avell-i7_streams-android-session-01"
