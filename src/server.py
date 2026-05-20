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

from .command_adapter import get_adapter, sanitize_mux_name
from .config import DEFAULT_BIND, DEFAULT_PORT, FLEET_MACHINES, SCAN_INTERVAL, detect_local_machine
from .fleet import discover_fleet
from .launcher import launch_claude_session, launch_tmux_attach, launch_tmux_attach_remote, launch_new_tmux_and_attach, launch_terminal, _ssh_path_prefix
from .scanner import ClaudeSession, scan_all
from .state_store import StateStore
from .subprocess_utils import run_with_timeout, _win32_kwargs
from .tmux_manager import TmuxSession, list_all_tmux, create_tmux_session, kill_tmux_session
from .session_link import enrich_tmux_dicts


async def _pool_exec(
    machine: str,
    cmd: str,
    *,
    timeout: float,
    input: bytes | None = None,
) -> tuple[int, bytes, bytes]:
    """Run a shell command on a fleet machine via the asyncssh pool.

    Primary path for all ad-hoc API endpoints — replaces the subprocess `ssh`
    calls that leaked sshd sessions on Windows targets (no ControlMaster
    multiplexing on Windows OpenSSH). Reuses one long-lived connection per
    machine for the app's lifetime.

    Falls back to subprocess `ssh` only if asyncssh is missing or the pool
    is in its reconnect backoff window.
    """
    from .ssh_pool import default_pool, asyncssh as _asyncssh
    info = FLEET_MACHINES.get(machine, {})
    if _asyncssh is not None:
        try:
            return await default_pool().run(machine, cmd, timeout=timeout, input=input)
        except Exception as exc:
            log.debug("_pool_exec(%s) pool failed, falling back to subprocess: %s", machine, exc)
    ssh_alias = info.get("ssh_alias", machine)
    # For Windows targets the SSH default shell is PowerShell (enforced per
    # global CLAUDE.md). Wrapping the PowerShell-syntax payload in `bash -c`
    # would (a) require Git Bash on the remote PATH, and (b) mangle PS
    # semantics like Set-Location and `;` chaining. Pass the cmd straight to
    # the remote's default shell instead.
    if info.get("os") == "win32":
        ssh_argv = ["ssh", "-o", "ConnectTimeout=5", "-o", "BatchMode=yes",
                    "-o", "StrictHostKeyChecking=no", ssh_alias, cmd]
    else:
        ssh_argv = ["ssh", "-o", "ConnectTimeout=5", "-o", "BatchMode=yes",
                    "-o", "StrictHostKeyChecking=no", ssh_alias, "bash", "-c", cmd]
    return await run_with_timeout(ssh_argv, timeout=timeout, input=input)

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
# Error middleware — converts unhandled handler exceptions into JSON 500s
# ---------------------------------------------------------------------------
#
# Without this, an exception that escapes a route handler (e.g. launch_terminal
# raising OSError because `osascript` is missing, or asyncssh.Error bubbling
# out of the SSH pool for an offline Windows target) reaches aiohttp's default
# error handler, which returns `500 Internal Server Error` with an HTML /
# plain-text body. The UI's fetch wrapper tries JSON.parse on the response and
# falls back to showing just the status code — the user sees "error 500"
# with no actionable detail and the toast just says "HTTP 500".
#
# This middleware wraps every request so any non-HTTP exception becomes a
# structured `{ok: False, error: "<ExcType>: <msg>"}` payload that the UI
# can display. web.HTTPException subclasses (401/404/etc) are re-raised
# unchanged so existing semantics are preserved; asyncio.CancelledError is
# also passed through so task shutdown isn't masked as a 500.


@web.middleware
async def error_middleware(request: web.Request, handler) -> web.Response:
    try:
        return await handler(request)
    except web.HTTPException:
        raise
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.exception(
            "Unhandled exception in %s %s: %s",
            request.method, request.path, exc,
        )
        return web.json_response(
            {"ok": False, "error": f"{type(exc).__name__}: {exc}"},
            status=500,
        )


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


@web.middleware
async def trace_middleware(request: web.Request, handler) -> web.Response:
    """Emit cm.api.request / cm.api.response for every non-static, non-OPTIONS path.

    Static asset hits (/, /favicon.ico) and CORS preflights are filtered out
    to keep the trace volume bounded — every non-trivial endpoint and its
    duration land in the JSONL stream.
    """
    if request.method == "OPTIONS" or request.path in ("/", "/favicon.ico"):
        return await handler(request)
    from .tracking import tl
    import time as _t
    t0 = _t.monotonic()
    tl.event("cm.api.request", method=request.method, path=request.path)
    try:
        resp = await handler(request)
    except Exception as exc:
        tl.event("cm.api.response", method=request.method, path=request.path,
                 status=500, err=type(exc).__name__,
                 elapsed_ms=round((_t.monotonic() - t0) * 1000, 1))
        raise
    tl.event("cm.api.response", method=request.method, path=request.path,
             status=getattr(resp, "status", 0),
             elapsed_ms=round((_t.monotonic() - t0) * 1000, 1))
    return resp


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
    store: StateStore = app["store"]

    # Wait for first WS client (or 30s timeout) so the user sees first-scan progress
    log.info("background_scan: waiting for first WS client...")
    waited = 0.0
    while not store.has_ws_clients() and waited < 30.0:
        await asyncio.sleep(0.2)
        waited += 0.2
    if store.has_ws_clients():
        log.info("background_scan: WS client connected after %.1fs, starting first scan", waited)
    else:
        log.info("background_scan: 30s timeout reached, scanning anyway")

    from .tracking import tl
    while True:
        t0 = time.monotonic()
        tl.event("cm.scan.cycle.start")
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
                await store.push_raw(payload)

            sessions, tmux = await asyncio.gather(
                scan_all(local_machine, fleet, on_progress=_emit_scan_progress),
                list_all_tmux(local_machine, fleet),
            )
            await store.update_fleet(fleet)
            await store.update_sessions(sessions)
            await store.update_tmux(tmux)
            store.set_last_scan(_now_iso())
            elapsed = time.monotonic() - t0
            log.info(
                "background_scan: %d sessions, %d tmux, %d fleet machines in %.2fs",
                len(sessions), len(tmux), len(fleet), elapsed,
            )
            tl.event("cm.scan.cycle.done",
                     sessions=len(sessions), tmux=len(tmux), fleet=len(fleet),
                     elapsed_ms=round(elapsed * 1000, 1))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("background scan failed: %s", exc)
            tl.event("cm.scan.cycle.done", error=str(exc)[:200])

        await asyncio.sleep(SCAN_INTERVAL)


async def _push_to_ws(
    app: web.Application,
    channel: str,
    data: Any,
) -> None:
    """Send an update message to all WebSocket clients subscribed to channel."""
    store: StateStore = app["store"]
    await store.push_to_channel(channel, data)


# ---------------------------------------------------------------------------
# Startup / cleanup
# ---------------------------------------------------------------------------

async def on_startup(app: web.Application) -> None:
    # Persistent asyncssh pool — one connection per fleet machine for the
    # whole app lifecycle. Replaces subprocess-ssh storms (esp. on Windows
    # where ControlMaster is a no-op).
    from .ssh_pool import default_pool
    from .tracking import tl, init as tl_init
    tl_init()
    tl.event("cm.proc.start", local_machine=app.get("local_machine"))
    app["ssh_pool"] = default_pool()
    app["bg_task"] = asyncio.ensure_future(_background_scan(app))
    tl.event("cm.proc.ready")


async def on_cleanup(app: web.Application) -> None:
    task: asyncio.Task = app.get("bg_task")
    if task:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    # Tear down every pooled SSH connection so the app leaves no sshd-sessions
    # behind on remote machines.
    try:
        from .ssh_pool import shutdown_default
        await shutdown_default()
    except Exception as exc:
        log.warning("on_cleanup: ssh_pool shutdown error: %s", exc)

    # Close all open WebSocket connections cleanly
    store: StateStore = app.get("store")
    if store:
        for ws in store.iter_ws():
            try:
                await ws.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# REST handlers
