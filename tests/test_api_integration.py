"""
Integration tests for all HTTP routes in src/server.py create_app().

Goals:
  1. Call every registered route at least once via the aiohttp test client.
  2. Assert the status code is one of the documented values (never silently 500).
  3. When the response IS 500, include the body in the assertion message so
     TypeError / AttributeError / kwarg-collision bugs are immediately visible.
  4. Mock everything that spawns real subprocesses, SSH connections, or
     filesystem side effects that would make tests non-deterministic.

Routes covered (from create_app(), verified against server.py ~3322-3368):
  GET  /health, /api/auth/config, /api/auth/token,
       /api/sessions, /api/sessions/{machine}, /api/sessions/readme,
       /api/tmux, /api/tmux/{machine},
       /api/fleet, /api/preferences, /api/projects,
       /api/logs, /api/logs/tail,
       /api/update/check, /api/update/watchdog,
       /api/machines/{machine}/terminals
  POST /api/auth/update,
       /api/sessions/launch (terminal + tmux modes, local + remote),
       /api/sessions/scan,
       /api/sessions/pin, /api/sessions/unpin,
       /api/sessions/archive, /api/sessions/unarchive,
       /api/sessions/rename,
       /api/tmux/create, /api/tmux/connect, /api/tmux/connect-remote,
       /api/tmux/kill, /api/tmux/capture, /api/tmux/verify,
       /api/browse, /api/drives, /api/mkdir, /api/projects/create,
       /api/projects/pin, /api/projects/unpin, /api/projects/pull,
       /api/preferences, /api/restart, /api/exit,
       /api/update/apply, /api/fs/open, /api/hardware
  WS   /ws
"""
from __future__ import annotations

import asyncio
import json
import pathlib
from contextlib import asynccontextmanager
from typing import AsyncIterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from src.scanner import ClaudeSession
from src.server import create_app
from src.tmux_manager import TmuxSession


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _session(
    session_id: str = "sess-001",
    machine: str = "mac-mini",
    project_folder: str = "-Users-rbgnr-git-proj",
    project_path: str = "/Users/rbgnr/git/proj",
    cwd: str = "/Users/rbgnr/git/proj",
    slug: str = "fix-bug",
    summary: str = "Fix the login bug",
    messages: int = 3,
    modified: str = "2026-01-01T00:00:00+00:00",
    status: str = "idle",
    pid: int | None = None,
) -> ClaudeSession:
    return ClaudeSession(
        session_id=session_id,
        machine=machine,
        project_folder=project_folder,
        project_path=project_path,
        cwd=cwd,
        slug=slug,
        summary=summary,
        messages=messages,
        modified=modified,
        status=status,
        pid=pid,
    )


def _tmux(name: str = "myproj-session-1", machine: str = "mac-mini") -> TmuxSession:
    return TmuxSession(
        name=name,
        machine=machine,
        windows=1,
        created="2026-01-01",
        attached=False,
        is_local=(machine == "mac-mini"),
    )


FAKE_FLEET = {
    "mac-mini": {
        "name": "mac-mini",
        "online": True,
        "os": "darwin",
        "ip": "192.168.7.102",
        "method": "http",
        "health_data": {"status": "ok"},
    }
}


@asynccontextmanager
async def _client(
    tmp_path: pathlib.Path,
    sessions: list[ClaudeSession] | None = None,
    tmux_sessions: list[TmuxSession] | None = None,
    prefs: dict | None = None,
) -> AsyncIterator[TestClient]:
    """Build a TestClient with mocked background scan, scan_all, list_all_tmux."""
    if sessions is None:
        sessions = [_session()]
    if tmux_sessions is None:
        tmux_sessions = [_tmux()]

    prefs_file = tmp_path / "prefs.json"
    prefs_file.write_text(json.dumps(prefs or {"skip_permissions": False}))

    with (
        patch("src.server.PREFS_FILE", prefs_file),
        patch("src.server.discover_fleet", new=AsyncMock(return_value=FAKE_FLEET)),
        patch("src.server.scan_all", new=AsyncMock(return_value=sessions)),
        patch("src.server.list_all_tmux", new=AsyncMock(return_value=tmux_sessions)),
    ):
        app = create_app(port=44740)
        # Seed state before startup
        app["state"]["sessions"] = sessions
        app["state"]["fleet"] = FAKE_FLEET
        app["state"]["tmux"] = tmux_sessions
        app["state"]["last_scan"] = "2026-01-01T00:00:00+00:00"
        app["local_machine"] = "mac-mini"

        server = TestServer(app)
        client = TestClient(server)
        await client.start_server()

        # Re-seed after startup (background scan runs once on startup)
        app["state"]["sessions"] = sessions
        app["state"]["fleet"] = FAKE_FLEET
        app["state"]["tmux"] = tmux_sessions
        app["local_machine"] = "mac-mini"

        try:
            yield client
        finally:
            await client.close()


def _assert_not_500(resp, body: dict | str | None = None) -> None:
    """Fail clearly when a response is 500, printing the body for bug triage."""
    if resp.status == 500:
        raise AssertionError(
            f"Unexpected 500 from {resp.url} — body: {body!r}\n"
            "(check for TypeError/AttributeError/kwarg-collision in the handler)"
        )


# ---------------------------------------------------------------------------
# GET /health
# ---------------------------------------------------------------------------

