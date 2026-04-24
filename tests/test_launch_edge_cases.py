"""
Edge-case coverage for the launch API — mirrors exactly what the web UI POSTs.

The UI calls these endpoints when the user clicks:
  - "SSH + New session"  -> POST /api/sessions/launch (mode=terminal, no session_id)
  - "SSH + New tmux/psmux" -> POST /api/sessions/launch (mode=tmux,   no session_id)
  - "Resume"             -> POST /api/sessions/launch (mode=terminal, session_id=X)
  - "Resume in tmux"     -> POST /api/sessions/launch (mode=tmux,   session_id=X)
  - "Attach"             -> POST /api/tmux/connect
  - "Remote attach"      -> POST /api/tmux/connect-remote

Each test fakes the specific launcher function that the handler routes to and
asserts:
  (a) The right launcher is invoked for the right (machine, mode, session_id)
      tuple -- no silent routing misses that would 500 via unhandled exception.
  (b) A launcher returning ok=True produces HTTP 200 with ok=True.
  (c) A launcher returning ok=False produces HTTP 500 with a JSON body that
      carries the launcher's error verbatim (UI shows it in the toast).
  (d) An UNHANDLED exception inside a launcher produces a well-formed error
      response, not an opaque aiohttp 500.

The "offline remote" cases are the ones the user is most likely to see in
production because windows-desktop / avell-i7 go offline intermittently.
"""
from __future__ import annotations

import json as _json
from contextlib import asynccontextmanager
from typing import AsyncIterator
from unittest.mock import AsyncMock, patch

import pytest
from aiohttp.test_utils import TestClient, TestServer

from src.server import create_app


# ---------------------------------------------------------------------------
# Test client harness -- seeds state and local_machine without touching I/O.
# ---------------------------------------------------------------------------

@asynccontextmanager
async def client(
    local_machine: str = "mac-mini",
    fleet: dict | None = None,
    tmux: list | None = None,
) -> AsyncIterator[TestClient]:
    with patch("src.server.detect_local_machine", return_value=local_machine):
        app = create_app(port=44740)

    seeded = {
        "sessions": [],
        "fleet": fleet or {
            "mac-mini": {"online": True, "os": "darwin"},
            "ubuntu-desktop": {"online": True, "os": "linux"},
            "avell-i7": {"online": True, "os": "win32"},
            "windows-desktop": {"online": False, "os": "win32"},
        },
        "tmux": tmux or [],
        "last_scan": None,
        "ws_clients": set(),
    }

    async def _noop_startup(a):
        a["state"].update(seeded)
        a["state"]["ws_clients"] = set()
        a["local_machine"] = local_machine

    async def _noop_cleanup(a):
        pass

    app.on_startup.clear()
    app.on_startup.append(_noop_startup)
    app.on_cleanup.clear()
    app.on_cleanup.append(_noop_cleanup)

    c = TestClient(TestServer(app))
    await c.start_server()
    try:
        yield c
    finally:
        await c.close()


def _assert_json_500(status: int, body: str) -> dict:
    """Assert status==500 AND body is JSON with ok=False+error. Returns parsed."""
    assert status == 500, f"expected 500, got {status}: {body[:200]}"
    try:
        data = _json.loads(body)
    except Exception as exc:
        pytest.fail(f"500 body was not JSON (parse err={exc}): {body[:200]!r}")
    assert data.get("ok") is False, data
    assert data.get("error"), data
    return data


# ---------------------------------------------------------------------------
# /api/sessions/launch -- mode='terminal' (the "SSH + New session" button path)
# ---------------------------------------------------------------------------