# ---------------------------------------------------------------------------

def _read_version_metadata() -> dict:
    """Return running-code version metadata.

    Priority:
      1. Live git — the ONLY source that tracks what's actually checked out.
      2. VERSION.json — fallback for tarball installs with no .git directory.

    VERSION.json must NEVER be preferred over git. The file is committed to the
    repo so it always lags HEAD by exactly one commit (the commit that adds
    VERSION.json creates a new HEAD that VERSION.json cannot reference). Using
    it as primary made `update_available` permanently true and the Update &
    Restart button loop forever. See incident 2026-04-10 commit 217ba38 →
    0f8156b mismatch.
    """
    import pathlib
    repo = pathlib.Path(__file__).resolve().parent.parent

    # Primary: live git. This matches the running code bit-for-bit.
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

    # Fallback: VERSION.json (tarball installs with no .git).
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

    return {"version": 0, "commit": "unknown"}


_VERSION_METADATA = _read_version_metadata()

# Cached GitHub upstream version check — avoid rate-limiting the unauth API
_update_check_cache: dict = {"data": None, "ts": 0.0}
_UPDATE_CHECK_TTL = 60.0  # seconds
_GITHUB_API_URL = "https://api.github.com/repos/raphaelbgr/claude-manager/commits/master"


async def _fetch_github_latest() -> dict | None:
    """Fetch the latest commit from GitHub. Returns a metadata dict or None on failure.

    NOTE: The returned dict intentionally omits the `version` field. Our local
    `version` is `git rev-list --count HEAD` — the GitHub commits API has no
    equivalent and synthesizing one would require N additional round trips. The
    UI must tolerate `latest.version` being absent and render the commit hash
    alone in that case (see `Header` in src/web/index.html — bug fix for the
    `Update → v? · <sha>` rendering).
    """
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
            "commit_short": data["sha"][:7],
            "commit_full": data["sha"],
            "date": data["commit"]["author"]["date"],
            "message": msg,
            # `version` deliberately absent — see docstring.
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

    # update_available is True only when GitHub has a commit we don't.
    # If we're ahead of (or equal to) origin, there's nothing to pull.
    update_available = False
    latest_full = latest.get("commit_full")
    current_full = current.get("commit_full")
    if latest_full and current_full and latest_full != current_full:
        try:
            import pathlib as _p
            _repo = _p.Path(__file__).parent.parent
            rc, _, _ = await run_with_timeout(
                ["git", "merge-base", "--is-ancestor", latest_full, current_full],
                timeout=5, cwd=str(_repo),
            )
            # rc == 0 → latest is ancestor of current (we're ahead). rc != 0 → we need it.
            update_available = rc != 0
        except Exception:
            # If the check fails (e.g., shallow clone), fall back to "not equal"
            update_available = True

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
        import pathlib as _pathlib2
        rc_pull, stdout, stderr = await run_with_timeout(
            ["git", "pull", "--ff-only"],
            timeout=30,
            cwd=str(repo),
        )
    except asyncio.TimeoutError:
        return web.json_response({"ok": False, "error": "git pull timed out"}, status=504)
    except Exception as exc:
        return web.json_response({"ok": False, "error": str(exc)}, status=500)

    if rc_pull != 0:
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
    store: StateStore = request.app["store"]
    sessions = store.sessions()
    fleet = store.fleet()
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
            "last_scan": store.last_scan(),
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
    store: StateStore = request.app["store"]
    return web.json_response(_sessions_by_machine(store.sessions()))


async def handle_projects(request: web.Request) -> web.Response:
    """GET /api/projects — sessions grouped by project identity (cross-machine).

    Reads from in-memory sessions (no re-scan). Two-pass consolidation:

      Pass 1 — compute (project_id, basename) for every session. Build a
      basename → full-remote-id resolution table so that sessions whose id
      fell back to the bare basename (empty git_remote) can be merged into
      their sibling sessions that carry the full normalized-remote id. This
      fixes cross-machine duplicates when one machine (typically Windows)
      transiently returned git_remote="".

      Pass 2 — accumulate sessions into proj_sessions[resolved_id] using the
      resolution table. Same basename with distinct full remotes (e.g. a fork
      on GitLab vs origin on GitHub) stays separate by design.

    Sorts outer array by latest_modified desc.
    """
    from .project_identity import (
        project_id as _pid,
        project_display_name as _pdn,
        canonical_basename as _basename,
    )

    state = request.app["state"]
    sessions: list[ClaudeSession] = state["sessions"]

    # --- Pass 1: collect ids and build consolidation map ---
    # basename -> set of full-remote (non-fallback) ids observed for it
    basename_to_full_ids: dict[str, set[str]] = {}
    session_ids: list[tuple[str, str]] = []  # (raw_pid, basename) per session, parallel to `sessions`

    for sess in sessions:
        raw_pid = _pid(sess)
        base = _basename(sess)
        session_ids.append((raw_pid, base))
        # A full-remote id contains '/' (e.g. "github.com/owner/repo"); a bare
        # basename fallback does not. If raw_pid != base, this session was
        # keyed by git_remote, not the path fallback.
        if base and raw_pid != base and "/" in raw_pid:
            basename_to_full_ids.setdefault(base, set()).add(raw_pid)

    # Resolve each basename to a single canonical id. If exactly one full-remote
    # sibling exists, basename-fallback sessions collapse into that. If multiple
    # different full-remote ids share a basename (fork vs origin on different
    # hosts), DON'T collapse — that would incorrectly merge unrelated repos.
    basename_resolution: dict[str, str] = {}
    for base, full_ids in basename_to_full_ids.items():
        if len(full_ids) == 1:
            basename_resolution[base] = next(iter(full_ids))

    # --- Pass 2: accumulate using resolved ids ---
    proj_sessions: dict[str, list] = {}
    proj_meta: dict[str, dict] = {}

    for sess, (raw_pid, base) in zip(sessions, session_ids):
        # A session's id is rewritten ONLY when:
        #   (a) its raw id is the basename fallback (raw_pid == base), AND
        #   (b) the basename resolves to exactly one full-remote sibling.
        if raw_pid == base and base in basename_resolution:
            pid = basename_resolution[base]
        else:
            pid = raw_pid

        if pid not in proj_sessions:
            proj_sessions[pid] = []
            proj_meta[pid] = {
                "project_id": pid,
                "display_name": _pdn(pid),
                "git_remote": sess.git_remote or "",
                "machines": set(),
                "latest_modified": sess.modified or "",
            }
        proj_sessions[pid].append(sess)
        meta = proj_meta[pid]
        meta["machines"].add(sess.machine)
        if sess.modified and sess.modified > meta["latest_modified"]:
            meta["latest_modified"] = sess.modified
        # Prefer non-empty git_remote
        if not meta["git_remote"] and sess.git_remote:
            meta["git_remote"] = sess.git_remote

    result_list = []
    for pid, sess_list in proj_sessions.items():
        meta = proj_meta[pid]
        # sessions already sorted by modified desc from scan_all
        sorted_sessions = sorted(sess_list, key=lambda s: s.modified or "", reverse=True)
        # Order machines by most-recent activity within this project (first badge = most recent)
        machine_latest: dict[str, str] = {}
        for s in sorted_sessions:
            if s.machine not in machine_latest:
                machine_latest[s.machine] = s.modified or ""
        machines_by_recency = sorted(machine_latest.keys(), key=lambda m: machine_latest[m], reverse=True)

        # Phase C — per (machine, cwd) group with aggregated git state.
        # Same machine can appear multiple times when the user has multiple
        # clones of the same repo locally (e.g. ~/git/foo and
        # ~/AndroidStudioProjects/foo). Each group targets one Pull button.
        group_order: list[tuple[str, str]] = []
        groups: dict[tuple[str, str], dict] = {}
        for s in sorted_sessions:
            cwd = s.cwd or s.project_path or ""
            key = (s.machine, cwd)
            if key not in groups:
                group_order.append(key)
                groups[key] = {
                    "machine": s.machine,
                    "cwd": cwd,
                    "session_count": 0,
                    "latest_modified": s.modified or "",
                    # Git state comes from the freshest session — sessions are
                    # already sorted by modified desc, so first write wins.
                    "git_branch": s.git_branch or "",
                    "git_upstream": s.git_upstream,
                    "git_ahead": s.git_ahead,
                    "git_behind": s.git_behind,
                    "git_dirty": s.git_dirty,
                }
            groups[key]["session_count"] += 1

        machines_detail = [groups[k] for k in group_order]

        result_list.append({
            "project_id": pid,
            "display_name": meta["display_name"],
            "git_remote": meta["git_remote"],
            "session_count": len(sess_list),
            "machines": machines_by_recency,
            "machines_detail": machines_detail,
            "latest_modified": meta["latest_modified"],
            "sessions": [s.to_dict() for s in sorted_sessions],
        })

    result_list.sort(key=lambda p: p["latest_modified"], reverse=True)

    return web.json_response({
        "projects": result_list,
        "generated": _now_iso(),
    })


