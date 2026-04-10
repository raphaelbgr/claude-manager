"""
aiohttp web server for claude-manager Phase 1.

Provides REST endpoints and a WebSocket channel for session/fleet data.
A background asyncio task refreshes data every SCAN_INTERVAL seconds
and pushes diffs to WebSocket subscribers.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import pathlib
import platform
import subprocess
import sys
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any

PREFS_FILE = pathlib.Path(__file__).parent.parent / ".claude-manager-prefs.json"


def _load_prefs() -> dict:
    try:
        return json.loads(PREFS_FILE.read_text())
    except Exception:
        return {"skip_permissions": False}


def _save_prefs(prefs: dict) -> None:
    PREFS_FILE.write_text(json.dumps(prefs, indent=2))

from aiohttp import web

import shlex

from .command_adapter import get_adapter
from .config import DEFAULT_BIND, DEFAULT_PORT, FLEET_MACHINES, SCAN_INTERVAL, detect_local_machine
from .fleet import discover_fleet
from .launcher import launch_claude_session, launch_tmux_attach, launch_tmux_attach_remote, launch_new_tmux_and_attach, launch_terminal, _ssh_path_prefix
from .scanner import ClaudeSession, scan_all
from .tmux_manager import TmuxSession, list_all_tmux, create_tmux_session, kill_tmux_session

log = logging.getLogger("claude_manager.server")


# ---------------------------------------------------------------------------
# In-memory log ring buffer
# ---------------------------------------------------------------------------

class MemoryLogHandler(logging.Handler):
    """Captures log records in a ring buffer for the /api/logs endpoint."""

    def __init__(self, max_entries: int = 500):
        super().__init__()
        self.buffer: deque[dict] = deque(maxlen=max_entries)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.buffer.append({
                "timestamp": datetime.fromtimestamp(record.created).astimezone().isoformat(),
                "level": record.levelname,
                "module": record.name.split(".")[-1],
                "message": self.format(record),
            })
        except Exception:
            pass

    def get_logs(self, limit: int = 100, level: str | None = None) -> list[dict]:
        logs = list(self.buffer)
        if level:
            logs = [l for l in logs if l["level"] == level.upper()]
        return logs[-limit:]


# ---------------------------------------------------------------------------
# CORS middleware
# ---------------------------------------------------------------------------

@web.middleware
async def cors_middleware(request: web.Request, handler) -> web.Response:
    if request.method == "OPTIONS":
        return web.Response(
            status=204,
            headers={
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
                "Access-Control-Allow-Headers": "Content-Type, Authorization",
            },
        )
    response = await handler(request)
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    return response


# ---------------------------------------------------------------------------
# Auth middleware — SSH-key-derived bearer token
# ---------------------------------------------------------------------------

# Paths exempt from auth (even when enabled). Public metadata + static files only.
_AUTH_EXEMPT_PATHS = {
    "/",
    "/health",
    "/api/auth/config",
    "/api/update/check",
}
_AUTH_EXEMPT_PREFIXES = ("/static/",)


def _is_auth_exempt(path: str) -> bool:
    if path in _AUTH_EXEMPT_PATHS:
        return True
    return any(path.startswith(p) for p in _AUTH_EXEMPT_PREFIXES)


@web.middleware
async def auth_middleware(request: web.Request, handler) -> web.Response:
    """Enforce Bearer-token auth on /api/* routes when auth is enabled.

    Rules:
      - OPTIONS (CORS preflight): always allowed
      - Exempt paths (/, /health, /api/auth/config, /static/*): always allowed
      - Loopback clients (127.0.0.1, ::1): always allowed
      - Everything else: require 'Authorization: Bearer <token>' matching the
        configured token
    """
    from .auth import extract_bearer_token, is_loopback

    if request.method == "OPTIONS":
        return await handler(request)

    auth_cfg = request.app.get("auth_config")
    if not auth_cfg or not auth_cfg.enabled:
        return await handler(request)

    if _is_auth_exempt(request.path):
        return await handler(request)

    # Loopback bypass — local clients are trusted
    if is_loopback(request.remote):
        return await handler(request)

    # WebSocket auth is handled inside handle_ws (first message must be auth)
    if request.path == "/ws":
        return await handler(request)

    provided = extract_bearer_token(request.headers.get("Authorization"))
    if provided != auth_cfg.token:
        return web.json_response(
            {"ok": False, "error": "unauthorized — invalid or missing bearer token"},
            status=401,
        )

    return await handler(request)


# ---------------------------------------------------------------------------
# Rate limiting — token bucket per (IP, route)
# ---------------------------------------------------------------------------

# Per-route limits: (max_requests, window_seconds)
_RATE_LIMITS: dict[str, tuple[int, float]] = {
    "/api/sessions/launch": (5, 60.0),
    "/api/tmux/create": (10, 60.0),
    "/api/tmux/connect": (10, 60.0),
    "/api/sessions/scan": (4, 60.0),
    "/api/exit": (2, 300.0),
    "/api/restart": (3, 60.0),
}


def _rate_limit_check(app: web.Application, remote: str, path: str) -> tuple[bool, int]:
    """Return (allowed, retry_after_seconds). Uses a sliding window per (remote, path)."""
    limit = _RATE_LIMITS.get(path)
    if not limit:
        return True, 0
    max_req, window = limit
    now = time.monotonic()
    buckets = app.setdefault("rate_buckets", {})
    key = (remote or "unknown", path)
    timestamps: list[float] = buckets.setdefault(key, [])
    # Drop old entries
    cutoff = now - window
    while timestamps and timestamps[0] < cutoff:
        timestamps.pop(0)
    if len(timestamps) >= max_req:
        retry = int(window - (now - timestamps[0])) + 1
        return False, max(1, retry)
    timestamps.append(now)
    return True, 0


@web.middleware
async def rate_limit_middleware(request: web.Request, handler) -> web.Response:
    if request.method == "OPTIONS":
        return await handler(request)
    if request.path not in _RATE_LIMITS:
        return await handler(request)
    allowed, retry = _rate_limit_check(request.app, request.remote or "", request.path)
    if not allowed:
        return web.json_response(
            {"ok": False, "error": f"rate limit exceeded, retry in {retry}s"},
            status=429,
            headers={"Retry-After": str(retry)},
        )
    return await handler(request)


# ---------------------------------------------------------------------------
# State helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now().astimezone().isoformat()


def _sessions_by_machine(sessions: list[ClaudeSession]) -> dict[str, list[dict]]:
    """Group sessions by machine, then by project_folder within each machine."""
    by_machine: dict[str, dict[str, list[dict]]] = {}
    for s in sessions:
        m = by_machine.setdefault(s.machine, {})
        m.setdefault(s.project_folder, []).append(s.to_dict())

    result: dict[str, list[dict]] = {}
    for machine, projects in by_machine.items():
        result[machine] = [
            {"project_folder": pf, "sessions": sess}
            for pf, sess in projects.items()
        ]
    return result


# ---------------------------------------------------------------------------
# Background scan task
# ---------------------------------------------------------------------------

async def _background_scan(app: web.Application) -> None:
    """
    Periodically refresh fleet + session data.
    Pushes snapshots to all connected WebSocket clients on each refresh.

    First-scan behavior: waits up to 30s for the first WS client to connect
    BEFORE running. This ensures the initial scan_progress messages are
    actually delivered to the UI (otherwise the scan would complete before
    the desktop window is even open, and the UI would only see the snapshot).
    """
    local_machine = app["local_machine"]

    # Wait for first WS client (or 30s timeout) so the user sees first-scan progress
    log.info("background_scan: waiting for first WS client...")
    waited = 0.0
    while not app["state"]["ws_clients"] and waited < 30.0:
        await asyncio.sleep(0.2)
        waited += 0.2
    if app["state"]["ws_clients"]:
        log.info("background_scan: WS client connected after %.1fs, starting first scan", waited)
    else:
        log.info("background_scan: 30s timeout reached, scanning anyway")

    while True:
        t0 = time.monotonic()
        try:
            fleet = await discover_fleet()

            async def _emit_scan_progress(
                machine: str, found: int, total: int, current_file: str
            ) -> None:
                payload = json.dumps({
                    "type": "scan_progress",
                    "machine": machine,
                    "found": found,
                    "total": total,
                    "current_file": current_file,
                })
                dead: set = set()
                for ws in list(app["state"]["ws_clients"]):
                    try:
                        await ws.send_str(payload)
                    except Exception:
                        dead.add(ws)
                app["state"]["ws_clients"] -= dead

            sessions = await scan_all(local_machine, fleet, on_progress=_emit_scan_progress)
            tmux = await list_all_tmux(local_machine, fleet)
            app["state"]["fleet"] = fleet
            app["state"]["sessions"] = sessions
            app["state"]["tmux"] = tmux
            app["state"]["last_scan"] = _now_iso()
            elapsed = time.monotonic() - t0
            log.info(
                "background_scan: %d sessions, %d tmux, %d fleet machines in %.2fs",
                len(sessions), len(tmux), len(fleet), elapsed,
            )

            # Push to WebSocket subscribers
            await _push_to_ws(app, "sessions", [s.to_dict() for s in sessions])
            await _push_to_ws(app, "fleet", fleet)
            await _push_to_ws(app, "tmux", [t.to_dict() for t in tmux])
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("background scan failed: %s", exc)

        await asyncio.sleep(SCAN_INTERVAL)


async def _push_to_ws(
    app: web.Application,
    channel: str,
    data: Any,
) -> None:
    """Send an update message to all WebSocket clients subscribed to channel."""
    payload = json.dumps({"type": "update", "channel": channel, "data": data, "action": "refresh"})
    dead: set[web.WebSocketResponse] = set()
    for ws in list(app["state"]["ws_clients"]):
        subs: set[str] = getattr(ws, "_subscribed_channels", set())
        if channel in subs:
            try:
                await ws.send_str(payload)
            except Exception:
                dead.add(ws)
    app["state"]["ws_clients"] -= dead


# ---------------------------------------------------------------------------
# Startup / cleanup
# ---------------------------------------------------------------------------

async def on_startup(app: web.Application) -> None:
    app["bg_task"] = asyncio.ensure_future(_background_scan(app))


async def on_cleanup(app: web.Application) -> None:
    task: asyncio.Task = app.get("bg_task")
    if task:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    # Close all open WebSocket connections cleanly
    for ws in list(app["state"]["ws_clients"]):
        try:
            await ws.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# REST handlers
# ---------------------------------------------------------------------------

def _read_version_metadata() -> dict:
    """Read version from VERSION.json if present, fall back to live git."""
    import pathlib
    repo = pathlib.Path(__file__).resolve().parent.parent
    # Prefer committed VERSION.json — canonical fleet-wide standard
    try:
        version_file = repo / "VERSION.json"
        if version_file.is_file():
            data = json.loads(version_file.read_text())
            required = ("version", "commit", "commit_full", "branch", "date", "message")
            if all(k in data for k in required) \
                    and isinstance(data["version"], int) \
                    and all(isinstance(data[k], str) for k in required if k != "version"):
                return {
                    "version": data["version"],
                    "commit": data["commit"],
                    "commit_full": data["commit_full"],
                    "branch": data["branch"],
                    "date": data["date"],
                    "message": data["message"],
                }
    except Exception:
        pass
    # Fall back to live git — matches running code when VERSION.json is missing/stale
    try:
        import subprocess as _sp
        def _g(*args):
            return _sp.check_output(["git", *args], cwd=str(repo), text=True, timeout=3).strip()
        return {
            "version": int(_g("rev-list", "--count", "HEAD")),
            "commit": _g("rev-parse", "--short", "HEAD"),
            "commit_full": _g("rev-parse", "HEAD"),
            "branch": _g("rev-parse", "--abbrev-ref", "HEAD"),
            "date": _g("log", "-1", "--format=%cI"),
            "message": _g("log", "-1", "--format=%s"),
        }
    except Exception:
        pass
    return {"version": 0, "commit": "unknown"}


_VERSION_METADATA = _read_version_metadata()

# Cached GitHub upstream version check — avoid rate-limiting the unauth API
_update_check_cache: dict = {"data": None, "ts": 0.0}
_UPDATE_CHECK_TTL = 60.0  # seconds
_GITHUB_API_URL = "https://api.github.com/repos/raphaelbgr/claude-manager/commits/master"


async def _fetch_github_latest() -> dict | None:
    """Fetch the latest commit from GitHub. Returns a metadata dict or None on failure."""
    try:
        import aiohttp as _aiohttp
        async with _aiohttp.ClientSession() as session:
            async with session.get(
                _GITHUB_API_URL,
                headers={"Accept": "application/vnd.github.v3+json"},
                timeout=_aiohttp.ClientTimeout(total=5),
            ) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
        msg = (data.get("commit", {}).get("message") or "").split("\n", 1)[0][:120]
        return {
            "commit": data["sha"][:7],
            "commit_full": data["sha"],
            "date": data["commit"]["author"]["date"],
            "message": msg,
        }
    except Exception as exc:
        log.debug("github version fetch failed: %s", exc)
        return None


async def handle_update_check(request: web.Request) -> web.Response:
    """GET /api/update/check — compare local vs GitHub latest commit.

    Returns: {ok, current, latest, update_available}
    Cached for 60s to avoid GitHub rate limits.
    """
    now = time.monotonic()
    cached = _update_check_cache.get("data")
    if cached and (now - _update_check_cache["ts"]) < _UPDATE_CHECK_TTL:
        return web.json_response(cached)

    # Refresh current version from git (may have changed via out-of-band pull)
    global _VERSION_METADATA
    _VERSION_METADATA = _read_version_metadata()
    current = _VERSION_METADATA

    latest = await _fetch_github_latest()
    if latest is None:
        return web.json_response({
            "ok": False,
            "error": "failed to reach github",
            "current": current,
            "latest": None,
            "update_available": False,
        })

    update_available = bool(latest.get("commit_full") != current.get("commit_full"))

    result = {
        "ok": True,
        "current": current,
        "latest": latest,
        "update_available": update_available,
    }
    _update_check_cache["data"] = result
    _update_check_cache["ts"] = now
    return web.json_response(result)


async def handle_update_apply(request: web.Request) -> web.Response:
    """POST /api/update/apply — git pull + restart process.

    Loopback-only. Runs `git pull --ff-only`, updates the version cache,
    and schedules os.execv() so the response returns before the restart.
    The desktop window will close briefly and reopen with the new code.
    """
    from .auth import is_loopback
    if not is_loopback(request.remote):
        return web.json_response({"ok": False, "error": "loopback only"}, status=403)

    import pathlib as _pathlib
    repo = _pathlib.Path(__file__).parent.parent

    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "pull", "--ff-only",
            cwd=str(repo),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
    except asyncio.TimeoutError:
        return web.json_response({"ok": False, "error": "git pull timed out"}, status=504)
    except Exception as exc:
        return web.json_response({"ok": False, "error": str(exc)}, status=500)

    if proc.returncode != 0:
        err = stderr.decode("utf-8", errors="replace").strip()
        return web.json_response({
            "ok": False,
            "error": f"git pull failed: {err}",
        }, status=500)

    pull_output = stdout.decode("utf-8", errors="replace").strip()

    # Refresh version metadata and invalidate the GitHub cache
    global _VERSION_METADATA
    _VERSION_METADATA = _read_version_metadata()
    _update_check_cache["data"] = None
    _update_check_cache["ts"] = 0.0

    log.info("update: git pull ok — %s", pull_output.splitlines()[-1] if pull_output else "no output")

    # Schedule the restart AFTER we return the response
    async def _delayed_restart():
        await asyncio.sleep(0.8)
        log.info("update: restarting process via os.execv")
        os.execv(sys.executable, [sys.executable, *sys.argv])

    asyncio.ensure_future(_delayed_restart())

    return web.json_response({
        "ok": True,
        "pulled": pull_output,
        "new_version": _VERSION_METADATA,
        "restarting": True,
    })


async def handle_health(request: web.Request) -> web.Response:
    state = request.app["state"]
    sessions: list[ClaudeSession] = state["sessions"]
    fleet: dict = state["fleet"]
    # Detect LAN IP for Web Access URL
    local_ip = "localhost"
    try:
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("192.168.7.1", 80))
        local_ip = s.getsockname()[0]
        s.close()
    except Exception:
        pass
    return web.json_response(
        {
            "status": "ok",
            "port": request.app["port"],
            "local_machine": request.app.get("local_machine"),
            "local_ip": local_ip,
            "machines": len(fleet),
            "sessions": len(sessions),
            "last_scan": state["last_scan"],
            "version": _VERSION_METADATA,
        }
    )


async def handle_auth_config(request: web.Request) -> web.Response:
    """GET /api/auth/config — return current auth status (no secret leaked).

    Returns: {enabled, key_path, bind, available_keys}
    """
    from .auth import list_available_pubkeys, is_loopback
    auth_cfg = request.app.get("auth_config")
    available = [str(p) for p in list_available_pubkeys()]
    return web.json_response({
        "enabled": bool(auth_cfg and auth_cfg.enabled),
        "key_path": str(auth_cfg.key_path) if auth_cfg and auth_cfg.key_path else None,
        "bind": request.app.get("bind", "127.0.0.1"),
        "available_keys": available,
        "loopback": is_loopback(request.remote),
    })


async def handle_auth_update(request: web.Request) -> web.Response:
    """POST /api/auth/update — enable/disable auth, pick key file.

    Restricted to loopback clients (you must be on the server machine).
    Body: {"enabled": bool, "key_path": "/path/to/id_rsa.pub"}
    Returns the new auth status. Requires restart to take effect.
    """
    from .auth import is_loopback, save_auth_config, compute_token
    import pathlib as _pl

    if not is_loopback(request.remote):
        return web.json_response(
            {"ok": False, "error": "auth config can only be changed from loopback"},
            status=403,
        )

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "invalid JSON body"}, status=400)

    enabled = bool(body.get("enabled", False))
    key_path_str = body.get("key_path") or ""
    key_path = _pl.Path(key_path_str).expanduser() if key_path_str else None

    if enabled:
        if not key_path or not key_path.is_file():
            return web.json_response(
                {"ok": False, "error": f"key file not found: {key_path}"},
                status=400,
            )
        try:
            _ = compute_token(key_path)
        except Exception as exc:
            return web.json_response(
                {"ok": False, "error": f"cannot read key: {exc}"},
                status=400,
            )

    new_cfg = save_auth_config(enabled, key_path if enabled else None)
    # Update in-memory config so the change takes effect immediately
    request.app["auth_config"] = new_cfg
    log.info("auth: updated enabled=%s key=%s", new_cfg.enabled, new_cfg.key_path)
    return web.json_response({
        "ok": True,
        "enabled": new_cfg.enabled,
        "key_path": str(new_cfg.key_path) if new_cfg.key_path else None,
    })


async def handle_auth_token(request: web.Request) -> web.Response:
    """GET /api/auth/token — return the active token (loopback only).

    Desktop app reads this to inject into localStorage. Never exposed off-machine.
    """
    from .auth import is_loopback
    if not is_loopback(request.remote):
        return web.json_response({"ok": False, "error": "loopback only"}, status=403)
    auth_cfg = request.app.get("auth_config")
    if not auth_cfg or not auth_cfg.enabled or not auth_cfg.token:
        return web.json_response({"ok": True, "enabled": False, "token": None})
    return web.json_response({"ok": True, "enabled": True, "token": auth_cfg.token})


async def handle_sessions_all(request: web.Request) -> web.Response:
    state = request.app["state"]
    sessions: list[ClaudeSession] = state["sessions"]
    return web.json_response(_sessions_by_machine(sessions))


async def handle_sessions_machine(request: web.Request) -> web.Response:
    machine = request.match_info["machine"]
    state = request.app["state"]
    sessions: list[ClaudeSession] = state["sessions"]
    filtered = [s for s in sessions if s.machine == machine]
    return web.json_response(_sessions_by_machine(filtered).get(machine, []))


async def handle_sessions_scan(request: web.Request) -> web.Response:
    """Force an immediate rescan and return fresh results."""
    app = request.app
    local_machine = app["local_machine"]
    t0 = time.monotonic()
    try:
        fleet = await discover_fleet()
        sessions = await scan_all(local_machine, fleet)
        tmux = await list_all_tmux(local_machine, fleet)
        app["state"]["fleet"] = fleet
        app["state"]["sessions"] = sessions
        app["state"]["tmux"] = tmux
        app["state"]["last_scan"] = _now_iso()
        await _push_to_ws(app, "sessions", [s.to_dict() for s in sessions])
        await _push_to_ws(app, "fleet", fleet)
        await _push_to_ws(app, "tmux", [t.to_dict() for t in tmux])
        log.info("POST /api/sessions/scan: %d sessions, %.2fs", len(sessions), time.monotonic() - t0)
        return web.json_response(
            {
                "ok": True,
                "sessions": [s.to_dict() for s in sessions],
                "tmux": [t.to_dict() for t in tmux],
                "last_scan": app["state"]["last_scan"],
            }
        )
    except Exception as exc:
        log.exception("POST /api/sessions/scan: failed after %.2fs: %s", time.monotonic() - t0, exc)
        return web.json_response({"ok": False, "error": str(exc)}, status=500)


async def handle_sessions_launch(request: web.Request) -> web.Response:
    """Launch a terminal for a Claude Code session."""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "invalid JSON body"}, status=400)
    machine = body.get("machine", "")
    session_id = body.get("session_id", "")
    cwd = body.get("cwd", "")
    if not cwd:
        return web.json_response({"ok": False, "error": "cwd required"}, status=400)
    skip = body.get("skip_permissions", False)
    mode = body.get("mode", "terminal")
    t0 = time.monotonic()

    # mode='tmux' runs the create-session + auto-claude + attach path for
    # BOTH existing Claude sessions (session_id set → claude --resume <id>)
    # and brand-new sessions (session_id empty → plain claude). The same
    # function handles "Tmux launch" from a session row AND "New <mux> in
    # project" from a project row — no duplicate code paths.
    if mode == "tmux":
        import re
        local_machine = request.app["local_machine"]
        if not machine:
            machine = local_machine
        adapter = get_adapter(machine)
        is_remote_windows = (machine != local_machine and adapter.mux_type == "psmux")

        # Build the command that gets typed into the pane after cd. When
        # session_id is set we resume; otherwise we just run `claude` (fresh
        # conversation). build_session_command already chains cd && claude.
        if session_id:
            claude_cmd = adapter.build_session_command(cwd, session_id, skip)
        else:
            cd = adapter.cd_command(cwd)
            fresh = "claude --dangerously-skip-permissions" if skip else "claude"
            claude_cmd = adapter.chain_commands(cd, fresh)

        project = cwd.replace("\\", "/").rstrip("/").split("/")[-1] if cwd else "claude"
        project_safe = re.sub(r"[^a-zA-Z0-9_-]", "-", project) or "claude"
        existing_names = [t.name for t in request.app["state"]["tmux"] if t.machine == machine]
        safe_name = adapter.generate_mux_session_name(machine, project_safe, existing_names)

        if is_remote_windows:
            # Windows: server creates psmux session + sends cd+claude via send-keys.
            # Then open terminal: SSH -t powershell → psmux attach.
            from .tmux_manager import create_tmux_session
            create_result = await create_tmux_session(machine, safe_name, cwd=cwd, command=claude_cmd)
            if not create_result.get("ok"):
                result = create_result
            else:
                # Open terminal: SSH lands directly in PowerShell (fleet-wide
                # default, see global CLAUDE.md Windows SSH Shell Policy).
                # No need for an intermediate 'powershell' step — just SSH
                # then psmux attach.
                from .launcher import _launch_macos_multi
                info = FLEET_MACHINES.get(machine, {})
                alias = info.get("ssh_alias", machine)
                attach_cmd = adapter.mux_attach(safe_name)
                result = await _launch_macos_multi(
                    [
                        f"ssh {alias}",    # SSH into Windows → PowerShell
                        attach_cmd,         # psmux attach -t session-name
                    ],
                    delays=[0, 2],
                )
        else:
            # tmux on macOS/Linux: create session + attach works perfectly
            result = await launch_new_tmux_and_attach(safe_name, machine, cwd=cwd, command=claude_cmd)
    elif not session_id:
        # Terminal-mode new session (no tmux): cd to cwd and start a fresh claude
        local_machine = request.app["local_machine"]
        if not machine:
            machine = local_machine
        adapter = get_adapter(machine)
        is_local = (machine == local_machine)

        if is_local:
            # Local: use the target shell (cmd for psmux, bash for tmux)
            cd_cmd = adapter.cd_command(cwd)
            claude_cmd = "claude"
            if skip:
                claude_cmd += " --dangerously-skip-permissions"
            full_cmd = adapter.chain_commands(cd_cmd, claude_cmd)
            result = await launch_terminal(full_cmd)
        else:
            # Remote: build command in the SSH landing shell syntax
            # (PowerShell for Windows, bash for Linux/macOS)
            from .config import FLEET_MACHINES as _FM
            info = _FM.get(machine, {})
            alias = info.get("ssh_alias", machine)
            full_cmd = adapter.build_new_session_command_ssh(cwd, skip_permissions=skip)
            ssh_cmd = _ssh_path_prefix(machine) + full_cmd
            terminal_cmd = f"ssh {shlex.quote(alias)} -t {shlex.quote(ssh_cmd)}"
            result = await launch_terminal(terminal_cmd)

        status = 200 if result.get("ok") else 500
        log.info("POST /api/sessions/launch NEW machine=%s %d", machine, status)
        return web.json_response(result, status=status)
    else:
        result = await launch_claude_session(cwd, session_id, machine, skip_permissions=skip)
    status = 200 if result.get("ok") else 500
    elapsed = time.monotonic() - t0
    if result.get("ok"):
        log.info("POST /api/sessions/launch machine=%s mode=%s %d %.2fs", machine, mode, status, elapsed)
    else:
        log.error("POST /api/sessions/launch machine=%s mode=%s %d %.2fs: %s", machine, mode, status, elapsed, result.get("error"))
    # If tmux mode succeeded, refresh the tmux list immediately
    if mode == "tmux" and result.get("ok"):
        try:
            local_machine = request.app["local_machine"]
            fleet = request.app["state"]["fleet"]
            tmux = await list_all_tmux(local_machine, fleet)
            request.app["state"]["tmux"] = tmux
            await _push_to_ws(request.app, "tmux", [t.to_dict() for t in tmux])
        except Exception:
            pass
    return web.json_response(result, status=status)


async def handle_fleet(request: web.Request) -> web.Response:
    fleet = request.app["state"]["fleet"]
    return web.json_response(fleet)


async def handle_tmux(request: web.Request) -> web.Response:
    """Return all tmux sessions across fleet."""
    tmux: list[TmuxSession] = request.app["state"]["tmux"]
    return web.json_response([t.to_dict() for t in tmux])


async def handle_tmux_machine(request: web.Request) -> web.Response:
    """Return tmux sessions for a specific machine."""
    machine = request.match_info["machine"]
    tmux: list[TmuxSession] = request.app["state"]["tmux"]
    filtered = [t.to_dict() for t in tmux if t.machine == machine]
    return web.json_response(filtered)


async def handle_tmux_create(request: web.Request) -> web.Response:
    """Create a new tmux session.

    Body: {machine, cwd?, name?, command?}
    If 'name' is omitted, auto-generates a unique name from cwd using the
    adapter's generate_mux_session_name (auto-incrementing -session-NN suffix).
    """
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "invalid JSON body"}, status=400)
    machine = body.get("machine", "")
    name = body.get("name", "")
    cwd = body.get("cwd", "")
    if not machine:
        return web.json_response({"ok": False, "error": "machine required"}, status=400)

    # Auto-generate a unique name if none provided (or sanitize the provided one)
    if not name:
        if not cwd:
            return web.json_response({"ok": False, "error": "name or cwd required"}, status=400)
        adapter = get_adapter(machine)
        import re as _re
        project = cwd.replace("\\", "/").rstrip("/").split("/")[-1] or "session"
        project_safe = _re.sub(r"[^a-zA-Z0-9_-]", "-", project) or "session"
        existing_names = [t.name for t in request.app["state"]["tmux"] if t.machine == machine]
        name = adapter.generate_mux_session_name(machine, project_safe, existing_names)

    # Sanitize: tmux/psmux reject / and \ in session names
    name = name.replace("/", "_").replace("\\", "_")
    result = await create_tmux_session(machine, name, cwd or None, body.get("command"))
    status = 200 if result.get("ok") else 500
    if result.get("ok"):
        log.info("POST /api/tmux/create machine=%s name=%s %d", machine, name, status)
        local_machine = request.app["local_machine"]
        fleet = request.app["state"]["fleet"]
        tmux = await list_all_tmux(local_machine, fleet)
        request.app["state"]["tmux"] = tmux
        await _push_to_ws(request.app, "tmux", [t.to_dict() for t in tmux])
    else:
        log.error("POST /api/tmux/create machine=%s name=%s %d: %s", machine, name, status, result.get("error"))
    return web.json_response(result, status=status)


async def handle_tmux_connect(request: web.Request) -> web.Response:
    """Connect to an existing tmux session (opens terminal).

    Before attaching, launch_tmux_attach probes the pane and auto-starts
    `claude` if the session is idle at a shell prompt — so 'Attach' always
    lands inside a running claude, even for legacy sessions that were
    created without one.
    """
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "invalid JSON body"}, status=400)
    machine = body.get("machine", "")
    session_name = body.get("session_name", "")
    skip_permissions = bool(body.get("skip_permissions", False))
    if not machine or not session_name:
        return web.json_response({"ok": False, "error": "machine and session_name required"}, status=400)
    result = await launch_tmux_attach(session_name, machine, skip_permissions=skip_permissions)
    status = 200 if result.get("ok") else 500
    if result.get("ok"):
        log.info("POST /api/tmux/connect machine=%s session=%s %d", machine, session_name, status)
    else:
        log.error("POST /api/tmux/connect machine=%s session=%s %d: %s", machine, session_name, status, result.get("error"))
    return web.json_response(result, status=status)


async def handle_tmux_connect_remote(request: web.Request) -> web.Response:
    """Open a terminal ON THE REMOTE MACHINE attached to a tmux session."""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "invalid JSON body"}, status=400)
    machine = body.get("machine", "")
    session_name = body.get("session_name", "")
    if not machine or not session_name:
        return web.json_response({"ok": False, "error": "machine and session_name required"}, status=400)
    result = await launch_tmux_attach_remote(session_name, machine)
    status = 200 if result.get("ok") else 500
    return web.json_response(result, status=status)


async def handle_tmux_kill(request: web.Request) -> web.Response:
    """Kill a tmux session."""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "invalid JSON body"}, status=400)
    machine = body.get("machine", "")
    name = body.get("name", "")
    if not machine or not name:
        return web.json_response({"ok": False, "error": "machine and name required"}, status=400)
    result = await kill_tmux_session(machine, name)
    status = 200 if result.get("ok") else 500
    if result.get("ok"):
        log.info("POST /api/tmux/kill machine=%s name=%s %d", machine, name, status)
        local_machine = request.app["local_machine"]
        fleet = request.app["state"]["fleet"]
        tmux = await list_all_tmux(local_machine, fleet)
        request.app["state"]["tmux"] = tmux
        await _push_to_ws(request.app, "tmux", [t.to_dict() for t in tmux])
    else:
        log.error("POST /api/tmux/kill machine=%s name=%s %d: %s", machine, name, status, result.get("error"))
    return web.json_response(result, status=status)


# ---------------------------------------------------------------------------
# Preferences handlers
# ---------------------------------------------------------------------------

def _get_local_drive(path: str) -> str:
    """Return the mount point / drive root that contains the given path."""
    import sys as _sys
    import pathlib as _pathlib
    if _sys.platform == "win32":
        return _pathlib.Path(path).anchor  # e.g. "C:\"
    # Unix: find longest matching mountpoint
    try:
        import psutil as _psutil
        parts = _psutil.disk_partitions(all=False)
        mounts = sorted([p.mountpoint for p in parts], key=len, reverse=True)
        for mp in mounts:
            if path.startswith(mp):
                return mp
    except Exception:
        pass
    return "/"


def _get_local_drives() -> list[dict]:
    """Return list of drives/volumes on the local machine."""
    import sys as _sys
    import psutil as _psutil

    _SKIP = {"devfs", "autofs", "tmpfs", "sysfs", "proc", "cgroup", "overlay",
              "squashfs", "efivarfs", "securityfs", "pstore", "bpf", "tracefs",
              "debugfs", "hugetlbfs", "mqueue", "fusectl", "configfs"}

    drives = []
    seen = set()
    for part in _psutil.disk_partitions(all=False):
        mp = part.mountpoint
        if mp in seen:
            continue
        if part.fstype in _SKIP:
            continue
        # Skip macOS system/virtual mounts
        if _sys.platform == "darwin":
            if mp.startswith("/dev") or mp.startswith("/private/var/vm"):
                continue
        # Skip Linux virtual/system mounts
        if _sys.platform.startswith("linux"):
            skip_prefixes = ("/proc", "/sys", "/dev", "/run", "/snap")
            if any(mp.startswith(pf) for pf in skip_prefixes):
                continue
        try:
            usage = _psutil.disk_usage(mp)
        except Exception:
            continue
        seen.add(mp)
        # Derive a short name
        if _sys.platform == "win32":
            name = part.device.rstrip("\\")  # "C:", "D:", etc.
            label = name
        elif mp == "/":
            name = "Macintosh HD" if _sys.platform == "darwin" else "Root"
            label = name
        else:
            name = mp.rstrip("/").split("/")[-1] or mp
            label = name
        is_system = (mp == "/") or (_sys.platform == "win32" and part.device.upper().startswith("C:"))
        drives.append({
            "path": mp,
            "name": name,
            "label": label,
            "total_gb": round(usage.total / 1e9, 1),
            "free_gb": round(usage.free / 1e9, 1),
            "is_system": is_system,
        })

    drives.sort(key=lambda d: (not d["is_system"], d["path"]))
    return drives


async def handle_drives(request: web.Request) -> web.Response:
    """List drives/volumes on a machine."""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "invalid JSON body"}, status=400)

    machine = body.get("machine", "")
    from .config import FLEET_MACHINES
    local_machine = request.app["local_machine"]
    is_local = (not machine) or (machine == local_machine)

    if is_local:
        try:
            drives = _get_local_drives()
            return web.json_response({"ok": True, "drives": drives})
        except Exception as exc:
            log.exception("local drives failed")
            return web.json_response({"ok": False, "error": str(exc)}, status=500)

    info = FLEET_MACHINES.get(machine)
    if not info:
        return web.json_response({"ok": False, "error": f"Unknown machine: {machine}"}, status=400)
    ssh_alias = info.get("ssh_alias", machine)

    # Try dispatch daemon API first
    dispatch_port = info.get("dispatch_port")
    ip = info.get("ip", "")
    if dispatch_port and ip:
        try:
            import aiohttp as _aiohttp
            url = f"http://{ip}:{dispatch_port}/drives"
            async with _aiohttp.ClientSession() as sess:
                async with sess.get(url, timeout=_aiohttp.ClientTimeout(total=8)) as resp:
                    if resp.status == 200:
                        result = await resp.json()
                        if result.get("drives"):
                            result["ok"] = True
                            return web.json_response(result)
        except Exception as exc:
            log.debug("drives API failed for %s: %s, trying SSH", machine, exc)

    py_script = r"""
import json, sys
try:
    import psutil
    SKIP = {"devfs","autofs","tmpfs","sysfs","proc","cgroup","overlay","squashfs",
            "efivarfs","securityfs","pstore","bpf","tracefs","debugfs","hugetlbfs",
            "mqueue","fusectl","configfs"}
    drives = []
    seen = set()
    for part in psutil.disk_partitions(all=False):
        mp = part.mountpoint
        if mp in seen or part.fstype in SKIP:
            continue
        if sys.platform == "darwin" and (mp.startswith("/dev") or mp.startswith("/private/var/vm")):
            continue
        if sys.platform.startswith("linux"):
            if any(mp.startswith(pf) for pf in ("/proc","/sys","/dev","/run","/snap")):
                continue
        try:
            u = psutil.disk_usage(mp)
        except Exception:
            continue
        seen.add(mp)
        if sys.platform == "win32":
            name = part.device.rstrip("\\")
        elif mp == "/":
            name = "Macintosh HD" if sys.platform == "darwin" else "Root"
        else:
            name = mp.rstrip("/").split("/")[-1] or mp
        is_sys = (mp == "/") or (sys.platform == "win32" and part.device.upper().startswith("C:"))
        drives.append({"path": mp, "name": name, "label": name,
                       "total_gb": round(u.total/1e9, 1), "free_gb": round(u.free/1e9, 1),
                       "is_system": is_sys})
    drives.sort(key=lambda d: (not d["is_system"], d["path"]))
    print(json.dumps({"drives": drives}))
except ImportError:
    import subprocess
    drives = []
    if sys.platform == "win32":
        # Windows: use wmic to get drive info
        try:
            out = subprocess.check_output(
                ["wmic", "logicaldisk", "get", "caption,freespace,size,volumename", "/format:csv"],
                text=True, timeout=10)
            for ln in out.strip().splitlines()[1:]:
                parts = [p.strip() for p in ln.split(",")]
                if len(parts) < 5 or not parts[1]:
                    continue
                caption = parts[1]  # C:
                free = int(parts[2]) if parts[2] else 0
                total = int(parts[3]) if parts[3] else 0
                vol = parts[4] or caption
                path = caption + "\\\\"
                is_sys = caption.upper() == "C:"
                drives.append({"path": path, "name": f"{caption} ({vol})", "label": vol,
                               "total_gb": round(total/1e9, 1), "free_gb": round(free/1e9, 1),
                               "is_system": is_sys})
        except Exception:
            pass
    else:
        # Unix fallback: df -Pl
        try:
            lines = subprocess.check_output(["df","-Pl"], text=True).splitlines()[1:]
            for ln in lines:
                parts = ln.split()
                if len(parts) < 6:
                    continue
                mp = parts[5]
                if any(mp.startswith(pf) for pf in ("/proc","/sys","/dev","/run")):
                    continue
                try:
                    total = int(parts[1]) * 1024
                    avail = int(parts[3]) * 1024
                except Exception:
                    continue
                name = "Root" if mp == "/" else mp.rstrip("/").split("/")[-1] or mp
                drives.append({"path": mp, "name": name, "label": name,
                               "total_gb": round(total/1e9, 1), "free_gb": round(avail/1e9, 1),
                               "is_system": mp == "/"})
        except Exception:
            pass
    drives.sort(key=lambda d: (not d["is_system"], d["path"]))
    print(json.dumps({"drives": drives}))
"""

    proc = await asyncio.create_subprocess_exec(
        "ssh", "-o", "ConnectTimeout=5", "-o", "BatchMode=yes", ssh_alias,
        "python3", "-",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(input=py_script.encode()), timeout=15)
    except asyncio.TimeoutError:
        proc.kill()
        return web.json_response({"ok": False, "error": "SSH timeout"}, status=504)

    if proc.returncode != 0:
        err = stderr.decode().strip()
        return web.json_response({"ok": False, "error": err or "SSH command failed"}, status=500)

    try:
        result = json.loads(stdout.decode().strip())
        result["ok"] = True
        return web.json_response(result)
    except Exception as exc:
        return web.json_response({"ok": False, "error": f"Parse error: {exc}"}, status=500)


async def handle_mkdir(request: web.Request) -> web.Response:
    """Create a new folder (one level only) on a machine."""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "invalid JSON body"}, status=400)

    machine = body.get("machine", "")
    path = body.get("path", "")

    if not path:
        return web.json_response({"ok": False, "error": "path required"}, status=400)

    from .config import FLEET_MACHINES
    local_machine = request.app["local_machine"]
    is_local = (not machine) or (machine == local_machine)

    if is_local:
        import pathlib as _pathlib
        try:
            p = _pathlib.Path(path)
            if not p.is_absolute():
                return web.json_response({"ok": False, "error": "path must be absolute"}, status=400)
            if p == p.parent:
                return web.json_response({"ok": False, "error": "cannot create root"}, status=400)
            if not p.parent.exists():
                return web.json_response({"ok": False, "error": f"Parent does not exist: {p.parent}"}, status=400)
            if p.exists():
                return web.json_response({"ok": False, "error": f"Already exists: {p}"}, status=409)
            p.mkdir(parents=False, exist_ok=False)
            return web.json_response({"ok": True, "path": str(p)})
        except PermissionError as exc:
            return web.json_response({"ok": False, "error": f"Permission denied: {exc}"}, status=403)
        except Exception as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=500)

    info = FLEET_MACHINES.get(machine)
    if not info:
        return web.json_response({"ok": False, "error": f"Unknown machine: {machine}"}, status=400)
    ssh_alias = info.get("ssh_alias", machine)

    escaped = path.replace("'", "\\'")
    py_script = (
        "import pathlib,sys;"
        f"p=pathlib.Path('{escaped}');"
        "assert p.is_absolute(), 'path must be absolute';"
        "assert p!=p.parent, 'cannot create root';"
        "assert p.parent.exists(), f'Parent does not exist: {{p.parent}}';"
        "assert not p.exists(), f'Already exists: {{p}}';"
        "p.mkdir(parents=False,exist_ok=False);"
        "print('ok')"
    )

    proc = await asyncio.create_subprocess_exec(
        "ssh", "-o", "ConnectTimeout=5", "-o", "BatchMode=yes", ssh_alias,
        "python3", "-",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(input=py_script.encode()), timeout=10)
    except asyncio.TimeoutError:
        proc.kill()
        return web.json_response({"ok": False, "error": "SSH timeout"}, status=504)

    if proc.returncode != 0:
        err = stderr.decode().strip()
        return web.json_response({"ok": False, "error": err or "SSH mkdir failed"}, status=500)

    return web.json_response({"ok": True, "path": path})


async def handle_browse(request: web.Request) -> web.Response:
    """Browse directories on a given machine."""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "invalid JSON body"}, status=400)

    machine = body.get("machine", "")
    path = body.get("path", "")

    from .config import FLEET_MACHINES
    local_machine = request.app["local_machine"]

    is_local = (not machine) or (machine == local_machine)

    if is_local:
        try:
            import pathlib as _pathlib
            p = _pathlib.Path(path).expanduser() if path else _pathlib.Path.home()
            p = p.resolve()
            if not p.exists() or not p.is_dir():
                return web.json_response({"ok": False, "error": f"Path does not exist: {p}"}, status=404)
            dirs = []
            try:
                entries = sorted(
                    (d for d in p.iterdir() if d.is_dir() and not d.name.startswith(".")),
                    key=lambda d: d.name.lower(),
                )
                dirs = [{"name": d.name, "path": str(d)} for d in entries[:200]]
            except PermissionError as pe:
                return web.json_response({"ok": False, "error": f"Permission denied: {pe}"}, status=403)
            drive = _get_local_drive(str(p))
            return web.json_response({
                "ok": True,
                "path": str(p),
                "parent": str(p.parent),
                "drive": drive,
                "dirs": dirs,
            })
        except Exception as exc:
            log.exception("local browse failed")
            return web.json_response({"ok": False, "error": str(exc)}, status=500)

    # Remote machine
    info = FLEET_MACHINES.get(machine)
    if not info:
        return web.json_response({"ok": False, "error": f"Unknown machine: {machine}"}, status=400)
    ssh_alias = info.get("ssh_alias", machine)

    # Try dispatch daemon API first, fall back to SSH
    dispatch_port = info.get("dispatch_port")
    ip = info.get("ip", "")

    if dispatch_port and ip:
        try:
            import aiohttp as _aiohttp
            url = f"http://{ip}:{dispatch_port}/browse"
            async with _aiohttp.ClientSession() as sess:
                async with sess.post(url, json={"path": path}, timeout=_aiohttp.ClientTimeout(total=8)) as resp:
                    if resp.status == 200:
                        result = await resp.json()
                        if result.get("ok") or result.get("path"):
                            result["ok"] = True
                            return web.json_response(result)
        except Exception as exc:
            log.debug("browse API failed for %s: %s, trying SSH", machine, exc)

    # SSH fallback — pipe a full Python script via stdin (works on all shells)
    if path:
        escaped = path.replace("\\", "\\\\").replace("'", "\\'")
        py_path_expr = f"pathlib.Path('{escaped}')"
    else:
        py_path_expr = "pathlib.Path.home()"

    py_script = f"""
import json, pathlib, sys
p = {py_path_expr}.expanduser().resolve()
if not p.exists() or not p.is_dir():
    print(json.dumps({{"error": "Path does not exist"}}))
    sys.exit(0)
dirs = sorted(
    [{{"name": d.name, "path": str(d)}} for d in p.iterdir() if d.is_dir() and not d.name.startswith(".")],
    key=lambda x: x["name"],
)[:200]
drive = "/"
try:
    import psutil
    mps = sorted([pt.mountpoint for pt in psutil.disk_partitions(all=False)], key=len, reverse=True)
    drive = next((m for m in mps if str(p).startswith(m)), "/")
except Exception:
    pass
print(json.dumps({{"path": str(p), "parent": str(p.parent), "drive": drive, "dirs": dirs}}))
"""

    proc = await asyncio.create_subprocess_exec(
        "ssh", "-o", "ConnectTimeout=5", "-o", "BatchMode=yes", ssh_alias,
        "python3", "-",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(input=py_script.encode()), timeout=10)
    except asyncio.TimeoutError:
        proc.kill()
        return web.json_response({"ok": False, "error": "SSH timeout"}, status=504)

    if proc.returncode != 0:
        err = stderr.decode().strip()
        return web.json_response({"ok": False, "error": err or "SSH command failed"}, status=500)

    try:
        result = json.loads(stdout.decode().strip())
        result["ok"] = True
        return web.json_response(result)
    except Exception as exc:
        return web.json_response({"ok": False, "error": f"Parse error: {exc}"}, status=500)


async def handle_logs(request: web.Request) -> web.Response:
    """GET /api/logs — return last N log entries from the in-memory ring buffer."""
    try:
        limit = int(request.query.get("limit", "100"))
    except (ValueError, TypeError):
        limit = 100
    level = request.query.get("level")
    handler: MemoryLogHandler | None = request.app.get("log_handler")
    if not handler:
        return web.json_response({"logs": []})
    logs = handler.get_logs(limit=limit, level=level)
    return web.json_response({"logs": logs})


async def handle_restart(request: web.Request) -> web.Response:
    """POST /api/restart — reset the scan cycle without killing the server."""
    app = request.app

    try:
        # Cancel the background scan task
        bg = app.get("bg_task")
        if bg:
            bg.cancel()
            try:
                await bg
            except asyncio.CancelledError:
                pass

        # Clear state to force a fresh scan on next cycle
        app["state"]["sessions"] = []
        app["state"]["fleet"] = {}
        app["state"]["tmux"] = []
        app["state"]["last_scan"] = None

        # Restart background scan
        app["bg_task"] = asyncio.ensure_future(_background_scan(app))
        log.info("Server restarted (background scan reset)")

        return web.json_response({"ok": True, "message": "Scan cycle restarted"})
    except Exception as exc:
        log.exception("restart failed")
        return web.json_response({"ok": False, "error": str(exc)}, status=500)


async def handle_exit(request: web.Request) -> web.Response:
    """POST /api/exit — gracefully shut down the server and exit."""
    import os

    log.info("Exit requested — shutting down")

    async def _shutdown():
        await asyncio.sleep(0.5)
        os._exit(0)

    asyncio.ensure_future(_shutdown())
    return web.json_response({"ok": True, "message": "Shutting down..."})


async def handle_preferences_get(request: web.Request) -> web.Response:
    return web.json_response(_load_prefs())


async def handle_preferences_post(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "invalid JSON body"}, status=400)
    prefs = _load_prefs()
    prefs.update(body)
    _save_prefs(prefs)
    return web.json_response(prefs)


async def handle_sessions_pin(request: web.Request) -> web.Response:
    """Add a session ID to the pinned list."""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "invalid JSON body"}, status=400)
    session_id = body.get("session_id", "")
    if not session_id:
        return web.json_response({"ok": False, "error": "session_id required"}, status=400)
    prefs = _load_prefs()
    pinned = prefs.get("pinned_sessions", [])
    if session_id not in pinned:
        pinned.append(session_id)
    prefs["pinned_sessions"] = pinned
    _save_prefs(prefs)
    return web.json_response({"ok": True, "pinned_sessions": pinned})


async def handle_sessions_unpin(request: web.Request) -> web.Response:
    """Remove a session ID from the pinned list."""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "invalid JSON body"}, status=400)
    session_id = body.get("session_id", "")
    if not session_id:
        return web.json_response({"ok": False, "error": "session_id required"}, status=400)
    prefs = _load_prefs()
    pinned = prefs.get("pinned_sessions", [])
    pinned = [p for p in pinned if p != session_id]
    prefs["pinned_sessions"] = pinned
    _save_prefs(prefs)
    return web.json_response({"ok": True, "pinned_sessions": pinned})


async def handle_sessions_archive(request: web.Request) -> web.Response:
    """Add a session ID to the archived list."""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "invalid JSON body"}, status=400)
    session_id = body.get("session_id", "")
    if not session_id:
        return web.json_response({"ok": False, "error": "session_id required"}, status=400)
    prefs = _load_prefs()
    archived = prefs.get("archived_sessions", [])
    if session_id not in archived:
        archived.append(session_id)
    prefs["archived_sessions"] = archived
    _save_prefs(prefs)
    return web.json_response({"ok": True, "archived_sessions": archived})


async def handle_sessions_unarchive(request: web.Request) -> web.Response:
    """Remove a session ID from the archived list."""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "invalid JSON body"}, status=400)
    session_id = body.get("session_id", "")
    if not session_id:
        return web.json_response({"ok": False, "error": "session_id required"}, status=400)
    prefs = _load_prefs()
    archived = prefs.get("archived_sessions", [])
    archived = [a for a in archived if a != session_id]
    prefs["archived_sessions"] = archived
    _save_prefs(prefs)
    return web.json_response({"ok": True, "archived_sessions": archived})


# ---------------------------------------------------------------------------
# Hardware info endpoint
# ---------------------------------------------------------------------------

# Cache: machine -> {"data": {...}, "ts": float}
_hw_cache: dict[str, dict] = {}
_HW_CACHE_TTL = 30.0  # seconds


def _get_local_hardware() -> dict:
    """Collect CPU/GPU/memory info from the local machine."""
    import psutil as _psutil

    # CPU name
    if platform.system() == "Darwin":
        try:
            cpu_name = subprocess.check_output(
                ["sysctl", "-n", "machdep.cpu.brand_string"], text=True, timeout=3
            ).strip()
        except Exception:
            cpu_name = platform.processor() or "Unknown"
    else:
        cpu_name = platform.processor() or "Unknown"

    cpu_cores = _psutil.cpu_count(logical=True)
    cpu_usage = _psutil.cpu_percent(interval=0.5)

    # CPU temperature
    cpu_temp = None
    try:
        temps = _psutil.sensors_temperatures()
        if temps:
            # Try common keys
            for key in ("coretemp", "cpu_thermal", "k10temp", "zenpower"):
                entries = temps.get(key, [])
                if entries:
                    cpu_temp = round(entries[0].current, 1)
                    break
            if cpu_temp is None:
                # take first available
                for entries in temps.values():
                    if entries:
                        cpu_temp = round(entries[0].current, 1)
                        break
    except Exception:
        pass

    # GPU info
    gpus: list[dict] = []
    # Try nvidia-smi first
    try:
        nv_out = subprocess.check_output(
            [
                "nvidia-smi",
                "--query-gpu=name,temperature.gpu,utilization.gpu,memory.used,memory.total",
                "--format=csv,noheader",
            ],
            text=True,
            timeout=5,
        ).strip()
        for line in nv_out.splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 5:
                def _parse_num(s: str):
                    try:
                        return float(s.split()[0])
                    except Exception:
                        return None
                gpus.append({
                    "name": parts[0],
                    "temp_c": _parse_num(parts[1]),
                    "usage_percent": _parse_num(parts[2]),
                    "memory_used_mb": _parse_num(parts[3]),
                    "memory_total_mb": _parse_num(parts[4]),
                })
    except Exception:
        pass

    # macOS fallback: system_profiler for GPU name
    if not gpus and platform.system() == "Darwin":
        try:
            sp_out = subprocess.check_output(
                ["system_profiler", "SPDisplaysDataType", "-json"],
                text=True,
                timeout=10,
            )
            sp_data = json.loads(sp_out)
            displays = sp_data.get("SPDisplaysDataType", [])
            for disp in displays:
                name = disp.get("sppci_model") or disp.get("_name") or "Unknown GPU"
                gpus.append({
                    "name": name,
                    "temp_c": None,
                    "usage_percent": None,
                    "memory_used_mb": None,
                    "memory_total_mb": None,
                })
        except Exception:
            pass

    # Memory
    vm = _psutil.virtual_memory()
    memory = {
        "total_gb": round(vm.total / 1e9, 1),
        "used_gb": round(vm.used / 1e9, 1),
        "percent": round(vm.percent, 1),
    }

    return {
        "ok": True,
        "cpu": {
            "name": cpu_name,
            "cores": cpu_cores,
            "usage_percent": round(cpu_usage, 1),
            "temp_c": cpu_temp,
        },
        "gpus": gpus,
        "memory": memory,
    }


# Remote hardware collection script (stdlib + optional psutil/nvidia-smi)
_REMOTE_HW_SCRIPT = r"""
import json, platform, subprocess, sys

def _parse_num(s):
    try: return float(s.strip().split()[0])
    except: return None

# CPU name
if platform.system() == "Darwin":
    try:
        cpu_name = subprocess.check_output(["sysctl", "-n", "machdep.cpu.brand_string"],
                                           text=True, timeout=3).strip()
    except:
        cpu_name = platform.processor() or "Unknown"
else:
    cpu_name = platform.processor() or "Unknown"

cpu_cores = None
cpu_usage = None
cpu_temp = None
try:
    import psutil as _p
    cpu_cores = _p.cpu_count(logical=True)
    cpu_usage = round(_p.cpu_percent(interval=0.5), 1)
    try:
        temps = _p.sensors_temperatures()
        if temps:
            for key in ("coretemp","cpu_thermal","k10temp","zenpower"):
                entries = temps.get(key, [])
                if entries: cpu_temp = round(entries[0].current, 1); break
            if cpu_temp is None:
                for entries in temps.values():
                    if entries: cpu_temp = round(entries[0].current, 1); break
    except: pass
except: pass

gpus = []
try:
    nv = subprocess.check_output(
        ["nvidia-smi","--query-gpu=name,temperature.gpu,utilization.gpu,memory.used,memory.total",
         "--format=csv,noheader"], text=True, timeout=5).strip()
    for line in nv.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 5:
            gpus.append({"name": parts[0], "temp_c": _parse_num(parts[1]),
                         "usage_percent": _parse_num(parts[2]),
                         "memory_used_mb": _parse_num(parts[3]),
                         "memory_total_mb": _parse_num(parts[4])})
except: pass

if not gpus and platform.system() == "Darwin":
    try:
        import json as _j
        sp = subprocess.check_output(["system_profiler","SPDisplaysDataType","-json"],
                                     text=True, timeout=10)
        for disp in _j.loads(sp).get("SPDisplaysDataType",[]):
            name = disp.get("sppci_model") or disp.get("_name") or "Unknown GPU"
            gpus.append({"name": name, "temp_c": None, "usage_percent": None,
                         "memory_used_mb": None, "memory_total_mb": None})
    except: pass

memory = {"total_gb": None, "used_gb": None, "percent": None}
try:
    import psutil as _p
    vm = _p.virtual_memory()
    memory = {"total_gb": round(vm.total/1e9,1), "used_gb": round(vm.used/1e9,1),
              "percent": round(vm.percent,1)}
except: pass

print(json.dumps({"ok": True,
    "cpu": {"name": cpu_name, "cores": cpu_cores, "usage_percent": cpu_usage, "temp_c": cpu_temp},
    "gpus": gpus, "memory": memory}))
"""


async def handle_hardware(request: web.Request) -> web.Response:
    """POST /api/hardware — get CPU/GPU/memory info for a machine.

    Request: {"machine": "mac-mini"}
    Response: {"ok": true, "cpu": {...}, "gpus": [...], "memory": {...}}
    """
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "invalid JSON body"}, status=400)

    machine = body.get("machine", "")
    from .config import FLEET_MACHINES
    local_machine = request.app["local_machine"]
    is_local = (not machine) or (machine == local_machine)

    cache_key = machine or "__local__"
    cached = _hw_cache.get(cache_key)
    if cached and (time.monotonic() - cached["ts"]) < _HW_CACHE_TTL:
        return web.json_response(cached["data"])

    if is_local:
        try:
            loop = asyncio.get_running_loop()
            data = await loop.run_in_executor(None, _get_local_hardware)
            _hw_cache[cache_key] = {"data": data, "ts": time.monotonic()}
            return web.json_response(data)
        except Exception as exc:
            log.exception("local hardware query failed")
            return web.json_response({"ok": False, "error": str(exc)}, status=500)

    info = FLEET_MACHINES.get(machine)
    if not info:
        return web.json_response({"ok": False, "error": f"Unknown machine: {machine}"}, status=400)
    ssh_alias = info.get("ssh_alias", machine)

    script = _REMOTE_HW_SCRIPT.strip()
    proc = await asyncio.create_subprocess_exec(
        "ssh", "-o", "ConnectTimeout=5", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no",
        ssh_alias, "python3", "-",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(input=script.encode()), timeout=20)
    except asyncio.TimeoutError:
        proc.kill()
        return web.json_response({"ok": False, "error": "SSH timeout"}, status=504)

    if proc.returncode != 0:
        err = stderr.decode().strip()
        return web.json_response({"ok": False, "error": err or "SSH command failed"}, status=500)

    try:
        data = json.loads(stdout.decode().strip())
        _hw_cache[cache_key] = {"data": data, "ts": time.monotonic()}
        return web.json_response(data)
    except Exception as exc:
        return web.json_response({"ok": False, "error": f"Parse error: {exc}"}, status=500)


# ---------------------------------------------------------------------------
# Session rename endpoint
# ---------------------------------------------------------------------------

async def handle_sessions_rename(request: web.Request) -> web.Response:
    """POST /api/sessions/rename — rename a Claude Code session.

    Request: {"machine": "mac-mini", "session_id": "uuid", "pid": 24740, "name": "my-new-name"}
    Response: {"ok": true, "name": "my-new-name"}
    """
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "invalid JSON body"}, status=400)

    machine = body.get("machine", "")
    session_id = body.get("session_id", "")
    pid = body.get("pid")
    name = body.get("name", "")

    if not session_id:
        return web.json_response({"ok": False, "error": "session_id required"}, status=400)
    if not name or not name.strip():
        return web.json_response({"ok": False, "error": "name must be non-empty"}, status=400)
    name = name.strip()

    from .config import FLEET_MACHINES
    local_machine = request.app["local_machine"]
    is_local = (not machine) or (machine == local_machine)

    if is_local:
        sessions_dir = pathlib.Path.home() / ".claude" / "sessions"
        # Find the PID file: prefer <pid>.json, else scan for sessionId match
        pid_file: pathlib.Path | None = None
        if pid:
            candidate = sessions_dir / f"{pid}.json"
            if candidate.exists():
                pid_file = candidate
        if pid_file is None:
            # Scan all *.json files for matching sessionId
            if sessions_dir.is_dir():
                for jf in sessions_dir.glob("*.json"):
                    try:
                        d = json.loads(jf.read_text(encoding="utf-8"))
                        if d.get("sessionId") == session_id:
                            pid_file = jf
                            break
                    except Exception:
                        continue
        if pid_file is None:
            return web.json_response({"ok": False, "error": "No active PID file found for this session"}, status=404)
        try:
            data = json.loads(pid_file.read_text(encoding="utf-8"))
        except Exception as exc:
            return web.json_response({"ok": False, "error": f"Could not read session file: {exc}"}, status=500)
        if data.get("sessionId") and data["sessionId"] != session_id:
            return web.json_response(
                {"ok": False, "error": "sessionId mismatch in PID file"}, status=400
            )
        data["name"] = name
        try:
            pid_file.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except Exception as exc:
            return web.json_response({"ok": False, "error": f"Could not write session file: {exc}"}, status=500)
        return web.json_response({"ok": True, "name": name})

    # Remote machine
    info = FLEET_MACHINES.get(machine)
    if not info:
        return web.json_response({"ok": False, "error": f"Unknown machine: {machine}"}, status=400)
    ssh_alias = info.get("ssh_alias", machine)

    safe_name = name.replace("\\", "\\\\").replace("'", "\\'")
    safe_sid = session_id.replace("'", "\\'")
    pid_arg = str(int(pid)) if pid else "None"

    py_script = f"""
import json, pathlib, sys
session_id = '{safe_sid}'
pid = {pid_arg}
name = '{safe_name}'
sessions_dir = pathlib.Path.home() / '.claude' / 'sessions'
pid_file = None
if pid:
    candidate = sessions_dir / f'{{pid}}.json'
    if candidate.exists():
        pid_file = candidate
if pid_file is None and sessions_dir.is_dir():
    for jf in sessions_dir.glob('*.json'):
        try:
            d = json.loads(jf.read_text(encoding='utf-8'))
            if d.get('sessionId') == session_id:
                pid_file = jf
                break
        except Exception:
            pass
if pid_file is None:
    print(json.dumps({{'ok': False, 'error': 'No active PID file found'}}))
    sys.exit(0)
try:
    data = json.loads(pid_file.read_text(encoding='utf-8'))
except Exception as exc:
    print(json.dumps({{'ok': False, 'error': str(exc)}}))
    sys.exit(0)
if data.get('sessionId') and data['sessionId'] != session_id:
    print(json.dumps({{'ok': False, 'error': 'sessionId mismatch'}}))
    sys.exit(0)
data['name'] = name
pid_file.write_text(json.dumps(data, indent=2), encoding='utf-8')
print(json.dumps({{'ok': True, 'name': name}}))
"""

    proc = await asyncio.create_subprocess_exec(
        "ssh", "-o", "ConnectTimeout=5", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no",
        ssh_alias, "python3", "-",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(input=py_script.encode()), timeout=15)
    except asyncio.TimeoutError:
        proc.kill()
        return web.json_response({"ok": False, "error": "SSH timeout"}, status=504)

    if proc.returncode != 0:
        err = stderr.decode().strip()
        return web.json_response({"ok": False, "error": err or "SSH command failed"}, status=500)

    try:
        result = json.loads(stdout.decode().strip())
        return web.json_response(result)
    except Exception as exc:
        return web.json_response({"ok": False, "error": f"Parse error: {exc}"}, status=500)


# ---------------------------------------------------------------------------
# Pane streaming helpers
# ---------------------------------------------------------------------------

def _remove_pane_subscriber(state: dict, machine: str, session_name: str, ws) -> None:
    """Remove a WS client from pane stream subscribers. Stop stream if no subscribers left."""
    key = (machine, session_name)
    info = state["pane_streams"].get(key)
    if not info:
        return
    info["subscribers"].discard(ws)
    if not info["subscribers"]:
        if info["task"] and not info["task"].done():
            info["task"].cancel()
        del state["pane_streams"][key]


async def _push_pane_output(state: dict, key: tuple, content: str) -> None:
    """Push pane output to all subscribers of this stream."""
    info = state["pane_streams"].get(key)
    if not info or not info["subscribers"]:
        return
    info["last_content"] = content
    machine, session_name = key
    payload = json.dumps({
        "type": "pane_output",
        "machine": machine,
        "session_name": session_name,
        "content": content,
    })
    dead = set()
    for ws in list(info["subscribers"]):
        try:
            await ws.send_str(payload)
        except Exception:
            dead.add(ws)
    info["subscribers"] -= dead


async def _pane_poll_loop(app: web.Application, machine: str, session_name: str, interval: float = 2.0) -> None:
    """Poll capture-pane at intervals and push changes to subscribers."""
    from .tmux_manager import capture_pane

    state = app["state"]
    key = (machine, session_name)
    last_content = ""
    log.info("pane_poll_loop started: %s/%s (interval=%.1fs)", machine, session_name, interval)

    try:
        while key in state["pane_streams"] and state["pane_streams"][key]["subscribers"]:
            content = await capture_pane(machine, session_name)
            if content != last_content:
                await _push_pane_output(state, key, content)
                last_content = content
            await asyncio.sleep(interval)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.exception("pane_poll_loop(%s/%s) crashed: %s", machine, session_name, exc)
    finally:
        log.info("pane_poll_loop ended: %s/%s", machine, session_name)


async def _pane_stream_loop(app: web.Application, machine: str, session_name: str) -> None:
    """Background task that streams pane content to subscribers.

    Uses capture-pane polling for all machines (local and remote, tmux and psmux).
    """
    state = app["state"]
    key = (machine, session_name)

    log.info("pane_stream_loop started: %s/%s", machine, session_name)

    # Use polling for all machines (local and remote, tmux and psmux).
    # pipe-pane is fragile over SSH and adds complexity for marginal gain.
    # Local tmux polls at 1.5s, remote at 2.5s for lower SSH overhead.
    is_local = (machine == app.get("local_machine", ""))
    interval = 1.5 if is_local else 2.5

    try:
        await _pane_poll_loop(app, machine, session_name, interval)
    except asyncio.CancelledError:
        pass


async def handle_tmux_capture(request: web.Request) -> web.Response:
    """Capture current pane content (one-shot, no streaming)."""
    from .tmux_manager import capture_pane
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "invalid JSON body"}, status=400)
    machine = body.get("machine", "")
    session_name = body.get("session_name", "")
    if not machine or not session_name:
        return web.json_response({"ok": False, "error": "machine and session_name required"}, status=400)
    content = await capture_pane(machine, session_name)
    return web.json_response({"ok": True, "content": content})


# ---------------------------------------------------------------------------
# WebSocket handler
# ---------------------------------------------------------------------------

async def handle_ws(request: web.Request) -> web.WebSocketResponse:
    from .auth import is_loopback

    ws = web.WebSocketResponse()
    await ws.prepare(request)

    ws._subscribed_channels: set[str] = set()  # type: ignore[attr-defined]
    request.app["state"]["ws_clients"].add(ws)
    client_addr = request.remote or "unknown"

    # Auth gate: if enabled and client is NOT loopback, require first message = auth
    auth_cfg = request.app.get("auth_config")
    ws_authed = (not auth_cfg) or (not auth_cfg.enabled) or is_loopback(client_addr)

    log.info("WS connect: %s (authed=%s, total: %d)",
             client_addr, ws_authed, len(request.app["state"]["ws_clients"]))

    state = request.app["state"]

    try:
        async for msg in ws:
            if msg.type == web.WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                except json.JSONDecodeError:
                    await ws.send_json({"type": "error", "message": "invalid JSON"})
                    continue

                msg_type = data.get("type")
                channel = data.get("channel", "")

                # Enforce auth: first non-auth message is rejected if not yet authed
                if not ws_authed:
                    if msg_type == "auth":
                        if data.get("token") == auth_cfg.token:
                            ws_authed = True
                            await ws.send_json({"type": "auth_ok"})
                        else:
                            await ws.send_json({"type": "auth_error", "message": "invalid token"})
                            await ws.close()
                            break
                        continue
                    await ws.send_json({"type": "auth_required"})
                    await ws.close()
                    break

                if msg_type == "subscribe":
                    ws._subscribed_channels.add(channel)  # type: ignore[attr-defined]
                    # Send immediate snapshot
                    if channel == "sessions":
                        snap = [s.to_dict() for s in state["sessions"]]
                    elif channel == "fleet":
                        snap = state["fleet"]
                    elif channel == "tmux":
                        snap = [t.to_dict() for t in state["tmux"]]
                    else:
                        snap = []
                    await ws.send_str(
                        json.dumps({"type": "snapshot", "channel": channel, "data": snap})
                    )

                elif msg_type == "unsubscribe":
                    ws._subscribed_channels.discard(channel)  # type: ignore[attr-defined]

                elif msg_type == "subscribe_pane":
                    machine = data.get("machine", "")
                    session_name = data.get("session_name", "")
                    if machine and session_name:
                        key = (machine, session_name)
                        streams = state["pane_streams"]
                        if key not in streams:
                            streams[key] = {
                                "subscribers": set(),
                                "last_content": "",
                                "task": None,
                            }
                        streams[key]["subscribers"].add(ws)
                        # Start stream task if not running
                        if streams[key]["task"] is None or streams[key]["task"].done():
                            streams[key]["task"] = asyncio.ensure_future(
                                _pane_stream_loop(request.app, machine, session_name)
                            )
                        # Send initial capture immediately
                        from .tmux_manager import capture_pane
                        content = await capture_pane(machine, session_name)
                        if content:
                            await ws.send_str(json.dumps({
                                "type": "pane_output",
                                "machine": machine,
                                "session_name": session_name,
                                "content": content,
                            }))

                elif msg_type == "unsubscribe_pane":
                    machine = data.get("machine", "")
                    session_name = data.get("session_name", "")
                    if machine and session_name:
                        _remove_pane_subscriber(state, machine, session_name, ws)

            elif msg.type in (web.WSMsgType.ERROR, web.WSMsgType.CLOSE):
                break
    finally:
        request.app["state"]["ws_clients"].discard(ws)
        # Clean up pane stream subscriptions for this client
        for key in list(state["pane_streams"]):
            info = state["pane_streams"].get(key)
            if info:
                info["subscribers"].discard(ws)
                if not info["subscribers"]:
                    if info["task"] and not info["task"].done():
                        info["task"].cancel()
                    del state["pane_streams"][key]
        log.info("WS disconnect: %s (total: %d)", client_addr, len(request.app["state"]["ws_clients"]))

    return ws


# ---------------------------------------------------------------------------
# Application factory
# ---------------------------------------------------------------------------

def create_app(
    port: int = DEFAULT_PORT,
    bind: str = DEFAULT_BIND,
) -> web.Application:
    """
    Build and return the aiohttp Application.

    Static files under src/web/ are served at /.
    All routes are registered here.
    """
    app = web.Application(middlewares=[cors_middleware, rate_limit_middleware, auth_middleware])

    # Install in-memory log handler on the claude_manager logger hierarchy
    mem_handler = MemoryLogHandler(max_entries=500)
    mem_handler.setFormatter(logging.Formatter("%(message)s"))
    cm_logger = logging.getLogger("claude_manager")
    cm_logger.addHandler(mem_handler)
    app["log_handler"] = mem_handler

    # Load auth config from ~/.claude-manager/auth.json
    from .auth import load_auth_config
    auth_cfg = load_auth_config()
    app["auth_config"] = auth_cfg
    if auth_cfg.enabled:
        log.info("auth: enabled (key=%s, token=%s…)", auth_cfg.key_path, (auth_cfg.token or "")[:8])
    else:
        log.info("auth: disabled (loopback-only bind recommended)")

    # Shared state
    app["port"] = port
    app["bind"] = bind
    app["local_machine"] = detect_local_machine()
    app["state"] = {
        "sessions": [],
        "fleet": {},
        "tmux": [],
        "last_scan": None,
        "ws_clients": set(),
        "pane_streams": {},  # {(machine, session_name): {"task": Task, "subscribers": set(ws), "last_content": str}}
    }

    # Lifecycle hooks
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)

    # REST routes
    app.router.add_get("/health", handle_health)
    app.router.add_get("/api/auth/config", handle_auth_config)
    app.router.add_post("/api/auth/update", handle_auth_update)
    app.router.add_get("/api/auth/token", handle_auth_token)
    app.router.add_get("/api/update/check", handle_update_check)
    app.router.add_post("/api/update/apply", handle_update_apply)
    app.router.add_get("/api/logs", handle_logs)
    app.router.add_get("/api/sessions", handle_sessions_all)
    app.router.add_get("/api/sessions/{machine}", handle_sessions_machine)
    app.router.add_post("/api/sessions/scan", handle_sessions_scan)
    app.router.add_post("/api/sessions/launch", handle_sessions_launch)
    app.router.add_post("/api/sessions/pin", handle_sessions_pin)
    app.router.add_post("/api/sessions/unpin", handle_sessions_unpin)
    app.router.add_post("/api/sessions/archive", handle_sessions_archive)
    app.router.add_post("/api/sessions/unarchive", handle_sessions_unarchive)
    app.router.add_post("/api/sessions/rename", handle_sessions_rename)
    app.router.add_post("/api/hardware", handle_hardware)
    app.router.add_get("/api/fleet", handle_fleet)
    app.router.add_get("/api/tmux", handle_tmux)
    app.router.add_get("/api/tmux/{machine}", handle_tmux_machine)
    app.router.add_post("/api/tmux/create", handle_tmux_create)
    app.router.add_post("/api/tmux/connect", handle_tmux_connect)
    app.router.add_post("/api/tmux/connect-remote", handle_tmux_connect_remote)
    app.router.add_post("/api/tmux/kill", handle_tmux_kill)
    app.router.add_post("/api/tmux/capture", handle_tmux_capture)
    app.router.add_post("/api/browse", handle_browse)
    app.router.add_post("/api/drives", handle_drives)
    app.router.add_post("/api/mkdir", handle_mkdir)
    app.router.add_get("/api/preferences", handle_preferences_get)
    app.router.add_post("/api/preferences", handle_preferences_post)
    app.router.add_post("/api/restart", handle_restart)
    app.router.add_post("/api/exit", handle_exit)

    # WebSocket
    app.router.add_get("/ws", handle_ws)

    # Static web UI — serve index.html at / and static assets
    import pathlib
    web_dir = pathlib.Path(__file__).parent / "web"
    if web_dir.is_dir():
        index_html = web_dir / "index.html"

        async def handle_index(request: web.Request) -> web.Response:
            return web.FileResponse(index_html)

        app.router.add_get("/", handle_index)
        app.router.add_static("/static/", web_dir)

    return app


def run_server(
    port: int = DEFAULT_PORT,
    bind: str = DEFAULT_BIND,
) -> None:
    """Start the aiohttp server (blocking)."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    app = create_app(port=port, bind=bind)
    log.info("claude-manager API starting on http://%s:%d", bind, port)
    web.run_app(app, host=bind, port=port, print=None)