class TestNewSshTerminalLocal:
    async def test_happy_path_returns_200(self):
        with patch("src.server.launch_terminal",
                   new=AsyncMock(return_value={"ok": True})):
            async with client() as cli:
                resp = await cli.post(
                    "/api/sessions/launch",
                    json={"machine": "mac-mini", "cwd": "/tmp/proj",
                          "mode": "terminal", "skip_permissions": False},
                )
        assert resp.status == 200

    async def test_launcher_failure_surfaces_error_json(self):
        with patch("src.server.launch_terminal",
                   new=AsyncMock(return_value={"ok": False, "error": "no iTerm2"})):
            async with client() as cli:
                resp = await cli.post(
                    "/api/sessions/launch",
                    json={"machine": "mac-mini", "cwd": "/tmp/proj", "mode": "terminal"},
                )
                data = await resp.json()
        assert resp.status == 500
        assert data["ok"] is False
        assert data["error"] == "no iTerm2"


class TestNewSshTerminalRemoteLinux:
    async def test_happy_path_wraps_in_ssh_minus_t(self):
        captured = {}
        async def fake_terminal(cmd, **kw):
            captured["cmd"] = cmd
            return {"ok": True}
        with patch("src.server.launch_terminal", side_effect=fake_terminal):
            async with client(local_machine="mac-mini") as cli:
                resp = await cli.post(
                    "/api/sessions/launch",
                    json={"machine": "ubuntu-desktop",
                          "cwd": "/home/rbgnr/git/x",
                          "mode": "terminal"},
                )
        assert resp.status == 200
        assert captured["cmd"].startswith("ssh ")
        assert " -t " in captured["cmd"]
        assert "claude" in captured["cmd"]

    async def test_launcher_raising_returns_clean_500(self):
        async def boom(*a, **kw):
            raise RuntimeError("osascript binary missing")
        with patch("src.server.launch_terminal", side_effect=boom):
            async with client() as cli:
                resp = await cli.post(
                    "/api/sessions/launch",
                    json={"machine": "ubuntu-desktop",
                          "cwd": "/home/rbgnr/git/x",
                          "mode": "terminal"},
                )
                body = await resp.text()
        _assert_json_500(resp.status, body)


class TestNewSshTerminalRemoteWindowsOnline:
    async def test_happy_path_uses_powershell_cd(self):
        captured = {}
        async def fake_terminal(cmd, **kw):
            captured["cmd"] = cmd
            return {"ok": True}
        with patch("src.server.launch_terminal", side_effect=fake_terminal):
            async with client() as cli:
                resp = await cli.post(
                    "/api/sessions/launch",
                    json={"machine": "avell-i7",
                          "cwd": "C:\\Users\\rbgnr\\git\\x",
                          "mode": "terminal"},
                )
        assert resp.status == 200
        cmd = captured["cmd"]
        assert "Set-Location" in cmd
        assert "cd /d" not in cmd


class TestNewSshTerminalRemoteWindowsOffline:
    async def test_ssh_fail_produces_500_with_error_json(self):
        async def fake_terminal(cmd, **kw):
            return {"ok": False,
                    "error": "ssh: connect to host 192.168.7.101 port 22: "
                             "Operation timed out"}
        with patch("src.server.launch_terminal", side_effect=fake_terminal):
            async with client() as cli:
                resp = await cli.post(
                    "/api/sessions/launch",
                    json={"machine": "windows-desktop",
                          "cwd": "C:\\Users\\rbgnr\\git\\x",
                          "mode": "terminal"},
                )
                data = await resp.json()
        assert resp.status == 500
        assert data["ok"] is False
        assert "timed out" in data["error"].lower() or "connect" in data["error"].lower()


# ---------------------------------------------------------------------------
# /api/sessions/launch -- mode='tmux' (the "SSH + New tmux/psmux" button path)
# ---------------------------------------------------------------------------

class TestNewSshTmuxLocal:
    async def test_happy_path_returns_200(self):
        with patch("src.server.launch_new_tmux_and_attach",
                   new=AsyncMock(return_value={"ok": True})):
            async with client() as cli:
                resp = await cli.post(
                    "/api/sessions/launch",
                    json={"machine": "mac-mini",
                          "cwd": "/tmp/proj",
                          "mode": "tmux"},
                )
        assert resp.status == 200

    async def test_create_failure_returns_500(self):
        with patch(
            "src.server.launch_new_tmux_and_attach",
            new=AsyncMock(return_value={"ok": False, "error": "tmux: command not found"}),
        ):
            async with client() as cli:
                resp = await cli.post(
                    "/api/sessions/launch",
                    json={"machine": "mac-mini", "cwd": "/tmp/proj", "mode": "tmux"},
                )
                data = await resp.json()
        assert resp.status == 500
        assert "tmux" in data["error"]