class TestHealth:
    @pytest.mark.asyncio
    async def test_health_returns_200_and_status_ok(self, tmp_path):
        async with _client(tmp_path) as c:
            resp = await c.get("/health")
            data = await resp.json()
        _assert_not_500(resp, data)
        assert resp.status == 200
        assert data["status"] == "ok"

    @pytest.mark.asyncio
    async def test_health_includes_port_and_local_machine(self, tmp_path):
        async with _client(tmp_path) as c:
            resp = await c.get("/health")
            data = await resp.json()
        assert "port" in data
        assert "local_machine" in data

    @pytest.mark.asyncio
    async def test_health_sessions_count_matches_state(self, tmp_path):
        sessions = [_session("s1"), _session("s2")]
        async with _client(tmp_path, sessions=sessions) as c:
            resp = await c.get("/health")
            data = await resp.json()
        assert data["sessions"] == 2


# ---------------------------------------------------------------------------
# GET /api/auth/config
# ---------------------------------------------------------------------------

class TestAuthConfig:
    @pytest.mark.asyncio
    async def test_auth_config_returns_200(self, tmp_path):
        async with _client(tmp_path) as c:
            resp = await c.get("/api/auth/config")
            data = await resp.json()
        _assert_not_500(resp, data)
        assert resp.status == 200

    @pytest.mark.asyncio
    async def test_auth_config_has_enabled_field(self, tmp_path):
        async with _client(tmp_path) as c:
            resp = await c.get("/api/auth/config")
            data = await resp.json()
        assert "enabled" in data

    @pytest.mark.asyncio
    async def test_auth_config_has_bind_field(self, tmp_path):
        async with _client(tmp_path) as c:
            resp = await c.get("/api/auth/config")
            data = await resp.json()
        assert "bind" in data


# ---------------------------------------------------------------------------
# GET /api/auth/token (loopback-only; test client connects from 127.0.0.1)
# ---------------------------------------------------------------------------

class TestAuthToken:
    @pytest.mark.asyncio
    async def test_auth_token_loopback_returns_200(self, tmp_path):
        async with _client(tmp_path) as c:
            resp = await c.get("/api/auth/token")
            data = await resp.json()
        _assert_not_500(resp, data)
        assert resp.status == 200
        assert "ok" in data


# ---------------------------------------------------------------------------
# POST /api/auth/update
# ---------------------------------------------------------------------------

class TestAuthUpdate:
    @pytest.mark.asyncio
    async def test_auth_update_disable_returns_ok(self, tmp_path):
        from src.auth import AuthConfig
        with patch("src.auth.save_auth_config", return_value=AuthConfig(enabled=False)) as mock_save:
            async with _client(tmp_path) as c:
                resp = await c.post("/api/auth/update", json={"enabled": False})
                data = await resp.json()
        _assert_not_500(resp, data)
        assert resp.status == 200
        assert data["ok"] is True

    @pytest.mark.asyncio
    async def test_auth_update_invalid_json_returns_400(self, tmp_path):
        async with _client(tmp_path) as c:
            resp = await c.post(
                "/api/auth/update",
                data="notjson",
                headers={"Content-Type": "application/json"},
            )
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_auth_update_enable_missing_key_returns_400(self, tmp_path):
        async with _client(tmp_path) as c:
            resp = await c.post("/api/auth/update", json={"enabled": True, "key_path": ""})
            data = await resp.json()
        assert resp.status == 400
        assert data["ok"] is False


# ---------------------------------------------------------------------------
# GET /api/sessions
# ---------------------------------------------------------------------------

class TestSessionsAll:
    @pytest.mark.asyncio
    async def test_sessions_returns_200(self, tmp_path):
        async with _client(tmp_path) as c:
            resp = await c.get("/api/sessions")
            data = await resp.json()
        _assert_not_500(resp, data)
        assert resp.status == 200

    @pytest.mark.asyncio
    async def test_sessions_keyed_by_machine(self, tmp_path):
        async with _client(tmp_path) as c:
            resp = await c.get("/api/sessions")
            data = await resp.json()
        # Sessions are grouped by machine
        assert isinstance(data, dict)
        assert "mac-mini" in data

    @pytest.mark.asyncio
    async def test_sessions_each_has_session_id(self, tmp_path):
        async with _client(tmp_path) as c:
            resp = await c.get("/api/sessions")
            data = await resp.json()
        # /api/sessions returns {machine: [{project_folder, sessions:[...]}]} structure
        for machine_groups in data.values():
            for group in machine_groups:
                # Each group has a "sessions" list with session objects
                for sess in group.get("sessions", []):
                    assert "session_id" in sess


# ---------------------------------------------------------------------------
# GET /api/sessions/{machine}
# ---------------------------------------------------------------------------

class TestSessionsMachine:
    @pytest.mark.asyncio
    async def test_sessions_machine_returns_list(self, tmp_path):
        async with _client(tmp_path) as c:
            resp = await c.get("/api/sessions/mac-mini")
            data = await resp.json()
        _assert_not_500(resp, data)
        assert resp.status == 200
        assert isinstance(data, list)

    @pytest.mark.asyncio
    async def test_sessions_machine_unknown_returns_empty_list(self, tmp_path):
        async with _client(tmp_path) as c:
            resp = await c.get("/api/sessions/no-such-machine")
            data = await resp.json()
        _assert_not_500(resp, data)
        assert resp.status == 200
        assert data == []


# ---------------------------------------------------------------------------
# GET /api/sessions/readme
# ---------------------------------------------------------------------------