# ---------------------------------------------------------------------------
# POST /api/projects/pull — fast-forward the checkout on a specific machine
# ---------------------------------------------------------------------------
#
# The script runs on the target machine via `python3 -` (same channel as
# REMOTE_SCAN_SCRIPT). It resolves git.exe on Windows, validates the branch
# is master/main, fetches origin, and fast-forwards ONLY if the tree is clean
# and the local has no ahead commits. Output is JSON to stdout — never raises.
#
# Security: {cwd_literal} is a Python string literal (via json.dumps) so
# arbitrary paths cannot escape the quoting. The endpoint ALSO whitelists
# the cwd against cwds observed in known sessions on that machine before
# ever dispatching, so the script only sees caller-controlled paths when
# those same paths already exist in the state store.

PULL_SCRIPT = r"""
import json, os, sys, subprocess

CWD = {cwd_literal}

_win_kw = {{}}
_GIT = 'git'
if sys.platform == 'win32':
    import shutil
    _si = subprocess.STARTUPINFO()
    _si.dwFlags |= 0x00000001
    _si.wShowWindow = 0
    _win_kw = {{'creationflags': 0x08000000, 'startupinfo': _si}}
    for _p in (shutil.which('git'), shutil.which('git.exe'),
               r'C:\Program Files\Git\cmd\git.exe',
               r'C:\Program Files\Git\bin\git.exe'):
        if _p and os.path.isfile(_p):
            _GIT = _p
            break

def _run(args, timeout=15):
    try:
        r = subprocess.run([_GIT, '-C', CWD] + args,
                           capture_output=True, text=True, timeout=timeout,
                           env={{**os.environ, 'GIT_TERMINAL_PROMPT': '0'}},
                           **_win_kw)
        return r.returncode, (r.stdout or '').strip(), (r.stderr or '').strip()
    except subprocess.TimeoutExpired:
        return -1, '', 'timeout'
    except Exception as exc:
        return -1, '', str(exc)

def _emit(d):
    print(json.dumps(d))
    sys.exit(0)

# Branch gate — master / main only.
rc, branch, err = _run(['rev-parse', '--abbrev-ref', 'HEAD'], timeout=5)
if rc != 0:
    _emit({{'ok': False, 'error': 'not_a_git_repo', 'stderr': err}})
if branch not in ('master', 'main'):
    _emit({{'ok': False, 'error': 'branch_not_allowed', 'branch': branch}})

# Dirty gate.
rc, out, err = _run(['status', '--porcelain', '--untracked-files=no'], timeout=5)
if rc != 0:
    _emit({{'ok': False, 'error': 'status_failed', 'branch': branch, 'stderr': err}})
if out.strip():
    _emit({{'ok': False, 'error': 'working_tree_dirty', 'branch': branch}})

# Upstream.
rc, upstream, err = _run(['rev-parse', '--abbrev-ref', '--symbolic-full-name', '@{{u}}'], timeout=5)
if rc != 0 or not upstream:
    _emit({{'ok': False, 'error': 'no_upstream', 'branch': branch, 'stderr': err}})

def _ahead_behind(up):
    rc, out, _ = _run(['rev-list', '--left-right', '--count', up + '...HEAD'], timeout=5)
    if rc != 0:
        return None, None
    parts = out.split()
    if len(parts) != 2:
        return None, None
    try:
        return int(parts[1]), int(parts[0])  # ahead, behind
    except ValueError:
        return None, None

ahead_before, behind_before = _ahead_behind(upstream)
if ahead_before is None:
    _emit({{'ok': False, 'error': 'ahead_behind_failed', 'branch': branch, 'upstream': upstream}})
if ahead_before > 0:
    _emit({{'ok': False, 'error': 'local_commits_block_ff',
            'branch': branch, 'upstream': upstream,
            'ahead_before': ahead_before, 'behind_before': behind_before}})

# Fetch.
rc, out, err = _run(['fetch', '--no-tags', 'origin'], timeout=60)
fetched = rc == 0
if not fetched:
    _emit({{'ok': False, 'error': 'fetch_failed', 'branch': branch, 'upstream': upstream,
            'stderr': err}})

ahead_before2, behind_before2 = _ahead_behind(upstream)
# If fetch revealed nothing behind, nothing to merge — success no-op.
if behind_before2 == 0:
    _emit({{'ok': True, 'branch': branch, 'upstream': upstream,
            'ahead_before': ahead_before2, 'behind_before': behind_before2,
            'ahead_after': ahead_before2, 'behind_after': 0,
            'fetched': True, 'merged': False, 'stdout': 'already up to date',
            'stderr': ''}})

# Fast-forward only.
rc, out, err = _run(['merge', '--ff-only', upstream], timeout=60)
merged = rc == 0
ahead_after, behind_after = _ahead_behind(upstream) if merged else (ahead_before2, behind_before2)
_emit({{'ok': merged, 'branch': branch, 'upstream': upstream,
        'ahead_before': ahead_before2, 'behind_before': behind_before2,
        'ahead_after': ahead_after, 'behind_after': behind_after,
        'fetched': True, 'merged': merged,
        'stdout': out, 'stderr': err,
        **({{'error': 'merge_failed'}} if not merged else {{}})}})
"""


async def handle_projects_pull(request: web.Request) -> web.Response:
    """POST /api/projects/pull — fast-forward a checkout on a fleet machine.

    Body: {"machine": "avell-i7", "cwd": "C:\\Users\\rbgnr\\git\\foo"}

    Validates machine + cwd against known sessions (path whitelist),
    dispatches PULL_SCRIPT via `python3 -` stdin, returns the script's JSON
    result. Triggers a background rescan on success so the UI badges refresh.
    """
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "bad_json"}, status=400)

    machine = (body.get("machine") or "").strip()
    cwd = body.get("cwd") or ""
    if not machine or not cwd:
        return web.json_response(
            {"ok": False, "error": "missing machine or cwd"}, status=400,
        )
    if machine not in FLEET_MACHINES:
        return web.json_response(
            {"ok": False, "error": f"unknown machine: {machine}"}, status=400,
        )

    # Path whitelist: cwd must match a known session's cwd for this machine.
    state = request.app["state"]
    sessions: list[ClaudeSession] = state["sessions"]
    allowed = {s.cwd for s in sessions if s.machine == machine and s.cwd}
    if cwd not in allowed:
        return web.json_response(
            {"ok": False, "error": f"cwd not in known session paths for {machine}"},
            status=400,
        )

    script = PULL_SCRIPT.format(cwd_literal=json.dumps(cwd))

    t0 = time.monotonic()
    try:
        rc, stdout, stderr = await _pool_exec(
            machine, "python3 -", timeout=90, input=script.encode("utf-8"),
        )
    except Exception as exc:
        log.warning("POST /api/projects/pull(%s, %s): dispatch failed: %s", machine, cwd, exc)
        return web.json_response(
            {"ok": False, "error": "dispatch_failed", "stderr": str(exc)},
            status=500,
        )

    duration_ms = int((time.monotonic() - t0) * 1000)

    if rc != 0:
        return web.json_response({
            "ok": False, "error": "remote_nonzero_exit",
            "rc": rc, "stderr": stderr.decode("utf-8", errors="replace"),
            "duration_ms": duration_ms,
        }, status=500)

    try:
        result = json.loads(stdout.decode("utf-8", errors="replace"))
    except Exception as exc:
        return web.json_response({
            "ok": False, "error": "remote_bad_json",
            "stdout": stdout.decode("utf-8", errors="replace")[:400],
            "stderr": stderr.decode("utf-8", errors="replace")[:400],
            "duration_ms": duration_ms,
        }, status=500)

    result["machine"] = machine
    result["cwd"] = cwd
    result["duration_ms"] = duration_ms

    # On success trigger an async rescan so the UI's ahead/behind badges
    # refresh via the next WS sessions-channel push. Fire-and-forget.
    if result.get("ok") and result.get("merged"):
        app = request.app
        asyncio.ensure_future(_scan_and_push(app))

    return web.json_response(result)