class TestNewSshTmuxRemoteLinux:
    async def test_happy_path_returns_200(self):
        with patch("src.server.launch_new_tmux_and_attach",
                   new=AsyncMock(return_value={"ok": True})):
            async with client() as cli:
                resp = await cli.post(
                    "/api/sessions/launch",
                    json={"machine": "ubuntu-desktop",
                          "cwd": "/home/rbgnr/git/x",
                          "mode": "tmux"},
                )
        assert resp.status == 200

    async def test_offline_remote_returns_500_with_ssh_error(self):
        with patch(
            "src.server.launch_new_tmux_and_attach",
            new=AsyncMock(return_value={
                "ok": False,
                "error": "ssh: connect to host 192.168.7.13 port 22: No route to host",
            }),
        ):
            async with client() as cli:
                resp = await cli.post(
                    "/api/sessions/launch",
                    json={"machine": "ubuntu-desktop",
                          "cwd": "/home/rbgnr/git/x",
                          "mode": "tmux"},
                )
                data = await resp.json()
        assert resp.status == 500
        assert "no route" in data["error"].lower() or "connect" in data["error"].lower()


class TestNewSshTmuxRemoteWindowsOffline:
    async def test_pool_exec_nonzero_returns_500(self):
        async def fake_pool(machine, cmd, **kw):
            return (255, b"", b"ssh: connect to host 192.168.7.101: Operation timed out")
        fake_launch = AsyncMock()
        with patch("src.server._pool_exec", side_effect=fake_pool), \
             patch("src.server.launch_terminal", fake_launch):
            async with client() as cli:
                resp = await cli.post(
                    "/api/sessions/launch",
                    json={"machine": "windows-desktop",
                          "cwd": "C:\\proj", "mode": "tmux"},
                )
                data = await resp.json()
        assert resp.status == 500
        assert "timed out" in data["error"].lower() or "connect" in data["error"].lower()
        fake_launch.assert_not_awaited()

    async def test_pool_exec_raising_returns_clean_500(self):
        async def boom(*a, **kw):
            raise TimeoutError("asyncssh pool exhausted")
        with patch("src.server._pool_exec", side_effect=boom):
            async with client() as cli:
                resp = await cli.post(
                    "/api/sessions/launch",
                    json={"machine": "windows-desktop",
                          "cwd": "C:\\proj", "mode": "tmux"},
                )
                body = await resp.text()
        _assert_json_500(resp.status, body)


# ---------------------------------------------------------------------------
# Payload edge cases -- /api/sessions/launch
# ---------------------------------------------------------------------------