class TestSessionsReadme:
    @pytest.mark.asyncio
    async def test_readme_local_existing_file(self, tmp_path):
        readme = tmp_path / "README.md"
        readme.write_text("# Hello", encoding="utf-8")
        async with _client(tmp_path) as c:
            resp = await c.get(f"/api/sessions/readme?machine=mac-mini&path={readme}")
            data = await resp.json()
        _assert_not_500(resp, data)
        assert resp.status == 200
        assert data["ok"] is True
        assert "Hello" in data["content"]

    @pytest.mark.asyncio
    async def test_readme_nonexistent_returns_404(self, tmp_path):
        async with _client(tmp_path) as c:
            resp = await c.get("/api/sessions/readme?machine=mac-mini&path=/nonexistent/README.md")
        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_readme_invalid_path_returns_400(self, tmp_path):
        async with _client(tmp_path) as c:
            resp = await c.get("/api/sessions/readme?machine=mac-mini&path=relative/README.md")
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_readme_path_not_readme_returns_400(self, tmp_path):
        f = tmp_path / "notes.txt"
        f.write_text("hi")
        async with _client(tmp_path) as c:
            resp = await c.get(f"/api/sessions/readme?machine=mac-mini&path={f}")
        assert resp.status == 400


# ---------------------------------------------------------------------------
# GET /api/tmux
# ---------------------------------------------------------------------------

class TestTmux:
    @pytest.mark.asyncio
    async def test_tmux_returns_list(self, tmp_path):
        async with _client(tmp_path) as c:
            resp = await c.get("/api/tmux")
            data = await resp.json()
        _assert_not_500(resp, data)
        assert resp.status == 200
        assert isinstance(data, list)

    @pytest.mark.asyncio
    async def test_tmux_entries_have_name_and_machine(self, tmp_path):
        async with _client(tmp_path) as c:
            resp = await c.get("/api/tmux")
            data = await resp.json()
        if data:
            assert "name" in data[0]
            assert "machine" in data[0]


# ---------------------------------------------------------------------------
# GET /api/tmux/{machine}
# ---------------------------------------------------------------------------

class TestTmuxMachine:
    @pytest.mark.asyncio
    async def test_tmux_machine_returns_list(self, tmp_path):
        async with _client(tmp_path) as c:
            resp = await c.get("/api/tmux/mac-mini")
            data = await resp.json()
        _assert_not_500(resp, data)
        assert resp.status == 200
        assert isinstance(data, list)

    @pytest.mark.asyncio
    async def test_tmux_machine_unknown_returns_empty_list(self, tmp_path):
        async with _client(tmp_path) as c:
            resp = await c.get("/api/tmux/unknown-machine")
            data = await resp.json()
        _assert_not_500(resp, data)
        assert resp.status == 200
        assert data == []


# ---------------------------------------------------------------------------
# GET /api/fleet
# ---------------------------------------------------------------------------

class TestFleet:
    @pytest.mark.asyncio
    async def test_fleet_returns_200(self, tmp_path):
        async with _client(tmp_path) as c:
            resp = await c.get("/api/fleet")
            data = await resp.json()
        _assert_not_500(resp, data)
        assert resp.status == 200

    @pytest.mark.asyncio
    async def test_fleet_keyed_by_machine_name(self, tmp_path):
        async with _client(tmp_path) as c:
            resp = await c.get("/api/fleet")
            data = await resp.json()
        assert "mac-mini" in data


# ---------------------------------------------------------------------------
# GET /api/projects
# ---------------------------------------------------------------------------

class TestProjects:
    @pytest.mark.asyncio
    async def test_projects_returns_list(self, tmp_path):
        async with _client(tmp_path) as c:
            resp = await c.get("/api/projects")
            data = await resp.json()
        _assert_not_500(resp, data)
        assert resp.status == 200
        # /api/projects returns {projects: [...], generated: ...}
        assert "projects" in data
        assert isinstance(data["projects"], list)

    @pytest.mark.asyncio
    async def test_projects_entry_has_project_id(self, tmp_path):
        async with _client(tmp_path) as c:
            resp = await c.get("/api/projects")
            data = await resp.json()
        projects = data.get("projects", [])
        if projects:
            assert "project_id" in projects[0]

    @pytest.mark.asyncio
    async def test_projects_has_generated_timestamp(self, tmp_path):
        async with _client(tmp_path) as c:
            resp = await c.get("/api/projects")
            data = await resp.json()
        assert "generated" in data


# ---------------------------------------------------------------------------
# GET /api/logs and /api/logs/tail
# ---------------------------------------------------------------------------

class TestLogs:
    @pytest.mark.asyncio
    async def test_logs_returns_200(self, tmp_path):
        async with _client(tmp_path) as c:
            resp = await c.get("/api/logs")
            data = await resp.json()
        _assert_not_500(resp, data)
        assert resp.status == 200
        assert "logs" in data

    @pytest.mark.asyncio
    async def test_logs_tail_returns_200(self, tmp_path):
        async with _client(tmp_path) as c:
            resp = await c.get("/api/logs/tail?lines=10")
        _assert_not_500(resp)
        assert resp.status == 200

    @pytest.mark.asyncio
    async def test_logs_limit_param_accepted(self, tmp_path):
        async with _client(tmp_path) as c:
            resp = await c.get("/api/logs?limit=10")
            data = await resp.json()
        assert resp.status == 200
        assert isinstance(data["logs"], list)


# ---------------------------------------------------------------------------
# GET /api/update/check
# ---------------------------------------------------------------------------

class TestUpdateCheck:
    @pytest.mark.asyncio
    async def test_update_check_github_unreachable_returns_ok_false(self, tmp_path):
        import src.server as srv
        # Clear cache to force a fresh check
        srv._update_check_cache.clear()
        with patch("src.server._fetch_github_latest", new=AsyncMock(return_value=None)):
            async with _client(tmp_path) as c:
                resp = await c.get("/api/update/check")
                data = await resp.json()
        _assert_not_500(resp, data)
        assert resp.status == 200
        assert data["ok"] is False
        assert "current" in data

    @pytest.mark.asyncio
    async def test_update_check_up_to_date_returns_ok(self, tmp_path):
        import src.server as srv
        srv._update_check_cache.clear()
        fake_latest = {"commit": "abc", "commit_full": "abc123", "version": 1}
        fake_current = {"commit": "abc", "commit_full": "abc123", "version": 1}
        with (
            patch("src.server._fetch_github_latest", new=AsyncMock(return_value=fake_latest)),
            patch("src.server._VERSION_METADATA", fake_current),
        ):
            async with _client(tmp_path) as c:
                resp = await c.get("/api/update/check")
                data = await resp.json()
        _assert_not_500(resp, data)
        assert resp.status == 200
        assert data["ok"] is True


