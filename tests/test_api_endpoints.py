"""
Integration tests for src/server.py REST endpoints not covered by test_server.py.

Covers:
  - POST /api/sessions/pin — pin a session, verify in prefs
  - POST /api/sessions/unpin — unpin, verify removed
  - POST /api/sessions/archive — archive a session
  - POST /api/sessions/unarchive — unarchive
  - POST /api/sessions/rename — rename an active session (mock filesystem)
  - GET /api/preferences — returns prefs
  - POST /api/preferences — saves prefs
  - POST /api/browse — browse local dirs (use tmp_path)
  - POST /api/drives — list local drives (mock psutil)
  - POST /api/mkdir — create directory (use tmp_path)
  - POST /api/hardware — get hardware info (mock psutil + subprocess)
  - POST /api/restart — verify response (don't actually restart)
  - POST /api/tmux/connect-remote — verify calls launcher (mock)
  - Error cases: missing fields, invalid JSON, unknown machine
"""
from __future__ import annotations

import asyncio
import json
import os
import pathlib
import platform
import sys
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
# Helpers / Fixtures
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


FAKE_FLEET: dict = {
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
async def make_client(
    tmp_path: pathlib.Path,
    sessions: list[ClaudeSession] | None = None,
    prefs_override: dict | None = None,
) -> AsyncIterator[TestClient]:
    """Build a TestClient with mocked background scan and prefs file."""
    if sessions is None:
        sessions = [make_claude_session()]

    prefs_file = tmp_path / "prefs.json"
    if prefs_override is not None:
        prefs_file.write_text(json.dumps(prefs_override))
    else:
        prefs_file.write_text(json.dumps({"skip_permissions": False}))

    with patch("src.server.PREFS_FILE", prefs_file), \
         patch("src.server.discover_fleet", new=AsyncMock(return_value=FAKE_FLEET)), \
         patch("src.server.scan_all", new=AsyncMock(return_value=[])), \
         patch("src.server.list_all_tmux", new=AsyncMock(return_value=[])):

        app = create_app(port=44740)

        # Seed state before startup (background scan will override but this sets initial)
        app["state"]["sessions"] = sessions
        app["state"]["fleet"] = FAKE_FLEET
        app["state"]["tmux"] = []
        app["state"]["last_scan"] = "2026-01-01T00:00:00+00:00"
        app["local_machine"] = "mac-mini"

        server = TestServer(app)
        client = TestClient(server)
        await client.start_server()

        # Re-seed state after startup (background scan may have overwritten it)
        app["state"]["sessions"] = sessions
        app["state"]["fleet"] = FAKE_FLEET
        app["state"]["tmux"] = []
        app["local_machine"] = "mac-mini"

        try:
            yield client
        finally:
            await client.close()


# ---------------------------------------------------------------------------
# POST /api/sessions/pin
# ---------------------------------------------------------------------------

class TestSessionsPin:
    @pytest.mark.asyncio
    async def test_pin_session_returns_ok(self, tmp_path):
        async with make_client(tmp_path) as client:
            resp = await client.post(
                "/api/sessions/pin",
                json={"session_id": "sess-001"},
            )
            data = await resp.json()
        assert data["ok"] is True
        assert "sess-001" in data["pinned_sessions"]

    @pytest.mark.asyncio
    async def test_pin_session_persists_in_prefs(self, tmp_path):
        prefs_file = tmp_path / "prefs.json"
        prefs_file.write_text(json.dumps({"skip_permissions": False}))
        async with make_client(tmp_path) as client:
            await client.post("/api/sessions/pin", json={"session_id": "sess-abc"})
            # GET prefs to verify persistence
            resp = await client.get("/api/preferences")
            prefs = await resp.json()
        assert "sess-abc" in prefs.get("pinned_sessions", [])

    @pytest.mark.asyncio
    async def test_pin_same_session_twice_not_duplicated(self, tmp_path):
        async with make_client(tmp_path) as client:
            await client.post("/api/sessions/pin", json={"session_id": "sess-001"})
            resp = await client.post("/api/sessions/pin", json={"session_id": "sess-001"})
            data = await resp.json()
        assert data["pinned_sessions"].count("sess-001") == 1

    @pytest.mark.asyncio
    async def test_pin_missing_session_id_returns_400(self, tmp_path):
        async with make_client(tmp_path) as client:
            resp = await client.post("/api/sessions/pin", json={})
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_pin_invalid_json_returns_400(self, tmp_path):
        async with make_client(tmp_path) as client:
            resp = await client.post(
                "/api/sessions/pin",
                data="not-json",
                headers={"Content-Type": "application/json"},
            )
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_pin_multiple_sessions(self, tmp_path):
        async with make_client(tmp_path) as client:
            await client.post("/api/sessions/pin", json={"session_id": "sess-001"})
            await client.post("/api/sessions/pin", json={"session_id": "sess-002"})
            resp = await client.get("/api/preferences")
            prefs = await resp.json()
        assert "sess-001" in prefs["pinned_sessions"]
        assert "sess-002" in prefs["pinned_sessions"]


# ---------------------------------------------------------------------------
# POST /api/sessions/unpin
# ---------------------------------------------------------------------------

class TestSessionsUnpin:
    @pytest.mark.asyncio
    async def test_unpin_removes_session(self, tmp_path):
        prefs = {"skip_permissions": False, "pinned_sessions": ["sess-001", "sess-002"]}
        async with make_client(tmp_path, prefs_override=prefs) as client:
            resp = await client.post(
                "/api/sessions/unpin",
                json={"session_id": "sess-001"},
            )
            data = await resp.json()
        assert data["ok"] is True
        assert "sess-001" not in data["pinned_sessions"]
        assert "sess-002" in data["pinned_sessions"]

    @pytest.mark.asyncio
    async def test_unpin_nonexistent_session_still_ok(self, tmp_path):
        async with make_client(tmp_path) as client:
            resp = await client.post(
                "/api/sessions/unpin",
                json={"session_id": "not-pinned"},
            )
            data = await resp.json()
        assert data["ok"] is True
        assert "not-pinned" not in data["pinned_sessions"]

    @pytest.mark.asyncio
    async def test_unpin_missing_session_id_returns_400(self, tmp_path):
        async with make_client(tmp_path) as client:
            resp = await client.post("/api/sessions/unpin", json={})
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_unpin_invalid_json_returns_400(self, tmp_path):
        async with make_client(tmp_path) as client:
            resp = await client.post(
                "/api/sessions/unpin",
                data="bad",
                headers={"Content-Type": "application/json"},
            )
        assert resp.status == 400


# ---------------------------------------------------------------------------
# POST /api/sessions/archive
# ---------------------------------------------------------------------------

class TestSessionsArchive:
    @pytest.mark.asyncio
    async def test_archive_session_returns_ok(self, tmp_path):
        async with make_client(tmp_path) as client:
            resp = await client.post(
                "/api/sessions/archive",
                json={"session_id": "sess-001"},
            )
            data = await resp.json()
        assert data["ok"] is True
        assert "sess-001" in data["archived_sessions"]

    @pytest.mark.asyncio
    async def test_archive_persists(self, tmp_path):
        async with make_client(tmp_path) as client:
            await client.post("/api/sessions/archive", json={"session_id": "sess-001"})
            resp = await client.get("/api/preferences")
            prefs = await resp.json()
        assert "sess-001" in prefs.get("archived_sessions", [])

    @pytest.mark.asyncio
    async def test_archive_same_session_twice_not_duplicated(self, tmp_path):
        async with make_client(tmp_path) as client:
            await client.post("/api/sessions/archive", json={"session_id": "sess-001"})
            resp = await client.post("/api/sessions/archive", json={"session_id": "sess-001"})
            data = await resp.json()
        assert data["archived_sessions"].count("sess-001") == 1

    @pytest.mark.asyncio
    async def test_archive_missing_session_id_returns_400(self, tmp_path):
        async with make_client(tmp_path) as client:
            resp = await client.post("/api/sessions/archive", json={})
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_archive_invalid_json_returns_400(self, tmp_path):
        async with make_client(tmp_path) as client:
            resp = await client.post(
                "/api/sessions/archive",
                data="notjson",
                headers={"Content-Type": "application/json"},
            )
        assert resp.status == 400


# ---------------------------------------------------------------------------
# POST /api/sessions/unarchive
# ---------------------------------------------------------------------------

class TestSessionsUnarchive:
    @pytest.mark.asyncio
    async def test_unarchive_removes_from_archived(self, tmp_path):
        prefs = {"archived_sessions": ["sess-001", "sess-002"]}
        async with make_client(tmp_path, prefs_override=prefs) as client:
            resp = await client.post(
                "/api/sessions/unarchive",
                json={"session_id": "sess-001"},
            )
            data = await resp.json()
        assert data["ok"] is True
        assert "sess-001" not in data["archived_sessions"]
        assert "sess-002" in data["archived_sessions"]

    @pytest.mark.asyncio
    async def test_unarchive_nonexistent_still_ok(self, tmp_path):
        async with make_client(tmp_path) as client:
            resp = await client.post(
                "/api/sessions/unarchive",
                json={"session_id": "not-archived"},
            )
            data = await resp.json()
        assert data["ok"] is True

    @pytest.mark.asyncio
    async def test_unarchive_missing_session_id_returns_400(self, tmp_path):
        async with make_client(tmp_path) as client:
            resp = await client.post("/api/sessions/unarchive", json={})
        assert resp.status == 400


# ---------------------------------------------------------------------------
# POST /api/sessions/rename
# ---------------------------------------------------------------------------

class TestSessionsRename:
    @pytest.mark.asyncio
    async def test_rename_local_session_via_pid_file(self, tmp_path):
        """Rename succeeds when PID file found by pid argument."""
        sessions_dir = tmp_path / ".claude" / "sessions"
        sessions_dir.mkdir(parents=True)
        pid = 99999
        pid_file = sessions_dir / f"{pid}.json"
        pid_file.write_text(json.dumps({
            "sessionId": "sess-001",
            "pid": pid,
        }), encoding="utf-8")

        async with make_client(tmp_path) as client:
            with patch("src.server.pathlib.Path.home", return_value=tmp_path):
                resp = await client.post(
                    "/api/sessions/rename",
                    json={
                        "machine": "mac-mini",
                        "session_id": "sess-001",
                        "pid": pid,
                        "name": "my-new-name",
                    },
                )
                data = await resp.json()

        assert data["ok"] is True
        assert data["name"] == "my-new-name"

    @pytest.mark.asyncio
    async def test_rename_missing_session_id_returns_400(self, tmp_path):
        async with make_client(tmp_path) as client:
            resp = await client.post(
                "/api/sessions/rename",
                json={"machine": "mac-mini", "name": "new-name"},
            )
            assert resp.status == 400
            data = await resp.json()
        assert "session_id" in data["error"].lower()

    @pytest.mark.asyncio
    async def test_rename_empty_name_returns_400(self, tmp_path):
        async with make_client(tmp_path) as client:
            resp = await client.post(
                "/api/sessions/rename",
                json={"session_id": "sess-001", "name": "  "},
            )
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_rename_missing_name_returns_400(self, tmp_path):
        async with make_client(tmp_path) as client:
            resp = await client.post(
                "/api/sessions/rename",
                json={"session_id": "sess-001", "name": ""},
            )
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_rename_invalid_json_returns_400(self, tmp_path):
        async with make_client(tmp_path) as client:
            resp = await client.post(
                "/api/sessions/rename",
                data="notjson",
                headers={"Content-Type": "application/json"},
            )
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_rename_no_pid_file_returns_404(self, tmp_path):
        """When no PID file exists for session, returns 404."""
        sessions_dir = tmp_path / ".claude" / "sessions"
        sessions_dir.mkdir(parents=True)

        async with make_client(tmp_path) as client:
            with patch("src.server.pathlib.Path.home", return_value=tmp_path):
                resp = await client.post(
                    "/api/sessions/rename",
                    json={
                        "machine": "mac-mini",
                        "session_id": "nonexistent-sess",
                        "name": "new-name",
                    },
                )
            assert resp.status == 404

    @pytest.mark.asyncio
    async def test_rename_unknown_remote_machine_returns_400(self, tmp_path):
        async with make_client(tmp_path) as client:
            resp = await client.post(
                "/api/sessions/rename",
                json={
                    "machine": "unknown-machine-xyz",
                    "session_id": "sess-001",
                    "name": "new-name",
                },
            )
            assert resp.status == 400
            data = await resp.json()
        assert "Unknown machine" in data["error"]


# ---------------------------------------------------------------------------
# GET /api/preferences
# ---------------------------------------------------------------------------

class TestGetPreferences:
    @pytest.mark.asyncio
    async def test_get_preferences_returns_json(self, tmp_path):
        prefs = {"skip_permissions": True, "theme": "dark"}
        async with make_client(tmp_path, prefs_override=prefs) as client:
            resp = await client.get("/api/preferences")
            data = await resp.json()
        assert data["skip_permissions"] is True
        assert data["theme"] == "dark"

    @pytest.mark.asyncio
    async def test_get_preferences_defaults_when_file_missing(self, tmp_path):
        # Don't create prefs file — server falls back to defaults
        missing_file = tmp_path / "does-not-exist.json"
        with patch("src.server.PREFS_FILE", missing_file), \
             patch("src.server.discover_fleet", new=AsyncMock(return_value={})), \
             patch("src.server.scan_all", new=AsyncMock(return_value=[])), \
             patch("src.server.list_all_tmux", new=AsyncMock(return_value=[])):
            app = create_app(port=44740)
            app["state"]["sessions"] = []
            app["state"]["fleet"] = {}
            app["state"]["tmux"] = []
            app["state"]["last_scan"] = None
            app["local_machine"] = "mac-mini"
            server = TestServer(app)
            client = TestClient(server)
            await client.start_server()
            try:
                resp = await client.get("/api/preferences")
                data = await resp.json()
            finally:
                await client.close()
        # Default prefs should have at least skip_permissions
        assert "skip_permissions" in data

    @pytest.mark.asyncio
    async def test_get_preferences_200_status(self, tmp_path):
        async with make_client(tmp_path) as client:
            resp = await client.get("/api/preferences")
        assert resp.status == 200


# ---------------------------------------------------------------------------
# POST /api/preferences
# ---------------------------------------------------------------------------

class TestPostPreferences:
    @pytest.mark.asyncio
    async def test_post_preferences_updates_and_returns(self, tmp_path):
        async with make_client(tmp_path) as client:
            resp = await client.post(
                "/api/preferences",
                json={"skip_permissions": True, "theme": "dark"},
            )
            data = await resp.json()
        assert data["skip_permissions"] is True
        assert data["theme"] == "dark"

    @pytest.mark.asyncio
    async def test_post_preferences_persists(self, tmp_path):
        async with make_client(tmp_path) as client:
            await client.post("/api/preferences", json={"my_setting": "value123"})
            resp = await client.get("/api/preferences")
            data = await resp.json()
        assert data["my_setting"] == "value123"

    @pytest.mark.asyncio
    async def test_post_preferences_merges_with_existing(self, tmp_path):
        prefs = {"existing_key": "existing_value"}
        async with make_client(tmp_path, prefs_override=prefs) as client:
            await client.post("/api/preferences", json={"new_key": "new_value"})
            resp = await client.get("/api/preferences")
            data = await resp.json()
        assert data["existing_key"] == "existing_value"
        assert data["new_key"] == "new_value"

    @pytest.mark.asyncio
    async def test_post_preferences_invalid_json_returns_400(self, tmp_path):
        async with make_client(tmp_path) as client:
            resp = await client.post(
                "/api/preferences",
                data="not-json",
                headers={"Content-Type": "application/json"},
            )
        assert resp.status == 400


# ---------------------------------------------------------------------------
# POST /api/browse
# ---------------------------------------------------------------------------

class TestBrowse:
    @pytest.mark.asyncio
    async def test_browse_local_home_returns_dirs(self, tmp_path):
        """Browse local home dir (uses tmp_path as home)."""
        # Create some subdirs
        (tmp_path / "projects").mkdir()
        (tmp_path / "downloads").mkdir()
        (tmp_path / ".hidden").mkdir()  # should be excluded

        async with make_client(tmp_path) as client:
            # Browse a known directory
            resp = await client.post(
                "/api/browse",
                json={"machine": "mac-mini", "path": str(tmp_path)},
            )
            data = await resp.json()

        assert data["ok"] is True
        assert "dirs" in data
        dir_names = [d["name"] for d in data["dirs"]]
        assert "projects" in dir_names
        assert "downloads" in dir_names
        # Hidden dirs should be excluded
        assert ".hidden" not in dir_names

    @pytest.mark.asyncio
    async def test_browse_local_returns_path_and_parent(self, tmp_path):
        sub = tmp_path / "subdir"
        sub.mkdir()

        async with make_client(tmp_path) as client:
            resp = await client.post(
                "/api/browse",
                json={"machine": "mac-mini", "path": str(sub)},
            )
            data = await resp.json()

        assert data["ok"] is True
        assert "path" in data
        assert "parent" in data

    @pytest.mark.asyncio
    async def test_browse_nonexistent_path_returns_404(self, tmp_path):
        async with make_client(tmp_path) as client:
            resp = await client.post(
                "/api/browse",
                json={"machine": "mac-mini", "path": "/nonexistent/path/xyz"},
            )
        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_browse_invalid_json_returns_400(self, tmp_path):
        async with make_client(tmp_path) as client:
            resp = await client.post(
                "/api/browse",
                data="bad",
                headers={"Content-Type": "application/json"},
            )
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_browse_unknown_remote_machine_returns_400(self, tmp_path):
        async with make_client(tmp_path) as client:
            resp = await client.post(
                "/api/browse",
                json={"machine": "no-such-machine", "path": "/tmp"},
            )
            assert resp.status == 400
            data = await resp.json()
        assert "Unknown machine" in data["error"]

    @pytest.mark.asyncio
    async def test_browse_returns_drive_field(self, tmp_path):
        async with make_client(tmp_path) as client:
            resp = await client.post(
                "/api/browse",
                json={"machine": "mac-mini", "path": str(tmp_path)},
            )
            data = await resp.json()
        assert "drive" in data


# ---------------------------------------------------------------------------
# POST /api/drives
# ---------------------------------------------------------------------------

class TestDrives:
    @pytest.mark.asyncio
    async def test_drives_local_returns_list(self, tmp_path):
        """Local drives returns a list of drive dicts."""
        mock_partition = MagicMock()
        mock_partition.mountpoint = "/"
        mock_partition.fstype = "ext4"
        mock_partition.device = "/dev/sda1"

        mock_usage = MagicMock()
        mock_usage.total = 100 * 10**9
        mock_usage.free = 50 * 10**9

        with patch("psutil.disk_partitions", return_value=[mock_partition]), \
             patch("psutil.disk_usage", return_value=mock_usage):
            async with make_client(tmp_path) as client:
                resp = await client.post(
                    "/api/drives",
                    json={"machine": "mac-mini"},
                )
                data = await resp.json()

        assert data["ok"] is True
        assert "drives" in data
        assert isinstance(data["drives"], list)

    @pytest.mark.asyncio
    async def test_drives_invalid_json_returns_400(self, tmp_path):
        async with make_client(tmp_path) as client:
            resp = await client.post(
                "/api/drives",
                data="bad",
                headers={"Content-Type": "application/json"},
            )
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_drives_unknown_machine_returns_400(self, tmp_path):
        async with make_client(tmp_path) as client:
            resp = await client.post(
                "/api/drives",
                json={"machine": "unknown-machine-xyz"},
            )
            assert resp.status == 400
            data = await resp.json()
        assert "Unknown machine" in data["error"]

    @pytest.mark.asyncio
    async def test_drives_local_drive_has_required_fields(self, tmp_path):
        """Each drive entry must have: path, name, label, total_gb, free_gb, is_system."""
        mock_partition = MagicMock()
        mock_partition.mountpoint = "/"
        mock_partition.fstype = "ext4"
        mock_partition.device = "/dev/sda1"

        mock_usage = MagicMock()
        mock_usage.total = 100 * 10**9
        mock_usage.free = 50 * 10**9

        with patch("psutil.disk_partitions", return_value=[mock_partition]), \
             patch("psutil.disk_usage", return_value=mock_usage):
            async with make_client(tmp_path) as client:
                resp = await client.post(
                    "/api/drives",
                    json={"machine": "mac-mini"},
                )
                data = await resp.json()

        if data["ok"] and data["drives"]:
            drive = data["drives"][0]
            for field in ("path", "name", "label", "total_gb", "free_gb", "is_system"):
                assert field in drive, f"Missing field: {field}"


# ---------------------------------------------------------------------------
# POST /api/mkdir
# ---------------------------------------------------------------------------

class TestMkdir:
    @pytest.mark.asyncio
    async def test_mkdir_creates_directory(self, tmp_path):
        new_dir = tmp_path / "new-folder"

        async with make_client(tmp_path) as client:
            resp = await client.post(
                "/api/mkdir",
                json={"machine": "mac-mini", "path": str(new_dir)},
            )
            data = await resp.json()

        assert data["ok"] is True
        assert new_dir.is_dir()

    @pytest.mark.asyncio
    async def test_mkdir_returns_created_path(self, tmp_path):
        new_dir = tmp_path / "another-folder"

        async with make_client(tmp_path) as client:
            resp = await client.post(
                "/api/mkdir",
                json={"machine": "mac-mini", "path": str(new_dir)},
            )
            data = await resp.json()

        assert data["ok"] is True
        assert str(new_dir) in data["path"]

    @pytest.mark.asyncio
    async def test_mkdir_existing_dir_returns_409(self, tmp_path):
        existing = tmp_path / "existing"
        existing.mkdir()

        async with make_client(tmp_path) as client:
            resp = await client.post(
                "/api/mkdir",
                json={"machine": "mac-mini", "path": str(existing)},
            )
        assert resp.status == 409

    @pytest.mark.asyncio
    async def test_mkdir_nonexistent_parent_returns_400(self, tmp_path):
        deep = tmp_path / "no-parent" / "subdir"

        async with make_client(tmp_path) as client:
            resp = await client.post(
                "/api/mkdir",
                json={"machine": "mac-mini", "path": str(deep)},
            )
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_mkdir_relative_path_returns_400(self, tmp_path):
        async with make_client(tmp_path) as client:
            resp = await client.post(
                "/api/mkdir",
                json={"machine": "mac-mini", "path": "relative/path"},
            )
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_mkdir_missing_path_returns_400(self, tmp_path):
        async with make_client(tmp_path) as client:
            resp = await client.post(
                "/api/mkdir",
                json={"machine": "mac-mini"},
            )
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_mkdir_invalid_json_returns_400(self, tmp_path):
        async with make_client(tmp_path) as client:
            resp = await client.post(
                "/api/mkdir",
                data="bad",
                headers={"Content-Type": "application/json"},
            )
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_mkdir_unknown_remote_machine_returns_400(self, tmp_path):
        async with make_client(tmp_path) as client:
            resp = await client.post(
                "/api/mkdir",
                json={"machine": "unknown-machine", "path": "/tmp/newdir"},
            )
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_mkdir_empty_path_returns_400(self, tmp_path):
        async with make_client(tmp_path) as client:
            resp = await client.post(
                "/api/mkdir",
                json={"machine": "mac-mini", "path": ""},
            )
        assert resp.status == 400


# ---------------------------------------------------------------------------
# POST /api/hardware
# ---------------------------------------------------------------------------

class TestHardware:
    @pytest.mark.asyncio
    async def test_hardware_local_returns_cpu_memory_gpus(self, tmp_path):
        """Local hardware call returns expected structure."""
        mock_hw = {
            "ok": True,
            "cpu": {
                "name": "Apple M4",
                "cores": 10,
                "usage_percent": 12.5,
                "temp_c": None,
            },
            "gpus": [],
            "memory": {
                "total_gb": 16.0,
                "used_gb": 8.0,
                "percent": 50.0,
            },
        }

        with patch("src.server._get_local_hardware", return_value=mock_hw):
            async with make_client(tmp_path) as client:
                resp = await client.post(
                    "/api/hardware",
                    json={"machine": "mac-mini"},
                )
                data = await resp.json()

        assert data["ok"] is True
        assert "cpu" in data
        assert "memory" in data
        assert "gpus" in data

    @pytest.mark.asyncio
    async def test_hardware_invalid_json_returns_400(self, tmp_path):
        async with make_client(tmp_path) as client:
            resp = await client.post(
                "/api/hardware",
                data="bad",
                headers={"Content-Type": "application/json"},
            )
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_hardware_unknown_machine_returns_400(self, tmp_path):
        async with make_client(tmp_path) as client:
            resp = await client.post(
                "/api/hardware",
                json={"machine": "no-such-machine"},
            )
            assert resp.status == 400
            data = await resp.json()
        assert "Unknown machine" in data["error"]

    @pytest.mark.asyncio
    async def test_hardware_cached_result_returned(self, tmp_path):
        """Hardware results are cached; second call within TTL uses cache."""
        import src.server as server_module

        mock_hw = {
            "ok": True,
            "cpu": {"name": "Apple M4", "cores": 10, "usage_percent": 5.0, "temp_c": None},
            "gpus": [],
            "memory": {"total_gb": 16.0, "used_gb": 4.0, "percent": 25.0},
        }

        call_count = [0]

        def fake_hw():
            call_count[0] += 1
            return mock_hw

        # Clear cache before test
        server_module._hw_cache.clear()

        with patch("src.server._get_local_hardware", side_effect=fake_hw):
            async with make_client(tmp_path) as client:
                await client.post("/api/hardware", json={"machine": "mac-mini"})
                await client.post("/api/hardware", json={"machine": "mac-mini"})

        # Should only have called the real function once (second hit cached)
        assert call_count[0] == 1


# ---------------------------------------------------------------------------
# POST /api/restart
# ---------------------------------------------------------------------------

class TestRestart:
    @pytest.mark.asyncio
    async def test_restart_returns_ok(self, tmp_path):
        async with make_client(tmp_path) as client:
            resp = await client.post("/api/restart")
            data = await resp.json()
        assert data["ok"] is True

    @pytest.mark.asyncio
    async def test_restart_returns_message(self, tmp_path):
        async with make_client(tmp_path) as client:
            resp = await client.post("/api/restart")
            data = await resp.json()
        assert "message" in data

    @pytest.mark.asyncio
    async def test_restart_200_status(self, tmp_path):
        async with make_client(tmp_path) as client:
            resp = await client.post("/api/restart")
        assert resp.status == 200


# ---------------------------------------------------------------------------
# POST /api/tmux/connect-remote
# ---------------------------------------------------------------------------

class TestTmuxConnectRemote:
    @pytest.mark.asyncio
    async def test_connect_remote_calls_launcher(self, tmp_path):
        """connect-remote endpoint calls launch_tmux_attach_remote."""
        async with make_client(tmp_path) as client:
            with patch(
                "src.server.launch_tmux_attach_remote",
                new=AsyncMock(return_value={"ok": True}),
            ) as mock_launch:
                resp = await client.post(
                    "/api/tmux/connect-remote",
                    json={"machine": "mac-mini", "session_name": "my-sess"},
                )
                data = await resp.json()

        assert data["ok"] is True
        mock_launch.assert_awaited_once_with("my-sess", "mac-mini")

    @pytest.mark.asyncio
    async def test_connect_remote_missing_machine_returns_400(self, tmp_path):
        async with make_client(tmp_path) as client:
            resp = await client.post(
                "/api/tmux/connect-remote",
                json={"session_name": "my-sess"},
            )
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_connect_remote_missing_session_name_returns_400(self, tmp_path):
        async with make_client(tmp_path) as client:
            resp = await client.post(
                "/api/tmux/connect-remote",
                json={"machine": "mac-mini"},
            )
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_connect_remote_invalid_json_returns_400(self, tmp_path):
        async with make_client(tmp_path) as client:
            resp = await client.post(
                "/api/tmux/connect-remote",
                data="bad",
                headers={"Content-Type": "application/json"},
            )
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_connect_remote_launcher_failure_returns_500(self, tmp_path):
        async with make_client(tmp_path) as client:
            with patch(
                "src.server.launch_tmux_attach_remote",
                new=AsyncMock(return_value={"ok": False, "error": "SSH failed"}),
            ):
                resp = await client.post(
                    "/api/tmux/connect-remote",
                    json={"machine": "mac-mini", "session_name": "my-sess"},
                )
        assert resp.status == 500