async def _scan_and_push(app: web.Application) -> None:
    """Kick a rescan and push fresh sessions/fleet/tmux to subscribers."""
    try:
        fleet = await discover_fleet()
        sessions = await scan_all(app["local_machine"], fleet)
        tmux = await list_all_tmux(app["local_machine"], fleet)
        store: StateStore = app["store"]
        await store.update_fleet(fleet)
        await store.update_sessions(sessions)
        await store.update_tmux(tmux)
        store.set_last_scan(_now_iso())
    except Exception as exc:
        log.warning("_scan_and_push: %s", exc)


async def handle_sessions_machine(request: web.Request) -> web.Response:
    machine = request.match_info["machine"]
    store: StateStore = request.app["store"]
    filtered = [s for s in store.sessions() if s.machine == machine]
    return web.json_response(_sessions_by_machine(filtered).get(machine, []))


async def handle_sessions_scan(request: web.Request) -> web.Response:
    """Force an immediate rescan and return fresh results."""
    app = request.app
    local_machine = app["local_machine"]
    store: StateStore = app["store"]
    t0 = time.monotonic()
    try:
        fleet = await discover_fleet()
        sessions = await scan_all(local_machine, fleet)
        tmux = await list_all_tmux(local_machine, fleet)
        await store.update_fleet(fleet)
        await store.update_sessions(sessions)
        await store.update_tmux(tmux)
        store.set_last_scan(_now_iso())
        log.info("POST /api/sessions/scan: %d sessions, %.2fs", len(sessions), time.monotonic() - t0)
        return web.json_response(
            {
                "ok": True,
                "sessions": [s.to_dict() for s in sessions],
                "tmux": enrich_tmux_dicts(tmux, sessions),
                "last_scan": store.last_scan(),
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
    terminal_id = body.get("terminal_id") or None
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

        # Build the command that gets typed into the pane after cd. Uses
        # build_pane_command so psmux panes on Windows (which default to
        # PowerShell, not cmd.exe) get `Set-Location 'C:\\...'; claude`
        # instead of the broken `cd /d C:\\... && claude` form.
        claude_cmd = adapter.build_pane_command(
            cwd,
            session_id=session_id if session_id else None,
            skip_permissions=skip,
        )

        project = cwd.replace("\\", "/").rstrip("/").split("/")[-1] if cwd else "claude"
        project_safe = re.sub(r"[^a-zA-Z0-9_-]", "-", project) or "claude"
        store: StateStore = request.app["store"]
        existing_names = [t.name for t in store.tmux() if t.machine == machine]
        safe_name = adapter.generate_mux_session_name(machine, project_safe, existing_names)

        if is_remote_windows:
            # Windows psmux 3.3.0+ daemonizes cleanly — verified empirically:
            # a session created with `new-session -d` from one SSH call
            # SURVIVES after the SSH disconnects, and send-keys from a second
            # SSH call correctly drives the pane. So:
            #
            #   (1) Run create + send-keys in ONE blocking SSH call from the
            #       server. Non-interactive, definite exit code, no racing.
            #   (2) Open a local terminal that runs a single `ssh -t "psmux
            #       attach -t NAME"`. One command, no AppleScript keystroke
            #       automation, no delays.
            #
            # Replaces the old _launch_macos_multi dance that typed 4
            # commands into an iTerm2 window with fixed delays between them.
            # That was inherently race-prone: if SSH took longer than the
            # fixed delay, subsequent psmux commands leaked into the local
            # zsh shell instead of landing in PowerShell on the remote.
            info = FLEET_MACHINES.get(machine, {})
            alias = info.get("ssh_alias", machine)
            quoted_name = adapter._ps_single_quote(safe_name)
            create_line = f"{adapter.mux_type} new-session -d -s {quoted_name}"
            sendkeys_line = adapter.mux_send_keys_ps(safe_name, claude_cmd)
            setup_payload = f"{create_line}; {sendkeys_line}"

            rc, _stdout, stderr = await _pool_exec(machine, setup_payload, timeout=20)
            if rc != 0:
                err = stderr.decode("utf-8", errors="replace").strip() or "psmux setup failed"
                log.error("is_remote_windows setup rc=%d: %s", rc, err[:300])
                return web.json_response({"ok": False, "error": err[:400]}, status=500)

            # `safe_name` is identifier-sanitised (see generate_mux_session_name)
            # — no shell metacharacters. Skip the PS single-quote wrap here:
            # shlex.quote would otherwise inject POSIX `'"'"'` escapes around
            # those inner singles, and the local PowerShell terminal that runs
            # this SSH wrapper can't parse them — the trailing parts leak as
            # separate LOCAL statements (see bug class addressed in May 2026).
            attach_remote_cmd = f"{adapter.mux_type} attach -t {safe_name}"
            terminal_cmd = f"ssh {shlex.quote(alias)} -t {shlex.quote(attach_remote_cmd)}"
            result = await launch_terminal(terminal_cmd, terminal_id=terminal_id)
        else:
            # tmux on macOS/Linux: create session + attach works perfectly
            result = await launch_new_tmux_and_attach(safe_name, machine, cwd=cwd, command=claude_cmd, terminal_id=terminal_id)
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
            result = await launch_terminal(full_cmd, terminal_id=terminal_id)
        else:
            # Remote: build command in the SSH landing shell syntax
            # (PowerShell for Windows, bash for Linux/macOS)
            from .config import FLEET_MACHINES as _FM
            info = _FM.get(machine, {})
            alias = info.get("ssh_alias", machine)
            full_cmd = adapter.build_new_session_command_ssh(cwd, skip_permissions=skip)
            ssh_cmd = _ssh_path_prefix(machine) + full_cmd
            terminal_cmd = f"ssh {shlex.quote(alias)} -t {shlex.quote(ssh_cmd)}"
            result = await launch_terminal(terminal_cmd, terminal_id=terminal_id)

        status = 200 if result.get("ok") else 500
        log.info("POST /api/sessions/launch NEW machine=%s %d", machine, status)
        return web.json_response(result, status=status)
    else:
        result = await launch_claude_session(cwd, session_id, machine, skip_permissions=skip, terminal_id=terminal_id)
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
            store: StateStore = request.app["store"]
            tmux = await list_all_tmux(local_machine, store.fleet())
            await store.update_tmux(tmux)
        except Exception:
            pass
    return web.json_response(result, status=status)


async def handle_fleet(request: web.Request) -> web.Response:
    store: StateStore = request.app["store"]
    return web.json_response(store.fleet())


async def handle_tmux(request: web.Request) -> web.Response:
    """Return all tmux sessions across fleet."""
    store: StateStore = request.app["store"]
    return web.json_response(enrich_tmux_dicts(store.tmux(), store.sessions()))


async def handle_tmux_machine(request: web.Request) -> web.Response:
    """Return tmux sessions for a specific machine."""
    machine = request.match_info["machine"]
    store: StateStore = request.app["store"]
    tmux_for_machine = [t for t in store.tmux() if t.machine == machine]
    return web.json_response(enrich_tmux_dicts(tmux_for_machine, store.sessions()))


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
        store: StateStore = request.app["store"]
        existing_names = [t.name for t in store.tmux() if t.machine == machine]
        name = adapter.generate_mux_session_name(machine, project_safe, existing_names)

    name = sanitize_mux_name(name)
    result = await create_tmux_session(machine, name, cwd or None, body.get("command"))
    status = 200 if result.get("ok") else 500
    if result.get("ok"):
        result["name"] = result.get("name", name)
        log.info("POST /api/tmux/create machine=%s name=%s %d", machine, result["name"], status)
        local_machine = request.app["local_machine"]
        store: StateStore = request.app["store"]
        tmux = await list_all_tmux(local_machine, store.fleet())
        await store.update_tmux(tmux)
    else:
        log.error("POST /api/tmux/create machine=%s name=%s %d: %s", machine, name, status, result.get("error"))
    return web.json_response(result, status=status)


async def _tmux_has_session(machine: str, session_name: str, mux: str) -> tuple[int, str, str]:
    """Run `<mux> has-session -t <name>` on a fleet machine. Returns (rc, stdout, stderr).

    rc == 0 → session exists. rc > 0 → session does not exist. rc < 0 → probe
    itself failed (SSH error, timeout, etc.) and the caller should treat the
    target as unreachable rather than definitively dead.
    """
    from .executor import get_executor, SSHExecutor
    executor = get_executor(machine)
    try:
        if executor.is_local:
            rc, stdout, stderr = await executor.exec(
                [mux, "has-session", "-t", session_name], timeout=5,
            )
            return rc, stdout.decode("utf-8", errors="replace"), stderr.decode("utf-8", errors="replace")
        else:
            assert isinstance(executor, SSHExecutor)
            quoted = shlex.quote(session_name)
            rc, stdout, stderr = await executor.exec_shell(
                f"{mux} has-session -t {quoted}", timeout=8,
            )
            return rc, stdout.decode("utf-8", errors="replace"), stderr.decode("utf-8", errors="replace")
    except asyncio.TimeoutError:
        return -1, "", "timed out"
    except OSError as exc:
        return -1, "", str(exc)


async def handle_tmux_verify(request: web.Request) -> web.Response:
    """Pre-flight liveness probe for a tmux/psmux session on a fleet machine.

    Used by the frontend Attach button to avoid spawning a terminal for a
    session that no longer exists (stale scan list). Response is informational
    only — no side effects, no state mutation.
    """
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "invalid JSON body"}, status=400)
    machine = body.get("machine", "")
    session_name = body.get("session_name", "")
    if not machine or not session_name:
        return web.json_response(
            {"ok": False, "error": "machine and session_name required"}, status=400,
        )
    info = FLEET_MACHINES.get(machine, {})
    mux = info.get("mux", "tmux")
    rc, _stdout, stderr = await _tmux_has_session(machine, session_name, mux)
    alive = (rc == 0)
    payload: dict = {
        "ok": True,
        "alive": alive,
        "machine": machine,
        "session_name": session_name,
    }
    if not alive and rc < 0:
        # Probe itself failed — surface the reason so the UI can distinguish
        # "session gone" from "host unreachable".
        payload["error"] = stderr.strip() or "probe failed"
    elif not alive and stderr:
        payload["error"] = stderr.strip()
    return web.json_response(payload)


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
    terminal_id = body.get("terminal_id") or None
    if not machine or not session_name:
        return web.json_response({"ok": False, "error": "machine and session_name required"}, status=400)
    result = await launch_tmux_attach(
        session_name, machine,
        skip_permissions=skip_permissions,
        terminal_id=terminal_id,
    )
    status = 200 if result.get("ok") else 500
    if result.get("ok"):
        log.info("POST /api/tmux/connect machine=%s session=%s terminal=%s %d", machine, session_name, terminal_id or "auto", status)
    else:
        log.error("POST /api/tmux/connect machine=%s session=%s terminal=%s %d: %s", machine, session_name, terminal_id or "auto", status, result.get("error"))
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
        store: StateStore = request.app["store"]
        tmux = await list_all_tmux(local_machine, store.fleet())
        await store.update_tmux(tmux)
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

    try:
        rc, stdout, stderr = await _pool_exec(
            machine, "python3 -", timeout=15, input=py_script.encode(),
        )
    except asyncio.TimeoutError:
        return web.json_response({"ok": False, "error": "SSH timeout"}, status=504)

    if rc != 0:
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

    try:
        rc, stdout, stderr = await _pool_exec(
            machine, "python3 -", timeout=10, input=py_script.encode(),
        )
    except asyncio.TimeoutError:
        return web.json_response({"ok": False, "error": "SSH timeout"}, status=504)

    if rc != 0:
        err = stderr.decode().strip()
        return web.json_response({"ok": False, "error": err or "SSH mkdir failed"}, status=500)

    return web.json_response({"ok": True, "path": path})


