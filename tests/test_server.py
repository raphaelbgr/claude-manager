"""
Comprehensive unit tests for src/server.py.

Covers:
  - All REST endpoints (happy path + error cases)
  - CORS middleware (OPTIONS pre-flight + header propagation)
  - WebSocket handler (subscribe/unsubscribe/snapshot/invalid JSON)
  - Helper functions (_sessions_by_machine, _now_iso)
  - Application factory (create_app routes, middleware)
  - Background scan task cancellation on cleanup

External dependencies (fleet, scanner, launcher, tmux) are mocked at the
src.server module level so no real SSH/HTTP calls are made.

Uses aiohttp.test_utils.TestClient + TestServer (built-in, no pytest-aiohttp
plugin needed). All async test/fixture methods are driven by pytest-asyncio
in AUTO mode.
"""
from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, AsyncIterator
from unittest.mock import AsyncMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from src.scanner import ClaudeSession
from src.server import (
    _now_iso,
    _sessions_by_machine,
    create_app,
    cors_middleware,
)
from src.tmux_manager import TmuxSession


# ---------------------------------------------------------------------------
# Fake data factories
# ---------------------------------------------------------------------------

def make_claude_session(
    session_id: str = "sess-001",
    machine: str = "mac-mini",
    project_folder: str = "-Users-rbgnr-git-myproject",
    project_path: str = "/Users/rbgnr/git/myproject",
    cwd: str = "/Users/rbgnr/git/myproject",
    slug: str = "fix-bug",
    summary: str = "Fix the login bug",
    messages: int = 5,
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


def make_tmux_session(
    name: str = "work",
    machine: str = "mac-mini",
    created: str = "2026-01-01T00:00:00+00:00",
    windows: int = 2,
    attached: bool = False,
    is_local: bool = True,
) -> TmuxSession:
    return TmuxSession(
        name=name,
        machine=machine,
        created=created,
        windows=windows,
        attached=attached,
        is_local=is_local,
    )


FAKE_FLEET: dict[str, Any] = {
    "mac-mini": {
        "name": "mac-mini",
        "online": True,
        "os": "darwin",
        "ip": "192.168.7.102",
        "method": "http",
        "health_data": {"status": "ok", "jobs": 0},
    },
    "ubuntu-desktop": {
        "name": "ubuntu-desktop",
        "online": False,
        "os": "linux",
        "ip": "192.168.7.13",
        "method": "unreachable",
        "health_data": None,
    },
}

FAKE_SESSIONS: list[ClaudeSession] = [
    make_claude_session(
        session_id="sess-001",
        machine="mac-mini",
        project_folder="-Users-rbgnr-git-myproject",
        project_path="/Users/rbgnr/git/myproject",
        slug="fix-bug",
    ),
    make_claude_session(
        session_id="sess-002",
        machine="mac-mini",
        project_folder="-Users-rbgnr-git-other",
        project_path="/Users/rbgnr/git/other",
        slug="add-feature",
    ),
    make_claude_session(
        session_id="sess-003",
        machine="ubuntu-desktop",
        project_folder="-home-rbgnr-git-server",
        project_path="/home/rbgnr/git/server",
        slug="deploy",
    ),
]

FAKE_TMUX: list[TmuxSession] = [
    make_tmux_session(name="work", machine="mac-mini", is_local=True),
    make_tmux_session(name="remote-work", machine="ubuntu-desktop", is_local=False),
]


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def fake_sessions() -> list[ClaudeSession]:
    return list(FAKE_SESSIONS)


@pytest.fixture
def fake_fleet() -> dict:
    return dict(FAKE_FLEET)


@pytest.fixture
def fake_tmux() -> list[TmuxSession]:
    return list(FAKE_TMUX)


@pytest.fixture
def fake_state(fake_sessions, fake_fleet, fake_tmux) -> dict:
    return {
        "sessions": fake_sessions,
        "fleet": fake_fleet,
        "tmux": fake_tmux,
        "last_scan": "2026-01-01T00:00:00+00:00",
        "ws_clients": set(),
    }


# ---------------------------------------------------------------------------
# App + client builder helpers
# ---------------------------------------------------------------------------

def _build_app(state: dict | None = None) -> web.Application:
    """
    Build the app with all external I/O mocked.
    Replaces on_startup to avoid running the real background scan,
    and on_cleanup to avoid errors when ws_clients is empty.
    """
    with patch("src.server.detect_local_machine", return_value="mac-mini"):
        app = create_app(port=44740)

    injected_state = state or {
        "sessions": [],
        "fleet": {},
        "tmux": [],
        "last_scan": None,
        "ws_clients": set(),
    }

    async def _noop_startup(a: web.Application) -> None:
        a["state"].update(injected_state)
        a["state"]["ws_clients"] = set()

    async def _noop_cleanup(a: web.Application) -> None:
        for ws in list(a["state"]["ws_clients"]):
            try:
                await ws.close()
            except Exception:
                pass

    app.on_startup.clear()
    app.on_startup.append(_noop_startup)
    app.on_cleanup.clear()
    app.on_cleanup.append(_noop_cleanup)

    return app


@asynccontextmanager
async def make_client(state: dict | None = None) -> AsyncIterator[TestClient]:
    """Context manager that yields a started TestClient and tears it down."""
    app = _build_app(state)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        yield client
    finally:
        await client.close()


# ---------------------------------------------------------------------------
# Convenience: pytest fixture that wraps make_client with default fake state
# ---------------------------------------------------------------------------

@pytest.fixture
async def cli(fake_state):
    """TestClient pre-loaded with FAKE_* state."""
    async with make_client(fake_state) as client:
        yield client


# ---------------------------------------------------------------------------
# Helper function tests
# ---------------------------------------------------------------------------

class TestNowIso:
    def test_returns_string(self):
        result = _now_iso()
        assert isinstance(result, str)

    def test_parseable_as_datetime(self):
        result = _now_iso()
        dt = datetime.fromisoformat(result)
        assert dt.tzinfo is not None

    def test_is_utc(self):
        result = _now_iso()
        dt = datetime.fromisoformat(result)
        assert dt.utcoffset().total_seconds() == 0  # type: ignore[union-attr]

    def test_recent_timestamp(self):
        before = datetime.now(tz=timezone.utc)
        result = _now_iso()
        after = datetime.now(tz=timezone.utc)
        dt = datetime.fromisoformat(result)
        assert before <= dt <= after


class TestSessionsByMachine:
    def test_empty_input(self):
        assert _sessions_by_machine([]) == {}

    def test_single_session(self):
        sess = make_claude_session(machine="mac-mini", project_folder="-Users-rbgnr-git-foo")
        result = _sessions_by_machine([sess])
        assert "mac-mini" in result
        groups = result["mac-mini"]
        assert len(groups) == 1
        assert groups[0]["project_folder"] == "-Users-rbgnr-git-foo"
        assert len(groups[0]["sessions"]) == 1
        assert groups[0]["sessions"][0]["session_id"] == sess.session_id

    def test_groups_by_machine(self):
        sessions = [
            make_claude_session(session_id="a", machine="mac-mini", project_folder="-Users-foo"),
            make_claude_session(session_id="b", machine="ubuntu-desktop", project_folder="-home-foo"),
        ]
        result = _sessions_by_machine(sessions)
        assert set(result.keys()) == {"mac-mini", "ubuntu-desktop"}
        assert len(result["mac-mini"]) == 1
        assert len(result["ubuntu-desktop"]) == 1

    def test_groups_by_project_folder_within_machine(self):
        sessions = [
            make_claude_session(session_id="a", machine="mac-mini", project_folder="-Users-foo"),
            make_claude_session(session_id="b", machine="mac-mini", project_folder="-Users-foo"),
            make_claude_session(session_id="c", machine="mac-mini", project_folder="-Users-bar"),
        ]
        result = _sessions_by_machine(sessions)
        groups = result["mac-mini"]
        assert len(groups) == 2
        folder_map = {g["project_folder"]: g["sessions"] for g in groups}
        assert len(folder_map["-Users-foo"]) == 2
        assert len(folder_map["-Users-bar"]) == 1

    def test_session_dict_has_expected_keys(self):
        sess = make_claude_session()
        result = _sessions_by_machine([sess])
        session_dict = result["mac-mini"][0]["sessions"][0]
        expected_keys = {
            "session_id", "machine", "project_folder", "project_path",
            "cwd", "slug", "summary", "messages", "modified", "status", "pid",
        }
        assert expected_keys.issubset(session_dict.keys())


# ---------------------------------------------------------------------------
# REST endpoint tests — GET /health
# ---------------------------------------------------------------------------

class TestHealthEndpoint:
    async def test_returns_200(self, cli):
        resp = await cli.get("/health")
        assert resp.status == 200

    async def test_json_structure(self, cli):
        resp = await cli.get("/health")
        data = await resp.json()
        assert data["status"] == "ok"
        assert "port" in data
        assert "machines" in data
        assert "sessions" in data
        assert "last_scan" in data

    async def test_machines_count(self, cli, fake_fleet):
        resp = await cli.get("/health")
        data = await resp.json()
        assert data["machines"] == len(fake_fleet)

    async def test_sessions_count(self, cli, fake_sessions):
        resp = await cli.get("/health")
        data = await resp.json()
        assert data["sessions"] == len(fake_sessions)

    async def test_last_scan_value(self, cli):
        resp = await cli.get("/health")
        data = await resp.json()
        assert data["last_scan"] == "2026-01-01T00:00:00+00:00"

    async def test_port_matches_factory(self, cli):
        resp = await cli.get("/health")
        data = await resp.json()
        assert data["port"] == 44740


# ---------------------------------------------------------------------------
# GET /api/sessions
# ---------------------------------------------------------------------------

class TestSessionsAllEndpoint:
    async def test_returns_200(self, cli):
        resp = await cli.get("/api/sessions")
        assert resp.status == 200

    async def test_groups_by_machine(self, cli):
        resp = await cli.get("/api/sessions")
        data = await resp.json()
        assert "mac-mini" in data
        assert "ubuntu-desktop" in data

    async def test_structure_has_project_folder_and_sessions(self, cli):
        resp = await cli.get("/api/sessions")
        data = await resp.json()
        groups = data["mac-mini"]
        assert isinstance(groups, list)
        assert all("project_folder" in g for g in groups)
        assert all("sessions" in g for g in groups)

    async def test_mac_mini_has_two_projects(self, cli):
        resp = await cli.get("/api/sessions")
        data = await resp.json()
        assert len(data["mac-mini"]) == 2

    async def test_ubuntu_desktop_has_one_project(self, cli):
        resp = await cli.get("/api/sessions")
        data = await resp.json()
        assert len(data["ubuntu-desktop"]) == 1

    async def test_empty_sessions_returns_empty_dict(self):
        empty_state = {
            "sessions": [],
            "fleet": {},
            "tmux": [],
            "last_scan": None,
            "ws_clients": set(),
        }
        async with make_client(empty_state) as cli:
            resp = await cli.get("/api/sessions")
            data = await resp.json()
            assert data == {}


# ---------------------------------------------------------------------------
# GET /api/sessions/{machine}
# ---------------------------------------------------------------------------

class TestSessionsMachineEndpoint:
    async def test_known_machine_returns_its_sessions(self, cli):
        resp = await cli.get("/api/sessions/mac-mini")
        assert resp.status == 200
        data = await resp.json()
        assert isinstance(data, list)
        assert len(data) == 2  # two projects on mac-mini

    async def test_other_known_machine(self, cli):
        resp = await cli.get("/api/sessions/ubuntu-desktop")
        assert resp.status == 200
        data = await resp.json()
        assert len(data) == 1
        assert data[0]["project_folder"] == "-home-rbgnr-git-server"

    async def test_unknown_machine_returns_empty_list(self, cli):
        resp = await cli.get("/api/sessions/nonexistent-machine")
        assert resp.status == 200
        data = await resp.json()
        assert data == []

    async def test_response_shape(self, cli):
        resp = await cli.get("/api/sessions/mac-mini")
        data = await resp.json()
        for group in data:
            assert "project_folder" in group
            assert "sessions" in group
            assert isinstance(group["sessions"], list)


# ---------------------------------------------------------------------------
# POST /api/sessions/scan
# ---------------------------------------------------------------------------

class TestSessionsScanEndpoint:
    async def test_returns_200_on_success(self, cli, fake_sessions, fake_fleet):
        with (
            patch("src.server.discover_fleet", new_callable=AsyncMock, return_value=fake_fleet),
            patch("src.server.scan_all", new_callable=AsyncMock, return_value=fake_sessions),
        ):
            resp = await cli.post("/api/sessions/scan")
            assert resp.status == 200

    async def test_response_ok_true(self, cli, fake_sessions, fake_fleet):
        with (
            patch("src.server.discover_fleet", new_callable=AsyncMock, return_value=fake_fleet),
            patch("src.server.scan_all", new_callable=AsyncMock, return_value=fake_sessions),
        ):
            resp = await cli.post("/api/sessions/scan")
            data = await resp.json()
            assert data["ok"] is True

    async def test_response_contains_sessions(self, cli, fake_sessions, fake_fleet):
        with (
            patch("src.server.discover_fleet", new_callable=AsyncMock, return_value=fake_fleet),
            patch("src.server.scan_all", new_callable=AsyncMock, return_value=fake_sessions),
        ):
            resp = await cli.post("/api/sessions/scan")
            data = await resp.json()
            assert "sessions" in data
            assert len(data["sessions"]) == len(fake_sessions)

    async def test_response_contains_last_scan(self, cli, fake_sessions, fake_fleet):
        with (
            patch("src.server.discover_fleet", new_callable=AsyncMock, return_value=fake_fleet),
            patch("src.server.scan_all", new_callable=AsyncMock, return_value=fake_sessions),
        ):
            resp = await cli.post("/api/sessions/scan")
            data = await resp.json()
            assert "last_scan" in data
            assert data["last_scan"] is not None

    async def test_scan_failure_returns_500(self, cli):
        with patch(
            "src.server.discover_fleet",
            new_callable=AsyncMock,
            side_effect=RuntimeError("network down"),
        ):
            resp = await cli.post("/api/sessions/scan")
            assert resp.status == 500
            data = await resp.json()
            assert data["ok"] is False
            assert "network down" in data["error"]

    async def test_updates_app_state(self, cli, fake_fleet):
        new_session = make_claude_session(session_id="brand-new", machine="mac-mini")
        with (
            patch("src.server.discover_fleet", new_callable=AsyncMock, return_value=fake_fleet),
            patch("src.server.scan_all", new_callable=AsyncMock, return_value=[new_session]),
        ):
            await cli.post("/api/sessions/scan")
            resp = await cli.get("/api/sessions")
            data = await resp.json()
            all_session_ids = [
                s["session_id"]
                for groups in data.values()
                for g in groups
                for s in g["sessions"]
            ]
            assert "brand-new" in all_session_ids


# ---------------------------------------------------------------------------
# POST /api/sessions/launch
# ---------------------------------------------------------------------------

class TestSessionsLaunchEndpoint:
    async def test_valid_body_returns_200(self, cli):
        with patch("src.server.launch_claude_session", new_callable=AsyncMock, return_value={"ok": True}):
            resp = await cli.post(
                "/api/sessions/launch",
                json={"machine": "mac-mini", "session_id": "sess-001", "cwd": "/tmp/project"},
            )
            assert resp.status == 200
            data = await resp.json()
            assert data["ok"] is True

    async def test_missing_session_id_returns_400(self, cli):
        resp = await cli.post(
            "/api/sessions/launch",
            json={"machine": "mac-mini", "cwd": "/tmp/project"},
        )
        assert resp.status == 400
        data = await resp.json()
        assert data["ok"] is False

    async def test_missing_cwd_returns_400(self, cli):
        resp = await cli.post(
            "/api/sessions/launch",
            json={"machine": "mac-mini", "session_id": "sess-001"},
        )
        assert resp.status == 400
        data = await resp.json()
        assert data["ok"] is False

    async def test_empty_session_id_returns_400(self, cli):
        resp = await cli.post(
            "/api/sessions/launch",
            json={"machine": "mac-mini", "session_id": "", "cwd": "/tmp"},
        )
        assert resp.status == 400

    async def test_empty_cwd_returns_400(self, cli):
        resp = await cli.post(
            "/api/sessions/launch",
            json={"machine": "mac-mini", "session_id": "sess-001", "cwd": ""},
        )
        assert resp.status == 400

    async def test_invalid_json_returns_400(self, cli):
        resp = await cli.post(
            "/api/sessions/launch",
            data="not-json",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status == 400
        data = await resp.json()
        assert "invalid JSON" in data["error"]

    async def test_launcher_failure_returns_500(self, cli):
        with patch(
            "src.server.launch_claude_session",
            new_callable=AsyncMock,
            return_value={"ok": False, "error": "terminal not found"},
        ):
            resp = await cli.post(
                "/api/sessions/launch",
                json={"machine": "mac-mini", "session_id": "sess-001", "cwd": "/tmp"},
            )
            assert resp.status == 500
            data = await resp.json()
            assert data["ok"] is False

    async def test_calls_launch_with_correct_args(self, cli):
        mock_launcher = AsyncMock(return_value={"ok": True})
        with patch("src.server.launch_claude_session", mock_launcher):
            await cli.post(
                "/api/sessions/launch",
                json={"machine": "mac-mini", "session_id": "my-session", "cwd": "/my/path", "skip_permissions": True},
            )
            mock_launcher.assert_awaited_once_with("/my/path", "my-session", "mac-mini", skip_permissions=True)


# ---------------------------------------------------------------------------
# GET /api/fleet
# ---------------------------------------------------------------------------

class TestFleetEndpoint:
    async def test_returns_200(self, cli):
        resp = await cli.get("/api/fleet")
        assert resp.status == 200

    async def test_returns_fleet_dict(self, cli, fake_fleet):
        resp = await cli.get("/api/fleet")
        data = await resp.json()
        assert isinstance(data, dict)
        assert set(data.keys()) == set(fake_fleet.keys())

    async def test_machine_has_online_field(self, cli):
        resp = await cli.get("/api/fleet")
        data = await resp.json()
        for info in data.values():
            assert "online" in info

    async def test_mac_mini_is_online(self, cli):
        resp = await cli.get("/api/fleet")
        data = await resp.json()
        assert data["mac-mini"]["online"] is True

    async def test_ubuntu_desktop_is_offline(self, cli):
        resp = await cli.get("/api/fleet")
        data = await resp.json()
        assert data["ubuntu-desktop"]["online"] is False


# ---------------------------------------------------------------------------
# GET /api/tmux
# ---------------------------------------------------------------------------

class TestTmuxEndpoint:
    async def test_returns_200(self, cli):
        resp = await cli.get("/api/tmux")
        assert resp.status == 200

    async def test_returns_list(self, cli):
        resp = await cli.get("/api/tmux")
        data = await resp.json()
        assert isinstance(data, list)

    async def test_count_matches_fake_data(self, cli, fake_tmux):
        resp = await cli.get("/api/tmux")
        data = await resp.json()
        assert len(data) == len(fake_tmux)

    async def test_session_has_expected_fields(self, cli):
        resp = await cli.get("/api/tmux")
        data = await resp.json()
        for item in data:
            assert "name" in item
            assert "machine" in item
            assert "created" in item
            assert "windows" in item
            assert "attached" in item
            assert "is_local" in item


# ---------------------------------------------------------------------------
# GET /api/tmux/{machine}
# ---------------------------------------------------------------------------

class TestTmuxMachineEndpoint:
    async def test_known_machine_returns_its_sessions(self, cli):
        resp = await cli.get("/api/tmux/mac-mini")
        assert resp.status == 200
        data = await resp.json()
        assert isinstance(data, list)
        assert all(s["machine"] == "mac-mini" for s in data)

    async def test_other_machine(self, cli):
        resp = await cli.get("/api/tmux/ubuntu-desktop")
        assert resp.status == 200
        data = await resp.json()
        assert all(s["machine"] == "ubuntu-desktop" for s in data)

    async def test_unknown_machine_returns_empty_list(self, cli):
        resp = await cli.get("/api/tmux/nonexistent")
        assert resp.status == 200
        data = await resp.json()
        assert data == []

    async def test_filters_correctly(self, cli, fake_tmux):
        resp = await cli.get("/api/tmux/mac-mini")
        data = await resp.json()
        mac_sessions = [t for t in fake_tmux if t.machine == "mac-mini"]
        assert len(data) == len(mac_sessions)


# ---------------------------------------------------------------------------
# POST /api/tmux/create
# ---------------------------------------------------------------------------

class TestTmuxCreateEndpoint:
    async def test_valid_body_returns_200(self, cli):
        with patch(
            "src.server.create_tmux_session",
            new_callable=AsyncMock,
            return_value={"ok": True, "machine": "mac-mini", "session": "test"},
        ):
            resp = await cli.post(
                "/api/tmux/create",
                json={"machine": "mac-mini", "name": "test"},
            )
            assert resp.status == 200
            data = await resp.json()
            assert data["ok"] is True

    async def test_missing_machine_returns_400(self, cli):
        resp = await cli.post("/api/tmux/create", json={"name": "test"})
        assert resp.status == 400
        data = await resp.json()
        assert data["ok"] is False

    async def test_missing_name_returns_400(self, cli):
        resp = await cli.post("/api/tmux/create", json={"machine": "mac-mini"})
        assert resp.status == 400
        data = await resp.json()
        assert data["ok"] is False

    async def test_empty_machine_returns_400(self, cli):
        resp = await cli.post("/api/tmux/create", json={"machine": "", "name": "test"})
        assert resp.status == 400

    async def test_empty_name_returns_400(self, cli):
        resp = await cli.post("/api/tmux/create", json={"machine": "mac-mini", "name": ""})
        assert resp.status == 400

    async def test_invalid_json_returns_400(self, cli):
        resp = await cli.post(
            "/api/tmux/create",
            data="bad-json",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status == 400
        data = await resp.json()
        assert "invalid JSON" in data["error"]

    async def test_create_failure_returns_500(self, cli):
        with patch(
            "src.server.create_tmux_session",
            new_callable=AsyncMock,
            return_value={"ok": False, "error": "tmux not found"},
        ):
            resp = await cli.post(
                "/api/tmux/create",
                json={"machine": "mac-mini", "name": "test"},
            )
            assert resp.status == 500

    async def test_passes_optional_cwd_and_command(self, cli):
        mock_create = AsyncMock(return_value={"ok": True})
        with patch("src.server.create_tmux_session", mock_create):
            await cli.post(
                "/api/tmux/create",
                json={"machine": "mac-mini", "name": "work", "cwd": "/tmp", "command": "htop"},
            )
            mock_create.assert_awaited_once_with("mac-mini", "work", "/tmp", "htop")

    async def test_optional_fields_default_to_none(self, cli):
        mock_create = AsyncMock(return_value={"ok": True})
        with patch("src.server.create_tmux_session", mock_create):
            await cli.post(
                "/api/tmux/create",
                json={"machine": "mac-mini", "name": "work"},
            )
            mock_create.assert_awaited_once_with("mac-mini", "work", None, None)


# ---------------------------------------------------------------------------
# POST /api/tmux/connect
# ---------------------------------------------------------------------------

class TestTmuxConnectEndpoint:
    async def test_valid_body_returns_200(self, cli):
        with patch("src.server.launch_tmux_attach", new_callable=AsyncMock, return_value={"ok": True}):
            resp = await cli.post(
                "/api/tmux/connect",
                json={"machine": "mac-mini", "session_name": "work"},
            )
            assert resp.status == 200
            data = await resp.json()
            assert data["ok"] is True

    async def test_missing_machine_returns_400(self, cli):
        resp = await cli.post("/api/tmux/connect", json={"session_name": "work"})
        assert resp.status == 400

    async def test_missing_session_name_returns_400(self, cli):
        resp = await cli.post("/api/tmux/connect", json={"machine": "mac-mini"})
        assert resp.status == 400

    async def test_empty_machine_returns_400(self, cli):
        resp = await cli.post("/api/tmux/connect", json={"machine": "", "session_name": "work"})
        assert resp.status == 400

    async def test_empty_session_name_returns_400(self, cli):
        resp = await cli.post("/api/tmux/connect", json={"machine": "mac-mini", "session_name": ""})
        assert resp.status == 400

    async def test_invalid_json_returns_400(self, cli):
        resp = await cli.post(
            "/api/tmux/connect",
            data="not-json",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status == 400
        data = await resp.json()
        assert "invalid JSON" in data["error"]

    async def test_launch_failure_returns_500(self, cli):
        with patch(
            "src.server.launch_tmux_attach",
            new_callable=AsyncMock,
            return_value={"ok": False, "error": "no terminal"},
        ):
            resp = await cli.post(
                "/api/tmux/connect",
                json={"machine": "mac-mini", "session_name": "work"},
            )
            assert resp.status == 500

    async def test_calls_launcher_with_correct_args(self, cli):
        mock_attach = AsyncMock(return_value={"ok": True})
        with patch("src.server.launch_tmux_attach", mock_attach):
            await cli.post(
                "/api/tmux/connect",
                json={"machine": "ubuntu-desktop", "session_name": "remote-work"},
            )
            mock_attach.assert_awaited_once_with("remote-work", "ubuntu-desktop")


# ---------------------------------------------------------------------------
# POST /api/tmux/kill
# ---------------------------------------------------------------------------

class TestTmuxKillEndpoint:
    async def test_valid_body_returns_200(self, cli):
        with patch("src.server.kill_tmux_session", new_callable=AsyncMock, return_value={"ok": True}):
            resp = await cli.post(
                "/api/tmux/kill",
                json={"machine": "mac-mini", "name": "work"},
            )
            assert resp.status == 200
            data = await resp.json()
            assert data["ok"] is True

    async def test_missing_machine_returns_400(self, cli):
        resp = await cli.post("/api/tmux/kill", json={"name": "work"})
        assert resp.status == 400

    async def test_missing_name_returns_400(self, cli):
        resp = await cli.post("/api/tmux/kill", json={"machine": "mac-mini"})
        assert resp.status == 400

    async def test_invalid_json_returns_400(self, cli):
        resp = await cli.post(
            "/api/tmux/kill",
            data="garbage",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status == 400
        data = await resp.json()
        assert "invalid JSON" in data["error"]

    async def test_kill_failure_returns_500(self, cli):
        with patch(
            "src.server.kill_tmux_session",
            new_callable=AsyncMock,
            return_value={"ok": False, "error": "session not found"},
        ):
            resp = await cli.post(
                "/api/tmux/kill",
                json={"machine": "mac-mini", "name": "work"},
            )
            assert resp.status == 500

    async def test_calls_kill_with_correct_args(self, cli):
        mock_kill = AsyncMock(return_value={"ok": True})
        with patch("src.server.kill_tmux_session", mock_kill):
            await cli.post(
                "/api/tmux/kill",
                json={"machine": "ubuntu-desktop", "name": "my-session"},
            )
            mock_kill.assert_awaited_once_with("ubuntu-desktop", "my-session")


# ---------------------------------------------------------------------------
# CORS middleware tests
# ---------------------------------------------------------------------------

class TestCorsMiddleware:
    async def test_options_returns_204(self, cli):
        resp = await cli.options("/health")
        assert resp.status == 204

    async def test_options_has_allow_origin(self, cli):
        resp = await cli.options("/health")
        assert resp.headers.get("Access-Control-Allow-Origin") == "*"

    async def test_options_has_allow_methods(self, cli):
        resp = await cli.options("/health")
        methods = resp.headers.get("Access-Control-Allow-Methods", "")
        assert "GET" in methods
        assert "POST" in methods
        assert "OPTIONS" in methods

    async def test_options_has_allow_headers(self, cli):
        resp = await cli.options("/health")
        headers_val = resp.headers.get("Access-Control-Allow-Headers", "")
        assert "Content-Type" in headers_val

    async def test_get_response_has_cors_origin(self, cli):
        resp = await cli.get("/health")
        assert resp.headers.get("Access-Control-Allow-Origin") == "*"

    async def test_post_response_has_cors_origin(self, cli, fake_fleet, fake_sessions):
        with (
            patch("src.server.discover_fleet", new_callable=AsyncMock, return_value=fake_fleet),
            patch("src.server.scan_all", new_callable=AsyncMock, return_value=fake_sessions),
        ):
            resp = await cli.post("/api/sessions/scan")
            assert resp.headers.get("Access-Control-Allow-Origin") == "*"

    async def test_options_on_api_sessions(self, cli):
        resp = await cli.options("/api/sessions")
        assert resp.status == 204
        assert resp.headers.get("Access-Control-Allow-Origin") == "*"

    async def test_options_on_ws_endpoint(self, cli):
        resp = await cli.options("/ws")
        assert resp.status == 204


# ---------------------------------------------------------------------------
# WebSocket tests
# ---------------------------------------------------------------------------

class TestWebSocketHandler:
    async def test_connect_and_close(self, cli):
        async with cli.ws_connect("/ws") as ws:
            await ws.close()

    async def test_subscribe_sessions_receives_snapshot(self, cli, fake_sessions):
        async with cli.ws_connect("/ws") as ws:
            await ws.send_json({"type": "subscribe", "channel": "sessions"})
            msg = await asyncio.wait_for(ws.receive_json(), timeout=3)
            assert msg["type"] == "snapshot"
            assert msg["channel"] == "sessions"
            assert isinstance(msg["data"], list)
            assert len(msg["data"]) == len(fake_sessions)
            await ws.close()

    async def test_subscribe_sessions_snapshot_has_session_fields(self, cli):
        async with cli.ws_connect("/ws") as ws:
            await ws.send_json({"type": "subscribe", "channel": "sessions"})
            msg = await asyncio.wait_for(ws.receive_json(), timeout=3)
            if msg["data"]:
                session = msg["data"][0]
                assert "session_id" in session
                assert "machine" in session
            await ws.close()

    async def test_subscribe_fleet_receives_snapshot(self, cli, fake_fleet):
        async with cli.ws_connect("/ws") as ws:
            await ws.send_json({"type": "subscribe", "channel": "fleet"})
            msg = await asyncio.wait_for(ws.receive_json(), timeout=3)
            assert msg["type"] == "snapshot"
            assert msg["channel"] == "fleet"
            assert isinstance(msg["data"], dict)
            assert set(msg["data"].keys()) == set(fake_fleet.keys())
            await ws.close()

    async def test_subscribe_tmux_receives_snapshot(self, cli, fake_tmux):
        async with cli.ws_connect("/ws") as ws:
            await ws.send_json({"type": "subscribe", "channel": "tmux"})
            msg = await asyncio.wait_for(ws.receive_json(), timeout=3)
            assert msg["type"] == "snapshot"
            assert msg["channel"] == "tmux"
            assert isinstance(msg["data"], list)
            assert len(msg["data"]) == len(fake_tmux)
            await ws.close()

    async def test_subscribe_unknown_channel_returns_empty_snapshot(self, cli):
        async with cli.ws_connect("/ws") as ws:
            await ws.send_json({"type": "subscribe", "channel": "nonexistent"})
            msg = await asyncio.wait_for(ws.receive_json(), timeout=3)
            assert msg["type"] == "snapshot"
            assert msg["channel"] == "nonexistent"
            assert msg["data"] == []
            await ws.close()

    async def test_invalid_json_returns_error(self, cli):
        async with cli.ws_connect("/ws") as ws:
            await ws.send_str("this is not valid JSON {{{")
            msg = await asyncio.wait_for(ws.receive_json(), timeout=3)
            assert msg["type"] == "error"
            assert "invalid JSON" in msg["message"]
            await ws.close()

    async def test_unsubscribe_removes_channel(self, cli):
        """
        After unsubscribing from 'sessions', no subsequent update for that
        channel should arrive. We verify the subscribe/unsubscribe round-trip
        and that no error is emitted.
        """
        async with cli.ws_connect("/ws") as ws:
            await ws.send_json({"type": "subscribe", "channel": "sessions"})
            snapshot = await asyncio.wait_for(ws.receive_json(), timeout=3)
            assert snapshot["type"] == "snapshot"

            await ws.send_json({"type": "unsubscribe", "channel": "sessions"})

            # No immediate reply expected; brief wait to confirm silence
            try:
                unexpected = await asyncio.wait_for(ws.receive_json(), timeout=0.2)
                assert unexpected.get("type") != "error"
            except asyncio.TimeoutError:
                pass  # Expected

            await ws.close()

    async def test_multiple_subscriptions(self, cli):
        async with cli.ws_connect("/ws") as ws:
            await ws.send_json({"type": "subscribe", "channel": "sessions"})
            msg1 = await asyncio.wait_for(ws.receive_json(), timeout=3)
            assert msg1["channel"] == "sessions"

            await ws.send_json({"type": "subscribe", "channel": "fleet"})
            msg2 = await asyncio.wait_for(ws.receive_json(), timeout=3)
            assert msg2["channel"] == "fleet"

            await ws.close()

    async def test_client_registered_in_ws_clients(self, cli):
        """Client should appear in app state ws_clients after connecting."""
        app = cli.app
        before = len(app["state"]["ws_clients"])

        async with cli.ws_connect("/ws") as ws:
            await asyncio.sleep(0.05)  # let server register the client
            assert len(app["state"]["ws_clients"]) == before + 1
            await ws.close()

        # After close, server should remove the client
        await asyncio.sleep(0.05)
        assert len(app["state"]["ws_clients"]) == before


# ---------------------------------------------------------------------------
# Application factory tests
# ---------------------------------------------------------------------------

class TestCreateApp:
    def test_returns_application_instance(self):
        with patch("src.server.detect_local_machine", return_value=None):
            app = create_app()
            assert isinstance(app, web.Application)

    def test_custom_port_stored(self):
        with patch("src.server.detect_local_machine", return_value=None):
            app = create_app(port=12345)
            assert app["port"] == 12345

    def test_custom_bind_stored(self):
        with patch("src.server.detect_local_machine", return_value=None):
            app = create_app(bind="0.0.0.0")
            assert app["bind"] == "0.0.0.0"

    def test_initial_state_has_required_keys(self):
        with patch("src.server.detect_local_machine", return_value=None):
            app = create_app()
            state = app["state"]
            assert "sessions" in state
            assert "fleet" in state
            assert "tmux" in state
            assert "last_scan" in state
            assert "ws_clients" in state

    def test_initial_state_sessions_empty(self):
        with patch("src.server.detect_local_machine", return_value=None):
            app = create_app()
            assert app["state"]["sessions"] == []

    def test_initial_state_fleet_empty(self):
        with patch("src.server.detect_local_machine", return_value=None):
            app = create_app()
            assert app["state"]["fleet"] == {}

    def test_initial_state_tmux_empty(self):
        with patch("src.server.detect_local_machine", return_value=None):
            app = create_app()
            assert app["state"]["tmux"] == []

    def test_initial_state_last_scan_none(self):
        with patch("src.server.detect_local_machine", return_value=None):
            app = create_app()
            assert app["state"]["last_scan"] is None

    def test_initial_ws_clients_is_set(self):
        with patch("src.server.detect_local_machine", return_value=None):
            app = create_app()
            assert isinstance(app["state"]["ws_clients"], set)

    def test_has_health_route(self):
        with patch("src.server.detect_local_machine", return_value=None):
            app = create_app()
            canonicals = {r.resource.canonical for r in app.router.routes()}
            assert "/health" in canonicals

    def test_has_sessions_routes(self):
        with patch("src.server.detect_local_machine", return_value=None):
            app = create_app()
            resources = {r.canonical for r in app.router.resources()}
            assert "/api/sessions" in resources
            assert "/api/sessions/{machine}" in resources

    def test_has_fleet_route(self):
        with patch("src.server.detect_local_machine", return_value=None):
            app = create_app()
            resources = {r.canonical for r in app.router.resources()}
            assert "/api/fleet" in resources

    def test_has_tmux_routes(self):
        with patch("src.server.detect_local_machine", return_value=None):
            app = create_app()
            resources = {r.canonical for r in app.router.resources()}
            assert "/api/tmux" in resources
            assert "/api/tmux/{machine}" in resources

    def test_has_ws_route(self):
        with patch("src.server.detect_local_machine", return_value=None):
            app = create_app()
            resources = {r.canonical for r in app.router.resources()}
            assert "/ws" in resources

    def test_has_on_startup_hook(self):
        with patch("src.server.detect_local_machine", return_value=None):
            app = create_app()
            assert len(app.on_startup) > 0

    def test_has_on_cleanup_hook(self):
        with patch("src.server.detect_local_machine", return_value=None):
            app = create_app()
            assert len(app.on_cleanup) > 0

    def test_cors_middleware_registered(self):
        with patch("src.server.detect_local_machine", return_value=None):
            app = create_app()
            assert cors_middleware in app.middlewares


# ---------------------------------------------------------------------------
# Background scan task lifecycle tests
# ---------------------------------------------------------------------------

class TestBackgroundScanTask:
    async def test_bg_task_created_on_startup(self, fake_sessions, fake_fleet, fake_tmux):
        """bg_task key should be an asyncio.Task after app startup."""
        with (
            patch("src.server.discover_fleet", new_callable=AsyncMock, return_value=fake_fleet),
            patch("src.server.scan_all", new_callable=AsyncMock, return_value=fake_sessions),
            patch("src.server.list_all_tmux", new_callable=AsyncMock, return_value=fake_tmux),
            patch("src.server.detect_local_machine", return_value="mac-mini"),
            patch("src.server.SCAN_INTERVAL", 99999),
        ):
            app = create_app()
            server = TestServer(app)
            client = TestClient(server)
            await client.start_server()
            try:
                assert "bg_task" in client.app
                assert isinstance(client.app["bg_task"], asyncio.Task)
            finally:
                await client.close()

    async def test_bg_task_cancelled_on_cleanup(self, fake_sessions, fake_fleet, fake_tmux):
        """After client teardown, bg_task should be done (cancelled)."""
        with (
            patch("src.server.discover_fleet", new_callable=AsyncMock, return_value=fake_fleet),
            patch("src.server.scan_all", new_callable=AsyncMock, return_value=fake_sessions),
            patch("src.server.list_all_tmux", new_callable=AsyncMock, return_value=fake_tmux),
            patch("src.server.detect_local_machine", return_value="mac-mini"),
            patch("src.server.SCAN_INTERVAL", 99999),
        ):
            app = create_app()
            server = TestServer(app)
            client = TestClient(server)
            await client.start_server()
            task = client.app["bg_task"]
            await client.close()
            assert task.done()

    async def test_bg_task_populates_state(self, fake_sessions, fake_fleet, fake_tmux):
        """
        The background scan's first iteration should populate state with the
        data returned by the mocked functions.
        """
        with (
            patch("src.server.discover_fleet", new_callable=AsyncMock, return_value=fake_fleet),
            patch("src.server.scan_all", new_callable=AsyncMock, return_value=fake_sessions),
            patch("src.server.list_all_tmux", new_callable=AsyncMock, return_value=fake_tmux),
            patch("src.server.detect_local_machine", return_value="mac-mini"),
            patch("src.server.SCAN_INTERVAL", 99999),
        ):
            app = create_app()
            server = TestServer(app)
            client = TestClient(server)
            await client.start_server()
            try:
                # Yield control so the bg task can complete its first pass
                await asyncio.sleep(0.1)
                state = client.app["state"]
                assert state["sessions"] == fake_sessions
                assert state["fleet"] == fake_fleet
                assert state["tmux"] == fake_tmux
                assert state["last_scan"] is not None
            finally:
                await client.close()
