"""Unit tests for src/tmux_manager.py — all subprocess/SSH calls mocked."""
from __future__ import annotations

import asyncio
from dataclasses import asdict
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

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
# TmuxSession dataclass
# ---------------------------------------------------------------------------

class TestTmuxSessionDataclass:
    """TmuxSession fields and to_dict()."""

    def _import(self):
        from src.tmux_manager import TmuxSession
        return TmuxSession

    def test_fields_set_correctly(self):
        TmuxSession = self._import()
        s = TmuxSession(
            name="my-session",
            machine="mac-mini",
            created="2024-01-01T00:00:00+00:00",
            windows=3,
            attached=True,
            is_local=True,
        )
        assert s.name == "my-session"
        assert s.machine == "mac-mini"
        assert s.created == "2024-01-01T00:00:00+00:00"
        assert s.windows == 3
        assert s.attached is True
        assert s.is_local is True

    def test_to_dict_returns_all_fields(self):
        TmuxSession = self._import()
        s = TmuxSession(
            name="sess",
            machine="ubuntu-desktop",
            created="2024-06-15T12:00:00+00:00",
            windows=1,
            attached=False,
            is_local=False,
        )
        d = s.to_dict()
        assert isinstance(d, dict)
        assert d["name"] == "sess"
        assert d["machine"] == "ubuntu-desktop"
        assert d["created"] == "2024-06-15T12:00:00+00:00"
        assert d["windows"] == 1
        assert d["attached"] is False
        assert d["is_local"] is False

    def test_to_dict_matches_asdict(self):
        TmuxSession = self._import()
        s = TmuxSession(name="x", machine="y", created="z", windows=0, attached=False, is_local=True)
        assert s.to_dict() == asdict(s)


# ---------------------------------------------------------------------------
# _unix_to_iso
# ---------------------------------------------------------------------------

class TestMuxParser:
    """Test the universal mux parser used by tmux_manager."""

    def test_parse_pipe_format(self):
        from src.mux_parser import parse_mux_output
        output = "my-session|1712649600|3|0\nother|1712649600|1|1"
        result = parse_mux_output(output)
        assert len(result) == 2
        assert result[0]["name"] == "my-session"
        assert result[0]["windows"] == 3
        assert result[0]["attached"] is False
        assert result[1]["attached"] is True

    def test_parse_plain_text(self):
        from src.mux_parser import parse_mux_output
        output = "test-avell: 1 windows (created Thu Apr  9 03:25:03 2026)"
        result = parse_mux_output(output)
        assert len(result) == 1
        assert result[0]["name"] == "test-avell"
        assert result[0]["windows"] == 1

    def test_parse_plain_text_attached(self):
        from src.mux_parser import parse_mux_output
        output = "dev: 2 windows (created Mon Jan  1 10:00:00 2024) (attached)"
        result = parse_mux_output(output)
        assert result[0]["attached"] is True

    def test_parse_empty(self):
        from src.mux_parser import parse_mux_output
        assert parse_mux_output("") == []
        assert parse_mux_output("  \n  \n") == []

    def test_parse_name_only_fallback(self):
        from src.mux_parser import parse_mux_output
        output = "session1\nsession2"
        result = parse_mux_output(output)
        assert len(result) == 2
        assert result[0]["name"] == "session1"


# ---------------------------------------------------------------------------
# list_local_tmux
# ---------------------------------------------------------------------------