async def handle_projects_create(request: web.Request) -> web.Response:
    """Create a project folder (mkdir -p) and optionally git-init it."""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "invalid JSON body"}, status=400)

    machine = body.get("machine", "")
    path = body.get("path", "")
    init_git = bool(body.get("init_git", False))

    if not path:
        return web.json_response({"ok": False, "error": "path required"}, status=400)

    from .config import FLEET_MACHINES
    local_machine = request.app["local_machine"]
    is_local = (not machine) or (machine == local_machine)

    # Validate machine is known (allow empty/local)
    if machine and machine != local_machine and machine not in FLEET_MACHINES:
        return web.json_response({"ok": False, "error": f"Unknown machine: {machine}"}, status=400)

    if is_local:
        import pathlib as _pathlib
        import subprocess as _subprocess
        try:
            p = _pathlib.Path(path)
            if not p.is_absolute():
                return web.json_response({"ok": False, "error": "path must be absolute"}, status=400)
            created = not p.exists()
            p.mkdir(parents=True, exist_ok=True)
            git_initialized = False
            if init_git and not (p / ".git").exists():
                result = _subprocess.run(
                    ["git", "init"],
                    cwd=str(p),
                    capture_output=True,
                    text=True,
                    timeout=10,
                    **_win32_kwargs(),
                )
                if result.returncode != 0:
                    return web.json_response({
                        "ok": False,
                        "error": f"git init failed: {result.stderr.strip()}",
                    }, status=500)
                git_initialized = True
            return web.json_response({"ok": True, "created": created, "git_initialized": git_initialized})
        except PermissionError as exc:
            return web.json_response({"ok": False, "error": f"Permission denied: {exc}"}, status=403)
        except Exception as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=500)

    info = FLEET_MACHINES[machine]
    ssh_alias = info.get("ssh_alias", machine)
    os_type = info.get("os", "linux")  # "win32", "darwin", "linux"
    is_windows = os_type == "win32"

    if is_windows:
        # PowerShell one-liner: create dir, optionally git init
        esc = path.replace("'", "''")
        if init_git:
            ps_cmd = (
                f"$p='{esc}';"
                "$exists=Test-Path $p;"
                "New-Item -ItemType Directory -Force -Path $p | Out-Null;"
                "if (-not (Test-Path \"$p\\.git\")) { Set-Location $p; git init | Out-Null; "
                "Write-Output \"git_initialized=true\" } else { Write-Output \"git_initialized=false\" };"
                "Write-Output \"created=$(!$exists)\""
            )
        else:
            ps_cmd = (
                f"$p='{esc}';"
                "$exists=Test-Path $p;"
                "New-Item -ItemType Directory -Force -Path $p | Out-Null;"
                "Write-Output \"git_initialized=false\";"
                "Write-Output \"created=$(!$exists)\""
            )
        try:
            # Windows SSH login shell is PowerShell — send the PS command directly.
            rc, stdout, stderr = await _pool_exec(machine, ps_cmd, timeout=15)
        except asyncio.TimeoutError:
            return web.json_response({"ok": False, "error": "SSH timeout"}, status=504)
        if rc != 0:
            err = stderr.decode().strip() if isinstance(stderr, bytes) else str(stderr).strip()
            return web.json_response({"ok": False, "error": err or "Remote command failed"}, status=500)
        out = stdout.decode().strip() if isinstance(stdout, bytes) else str(stdout).strip()
        created = "created=True" in out
        git_inited = "git_initialized=true" in out
        return web.json_response({"ok": True, "created": created, "git_initialized": git_inited})
    else:
        # Unix: bash one-liner
        esc = path.replace("'", "\\'")
        if init_git:
            shell_cmd = (
                f"mkdir -p '{esc}' && "
                f"cd '{esc}' && "
                "if [ ! -d .git ]; then git init && echo git_initialized=true; "
                "else echo git_initialized=false; fi"
            )
        else:
            shell_cmd = f"mkdir -p '{esc}' && echo git_initialized=false"
        try:
            rc, stdout, stderr = await _pool_exec(machine, shell_cmd, timeout=15)
        except asyncio.TimeoutError:
            return web.json_response({"ok": False, "error": "SSH timeout"}, status=504)
        if rc != 0:
            err = stderr.decode().strip() if isinstance(stderr, bytes) else str(stderr).strip()
            return web.json_response({"ok": False, "error": err or "Remote command failed"}, status=500)
        out = stdout.decode().strip() if isinstance(stdout, bytes) else str(stdout).strip()
        git_inited = "git_initialized=true" in out
        return web.json_response({"ok": True, "created": True, "git_initialized": git_inited})


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

    try:
        rc, stdout, stderr = await _pool_exec(
            machine, "python3 -", timeout=10, input=py_script.encode(),
        )
    except asyncio.TimeoutError:
        return web.json_response({"ok": False, "error": "SSH timeout"}, status=504)

    if rc != 0:
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