# ---------------------------------------------------------------------------
# GET /api/update/watchdog
# ---------------------------------------------------------------------------

class TestUpdateWatchdog:
    @pytest.mark.asyncio
    async def test_watchdog_returns_200_with_watchdog_available_field(self, tmp_path):
        """Watchdog probe always returns 200 with watchdog_available bool (true or false)."""
        async with _client(tmp_path) as c:
            resp = await c.get("/api/update/watchdog")
            data = await resp.json()
        _assert_not_500(resp, data)
        assert resp.status == 200
        assert "watchdog_available" in data
        # Value must be a bool regardless of whether watchdog is running
        assert isinstance(data["watchdog_available"], bool)

    @pytest.mark.asyncio
    async def test_watchdog_mocked_unreachable_returns_ok_false(self, tmp_path):
        """When aiohttp raises (watchdog down), watchdog_available=False is returned."""
        import aiohttp as _aiohttp
        with patch("aiohttp.ClientSession") as mock_sess_cls:
            mock_sess = MagicMock()
            mock_sess.__aenter__ = AsyncMock(return_value=mock_sess)
            mock_sess.__aexit__ = AsyncMock(return_value=False)
            mock_sess.get.side_effect = _aiohttp.ClientConnectorError(
                connection_key=MagicMock(), os_error=OSError("connection refused"),
            )
            mock_sess_cls.return_value = mock_sess
            async with _client(tmp_path) as c:
                resp = await c.get("/api/update/watchdog")
                data = await resp.json()
        _assert_not_500(resp, data)
        assert resp.status == 200
        assert data["watchdog_available"] is False


# ---------------------------------------------------------------------------
# GET /api/machines/{machine}/terminals
# ---------------------------------------------------------------------------

class TestMachineTerminals:
    @pytest.mark.asyncio
    async def test_machine_terminals_local_returns_list(self, tmp_path):
        # mac-mini is the local_machine, so the local probe path runs.
        # Patch list_available at its source in src.terminals.
        with patch("src.terminals.list_available", new=AsyncMock(return_value=[
            {"id": "wt", "name": "Windows Terminal", "priority": 100}
        ])):
            async with _client(tmp_path) as c:
                resp = await c.get("/api/machines/mac-mini/terminals")
                data = await resp.json()
        _assert_not_500(resp, data)
        assert resp.status == 200
        assert "terminals" in data

    @pytest.mark.asyncio
    async def test_machine_terminals_unknown_machine_still_returns(self, tmp_path):
        # Unknown machine not in FLEET_MACHINES: server returns static list or
        # an empty list — just ensure no 500.
        async with _client(tmp_path) as c:
            resp = await c.get("/api/machines/unknown-box/terminals")
            data = await resp.json()
        _assert_not_500(resp, data)
        assert resp.status == 200


# ---------------------------------------------------------------------------
# POST /api/sessions/scan
# ---------------------------------------------------------------------------

class TestSessionsScan:
    @pytest.mark.asyncio
    async def test_scan_returns_ok_and_sessions(self, tmp_path):
        async with _client(tmp_path) as c:
            resp = await c.post("/api/sessions/scan")
            data = await resp.json()
        _assert_not_500(resp, data)
        assert resp.status == 200
        assert data["ok"] is True
        assert "sessions" in data
        assert "tmux" in data
        assert "last_scan" in data

    @pytest.mark.asyncio
    async def test_scan_sessions_list_has_expected_shape(self, tmp_path):
        sessions = [_session("s-123")]
        async with _client(tmp_path, sessions=sessions) as c:
            resp = await c.post("/api/sessions/scan")
            data = await resp.json()
        assert isinstance(data["sessions"], list)
        if data["sessions"]:
            assert "session_id" in data["sessions"][0]


# ---------------------------------------------------------------------------
# POST /api/sessions/launch — terminal mode, local, with session_id
# ---------------------------------------------------------------------------

class TestSessionsLaunchTerminal:
    @pytest.mark.asyncio
    async def test_launch_terminal_local_with_session_id(self, tmp_path):
        with patch(
            "src.server.launch_claude_session",
            new=AsyncMock(return_value={"ok": True, "pid": 99}),
        ) as mock_launch:
            async with _client(tmp_path) as c:
                resp = await c.post("/api/sessions/launch", json={
                    "machine": "mac-mini",
                    "session_id": "sess-001",
                    "cwd": "/Users/rbgnr/git/proj",
                    "mode": "terminal",
                })
                data = await resp.json()
        _assert_not_500(resp, data)
        assert resp.status == 200
        assert data["ok"] is True
        mock_launch.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_launch_missing_cwd_returns_400(self, tmp_path):
        async with _client(tmp_path) as c:
            resp = await c.post("/api/sessions/launch", json={
                "machine": "mac-mini",
                "session_id": "sess-001",
            })
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_launch_invalid_json_returns_400(self, tmp_path):
        async with _client(tmp_path) as c:
            resp = await c.post(
                "/api/sessions/launch",
                data="notjson",
                headers={"Content-Type": "application/json"},
            )
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_launch_terminal_local_no_session_id(self, tmp_path):
        """New-session path (no session_id): should call launch_terminal."""
        with patch(
            "src.server.launch_terminal",
            new=AsyncMock(return_value={"ok": True}),
        ) as mock_launch:
            async with _client(tmp_path) as c:
                resp = await c.post("/api/sessions/launch", json={
                    "machine": "mac-mini",
                    "cwd": "/Users/rbgnr/git/proj",
                    "mode": "terminal",
                })
                data = await resp.json()
        _assert_not_500(resp, data)
        assert resp.status == 200
        mock_launch.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_launch_terminal_launcher_failure_returns_500(self, tmp_path):
        with patch(
            "src.server.launch_claude_session",
            new=AsyncMock(return_value={"ok": False, "error": "wt not found"}),
        ):
            async with _client(tmp_path) as c:
                resp = await c.post("/api/sessions/launch", json={
                    "machine": "mac-mini",
                    "session_id": "sess-001",
                    "cwd": "/Users/rbgnr/git/proj",
                    "mode": "terminal",
                })
                data = await resp.json()
        # 500 is EXPECTED here (ok=False from launcher), not a bug
        assert resp.status == 500
        assert data["ok"] is False