class TestListLocalTmux:
    """list_local_tmux() parses output and handles errors."""

    SAMPLE_OUTPUT = (
        b"main-session|1705314600|2|0\n"
        b"dev-work|1705314700|1|1\n"
    )

    @pytest.mark.asyncio
    async def test_parses_tmux_output_correctly(self):
        from src.tmux_manager import list_local_tmux

        proc = _make_proc(0, stdout=self.SAMPLE_OUTPUT)
        with patch("src.tmux_manager.asyncio.create_subprocess_exec", return_value=proc) as mock_exec, \
             patch("src.tmux_manager.detect_local_machine", return_value="mac-mini"):
            sessions = await list_local_tmux()

        assert len(sessions) == 2
        # Sessions are returned in parse order (list_local_tmux doesn't sort)
        names = [s.name for s in sessions]
        assert "main-session" in names
        assert "dev-work" in names

    @pytest.mark.asyncio
    async def test_sets_machine_and_is_local(self):
        from src.tmux_manager import list_local_tmux

        proc = _make_proc(0, stdout=b"sess|1705314600|1|0\n")
        with patch("src.tmux_manager.asyncio.create_subprocess_exec", return_value=proc), \
             patch("src.tmux_manager.detect_local_machine", return_value="ubuntu-desktop"):
            sessions = await list_local_tmux()

        assert len(sessions) == 1
        assert sessions[0].machine == "ubuntu-desktop"
        assert sessions[0].is_local is True

    @pytest.mark.asyncio
    async def test_attached_flag_parsed(self):
        from src.tmux_manager import list_local_tmux

        proc = _make_proc(0, stdout=b"attached-sess|1705314600|2|1\nnot-attached|1705314600|2|0\n")
        with patch("src.tmux_manager.asyncio.create_subprocess_exec", return_value=proc), \
             patch("src.tmux_manager.detect_local_machine", return_value="mac-mini"):
            sessions = await list_local_tmux()

        by_name = {s.name: s for s in sessions}
        assert by_name["attached-sess"].attached is True
        assert by_name["not-attached"].attached is False

    @pytest.mark.asyncio
    async def test_windows_count_parsed(self):
        from src.tmux_manager import list_local_tmux

        proc = _make_proc(0, stdout=b"multi|1705314600|5|0\n")
        with patch("src.tmux_manager.asyncio.create_subprocess_exec", return_value=proc), \
             patch("src.tmux_manager.detect_local_machine", return_value="mac-mini"):
            sessions = await list_local_tmux()

        assert sessions[0].windows == 5

    @pytest.mark.asyncio
    async def test_no_server_running_returns_empty(self):
        """Non-zero returncode (e.g. 'no server running') → returns []."""
        from src.tmux_manager import list_local_tmux

        proc = _make_proc(1, stderr=b"no server running on /tmp/tmux-1000/default")
        with patch("src.tmux_manager.asyncio.create_subprocess_exec", return_value=proc), \
             patch("src.tmux_manager.detect_local_machine", return_value="mac-mini"):
            sessions = await list_local_tmux()

        assert sessions == []

    @pytest.mark.asyncio
    async def test_tmux_not_installed_returns_empty(self):
        """FileNotFoundError (tmux not on PATH) → returns []."""
        from src.tmux_manager import list_local_tmux

        with patch("src.tmux_manager.asyncio.create_subprocess_exec", side_effect=FileNotFoundError("tmux not found")), \
             patch("src.tmux_manager.detect_local_machine", return_value="mac-mini"):
            sessions = await list_local_tmux()

        assert sessions == []

    @pytest.mark.asyncio
    async def test_timeout_returns_empty(self):
        """asyncio.TimeoutError → returns []."""
        from src.tmux_manager import list_local_tmux

        proc = AsyncMock()
        proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError())
        with patch("src.tmux_manager.asyncio.create_subprocess_exec", return_value=proc), \
             patch("src.tmux_manager.asyncio.wait_for", side_effect=asyncio.TimeoutError()), \
             patch("src.tmux_manager.detect_local_machine", return_value="mac-mini"):
            sessions = await list_local_tmux()

        assert sessions == []

    @pytest.mark.asyncio
    async def test_empty_output_returns_empty(self):
        from src.tmux_manager import list_local_tmux

        proc = _make_proc(0, stdout=b"")
        with patch("src.tmux_manager.asyncio.create_subprocess_exec", return_value=proc), \
             patch("src.tmux_manager.detect_local_machine", return_value="mac-mini"):
            sessions = await list_local_tmux()

        assert sessions == []

    @pytest.mark.asyncio
    async def test_malformed_lines_skipped(self):
        """Lines with wrong number of pipe-separated fields are silently skipped."""
        from src.tmux_manager import list_local_tmux

        output = b"good|1705314600|2|0\nbad-line\nalso|bad\n"
        proc = _make_proc(0, stdout=output)
        with patch("src.tmux_manager.asyncio.create_subprocess_exec", return_value=proc), \
             patch("src.tmux_manager.detect_local_machine", return_value="mac-mini"):
            sessions = await list_local_tmux()

        assert len(sessions) == 1
        assert sessions[0].name == "good"

    @pytest.mark.asyncio
    async def test_correct_tmux_command_invoked(self):
        from src.tmux_manager import list_local_tmux

        proc = _make_proc(0, stdout=b"")
        with patch("src.tmux_manager.asyncio.create_subprocess_exec", return_value=proc) as mock_exec, \
             patch("src.tmux_manager.detect_local_machine", return_value="mac-mini"):
            await list_local_tmux()

        args = mock_exec.call_args[0]
        assert args[0] == "tmux"
        assert args[1] == "list-sessions"
        assert "-F" in args
        fmt_idx = list(args).index("-F")
        assert "#{session_name}" in args[fmt_idx + 1]
        assert "#{session_created}" in args[fmt_idx + 1]
        assert "#{session_windows}" in args[fmt_idx + 1]
        assert "#{session_attached}" in args[fmt_idx + 1]


# ---------------------------------------------------------------------------
# list_remote_tmux
# ---------------------------------------------------------------------------

