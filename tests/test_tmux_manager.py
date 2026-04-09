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

class TestUnixToIso:
    def _fn(self):
        from src.tmux_manager import _unix_to_iso
        return _unix_to_iso

    def test_converts_known_timestamp(self):
        fn = self._fn()
        # epoch 0 → 1970-01-01T00:00:00+00:00
        result = fn("0")
        assert result == "1970-01-01T00:00:00+00:00"

    def test_converts_recent_timestamp(self):
        fn = self._fn()
        # 2024-01-15 10:30:00 UTC → 1705314600
        ts = int(datetime(2024, 1, 15, 10, 30, 0, tzinfo=timezone.utc).timestamp())
        result = fn(str(ts))
        assert "2024-01-15" in result
        assert "+00:00" in result

    def test_returns_original_on_invalid_input(self):
        fn = self._fn()
        assert fn("not-a-number") == "not-a-number"
        assert fn("") == ""
        assert fn("abc123") == "abc123"

    def test_timezone_is_utc(self):
        fn = self._fn()
        result = fn("1000000")
        assert "+00:00" in result


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
        assert f"-o ConnectTimeout={SSH_TIMEOUT}" in args
        assert "-o BatchMode=yes" in args
        assert "-o StrictHostKeyChecking=no" in args
        assert "ubuntu-desktop" in args
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

        with patch("src.tmux_manager.list_local_tmux", new=fake_list_local), \
             patch("src.tmux_manager.list_remote_tmux", new=fake_list_remote):
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

        fleet_status = {
            "mac-mini": {"online": True},   # local machine — must be skipped
            "ubuntu-desktop": {"online": True},
        }

        with patch("src.tmux_manager.list_local_tmux", new=fake_list_local), \
             patch("src.tmux_manager.list_remote_tmux", new=fake_list_remote):
            await list_all_tmux("mac-mini", fleet_status)

        assert "mac-mini" not in remote_call_machines
        assert "ubuntu-desktop" in remote_call_machines

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

        fleet_status = {"ubuntu-desktop": {"online": True}}

        with patch("src.tmux_manager.list_local_tmux", new=fake_list_local), \
             patch("src.tmux_manager.list_remote_tmux", new=fake_list_remote):
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

        fleet_status = {"ubuntu-desktop": {"online": True}}

        with patch("src.tmux_manager.list_local_tmux", new=fake_list_local), \
             patch("src.tmux_manager.list_remote_tmux", new=fake_list_remote):
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

        assert result == {"ok": True}
        args = mock_exec.call_args[0]
        assert args[0] == "ssh"
        assert f"-o ConnectTimeout={SSH_TIMEOUT}" in args
        assert "ubuntu-desktop" in args

    @pytest.mark.asyncio
    async def test_remote_create_uses_psmux_for_windows(self):
        from src.tmux_manager import create_tmux_session

        proc = _make_proc(0)
        with patch("src.tmux_manager.asyncio.create_subprocess_exec", return_value=proc) as mock_exec, \
             patch("src.tmux_manager.detect_local_machine", return_value="mac-mini"):
            result = await create_tmux_session("avell-i7", "win-sess")

        assert result == {"ok": True}
        args = mock_exec.call_args[0]
        # The last arg is the remote command string — should use psmux
        remote_cmd = args[-1]
        assert "psmux" in remote_cmd

    @pytest.mark.asyncio
    async def test_remote_create_with_cwd_in_remote_cmd(self):
        from src.tmux_manager import create_tmux_session

        proc = _make_proc(0)
        with patch("src.tmux_manager.asyncio.create_subprocess_exec", return_value=proc) as mock_exec, \
             patch("src.tmux_manager.detect_local_machine", return_value="mac-mini"):
            result = await create_tmux_session("ubuntu-desktop", "sess", cwd="/remote/path")

        args = mock_exec.call_args[0]
        remote_cmd = args[-1]
        assert "/remote/path" in remote_cmd

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
        assert f"-o ConnectTimeout={SSH_TIMEOUT}" in args
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