class TestLaunchPayloadEdges:
    async def test_missing_cwd(self):
        async with client() as cli:
            resp = await cli.post("/api/sessions/launch", json={"machine": "mac-mini"})
            data = await resp.json()
        assert resp.status == 400
        assert "cwd" in data["error"].lower()

    async def test_empty_cwd(self):
        async with client() as cli:
            resp = await cli.post("/api/sessions/launch",
                                  json={"machine": "mac-mini", "cwd": ""})
            data = await resp.json()
        assert resp.status == 400

    async def test_missing_machine_defaults_to_local(self):
        with patch("src.server.launch_terminal",
                   new=AsyncMock(return_value={"ok": True})):
            async with client(local_machine="mac-mini") as cli:
                resp = await cli.post("/api/sessions/launch",
                                      json={"cwd": "/tmp/proj", "mode": "terminal"})
        assert resp.status == 200

    async def test_invalid_json(self):
        async with client() as cli:
            resp = await cli.post("/api/sessions/launch",
                                  data="<<not json>>",
                                  headers={"Content-Type": "application/json"})
            data = await resp.json()
        assert resp.status == 400
        assert "json" in data["error"].lower()

    async def test_unknown_machine_does_not_crash(self):
        with patch("src.server.launch_terminal",
                   new=AsyncMock(return_value={"ok": True})):
            async with client() as cli:
                resp = await cli.post("/api/sessions/launch",
                                      json={"machine": "ghost-machine",
                                            "cwd": "/tmp/x",
                                            "mode": "terminal"})
                body = await resp.text()
        assert resp.status in (200, 500)
        try:
            _json.loads(body)
        except Exception:
            pytest.fail(f"unknown-machine response not JSON: {body[:200]!r}")

    async def test_resume_session_id_with_trailing_whitespace(self):
        with patch("src.server.launch_claude_session",
                   new=AsyncMock(return_value={"ok": True})), \
             patch("src.server.launch_terminal",
                   new=AsyncMock(return_value={"ok": True})):
            async with client() as cli:
                resp = await cli.post(
                    "/api/sessions/launch",
                    json={"machine": "mac-mini", "cwd": "/tmp/x",
                          "session_id": "   ", "mode": "terminal"},
                )
        assert resp.status == 200


# ---------------------------------------------------------------------------
# /api/tmux/create -- "Create new tmux" modal path
# ---------------------------------------------------------------------------

class TestTmuxCreateEdges:
    async def test_happy_path_with_auto_name(self):
        with patch("src.server.create_tmux_session",
                   new=AsyncMock(return_value={"ok": True, "name": "x"})), \
             patch("src.server.list_all_tmux", new=AsyncMock(return_value=[])):
            async with client() as cli:
                resp = await cli.post("/api/tmux/create",
                                      json={"machine": "mac-mini", "cwd": "/tmp/x"})
                data = await resp.json()
        assert resp.status == 200
        assert data["ok"] is True

    async def test_missing_machine(self):
        async with client() as cli:
            resp = await cli.post("/api/tmux/create", json={"cwd": "/tmp/x"})
            data = await resp.json()
        assert resp.status == 400
        assert "machine" in data["error"].lower()

    async def test_no_name_and_no_cwd(self):
        async with client() as cli:
            resp = await cli.post("/api/tmux/create", json={"machine": "mac-mini"})
            data = await resp.json()
        assert resp.status == 400

    async def test_invalid_json(self):
        async with client() as cli:
            resp = await cli.post("/api/tmux/create",
                                  data="not json",
                                  headers={"Content-Type": "application/json"})
        assert resp.status == 400

    async def test_remote_offline_surfaces_ssh_error(self):
        with patch("src.server.create_tmux_session",
                   new=AsyncMock(return_value={"ok": False, "error":
                        "ssh: connect to host 192.168.7.101 port 22: Operation timed out"})):
            async with client() as cli:
                resp = await cli.post("/api/tmux/create",
                                      json={"machine": "windows-desktop",
                                            "name": "test"})
                data = await resp.json()
        assert resp.status == 500
        assert "timed out" in data["error"].lower() or "connect" in data["error"].lower()


# ---------------------------------------------------------------------------
# /api/tmux/connect -- "Attach" button path
# ---------------------------------------------------------------------------

class TestTmuxConnectEdges:
    async def test_happy_path(self):
        with patch("src.server.launch_tmux_attach",
                   new=AsyncMock(return_value={"ok": True})):
            async with client() as cli:
                resp = await cli.post("/api/tmux/connect",
                                      json={"machine": "mac-mini",
                                            "session_name": "work"})
        assert resp.status == 200

    async def test_missing_machine(self):
        async with client() as cli:
            resp = await cli.post("/api/tmux/connect",
                                  json={"session_name": "work"})
        assert resp.status == 400

    async def test_missing_session_name(self):
        async with client() as cli:
            resp = await cli.post("/api/tmux/connect",
                                  json={"machine": "mac-mini"})
        assert resp.status == 400

    async def test_launcher_raising_returns_clean_500(self):
        async def boom(*a, **kw):
            raise OSError("ssh binary not found")
        with patch("src.server.launch_tmux_attach", side_effect=boom):
            async with client() as cli:
                resp = await cli.post("/api/tmux/connect",
                                      json={"machine": "mac-mini",
                                            "session_name": "work"})
                body = await resp.text()
        _assert_json_500(resp.status, body)