class TestListRemoteTmux:
    """list_remote_tmux() with tmux (mac/linux) and psmux (Windows)."""

    SAMPLE_OUTPUT = b"remote-sess|1705314600|1|0\n"

    @pytest.mark.asyncio
    async def test_tmux_remote_parses_sessions(self):
        from src.tmux_manager import list_remote_tmux

        proc = _make_proc(0, stdout=self.SAMPLE_OUTPUT)
        with patch("src.tmux_manager.asyncio.create_subprocess_exec", return_value=proc):
            sessions = await list_remote_tmux("ubuntu-desktop", "ubuntu-desktop", "tmux")

        assert len(sessions) == 1
        assert sessions[0].name == "remote-sess"
        assert sessions[0].machine == "ubuntu-desktop"
        assert sessions[0].is_local is False

    @pytest.mark.asyncio
    async def test_tmux_remote_ssh_command_structure(self):
        from src.tmux_manager import list_remote_tmux
        from src.config import SSH_TIMEOUT

        proc = _make_proc(0, stdout=self.SAMPLE_OUTPUT)
        with patch("src.tmux_manager.asyncio.create_subprocess_exec", return_value=proc) as mock_exec:
            await list_remote_tmux("ubuntu-desktop", "ubuntu-desktop", "tmux")

        args = mock_exec.call_args[0]
        assert args[0] == "ssh"
        # SSH options may be single args ("-o ConnectTimeout=3") or split ("-o", "ConnectTimeout=3")
        args_str = " ".join(str(a) for a in args)
        assert f"ConnectTimeout={SSH_TIMEOUT}" in args_str
        assert "BatchMode=yes" in args_str
        assert "ubuntu-desktop" in args_str
        # Last arg is the remote command string
        remote_cmd = args[-1]
        assert "tmux list-sessions" in remote_cmd

    @pytest.mark.asyncio
    async def test_tmux_remote_nonzero_returns_empty(self):
        from src.tmux_manager import list_remote_tmux

        proc = _make_proc(1, stderr=b"Connection refused")
        with patch("src.tmux_manager.asyncio.create_subprocess_exec", return_value=proc):
            sessions = await list_remote_tmux("ubuntu-desktop", "ubuntu-desktop", "tmux")

        assert sessions == []

    @pytest.mark.asyncio
    async def test_psmux_with_format_success(self):
        """psmux list-sessions -F <fmt> succeeds → parsed normally."""
        from src.tmux_manager import list_remote_tmux

        proc = _make_proc(0, stdout=self.SAMPLE_OUTPUT)
        with patch("src.tmux_manager.asyncio.create_subprocess_exec", return_value=proc):
            sessions = await list_remote_tmux("avell-i7", "avell-i7", "psmux")

        assert len(sessions) == 1
        assert sessions[0].name == "remote-sess"

    @pytest.mark.asyncio
    async def test_psmux_falls_back_to_plain_list(self):
        """psmux -F fails → plain list-sessions → returns name-only sessions."""
        from src.tmux_manager import list_remote_tmux

        # First call (with -F) fails, second call (plain) succeeds
        proc_fail = _make_proc(1, stdout=b"", stderr=b"unsupported flag")
        proc_plain = _make_proc(0, stdout=b"session-alpha\nsession-beta\n")

        call_count = 0

        async def side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return proc_fail if call_count == 1 else proc_plain

        with patch("src.tmux_manager.asyncio.create_subprocess_exec", side_effect=side_effect):
            sessions = await list_remote_tmux("avell-i7", "avell-i7", "psmux")

        assert len(sessions) == 2
        names = {s.name for s in sessions}
        assert "session-alpha" in names
        assert "session-beta" in names
        # Plain psmux sessions have empty created, 0 windows, not attached
        for s in sessions:
            assert s.created == ""
            assert s.windows == 0
            assert s.attached is False
            assert s.is_local is False
            assert s.machine == "avell-i7"

    @pytest.mark.asyncio
    async def test_psmux_plain_list_also_fails_returns_empty(self):
        from src.tmux_manager import list_remote_tmux

        proc = _make_proc(1, stdout=b"", stderr=b"error")
        with patch("src.tmux_manager.asyncio.create_subprocess_exec", return_value=proc):
            sessions = await list_remote_tmux("avell-i7", "avell-i7", "psmux")

        assert sessions == []

    @pytest.mark.asyncio
    async def test_ssh_timeout_returns_empty(self):
        """asyncio.TimeoutError during SSH → returns []."""
        from src.tmux_manager import list_remote_tmux

        proc = AsyncMock()
        proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError())

        with patch("src.tmux_manager.asyncio.create_subprocess_exec", return_value=proc), \
             patch("src.tmux_manager.asyncio.wait_for", side_effect=asyncio.TimeoutError()):
            sessions = await list_remote_tmux("ubuntu-desktop", "ubuntu-desktop", "tmux")

        assert sessions == []

    @pytest.mark.asyncio
    async def test_connection_refused_oserror_returns_empty(self):
        """OSError (connection refused, etc.) → returns []."""
        from src.tmux_manager import list_remote_tmux

        with patch("src.tmux_manager.asyncio.create_subprocess_exec", side_effect=OSError("Connection refused")):
            sessions = await list_remote_tmux("ubuntu-desktop", "ubuntu-desktop", "tmux")

        assert sessions == []


# ---------------------------------------------------------------------------
# list_all_tmux
# ---------------------------------------------------------------------------