async def handle_logs_tail(request: web.Request) -> web.Response:
    """GET /api/logs/tail?lines=N — tail the rotating log file on disk.

    The file is capped at 5 MB with 2 rotated backups. Text/plain response
    matches `tail -n` so curl | less works.
    """
    try:
        lines = int(request.query.get("lines", "500"))
    except (ValueError, TypeError):
        lines = 500
    lines = max(1, min(lines, 20000))
    log_path: pathlib.Path | None = request.app.get("log_file_path")
    if not log_path or not log_path.exists():
        return web.Response(text="", content_type="text/plain")
    # Read tail efficiently from end of file.
    try:
        with log_path.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()
            block = 8192
            data = b""
            while size > 0 and data.count(b"\n") <= lines:
                read_size = min(block, size)
                size -= read_size
                f.seek(size)
                data = f.read(read_size) + data
        text = data.decode("utf-8", errors="replace")
        tail = "\n".join(text.splitlines()[-lines:])
    except Exception as exc:
        return web.Response(text=f"error reading log: {exc}\n", content_type="text/plain", status=500)
    return web.Response(text=tail + "\n", content_type="text/plain")


async def handle_update_watchdog(request: web.Request) -> web.Response:
    """GET /api/update/watchdog — proxy fleet-watchdog's view of this app.

    Hits http://127.0.0.1:44732/apps/claude-manager. If the watchdog is not
    running locally, returns {ok: false, watchdog_available: false}. Clients
    should fall back to /api/update/check (direct GitHub poll).
    """
    import aiohttp as _aiohttp
    url = "http://127.0.0.1:44732/apps/claude-manager"
    try:
        timeout = _aiohttp.ClientTimeout(total=3)
        async with _aiohttp.ClientSession(timeout=timeout) as sess:
            async with sess.get(url) as resp:
                if resp.status != 200:
                    return web.json_response({
                        "ok": False, "watchdog_available": True,
                        "error": f"watchdog returned {resp.status}",
                    })
                data = await resp.json()
    except Exception as exc:
        return web.json_response({
            "ok": False, "watchdog_available": False,
            "error": f"watchdog unreachable: {exc}",
        })
    installed = data.get("installed_version")
    remote = data.get("remote_version")
    return web.json_response({
        "ok": True,
        "watchdog_available": True,
        "installed_version": installed,
        "remote_version": remote,
        "update_available": bool(installed is not None and remote is not None and remote > installed),
        "raw": data,
    })


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
        store: StateStore = app["store"]
        await store.update_sessions([])
        await store.update_fleet({})
        await store.update_tmux([])
        store.set_last_scan(None)

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


async def handle_projects_pin(request: web.Request) -> web.Response:
    """Add a project id to the pinned list. Pinning is per-project now —
    sessions inherit their parent project's pin state."""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "invalid JSON body"}, status=400)
    project_id = body.get("project_id", "")
    if not project_id:
        return web.json_response({"ok": False, "error": "project_id required"}, status=400)
    prefs = _load_prefs()
    pinned = prefs.get("pinned_projects", [])
    if project_id not in pinned:
        pinned.append(project_id)
    prefs["pinned_projects"] = pinned
    _save_prefs(prefs)
    return web.json_response({"ok": True, "pinned_projects": pinned})


async def handle_projects_unpin(request: web.Request) -> web.Response:
    """Remove a project id from the pinned list."""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "invalid JSON body"}, status=400)
    project_id = body.get("project_id", "")
    if not project_id:
        return web.json_response({"ok": False, "error": "project_id required"}, status=400)
    prefs = _load_prefs()
    pinned = prefs.get("pinned_projects", [])
    pinned = [p for p in pinned if p != project_id]
    prefs["pinned_projects"] = pinned
    _save_prefs(prefs)
    return web.json_response({"ok": True, "pinned_projects": pinned})


# ---------------------------------------------------------------------------
# Terminal discovery
# ---------------------------------------------------------------------------
# Short-lived cache: (machine → (timestamp, result)). New terminals aren't
# installed every scan tick, so 5 min is plenty. Also spares remote machines
# from re-probe storms when many UI clients open the dropdown at once.
_TERMINAL_CACHE: dict[str, tuple[float, list[dict]]] = {}
_TERMINAL_CACHE_TTL = 300.0