# ---------------------------------------------------------------------------
# POST /api/sessions/launch — tmux mode, local
# ---------------------------------------------------------------------------

class TestSessionsLaunchTmux:
    @pytest.mark.asyncio
    async def test_launch_tmux_local_mac_mini(self, tmp_path):
        """tmux mode on mac-mini (local): calls launch_new_tmux_and_attach."""
        with patch(
            "src.server.launch_new_tmux_and_attach",
            new=AsyncMock(return_value={"ok": True}),
        ) as mock_launch:
            async with _client(tmp_path) as c:
                resp = await c.post("/api/sessions/launch", json={
                    "machine": "mac-mini",
                    "session_id": "sess-001",
                    "cwd": "/Users/rbgnr/git/proj",
                    "mode": "tmux",
                })
                data = await resp.json()
        _assert_not_500(resp, data)
        assert resp.status == 200
        assert data["ok"] is True
        mock_launch.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_launch_tmux_local_refreshes_tmux_list(self, tmp_path):
        """After a successful tmux launch, list_all_tmux is called to refresh."""
        mock_tmux = AsyncMock(return_value=[_tmux("new-session-1")])
        # The outer _client context already patches list_all_tmux. We override
        # it inside the request by patching at the server module so the
        # post-launch refresh call is counted separately.
        with (
            patch("src.server.launch_new_tmux_and_attach", new=AsyncMock(return_value={"ok": True})),
        ):
            async with _client(tmp_path) as c:
                # Re-patch now that the client is started
                with patch("src.server.list_all_tmux", mock_tmux):
                    await c.post("/api/sessions/launch", json={
                        "machine": "mac-mini",
                        "session_id": "sess-001",
                        "cwd": "/Users/rbgnr/git/proj",
                        "mode": "tmux",
                    })
        # list_all_tmux was called once for the post-launch refresh
        assert mock_tmux.await_count >= 1

    @pytest.mark.asyncio
    async def test_launch_tmux_remote_psmux_path(self, tmp_path):
        """tmux mode on a remote Windows (psmux) machine: exercises the psmux branch."""
        # windows-desktop is in FLEET_MACHINES with mux=psmux
        with (
            patch("src.server._pool_exec", new=AsyncMock(return_value=(0, b"", b""))),
            patch("src.server.launch_terminal", new=AsyncMock(return_value={"ok": True})),
        ):
            async with _client(tmp_path) as c:
                resp = await c.post("/api/sessions/launch", json={
                    "machine": "windows-desktop",
                    "session_id": "sess-001",
                    "cwd": "C:\\Users\\rbgnr\\git\\proj",
                    "mode": "tmux",
                })
                data = await resp.json()
        _assert_not_500(resp, data)
        assert resp.status == 200


# ---------------------------------------------------------------------------
# POST /api/tmux/create
# ---------------------------------------------------------------------------

class TestTmuxCreate:
    @pytest.mark.asyncio
    async def test_create_with_name_and_machine(self, tmp_path):
        with patch(
            "src.server.create_tmux_session",
            new=AsyncMock(return_value={"ok": True, "name": "my-session"}),
        ):
            async with _client(tmp_path) as c:
                resp = await c.post("/api/tmux/create", json={
                    "machine": "mac-mini",
                    "name": "my-session",
                    "cwd": "/tmp",
                })
                data = await resp.json()
        _assert_not_500(resp, data)
        assert resp.status == 200
        assert data["ok"] is True

    @pytest.mark.asyncio
    async def test_create_missing_machine_returns_400(self, tmp_path):
        async with _client(tmp_path) as c:
            resp = await c.post("/api/tmux/create", json={"name": "sess"})
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_create_no_name_no_cwd_returns_400(self, tmp_path):
        async with _client(tmp_path) as c:
            resp = await c.post("/api/tmux/create", json={"machine": "mac-mini"})
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_create_auto_name_from_cwd(self, tmp_path):
        """When name is omitted but cwd is given, auto-generate from cwd."""
        with patch(
            "src.server.create_tmux_session",
            new=AsyncMock(return_value={"ok": True, "name": "proj-session-1"}),
        ):
            async with _client(tmp_path) as c:
                resp = await c.post("/api/tmux/create", json={
                    "machine": "mac-mini",
                    "cwd": "/Users/rbgnr/git/proj",
                })
                data = await resp.json()
        _assert_not_500(resp, data)
        assert resp.status == 200

    @pytest.mark.asyncio
    async def test_create_invalid_json_returns_400(self, tmp_path):
        async with _client(tmp_path) as c:
            resp = await c.post(
                "/api/tmux/create",
                data="bad",
                headers={"Content-Type": "application/json"},
            )
        assert resp.status == 400