class TestListAllTmux:
    """list_all_tmux() aggregates local + remote, skips offline machines."""

    @pytest.mark.asyncio
    async def test_aggregates_local_and_remote(self):
        from src.tmux_manager import list_all_tmux

        local_session = MagicMock()
        local_session.machine = "mac-mini"
        local_session.name = "local-sess"

        remote_session = MagicMock()
        remote_session.machine = "ubuntu-desktop"
        remote_session.name = "remote-sess"

        async def fake_list_local():
            return [local_session]

        async def fake_list_remote(machine_name, ssh_alias, mux):
            return [remote_session]

        fleet_status = {
            "ubuntu-desktop": {"online": True},
            "avell-i7": {"online": False},
            "windows-desktop": {"online": False},
        }

        async def fake_list_remote_api(machine_name, ip, dispatch_port):
            return [remote_session]

        with patch("src.tmux_manager.list_local_tmux", new=fake_list_local), \
             patch("src.tmux_manager.list_remote_tmux", new=fake_list_remote), \
             patch("src.tmux_manager.list_remote_tmux_via_api", new=fake_list_remote_api):
            sessions = await list_all_tmux("mac-mini", fleet_status)

        names = [s.name for s in sessions]
        assert "local-sess" in names
        assert "remote-sess" in names

    @pytest.mark.asyncio
    async def test_skips_offline_machines(self):
        from src.tmux_manager import list_all_tmux

        async def fake_list_local():
            return []

        remote_call_machines = []

        async def fake_list_remote(machine_name, ssh_alias, mux):
            remote_call_machines.append(machine_name)
            return []

        fleet_status = {
            "ubuntu-desktop": {"online": False},
            "avell-i7": {"online": False},
            "windows-desktop": {"online": False},
        }

        with patch("src.tmux_manager.list_local_tmux", new=fake_list_local), \
             patch("src.tmux_manager.list_remote_tmux", new=fake_list_remote):
            sessions = await list_all_tmux("mac-mini", fleet_status)

        assert remote_call_machines == []
        assert sessions == []

    @pytest.mark.asyncio
    async def test_skips_local_machine_in_remote_loop(self):
        """The local machine should not appear in remote SSH calls."""
        from src.tmux_manager import list_all_tmux

        async def fake_list_local():
            return []

        remote_call_machines = []

        async def fake_list_remote(machine_name, ssh_alias, mux):
            remote_call_machines.append(machine_name)
            return []

        api_call_machines = []

        async def fake_list_remote_api(machine_name, ip, dispatch_port):
            api_call_machines.append(machine_name)
            return []

        fleet_status = {
            "mac-mini": {"online": True},   # local machine — must be skipped
            "ubuntu-desktop": {"online": True},
        }

        with patch("src.tmux_manager.list_local_tmux", new=fake_list_local), \
             patch("src.tmux_manager.list_remote_tmux", new=fake_list_remote), \
             patch("src.tmux_manager.list_remote_tmux_via_api", new=fake_list_remote_api):
            await list_all_tmux("mac-mini", fleet_status)

        all_remote = remote_call_machines + api_call_machines
        assert "mac-mini" not in all_remote
        assert "ubuntu-desktop" in all_remote

    @pytest.mark.asyncio
    async def test_result_is_sorted_by_machine_then_name(self):
        from src.tmux_manager import list_all_tmux
        from src.tmux_manager import TmuxSession

        def _s(name, machine):
            return TmuxSession(name=name, machine=machine, created="", windows=0, attached=False, is_local=False)

        async def fake_list_local():
            return [_s("zzz", "mac-mini"), _s("aaa", "mac-mini")]

        async def fake_list_remote(machine_name, ssh_alias, mux):
            return [_s("mid", "ubuntu-desktop")]

        async def fake_list_remote_api(machine_name, ip, dispatch_port):
            return [_s("mid", "ubuntu-desktop")]

        fleet_status = {"ubuntu-desktop": {"online": True}}

        with patch("src.tmux_manager.list_local_tmux", new=fake_list_local), \
             patch("src.tmux_manager.list_remote_tmux", new=fake_list_remote), \
             patch("src.tmux_manager.list_remote_tmux_via_api", new=fake_list_remote_api):
            sessions = await list_all_tmux("mac-mini", fleet_status)

        assert sessions[0].name == "aaa"
        assert sessions[1].name == "zzz"
        assert sessions[2].name == "mid"

    @pytest.mark.asyncio
    async def test_exceptions_from_remote_are_silently_skipped(self):
        """If a gather task raises an exception, it should not propagate."""
        from src.tmux_manager import list_all_tmux
        from src.tmux_manager import TmuxSession

        async def fake_list_local():
            return [TmuxSession(name="local", machine="mac-mini", created="", windows=0, attached=False, is_local=True)]

        async def fake_list_remote(machine_name, ssh_alias, mux):
            raise OSError("unreachable")

        async def fake_list_remote_api(machine_name, ip, dispatch_port):
            raise OSError("unreachable")

        fleet_status = {"ubuntu-desktop": {"online": True}}

        with patch("src.tmux_manager.list_local_tmux", new=fake_list_local), \
             patch("src.tmux_manager.list_remote_tmux", new=fake_list_remote), \
             patch("src.tmux_manager.list_remote_tmux_via_api", new=fake_list_remote_api):
            sessions = await list_all_tmux("mac-mini", fleet_status)

        # Only the local session survives
        assert len(sessions) == 1
        assert sessions[0].name == "local"

    @pytest.mark.asyncio
    async def test_empty_fleet_status_skips_all_remotes(self):
        from src.tmux_manager import list_all_tmux

        async def fake_list_local():
            return []

        remote_calls = []

        async def fake_list_remote(machine_name, ssh_alias, mux):
            remote_calls.append(machine_name)
            return []

        with patch("src.tmux_manager.list_local_tmux", new=fake_list_local), \
             patch("src.tmux_manager.list_remote_tmux", new=fake_list_remote):
            sessions = await list_all_tmux("mac-mini", {})

        assert remote_calls == []


# ---------------------------------------------------------------------------
# create_tmux_session
# ---------------------------------------------------------------------------