async def handle_machine_terminals(request: web.Request) -> web.Response:
    """Return the list of terminal emulators installed on the given machine.

    Each entry: {id, name, priority}. Priority-desc; the highest is the
    'auto' pick. Uses a 5-min cache per machine.
    """
    from . import terminals as _terms
    from .subprocess_utils import run_with_timeout
    from .executor import local_env

    machine = request.match_info["machine"]
    info = FLEET_MACHINES.get(machine, {})
    # Map config os codes to the adapter registry's os key.
    os_key = info.get("os") or "darwin"
    if os_key == "linux":
        reg_os = "linux"
    elif os_key == "win32":
        reg_os = "win32"
    else:
        reg_os = "darwin"

    now = time.monotonic()
    cached = _TERMINAL_CACHE.get(machine)
    if cached and (now - cached[0]) < _TERMINAL_CACHE_TTL:
        return web.json_response({"ok": True, "machine": machine, "os": reg_os, "terminals": cached[1]})

    local_machine = request.app.get("local_machine")
    is_local = (machine == local_machine)

    # Remote Windows gate: every SSH exec to a Windows target spawns a fresh
    # powershell.exe + ConPTY on the remote desktop (no ControlMaster on
    # Windows OpenSSH — see commit 2483c7e). Probing 5 adapters = 5 popup
    # flashes per UI terminal-pick. Skip the probe and return only the
    # adapters guaranteed to exist on every Windows install. wt/pwsh/git-bash
    # are omitted to avoid false-positives; users who need them can run the
    # daemon locally on that Windows box so is_local kicks in.
    if reg_os == "win32" and not is_local:
        static_adapters = [
            {"id": "powershell", "name": "PowerShell (classic window)", "priority": 80},
            {"id": "cmd",        "name": "Command Prompt",               "priority": 30},
        ]
        _TERMINAL_CACHE[machine] = (now, static_adapters)
        return web.json_response({
            "ok": True, "machine": machine, "os": reg_os, "terminals": static_adapters,
        })

    # Runner shape: async (shell_string) -> (rc, stdout, stderr).
    if is_local:
        # Native shell per OS. Daemon's host OS may differ from reg_os if the
        # daemon somehow runs on a machine not in the fleet config — in that
        # case fall back to the daemon's actual platform.
        if sys.platform == "win32":
            async def runner(sh: str):
                return await run_with_timeout(
                    ["powershell", "-NoProfile", "-Command", sh],
                    timeout=6, env=local_env(),
                )
        else:
            async def runner(sh: str):
                return await run_with_timeout(
                    ["/bin/bash", "-c", sh],
                    timeout=6, env=local_env(),
                )
    else:
        # Remote: route through the persistent asyncssh pool.
        from .ssh_pool import default_pool
        pool = default_pool()
        async def runner(sh: str):
            return await pool.run(machine, sh, timeout=8)

    try:
        avail = await _terms.list_available(reg_os, runner)
    except Exception as exc:
        log.warning("terminal probe(%s) failed: %s", machine, exc)
        return web.json_response({"ok": False, "error": str(exc), "terminals": []}, status=200)

    _TERMINAL_CACHE[machine] = (now, avail)
    return web.json_response({"ok": True, "machine": machine, "os": reg_os, "terminals": avail})


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
    try:
        rc, stdout, stderr = await _pool_exec(
            machine, "python3 -", timeout=20, input=script.encode(),
        )
    except asyncio.TimeoutError:
        return web.json_response({"ok": False, "error": "SSH timeout"}, status=504)

    if rc != 0:
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

    try:
        rc, stdout, stderr = await _pool_exec(
            machine, "python3 -", timeout=15, input=py_script.encode(),
        )
    except asyncio.TimeoutError:
        return web.json_response({"ok": False, "error": "SSH timeout"}, status=504)

    if rc != 0:
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
    """Discard a WebSocket from a pane stream's subscriber set; tear down the
    stream when it's the last subscriber.

    Safe to call for unknown (machine, session_name) keys — no-op.
    """
    key = (machine, session_name)
    info = state["pane_streams"].get(key)
    if not info:
        return
    info["subscribers"].discard(ws)
    if not info["subscribers"]:
        task = info.get("task")
        if task is not None and not task.done():
            task.cancel()
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
    store: StateStore = request.app["store"]
    store.add_ws(ws)
    client_addr = request.remote or "unknown"

    # Auth gate: if enabled and client is NOT loopback, require first message = auth
    auth_cfg = request.app.get("auth_config")
    ws_authed = (not auth_cfg) or (not auth_cfg.enabled) or is_loopback(client_addr)

    log.info("WS connect: %s (authed=%s, total: %d)",
             client_addr, ws_authed, store.ws_count())

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
                        snap = [s.to_dict() for s in store.sessions()]
                    elif channel == "fleet":
                        snap = store.fleet()
                    elif channel == "tmux":
                        snap = enrich_tmux_dicts(store.tmux(), store.sessions())
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
                        await store.subscribe_pane(
                            machine, session_name, ws,
                            lambda m, s: _pane_stream_loop(request.app, m, s),
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
                        await store.unsubscribe_pane(machine, session_name, ws)

            elif msg.type in (web.WSMsgType.ERROR, web.WSMsgType.CLOSE):
                break
    finally:
        store.remove_ws(ws)
        # Clean up pane stream subscriptions for this client
        await store.unsubscribe_pane_all(ws)
        log.info("WS disconnect: %s (total: %d)", client_addr, store.ws_count())

    return ws


# ---------------------------------------------------------------------------
# README render + OS-native folder open
# ---------------------------------------------------------------------------

_README_SUFFIXES_LOWER = {"readme.md", "readme.md", "readme", "readme"}  # canonical check is suffix-only


def _is_valid_readme_path(path: str) -> bool:
    """Return True if path looks like an absolute README* file (case-insensitive, no traversal)."""
    if not path:
        return False
    import pathlib as _pl
    p = _pl.PurePosixPath(path) if not path.startswith("C:") and not path.startswith("c:") else _pl.PureWindowsPath(path)
    # No traversal
    if ".." in path:
        return False
    # Must be absolute
    if not (path.startswith("/") or (len(path) >= 3 and path[1] == ":" and path[2] in ("/", "\\"))):
        return False
    # Filename must start with readme (case-insensitive)
    fname = path.replace("\\", "/").split("/")[-1]
    return fname.lower().startswith("readme")


async def handle_sessions_readme(request: web.Request) -> web.Response:
    """GET /api/sessions/readme?machine=<m>&path=<abs-readme-path>

    Returns {ok, content, truncated} on success, {ok: false, error} on failure.
    Content capped at 256 KiB.
    """
    machine = request.rel_url.query.get("machine", "")
    path = request.rel_url.query.get("path", "")

    if not machine or machine not in FLEET_MACHINES:
        # Also allow local machine name
        local_machine = request.app.get("local_machine")
        if machine and machine != local_machine:
            return web.json_response({"ok": False, "error": f"unknown machine: {machine}"}, status=400)

    if not _is_valid_readme_path(path):
        return web.json_response({"ok": False, "error": "invalid path — must be absolute and point to README*"}, status=400)

    MAX_BYTES = 256 * 1024  # 256 KiB

    local_machine = request.app.get("local_machine")
    is_local = (not machine) or (machine == local_machine)

    if is_local:
        import pathlib as _pl
        try:
            p = _pl.Path(path)
            if not p.is_file():
                return web.json_response({"ok": False, "error": "file not found"}, status=404)
            raw = p.read_bytes()
            truncated = len(raw) > MAX_BYTES
            content = raw[:MAX_BYTES].decode("utf-8", errors="replace")
            return web.json_response({"ok": True, "content": content, "truncated": truncated})
        except PermissionError:
            return web.json_response({"ok": False, "error": "permission denied"}, status=403)
        except Exception as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=500)

    # Remote machine
    info = FLEET_MACHINES.get(machine, {})
    is_windows = info.get("os") == "win32"
    from .executor import SSHExecutor
    executor = SSHExecutor(machine)

    try:
        if is_windows:
            # PowerShell: read up to 256 KiB
            # Escape single quotes in path for PS
            ps_path = path.replace("'", "''")
            ps_cmd = (
                f"$bytes = [System.IO.File]::ReadAllBytes('{ps_path}');"
                f"if ($bytes.Length -gt {MAX_BYTES}) {{ $bytes = $bytes[0..{MAX_BYTES - 1}]; Write-Host 'TRUNCATED' }};"
                f"[System.Text.Encoding]::UTF8.GetString($bytes)"
            )
            rc, stdout, stderr = await executor.exec_shell(
                f"powershell -NoProfile -Command \"{ps_cmd}\"",
                timeout=15,
            )
        else:
            # Unix: cat | head -c 262144
            import shlex as _shlex
            quoted = _shlex.quote(path)
            rc, stdout, stderr = await executor.exec_shell(
                f"cat {quoted} | head -c {MAX_BYTES + 1}",
                timeout=15,
            )
        if rc != 0:
            err = stderr.decode("utf-8", errors="replace").strip() if isinstance(stderr, bytes) else str(stderr)
            return web.json_response({"ok": False, "error": err or "remote read failed"}, status=500)
        raw = stdout if isinstance(stdout, bytes) else stdout.encode()
        truncated = len(raw) > MAX_BYTES
        content = raw[:MAX_BYTES].decode("utf-8", errors="replace")
        # Strip Windows TRUNCATED marker if present
        if is_windows and "TRUNCATED" in content:
            content = content.replace("TRUNCATED\n", "").replace("TRUNCATED", "")
            truncated = True
        return web.json_response({"ok": True, "content": content, "truncated": truncated})
    except asyncio.TimeoutError:
        return web.json_response({"ok": False, "error": "SSH timeout"}, status=504)
    except Exception as exc:
        return web.json_response({"ok": False, "error": str(exc)}, status=500)