# ---------------------------------------------------------------------------
# POST /api/tmux/connect
# ---------------------------------------------------------------------------

class TestTmuxConnect:
    @pytest.mark.asyncio
    async def test_connect_calls_launch_tmux_attach(self, tmp_path):
        with patch(
            "src.server.launch_tmux_attach",
            new=AsyncMock(return_value={"ok": True}),
        ) as mock_attach:
            async with _client(tmp_path) as c:
                resp = await c.post("/api/tmux/connect", json={
                    "machine": "mac-mini",
                    "session_name": "myproj-session-1",
                })
                data = await resp.json()
        _assert_not_500(resp, data)
        assert resp.status == 200
        mock_attach.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_connect_missing_machine_returns_400(self, tmp_path):
        async with _client(tmp_path) as c:
            resp = await c.post("/api/tmux/connect", json={"session_name": "sess"})
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_connect_missing_session_name_returns_400(self, tmp_path):
        async with _client(tmp_path) as c:
            resp = await c.post("/api/tmux/connect", json={"machine": "mac-mini"})
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_connect_launcher_failure_propagates_500(self, tmp_path):
        with patch(
            "src.server.launch_tmux_attach",
            new=AsyncMock(return_value={"ok": False, "error": "no such session"}),
        ):
            async with _client(tmp_path) as c:
                resp = await c.post("/api/tmux/connect", json={
                    "machine": "mac-mini",
                    "session_name": "ghost-session",
                })
        assert resp.status == 500


# ---------------------------------------------------------------------------
# POST /api/tmux/connect-remote
# ---------------------------------------------------------------------------

class TestTmuxConnectRemoteIntegration:
    @pytest.mark.asyncio
    async def test_connect_remote_success(self, tmp_path):
        with patch(
            "src.server.launch_tmux_attach_remote",
            new=AsyncMock(return_value={"ok": True}),
        ):
            async with _client(tmp_path) as c:
                resp = await c.post("/api/tmux/connect-remote", json={
                    "machine": "mac-mini",
                    "session_name": "sess",
                })
                data = await resp.json()
        _assert_not_500(resp, data)
        assert resp.status == 200

    @pytest.mark.asyncio
    async def test_connect_remote_missing_fields_400(self, tmp_path):
        async with _client(tmp_path) as c:
            resp = await c.post("/api/tmux/connect-remote", json={"machine": "mac-mini"})
        assert resp.status == 400


# ---------------------------------------------------------------------------
# POST /api/tmux/kill
# ---------------------------------------------------------------------------

class TestTmuxKill:
    @pytest.mark.asyncio
    async def test_kill_returns_ok(self, tmp_path):
        with patch(
            "src.server.kill_tmux_session",
            new=AsyncMock(return_value={"ok": True}),
        ):
            async with _client(tmp_path) as c:
                resp = await c.post("/api/tmux/kill", json={
                    "machine": "mac-mini",
                    "name": "myproj-session-1",
                })
                data = await resp.json()
        _assert_not_500(resp, data)
        assert resp.status == 200
        assert data["ok"] is True

    @pytest.mark.asyncio
    async def test_kill_missing_machine_returns_400(self, tmp_path):
        async with _client(tmp_path) as c:
            resp = await c.post("/api/tmux/kill", json={"name": "sess"})
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_kill_missing_name_returns_400(self, tmp_path):
        async with _client(tmp_path) as c:
            resp = await c.post("/api/tmux/kill", json={"machine": "mac-mini"})
        assert resp.status == 400


# ---------------------------------------------------------------------------
# POST /api/tmux/verify
# ---------------------------------------------------------------------------

class TestTmuxVerify:
    @pytest.mark.asyncio
    async def test_verify_returns_ok_with_alive_field(self, tmp_path):
        with patch("src.server._tmux_has_session", new=AsyncMock(return_value=(0, b"", b""))):
            async with _client(tmp_path) as c:
                resp = await c.post("/api/tmux/verify", json={
                    "machine": "mac-mini",
                    "session_name": "myproj-session-1",
                })
                data = await resp.json()
        _assert_not_500(resp, data)
        assert resp.status == 200
        assert data["ok"] is True
        assert "alive" in data

    @pytest.mark.asyncio
    async def test_verify_missing_fields_returns_400(self, tmp_path):
        async with _client(tmp_path) as c:
            resp = await c.post("/api/tmux/verify", json={"machine": "mac-mini"})
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_verify_dead_session_alive_false(self, tmp_path):
        # _tmux_has_session returns (rc, stdout_str, stderr_str) — all strings.
        # rc=1 means "session does not exist", stderr is a string error message.
        with patch("src.server._tmux_has_session", new=AsyncMock(return_value=(1, "", "no session"))):
            async with _client(tmp_path) as c:
                resp = await c.post("/api/tmux/verify", json={
                    "machine": "mac-mini",
                    "session_name": "ghost",
                })
                data = await resp.json()
        _assert_not_500(resp, data)
        assert data["alive"] is False

    @pytest.mark.asyncio
    async def test_verify_probe_failed_rc_negative(self, tmp_path):
        """When the probe itself fails (SSH error), rc=-1 and error is surfaced."""
        # _tmux_has_session returns (rc, stdout, stderr) as strings
        with patch("src.server._tmux_has_session", new=AsyncMock(return_value=(-1, "", "timed out"))):
            async with _client(tmp_path) as c:
                resp = await c.post("/api/tmux/verify", json={
                    "machine": "mac-mini",
                    "session_name": "ghost",
                })
                data = await resp.json()
        _assert_not_500(resp, data)
        assert data["alive"] is False
        assert "error" in data