class TestCreateTmuxSession:
    """create_tmux_session() — local and remote paths."""

    @pytest.mark.asyncio
    async def test_local_create_correct_command(self):
        from src.tmux_manager import create_tmux_session

        proc = _make_proc(0)
        with patch("src.tmux_manager.asyncio.create_subprocess_exec", return_value=proc) as mock_exec, \
             patch("src.tmux_manager.detect_local_machine", return_value="mac-mini"):
            result = await create_tmux_session("mac-mini", "my-session")

        assert result == {"ok": True}
        args = mock_exec.call_args[0]
        assert args[0] == "tmux"
        assert "new-session" in args
        assert "-d" in args
        assert "-s" in args
        assert "my-session" in args

    @pytest.mark.asyncio
    async def test_local_create_with_cwd(self):
        from src.tmux_manager import create_tmux_session

        proc = _make_proc(0)
        with patch("src.tmux_manager.asyncio.create_subprocess_exec", return_value=proc) as mock_exec, \
             patch("src.tmux_manager.detect_local_machine", return_value="mac-mini"):
            result = await create_tmux_session("mac-mini", "my-session", cwd="/home/user/project")

        assert result == {"ok": True}
        args = mock_exec.call_args[0]
        assert "-c" in args
        assert "/home/user/project" in args

    @pytest.mark.asyncio
    async def test_local_create_with_command(self):
        from src.tmux_manager import create_tmux_session

        proc = _make_proc(0)
        with patch("src.tmux_manager.asyncio.create_subprocess_exec", return_value=proc) as mock_exec, \
             patch("src.tmux_manager.detect_local_machine", return_value="mac-mini"):
            result = await create_tmux_session("mac-mini", "my-session", command="python server.py")

        assert result == {"ok": True}
        args = mock_exec.call_args[0]
        assert "python server.py" in args

    @pytest.mark.asyncio
    async def test_local_create_with_cwd_and_command(self):
        from src.tmux_manager import create_tmux_session

        proc = _make_proc(0)
        with patch("src.tmux_manager.asyncio.create_subprocess_exec", return_value=proc) as mock_exec, \
             patch("src.tmux_manager.detect_local_machine", return_value="mac-mini"):
            result = await create_tmux_session(
                "mac-mini", "my-session",
                cwd="/tmp/work",
                command="bash run.sh",
            )

        assert result == {"ok": True}
        args = mock_exec.call_args[0]
        assert "-c" in args
        assert "/tmp/work" in args
        assert "bash run.sh" in args

    @pytest.mark.asyncio
    async def test_local_create_failure_returns_error(self):
        from src.tmux_manager import create_tmux_session

        proc = _make_proc(1, stderr=b"session already exists")
        with patch("src.tmux_manager.asyncio.create_subprocess_exec", return_value=proc), \
             patch("src.tmux_manager.detect_local_machine", return_value="mac-mini"):
            result = await create_tmux_session("mac-mini", "existing-session")

        assert result["ok"] is False
        assert "session already exists" in result["error"]

    @pytest.mark.asyncio
    async def test_remote_create_uses_ssh(self):
        from src.tmux_manager import create_tmux_session
        from src.config import SSH_TIMEOUT

        proc = _make_proc(0)
        with patch("src.tmux_manager.asyncio.create_subprocess_exec", return_value=proc) as mock_exec, \
             patch("src.tmux_manager.detect_local_machine", return_value="mac-mini"):
            result = await create_tmux_session("ubuntu-desktop", "remote-sess")

        assert result.get("ok") is True
        first_call = mock_exec.call_args_list[0][0]
        assert first_call[0] == "ssh"
        assert f"ConnectTimeout={SSH_TIMEOUT}" in " ".join(str(a) for a in first_call)
        assert "ubuntu-desktop" in first_call

    @pytest.mark.asyncio
    async def test_remote_create_uses_psmux_for_windows(self):
        from src.tmux_manager import create_tmux_session

        proc = _make_proc(0)
        with patch("src.tmux_manager.asyncio.create_subprocess_exec", return_value=proc) as mock_exec, \
             patch("src.tmux_manager.detect_local_machine", return_value="mac-mini"):
            result = await create_tmux_session("avell-i7", "win-sess")

        assert result.get("ok") is True
        # New flow uses 2 SSH calls: create + send-keys. First call should use psmux.
        first_call_args = mock_exec.call_args_list[0][0]
        remote_cmd = first_call_args[-1]
        assert "psmux" in remote_cmd

    @pytest.mark.asyncio
    async def test_remote_create_with_command_uses_send_keys(self):
        from src.tmux_manager import create_tmux_session

        proc = _make_proc(0)
        with patch("src.tmux_manager.asyncio.create_subprocess_exec", return_value=proc) as mock_exec, \
             patch("src.tmux_manager.detect_local_machine", return_value="mac-mini"):
            result = await create_tmux_session("ubuntu-desktop", "sess", cwd="/remote/path", command="echo hello")

        # Should make 2 SSH calls: create session + send-keys with command
        assert mock_exec.call_count == 2
        second_call_args = mock_exec.call_args_list[1][0]
        remote_cmd = second_call_args[-1]
        assert "send-keys" in remote_cmd

    @pytest.mark.asyncio
    async def test_timeout_returns_error(self):
        from src.tmux_manager import create_tmux_session

        proc = AsyncMock()
        with patch("src.tmux_manager.asyncio.create_subprocess_exec", return_value=proc), \
             patch("src.tmux_manager.asyncio.wait_for", side_effect=asyncio.TimeoutError()), \
             patch("src.tmux_manager.detect_local_machine", return_value="mac-mini"):
            result = await create_tmux_session("mac-mini", "sess")

        assert result["ok"] is False
        assert "Timed out" in result["error"]

    @pytest.mark.asyncio
    async def test_oserror_returns_error(self):
        from src.tmux_manager import create_tmux_session

        with patch("src.tmux_manager.asyncio.create_subprocess_exec", side_effect=OSError("no such file")), \
             patch("src.tmux_manager.detect_local_machine", return_value="mac-mini"):
            result = await create_tmux_session("mac-mini", "sess")

        assert result["ok"] is False
        assert "no such file" in result["error"]


# ---------------------------------------------------------------------------
# kill_tmux_session
# ---------------------------------------------------------------------------