async def handle_fs_open(request: web.Request) -> web.Response:
    """POST /api/fs/open — open a folder in the OS native file manager.

    Body: {machine: str, path: str}
    Loopback-only. For local machine: launches open/explorer/xdg-open.
    For remote: returns smb:// URL info (the frontend uses <a href> directly).
    """
    from .auth import is_loopback
    if not is_loopback(request.remote):
        return web.json_response({"ok": False, "error": "loopback only"}, status=403)

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "invalid JSON body"}, status=400)

    machine = body.get("machine", "")
    path = body.get("path", "")
    if not path:
        return web.json_response({"ok": False, "error": "path required"}, status=400)

    local_machine = request.app.get("local_machine")
    is_local = (not machine) or (machine == local_machine)

    if not is_local:
        # Remote: compute smb/UNC URL authoritatively and return it
        info = FLEET_MACHINES.get(machine, {})
        ip = info.get("ip", machine)
        # Normalise path separators
        norm = path.replace("\\", "/")
        # Strip drive letter on Windows paths (C:/Users/... → Users/...)
        if len(norm) >= 3 and norm[1] == ":" and norm[2] == "/":
            share_root = norm[0]  # drive letter as share name
            rest = norm[3:]       # everything after C:/
            smb_url = f"smb://{ip}/{share_root}/{rest}"
            unc_url = f"\\\\{ip}\\{share_root}\\{rest.replace('/', chr(92))}"
        else:
            # Unix path: use first segment as share name
            parts = [p for p in norm.split("/") if p]
            if len(parts) >= 2:
                smb_url = f"smb://{ip}/{parts[0]}/{'/'.join(parts[1:])}"
                _unc_parts = "\\".join(parts)
                unc_url = f"\\\\{ip}\\{_unc_parts}"
            elif parts:
                smb_url = f"smb://{ip}/{parts[0]}"
                unc_url = f"\\\\{ip}\\{parts[0]}"
            else:
                smb_url = f"smb://{ip}"
                unc_url = f"\\\\{ip}"
        return web.json_response({
            "ok": False,
            "error": "use smb:// link for remote folders",
            "smb_url": smb_url,
            "unc_url": unc_url,
        })

    # Local: launch native file manager
    import shlex as _shlex
    try:
        if sys.platform == "darwin":
            cmd = ["open", path]
        elif sys.platform == "win32":
            cmd = ["explorer.exe", path]
        else:
            cmd = ["xdg-open", path]
        rc, _, stderr = await run_with_timeout(cmd, timeout=5)
        if rc != 0:
            err = stderr.decode("utf-8", errors="replace").strip() if isinstance(stderr, bytes) else str(stderr)
            return web.json_response({"ok": False, "error": err or f"command exited {rc}"}, status=500)
        return web.json_response({"ok": True})
    except asyncio.TimeoutError:
        # open/explorer usually returns immediately; timeout means something odd happened
        return web.json_response({"ok": False, "error": "command timed out"}, status=504)
    except Exception as exc:
        return web.json_response({"ok": False, "error": str(exc)}, status=500)


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
    # error_middleware is FIRST (outermost): it has to wrap every other
    # middleware and every handler so unhandled exceptions anywhere in the
    # chain become structured JSON 500 responses the UI can render.
    app = web.Application(middlewares=[
        error_middleware,
        trace_middleware,
        cors_middleware,
        rate_limit_middleware,
        auth_middleware,
    ])

    # Install in-memory log handler on the claude_manager logger hierarchy
    mem_handler = MemoryLogHandler(max_entries=500)
    mem_handler.setFormatter(logging.Formatter("%(message)s"))
    cm_logger = logging.getLogger("claude_manager")
    cm_logger.addHandler(mem_handler)
    app["log_handler"] = mem_handler

    # Rotating file log — 5 MB cap, 2 backups, oldest entries drop automatically.
    # Tailable via GET /api/logs/tail.
    from logging.handlers import RotatingFileHandler
    log_dir = pathlib.Path.home() / ".claude-manager" / "logs"
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / "claude-manager.log"
        file_handler = RotatingFileHandler(
            str(log_file), maxBytes=5 * 1024 * 1024, backupCount=2, encoding="utf-8"
        )
        file_handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s: %(message)s"
        ))
        # Attach to root so subprocess/aiohttp/asyncssh logs land there too.
        logging.getLogger().addHandler(file_handler)
        app["log_file_path"] = log_file
        log.info("rotating log file: %s (5MB x 2 backups)", log_file)
    except Exception as exc:
        log.warning("failed to set up rotating log file: %s", exc)
        app["log_file_path"] = None

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
    app["store"] = StateStore(app)

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
    app.router.add_get("/api/logs/tail", handle_logs_tail)
    app.router.add_get("/api/update/watchdog", handle_update_watchdog)
    app.router.add_get("/api/sessions", handle_sessions_all)
    app.router.add_get("/api/projects", handle_projects)
    app.router.add_get("/api/sessions/{machine}", handle_sessions_machine)
    app.router.add_post("/api/sessions/scan", handle_sessions_scan)
    app.router.add_post("/api/sessions/launch", handle_sessions_launch)
    app.router.add_get("/api/sessions/readme", handle_sessions_readme)
    app.router.add_post("/api/fs/open", handle_fs_open)
    app.router.add_post("/api/sessions/pin", handle_sessions_pin)
    app.router.add_post("/api/sessions/unpin", handle_sessions_unpin)
    app.router.add_post("/api/projects/pin", handle_projects_pin)
    app.router.add_post("/api/projects/unpin", handle_projects_unpin)
    app.router.add_post("/api/projects/pull", handle_projects_pull)
    app.router.add_get("/api/machines/{machine}/terminals", handle_machine_terminals)
    app.router.add_post("/api/sessions/archive", handle_sessions_archive)
    app.router.add_post("/api/sessions/unarchive", handle_sessions_unarchive)
    app.router.add_post("/api/sessions/rename", handle_sessions_rename)
    app.router.add_post("/api/hardware", handle_hardware)
    app.router.add_get("/api/fleet", handle_fleet)
    app.router.add_get("/api/tmux", handle_tmux)
    app.router.add_get("/api/tmux/{machine}", handle_tmux_machine)
    app.router.add_post("/api/tmux/create", handle_tmux_create)
    app.router.add_post("/api/tmux/verify", handle_tmux_verify)
    app.router.add_post("/api/tmux/connect", handle_tmux_connect)
    app.router.add_post("/api/tmux/connect-remote", handle_tmux_connect_remote)
    app.router.add_post("/api/tmux/kill", handle_tmux_kill)
    app.router.add_post("/api/tmux/capture", handle_tmux_capture)
    app.router.add_post("/api/browse", handle_browse)
    app.router.add_post("/api/drives", handle_drives)
    app.router.add_post("/api/mkdir", handle_mkdir)
    app.router.add_post("/api/projects/create", handle_projects_create)
    app.router.add_get("/api/preferences", handle_preferences_get)
    app.router.add_post("/api/preferences", handle_preferences_post)
    app.router.add_post("/api/restart", handle_restart)
    app.router.add_post("/api/exit", handle_exit)

    # WebSocket
    app.router.add_get("/ws", handle_ws)

    # Static web UI — serve index.html at / and static assets
    web_dir = pathlib.Path(__file__).parent / "web"
    if web_dir.is_dir():
        index_html = web_dir / "index.html"

        async def handle_index(request: web.Request) -> web.Response:
            # No-cache so pywebview picks up frontend changes after a restart.
            return web.FileResponse(index_html, headers={
                "Cache-Control": "no-cache, no-store, must-revalidate",
                "Pragma": "no-cache",
                "Expires": "0",
            })

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