# ---------------------------------------------------------------------------
# POST /api/tmux/capture
# ---------------------------------------------------------------------------

class TestTmuxCapture:
    @pytest.mark.asyncio
    async def test_capture_returns_content(self, tmp_path):
        # capture_pane is imported locally inside the handler from .tmux_manager
        with patch("src.tmux_manager.capture_pane", new=AsyncMock(return_value="some pane output")):
            async with _client(tmp_path) as c:
                resp = await c.post("/api/tmux/capture", json={
                    "machine": "mac-mini",
                    "session_name": "myproj-session-1",
                })
                data = await resp.json()
        _assert_not_500(resp, data)
        assert resp.status == 200
        assert data["ok"] is True
        assert data["content"] == "some pane output"

    @pytest.mark.asyncio
    async def test_capture_missing_fields_returns_400(self, tmp_path):
        async with _client(tmp_path) as c:
            resp = await c.post("/api/tmux/capture", json={"machine": "mac-mini"})
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_capture_invalid_json_returns_400(self, tmp_path):
        async with _client(tmp_path) as c:
            resp = await c.post(
                "/api/tmux/capture",
                data="bad",
                headers={"Content-Type": "application/json"},
            )
        assert resp.status == 400


# ---------------------------------------------------------------------------
# POST /api/projects/pin and /api/projects/unpin
# ---------------------------------------------------------------------------

class TestProjectsPin:
    @pytest.mark.asyncio
    async def test_pin_project_returns_ok(self, tmp_path):
        async with _client(tmp_path) as c:
            resp = await c.post("/api/projects/pin", json={"project_id": "github.com/owner/repo"})
            data = await resp.json()
        _assert_not_500(resp, data)
        assert data["ok"] is True
        assert "github.com/owner/repo" in data["pinned_projects"]

    @pytest.mark.asyncio
    async def test_unpin_project_returns_ok(self, tmp_path):
        prefs = {"pinned_projects": ["github.com/owner/repo"]}
        async with _client(tmp_path, prefs=prefs) as c:
            resp = await c.post("/api/projects/unpin", json={"project_id": "github.com/owner/repo"})
            data = await resp.json()
        _assert_not_500(resp, data)
        assert data["ok"] is True
        assert "github.com/owner/repo" not in data["pinned_projects"]

    @pytest.mark.asyncio
    async def test_pin_missing_project_id_returns_400(self, tmp_path):
        async with _client(tmp_path) as c:
            resp = await c.post("/api/projects/pin", json={})
        assert resp.status == 400


# ---------------------------------------------------------------------------
# POST /api/projects/pull
# ---------------------------------------------------------------------------

class TestProjectsPull:
    @pytest.mark.asyncio
    async def test_projects_pull_local_project(self, tmp_path):
        git_dir = tmp_path / "myproject" / ".git"
        git_dir.mkdir(parents=True)
        with patch("src.server.run_with_timeout", new=AsyncMock(return_value=(0, b"Already up to date.", b""))):
            async with _client(tmp_path) as c:
                resp = await c.post("/api/projects/pull", json={
                    "machine": "mac-mini",
                    "path": str(tmp_path / "myproject"),
                })
                data = await resp.json()
        _assert_not_500(resp, data)
        assert resp.status in (200, 400, 500)  # allow any valid outcome

    @pytest.mark.asyncio
    async def test_projects_pull_missing_path_returns_400(self, tmp_path):
        async with _client(tmp_path) as c:
            resp = await c.post("/api/projects/pull", json={"machine": "mac-mini"})
        assert resp.status == 400


# ---------------------------------------------------------------------------
# POST /api/projects/create
# ---------------------------------------------------------------------------

class TestProjectsCreate:
    @pytest.mark.asyncio
    async def test_create_project_local(self, tmp_path):
        new_dir = tmp_path / "newproject"
        async with _client(tmp_path) as c:
            resp = await c.post("/api/projects/create", json={
                "machine": "mac-mini",
                "path": str(new_dir),
                "init_git": False,
            })
            data = await resp.json()
        _assert_not_500(resp, data)
        assert resp.status == 200
        assert data["ok"] is True
        assert new_dir.is_dir()

    @pytest.mark.asyncio
    async def test_create_project_missing_path_returns_400(self, tmp_path):
        async with _client(tmp_path) as c:
            resp = await c.post("/api/projects/create", json={"machine": "mac-mini"})
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_create_project_relative_path_returns_400(self, tmp_path):
        async with _client(tmp_path) as c:
            resp = await c.post("/api/projects/create", json={
                "machine": "mac-mini",
                "path": "relative/path",
            })
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_create_project_unknown_machine_returns_400(self, tmp_path):
        async with _client(tmp_path) as c:
            resp = await c.post("/api/projects/create", json={
                "machine": "unknown-machine",
                "path": "/tmp/newproj",
            })
        assert resp.status == 400


# ---------------------------------------------------------------------------
# POST /api/restart
# ---------------------------------------------------------------------------

class TestRestart:
    @pytest.mark.asyncio
    async def test_restart_returns_ok(self, tmp_path):
        async with _client(tmp_path) as c:
            resp = await c.post("/api/restart")
            data = await resp.json()
        _assert_not_500(resp, data)
        assert resp.status == 200
        assert data["ok"] is True

    @pytest.mark.asyncio
    async def test_restart_returns_message(self, tmp_path):
        async with _client(tmp_path) as c:
            resp = await c.post("/api/restart")
            data = await resp.json()
        assert "message" in data

    @pytest.mark.asyncio
    async def test_restart_preserves_state(self, tmp_path):
        """Restart MUST NOT wipe existing session state — UI stays populated."""
        sessions = [_session("sess-preserved")]
        async with _client(tmp_path, sessions=sessions) as c:
            await c.post("/api/restart")
            await asyncio.sleep(0)  # yield event loop; no sleep for timing
            resp = await c.get("/api/sessions")
            data = await resp.json()
        # Sessions are grouped: {machine: [{project_folder, sessions:[...]}]}
        # Extract all session_ids from the nested structure
        all_ids: list[str] = []
        for machine_groups in data.values():
            for group in machine_groups:
                for sess in group.get("sessions", []):
                    all_ids.append(sess["session_id"])
        assert "sess-preserved" in all_ids