class TestKillTmuxSession:
    """kill_tmux_session() local and remote."""

    @pytest.mark.asyncio
    async def test_local_kill_uses_tmux(self):
        from src.tmux_manager import kill_tmux_session

        proc = _make_proc(0)
        with patch("src.tmux_manager.asyncio.create_subprocess_exec", return_value=proc) as mock_exec, \
             patch("src.tmux_manager.detect_local_machine", return_value="mac-mini"):
            result = await kill_tmux_session("mac-mini", "sess-to-kill")

        assert result == {"ok": True}
        args = mock_exec.call_args[0]
        assert args[0] == "tmux"
        assert "kill-session" in args
        assert "-t" in args
        assert "sess-to-kill" in args

    @pytest.mark.asyncio
    async def test_local_kill_failure_returns_error(self):
        from src.tmux_manager import kill_tmux_session

        proc = _make_proc(1, stderr=b"can't find session: sess-to-kill")
        with patch("src.tmux_manager.asyncio.create_subprocess_exec", return_value=proc), \
             patch("src.tmux_manager.detect_local_machine", return_value="mac-mini"):
            result = await kill_tmux_session("mac-mini", "sess-to-kill")

        assert result["ok"] is False
        assert "sess-to-kill" in result["error"]

    @pytest.mark.asyncio
    async def test_remote_kill_uses_ssh(self):
        from src.tmux_manager import kill_tmux_session
        from src.config import SSH_TIMEOUT

        proc = _make_proc(0)
        with patch("src.tmux_manager.asyncio.create_subprocess_exec", return_value=proc) as mock_exec, \
             patch("src.tmux_manager.detect_local_machine", return_value="mac-mini"):
            result = await kill_tmux_session("ubuntu-desktop", "remote-sess")

        assert result == {"ok": True}
        args = mock_exec.call_args[0]
        assert args[0] == "ssh"
        assert f"ConnectTimeout={SSH_TIMEOUT}" in " ".join(str(a) for a in args)
        assert "ubuntu-desktop" in args
        remote_cmd = args[-1]
        assert "kill-session" in remote_cmd
        assert "remote-sess" in remote_cmd

    @pytest.mark.asyncio
    async def test_remote_kill_uses_psmux_for_windows(self):
        from src.tmux_manager import kill_tmux_session

        proc = _make_proc(0)
        with patch("src.tmux_manager.asyncio.create_subprocess_exec", return_value=proc) as mock_exec, \
             patch("src.tmux_manager.detect_local_machine", return_value="mac-mini"):
            result = await kill_tmux_session("avell-i7", "win-sess")

        assert result == {"ok": True}
        args = mock_exec.call_args[0]
        remote_cmd = args[-1]
        assert "psmux" in remote_cmd

    @pytest.mark.asyncio
    async def test_kill_timeout_returns_error(self):
        from src.tmux_manager import kill_tmux_session

        with patch("src.tmux_manager.asyncio.create_subprocess_exec", return_value=AsyncMock()), \
             patch("src.tmux_manager.asyncio.wait_for", side_effect=asyncio.TimeoutError()), \
             patch("src.tmux_manager.detect_local_machine", return_value="mac-mini"):
            result = await kill_tmux_session("mac-mini", "sess")

        assert result["ok"] is False
        assert "Timed out" in result["error"]

    @pytest.mark.asyncio
    async def test_kill_oserror_returns_error(self):
        from src.tmux_manager import kill_tmux_session

        with patch("src.tmux_manager.asyncio.create_subprocess_exec", side_effect=OSError("tmux gone")), \
             patch("src.tmux_manager.detect_local_machine", return_value="mac-mini"):
            result = await kill_tmux_session("mac-mini", "sess")

        assert result["ok"] is False
        assert "tmux gone" in result["error"]


# ---------------------------------------------------------------------------
# capture_pane
# ---------------------------------------------------------------------------