# ---------------------------------------------------------------------------
# /api/tmux/connect-remote -- "Remote attach" button path
# ---------------------------------------------------------------------------

class TestTmuxConnectRemoteEdges:
    async def test_happy_path(self):
        with patch("src.server.launch_tmux_attach_remote",
                   new=AsyncMock(return_value={"ok": True})):
            async with client() as cli:
                resp = await cli.post("/api/tmux/connect-remote",
                                      json={"machine": "ubuntu-desktop",
                                            "session_name": "work"})
        assert resp.status == 200

    async def test_missing_fields(self):
        async with client() as cli:
            resp = await cli.post("/api/tmux/connect-remote", json={})
        assert resp.status == 400

    async def test_remote_offline_returns_500_with_error_json(self):
        with patch(
            "src.server.launch_tmux_attach_remote",
            new=AsyncMock(return_value={"ok": False, "error":
                 "ssh: connect to host 192.168.7.101: Operation timed out"}),
        ):
            async with client() as cli:
                resp = await cli.post("/api/tmux/connect-remote",
                                      json={"machine": "windows-desktop",
                                            "session_name": "work"})
                data = await resp.json()
        assert resp.status == 500
        assert data["ok"] is False
        assert "timed out" in data["error"].lower() or "connect" in data["error"].lower()

    async def test_launcher_raising_returns_clean_500(self):
        async def boom(*a, **kw):
            raise RuntimeError("unexpected launch_remote_terminal failure")
        with patch("src.server.launch_tmux_attach_remote", side_effect=boom):
            async with client() as cli:
                resp = await cli.post("/api/tmux/connect-remote",
                                      json={"machine": "ubuntu-desktop",
                                            "session_name": "work"})
                body = await resp.text()
        _assert_json_500(resp.status, body)


# ---------------------------------------------------------------------------
# Full matrix sanity -- every (machine, mode, session_id?) combination succeeds
# when the relevant launcher returns ok. Catches any routing miss that would
# fall into an unhandled branch in handle_sessions_launch.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("machine,cwd", [
    ("mac-mini",        "/tmp/x"),
    ("ubuntu-desktop",  "/home/rbgnr/x"),
    ("avell-i7",        "C:\\Users\\rbgnr\\x"),
])
@pytest.mark.parametrize("mode", ["terminal", "tmux"])
@pytest.mark.parametrize("session_id", ["", "abc-123"])
async def test_every_launch_combo_returns_200_when_launcher_ok(
    machine, cwd, mode, session_id,
):
    patches = [
        patch("src.server.launch_terminal",
              new=AsyncMock(return_value={"ok": True})),
        patch("src.server.launch_new_tmux_and_attach",
              new=AsyncMock(return_value={"ok": True})),
        patch("src.server.launch_claude_session",
              new=AsyncMock(return_value={"ok": True})),
        patch("src.server._pool_exec",
              new=AsyncMock(return_value=(0, b"", b""))),
        patch("src.server.list_all_tmux",
              new=AsyncMock(return_value=[])),
    ]
    for p in patches:
        p.start()
    try:
        async with client(local_machine="mac-mini") as cli:
            resp = await cli.post(
                "/api/sessions/launch",
                json={"machine": machine, "cwd": cwd, "mode": mode,
                      "session_id": session_id, "skip_permissions": False},
            )
            body = await resp.text()
        assert resp.status == 200, (
            f"machine={machine} mode={mode} session_id={session_id!r} -> "
            f"{resp.status}: {body[:200]}"
        )
    finally:
        for p in patches:
            p.stop()