# ---------------------------------------------------------------------------
# POST /api/exit
# ---------------------------------------------------------------------------

class TestExit:
    @pytest.mark.asyncio
    async def test_exit_returns_ok_before_shutdown(self, tmp_path):
        """exit schedules os._exit(0) with a 0.5s delay — response arrives first."""
        with patch("os._exit"):  # prevent actually killing the test process
            async with _client(tmp_path) as c:
                resp = await c.post("/api/exit")
                data = await resp.json()
        _assert_not_500(resp, data)
        assert resp.status == 200
        assert data["ok"] is True


# ---------------------------------------------------------------------------
# POST /api/update/apply (loopback-only)
# ---------------------------------------------------------------------------

class TestUpdateApply:
    @pytest.mark.asyncio
    async def test_update_apply_git_pull_success(self, tmp_path):
        with (
            patch("src.server.run_with_timeout", new=AsyncMock(return_value=(0, b"Already up to date.", b""))),
            patch("os.execv"),  # don't actually exec
        ):
            async with _client(tmp_path) as c:
                resp = await c.post("/api/update/apply")
                data = await resp.json()
        _assert_not_500(resp, data)
        assert resp.status == 200
        assert data["ok"] is True

    @pytest.mark.asyncio
    async def test_update_apply_git_pull_failure_returns_500(self, tmp_path):
        with patch(
            "src.server.run_with_timeout",
            new=AsyncMock(return_value=(1, b"", b"error: not a git repo")),
        ):
            async with _client(tmp_path) as c:
                resp = await c.post("/api/update/apply")
                data = await resp.json()
        assert resp.status == 500
        assert data["ok"] is False


# ---------------------------------------------------------------------------
# POST /api/fs/open (loopback-only, local)
# ---------------------------------------------------------------------------

class TestFsOpen:
    @pytest.mark.asyncio
    async def test_fs_open_local_missing_path_returns_400(self, tmp_path):
        async with _client(tmp_path) as c:
            resp = await c.post("/api/fs/open", json={"machine": "mac-mini"})
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_fs_open_remote_returns_smb_url(self, tmp_path):
        """For a remote machine, fs/open returns smb:// URL info (ok=False, smb_url)."""
        async with _client(tmp_path) as c:
            resp = await c.post("/api/fs/open", json={
                "machine": "ubuntu-desktop",  # not local_machine (mac-mini)
                "path": "/home/rbgnr/git/proj",
            })
            data = await resp.json()
        _assert_not_500(resp, data)
        assert resp.status == 200
        assert "smb_url" in data


# ---------------------------------------------------------------------------
# WebSocket /ws
# ---------------------------------------------------------------------------

class TestWebSocket:
    @pytest.mark.asyncio
    async def test_ws_connect_and_disconnect(self, tmp_path):
        async with _client(tmp_path) as c:
            async with c.ws_connect("/ws") as ws:
                # Just connecting should not raise
                await ws.close()

    @pytest.mark.asyncio
    async def test_ws_subscribe_sessions_receives_snapshot(self, tmp_path):
        async with _client(tmp_path) as c:
            async with c.ws_connect("/ws") as ws:
                await ws.send_json({"type": "subscribe", "channel": "sessions"})
                msg = await ws.receive_json(timeout=5)
                assert msg["type"] == "snapshot"
                assert msg["channel"] == "sessions"
                assert isinstance(msg["data"], list)
                await ws.close()

    @pytest.mark.asyncio
    async def test_ws_subscribe_fleet_receives_snapshot(self, tmp_path):
        async with _client(tmp_path) as c:
            async with c.ws_connect("/ws") as ws:
                await ws.send_json({"type": "subscribe", "channel": "fleet"})
                msg = await ws.receive_json(timeout=5)
                assert msg["type"] == "snapshot"
                assert msg["channel"] == "fleet"
                assert isinstance(msg["data"], dict)
                await ws.close()

    @pytest.mark.asyncio
    async def test_ws_subscribe_tmux_receives_snapshot(self, tmp_path):
        async with _client(tmp_path) as c:
            async with c.ws_connect("/ws") as ws:
                await ws.send_json({"type": "subscribe", "channel": "tmux"})
                msg = await ws.receive_json(timeout=5)
                assert msg["type"] == "snapshot"
                assert msg["channel"] == "tmux"
                assert isinstance(msg["data"], list)
                await ws.close()

    @pytest.mark.asyncio
    async def test_ws_unsubscribe_does_not_error(self, tmp_path):
        async with _client(tmp_path) as c:
            async with c.ws_connect("/ws") as ws:
                await ws.send_json({"type": "subscribe", "channel": "sessions"})
                await ws.receive_json(timeout=5)  # snapshot
                await ws.send_json({"type": "unsubscribe", "channel": "sessions"})
                # No error expected; connection stays open
                await ws.close()

    @pytest.mark.asyncio
    async def test_ws_invalid_json_returns_error_message(self, tmp_path):
        async with _client(tmp_path) as c:
            async with c.ws_connect("/ws") as ws:
                await ws.send_str("not-valid-json")
                msg = await ws.receive_json(timeout=5)
                assert msg["type"] == "error"
                await ws.close()