class TestCapturePane:
    """Tests for capture_pane() — one-shot pane content capture."""

    @pytest.mark.asyncio
    async def test_local_tmux_capture(self):
        """Local tmux: verifies `tmux capture-pane -t SESSION -e -p -S -50` executed."""
        from src.tmux_manager import capture_pane

        proc = _make_proc(0, stdout=b"some output\n")
        with patch("src.tmux_manager.asyncio.create_subprocess_exec", return_value=proc) as mock_exec, \
             patch("src.tmux_manager.detect_local_machine", return_value="mac-mini"), \
             patch("src.tmux_manager.get_adapter") as mock_adapter:
            mock_adapter.return_value.mux_type = "tmux"
            result = await capture_pane("mac-mini", "my-session")

        args = mock_exec.call_args[0]
        args_str = " ".join(str(a) for a in args)
        assert "tmux" in args_str
        assert "capture-pane" in args_str
        assert "my-session" in args_str
        assert "-p" in args_str

    @pytest.mark.asyncio
    async def test_local_psmux_capture(self):
        """Local psmux: verifies `psmux capture-pane -t SESSION -p` (no -S flag)."""
        from src.tmux_manager import capture_pane

        proc = _make_proc(0, stdout=b"psmux output\n")
        with patch("src.tmux_manager.asyncio.create_subprocess_exec", return_value=proc) as mock_exec, \
             patch("src.tmux_manager.detect_local_machine", return_value="avell-i7"), \
             patch("src.tmux_manager.get_adapter") as mock_adapter:
            mock_adapter.return_value.mux_type = "psmux"
            result = await capture_pane("avell-i7", "win-session")

        args = mock_exec.call_args[0]
        args_str = " ".join(str(a) for a in args)
        assert "psmux" in args_str
        assert "capture-pane" in args_str
        assert "win-session" in args_str
        # psmux does not use -S flag
        assert "-S" not in args_str

    @pytest.mark.asyncio
    async def test_remote_tmux_capture(self):
        """Remote tmux: verifies SSH wrapping with ConnectTimeout."""
        from src.tmux_manager import capture_pane
        from src.config import SSH_TIMEOUT

        proc = _make_proc(0, stdout=b"remote content\n")
        with patch("src.tmux_manager.asyncio.create_subprocess_exec", return_value=proc) as mock_exec, \
             patch("src.tmux_manager.detect_local_machine", return_value="mac-mini"), \
             patch("src.tmux_manager.get_adapter") as mock_adapter:
            mock_adapter.return_value.mux_type = "tmux"
            result = await capture_pane("ubuntu-desktop", "remote-sess")

        args = mock_exec.call_args[0]
        args_str = " ".join(str(a) for a in args)
        assert args[0] == "ssh"
        assert f"ConnectTimeout={SSH_TIMEOUT}" in args_str
        assert "BatchMode=yes" in args_str
        assert "ubuntu-desktop" in args_str

    @pytest.mark.asyncio
    async def test_remote_psmux_capture(self):
        """Remote psmux: verifies SSH + psmux command."""
        from src.tmux_manager import capture_pane

        proc = _make_proc(0, stdout=b"remote psmux content\n")
        with patch("src.tmux_manager.asyncio.create_subprocess_exec", return_value=proc) as mock_exec, \
             patch("src.tmux_manager.detect_local_machine", return_value="mac-mini"), \
             patch("src.tmux_manager.get_adapter") as mock_adapter:
            mock_adapter.return_value.mux_type = "psmux"
            result = await capture_pane("avell-i7", "win-sess")

        args = mock_exec.call_args[0]
        args_str = " ".join(str(a) for a in args)
        assert args[0] == "ssh"
        assert "avell-i7" in args_str
        # The remote command (last arg) uses psmux
        remote_cmd = args[-1]
        assert "psmux" in remote_cmd

    @pytest.mark.asyncio
    async def test_custom_line_count(self):
        """Verify `-S -100` when lines=100 for tmux."""
        from src.tmux_manager import capture_pane

        proc = _make_proc(0, stdout=b"content\n")
        with patch("src.tmux_manager.asyncio.create_subprocess_exec", return_value=proc) as mock_exec, \
             patch("src.tmux_manager.detect_local_machine", return_value="mac-mini"), \
             patch("src.tmux_manager.get_adapter") as mock_adapter:
            mock_adapter.return_value.mux_type = "tmux"
            await capture_pane("mac-mini", "my-session", lines=100)

        args = mock_exec.call_args[0]
        args_str = " ".join(str(a) for a in args)
        assert "-S" in args_str
        assert "-100" in args_str

    @pytest.mark.asyncio
    async def test_returns_empty_on_oserror(self):
        """OSError → returns empty string."""
        from src.tmux_manager import capture_pane

        with patch("src.tmux_manager.asyncio.create_subprocess_exec", side_effect=OSError("no tmux")), \
             patch("src.tmux_manager.detect_local_machine", return_value="mac-mini"), \
             patch("src.tmux_manager.get_adapter") as mock_adapter:
            mock_adapter.return_value.mux_type = "tmux"
            result = await capture_pane("mac-mini", "sess")

        assert result == ""

    @pytest.mark.asyncio
    async def test_returns_empty_on_timeout(self):
        """asyncio.TimeoutError → returns empty string."""
        from src.tmux_manager import capture_pane

        proc = AsyncMock()
        proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError())
        with patch("src.tmux_manager.asyncio.create_subprocess_exec", return_value=proc), \
             patch("src.tmux_manager.asyncio.wait_for", side_effect=asyncio.TimeoutError()), \
             patch("src.tmux_manager.detect_local_machine", return_value="mac-mini"), \
             patch("src.tmux_manager.get_adapter") as mock_adapter:
            mock_adapter.return_value.mux_type = "tmux"
            result = await capture_pane("mac-mini", "sess")

        assert result == ""

    @pytest.mark.asyncio
    async def test_returns_decoded_utf8(self):
        """Verify stdout bytes are decoded as UTF-8."""
        from src.tmux_manager import capture_pane

        proc = _make_proc(0, stdout="héllo wörld\n".encode("utf-8"))
        with patch("src.tmux_manager.asyncio.create_subprocess_exec", return_value=proc), \
             patch("src.tmux_manager.detect_local_machine", return_value="mac-mini"), \
             patch("src.tmux_manager.get_adapter") as mock_adapter:
            mock_adapter.return_value.mux_type = "tmux"
            result = await capture_pane("mac-mini", "sess")

        assert "héllo" in result
        assert "wörld" in result

    @pytest.mark.asyncio
    async def test_strips_ansi_control_codes(self):
        """_clean_pane_output strips non-color ANSI control codes from output."""
        from src.tmux_manager import _clean_pane_output

        # Cursor movement (\x1b[2A), OSC title (\x1b]0;title\x07), charset switch (\x1b(B)
        # should all be stripped. SGR color (\x1b[32m) should be KEPT.
        raw = "\x1b[2Ahello\x1b]0;mytitle\x07 \x1b(Bworld\x1b[32m green text\x1b[0m"
        cleaned = _clean_pane_output(raw)
        assert "\x1b[2A" not in cleaned         # cursor up stripped
        assert "mytitle" not in cleaned          # OSC title stripped
        assert "\x1b(B" not in cleaned           # charset stripped
        assert "\x1b[32m" in cleaned             # SGR color KEPT
        assert "hello" in cleaned
        assert "world" in cleaned

    @pytest.mark.asyncio
    async def test_session_name_is_passed_as_arg(self):
        """Session name with spaces is passed as a separate arg (no shell splitting)."""
        from src.tmux_manager import capture_pane

        proc = _make_proc(0, stdout=b"output\n")
        with patch("src.tmux_manager.asyncio.create_subprocess_exec", return_value=proc) as mock_exec, \
             patch("src.tmux_manager.detect_local_machine", return_value="mac-mini"), \
             patch("src.tmux_manager.get_adapter") as mock_adapter:
            mock_adapter.return_value.mux_type = "tmux"
            await capture_pane("mac-mini", "my session with spaces")

        args = mock_exec.call_args[0]
        # Session name passed as individual arg, not shell-quoted in a string
        assert "my session with spaces" in args

    @pytest.mark.asyncio
    async def test_returns_empty_on_nonzero_returncode(self):
        """Non-zero returncode → returns empty string."""
        from src.tmux_manager import capture_pane

        proc = _make_proc(1, stderr=b"no such session")
        with patch("src.tmux_manager.asyncio.create_subprocess_exec", return_value=proc), \
             patch("src.tmux_manager.detect_local_machine", return_value="mac-mini"), \
             patch("src.tmux_manager.get_adapter") as mock_adapter:
            mock_adapter.return_value.mux_type = "tmux"
            result = await capture_pane("mac-mini", "missing-sess")

        assert result == ""


# ---------------------------------------------------------------------------
# start_pipe_pane
# ---------------------------------------------------------------------------

class TestStartPipePane:
    """Tests for start_pipe_pane() — local tmux pipe-pane."""

    @pytest.mark.asyncio
    async def test_starts_pipe_pane_returns_true(self):
        """returncode=0 → returns True."""
        from src.tmux_manager import start_pipe_pane

        proc = _make_proc(0)
        with patch("src.tmux_manager.asyncio.create_subprocess_exec", return_value=proc):
            result = await start_pipe_pane("my-session", "/tmp/output.log")

        assert result is True

    @pytest.mark.asyncio
    async def test_returns_false_on_failure(self):
        """returncode=1 → returns False."""
        from src.tmux_manager import start_pipe_pane

        proc = _make_proc(1, stderr=b"no session")
        with patch("src.tmux_manager.asyncio.create_subprocess_exec", return_value=proc):
            result = await start_pipe_pane("my-session", "/tmp/output.log")

        assert result is False

    @pytest.mark.asyncio
    async def test_correct_command_format(self):
        """Verifies `tmux pipe-pane -t SESSION 'cat >> /tmp/file'`."""
        from src.tmux_manager import start_pipe_pane

        proc = _make_proc(0)
        with patch("src.tmux_manager.asyncio.create_subprocess_exec", return_value=proc) as mock_exec:
            await start_pipe_pane("work-session", "/tmp/pane.log")

        args = mock_exec.call_args[0]
        assert args[0] == "tmux"
        assert args[1] == "pipe-pane"
        assert "-t" in args
        t_idx = list(args).index("-t")
        assert args[t_idx + 1] == "work-session"
        # Last arg should contain the cat command with the output path
        last_arg = args[-1]
        assert "cat" in last_arg
        assert "/tmp/pane.log" in last_arg

    @pytest.mark.asyncio
    async def test_returns_false_on_exception(self):
        """OSError → returns False."""
        from src.tmux_manager import start_pipe_pane

        with patch("src.tmux_manager.asyncio.create_subprocess_exec", side_effect=OSError("no tmux")):
            result = await start_pipe_pane("my-session", "/tmp/output.log")

        assert result is False


# ---------------------------------------------------------------------------
# stop_pipe_pane
# ---------------------------------------------------------------------------

class TestStopPipePane:
    """Tests for stop_pipe_pane() — stops local tmux pipe-pane."""

    @pytest.mark.asyncio
    async def test_stops_pipe_pane_returns_true(self):
        """returncode=0 → returns True."""
        from src.tmux_manager import stop_pipe_pane

        proc = _make_proc(0)
        with patch("src.tmux_manager.asyncio.create_subprocess_exec", return_value=proc):
            result = await stop_pipe_pane("my-session")

        assert result is True

    @pytest.mark.asyncio
    async def test_correct_command_format(self):
        """Verifies `tmux pipe-pane -t SESSION` with no extra command (empty = stop)."""
        from src.tmux_manager import stop_pipe_pane

        proc = _make_proc(0)
        with patch("src.tmux_manager.asyncio.create_subprocess_exec", return_value=proc) as mock_exec:
            await stop_pipe_pane("work-session")

        args = mock_exec.call_args[0]
        assert args[0] == "tmux"
        assert args[1] == "pipe-pane"
        assert "-t" in args
        t_idx = list(args).index("-t")
        assert args[t_idx + 1] == "work-session"
        # No additional command argument after session name (stops the pipe)
        # args should be exactly: tmux, pipe-pane, -t, SESSION
        assert len(args) == 4

    @pytest.mark.asyncio
    async def test_returns_false_on_failure(self):
        """returncode=1 → returns False."""
        from src.tmux_manager import stop_pipe_pane

        proc = _make_proc(1, stderr=b"no session found")
        with patch("src.tmux_manager.asyncio.create_subprocess_exec", return_value=proc):
            result = await stop_pipe_pane("my-session")

        assert result is False


# ---------------------------------------------------------------------------
# _clean_pane_output
# ---------------------------------------------------------------------------

class TestCleanPaneOutput:
    """Tests for _clean_pane_output() and ANSI stripping."""

    def test_strips_cursor_movement_codes(self):
        from src.tmux_manager import _clean_pane_output
        # Cursor up (\x1b[2A), cursor down (\x1b[3B), cursor forward (\x1b[4C)
        raw = "\x1b[2Atext\x1b[3B more\x1b[4C end"
        cleaned = _clean_pane_output(raw)
        assert "\x1b[" not in cleaned.replace("\x1b[32m", "").replace("\x1b[0m", "")
        assert "text" in cleaned
        assert "more" in cleaned

    def test_strips_osc_title_sequences(self):
        from src.tmux_manager import _clean_pane_output
        raw = "\x1b]0;Window Title\x07hello world"
        cleaned = _clean_pane_output(raw)
        assert "Window Title" not in cleaned
        assert "\x1b]" not in cleaned
        assert "hello world" in cleaned

    def test_strips_charset_switching(self):
        from src.tmux_manager import _clean_pane_output
        raw = "before\x1b(Bafter"
        cleaned = _clean_pane_output(raw)
        assert "\x1b(B" not in cleaned
        assert "before" in cleaned
        assert "after" in cleaned

    def test_preserves_sgr_color_codes(self):
        from src.tmux_manager import _clean_pane_output
        raw = "\x1b[32mgreen text\x1b[0m normal"
        cleaned = _clean_pane_output(raw)
        assert "\x1b[32m" in cleaned
        assert "\x1b[0m" in cleaned
        assert "green text" in cleaned

    def test_trims_trailing_blank_lines(self):
        from src.tmux_manager import _clean_pane_output
        raw = "line1\nline2\n\n\n"
        cleaned = _clean_pane_output(raw)
        assert not cleaned.endswith("\n")
        assert "line1" in cleaned
        assert "line2" in cleaned

    def test_empty_string_returns_empty(self):
        from src.tmux_manager import _clean_pane_output
        assert _clean_pane_output("") == ""

    def test_plain_text_unchanged(self):
        from src.tmux_manager import _clean_pane_output
        raw = "hello world\nno escape codes here"
        cleaned = _clean_pane_output(raw)
        assert "hello world" in cleaned
        assert "no escape codes here" in cleaned
