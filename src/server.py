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
import pathlib
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

from .config import DEFAULT_BIND, DEFAULT_PORT, SCAN_INTERVAL, detect_local_machine
from .fleet import discover_fleet
from .launcher import launch_claude_session, launch_tmux_attach, launch_tmux_attach_remote, launch_new_tmux_and_attach
from .scanner import ClaudeSession, scan_all
from .tmux_manager import TmuxSession, list_all_tmux, create_tmux_session, kill_tmux_session

log = logging.getLogger("claude_manager.server")


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
# State helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


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
    """
    local_machine = app["local_machine"]
    while True:
        try:
            fleet = await discover_fleet()
            sessions = await scan_all(local_machine, fleet)
            tmux = await list_all_tmux(local_machine, fleet)
            app["state"]["fleet"] = fleet
            app["state"]["sessions"] = sessions
            app["state"]["tmux"] = tmux
            app["state"]["last_scan"] = _now_iso()

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

async def handle_health(request: web.Request) -> web.Response:
    state = request.app["state"]
    sessions: list[ClaudeSession] = state["sessions"]
    fleet: dict = state["fleet"]
    return web.json_response(
        {
            "status": "ok",
            "port": request.app["port"],
            "machines": len(fleet),
            "sessions": len(sessions),
            "last_scan": state["last_scan"],
        }
    )


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
        return web.json_response(
            {
                "ok": True,
                "sessions": [s.to_dict() for s in sessions],
                "tmux": [t.to_dict() for t in tmux],
                "last_scan": app["state"]["last_scan"],
            }
        )
    except Exception as exc:
        log.exception("forced scan failed")
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
    if not session_id or not cwd:
        return web.json_response({"ok": False, "error": "session_id and cwd required"}, status=400)
    skip = body.get("skip_permissions", False)
    mode = body.get("mode", "terminal")
    if mode == "tmux":
        # Launch claude inside a new tmux session, then attach to it
        import re
        safe_name = re.sub(r"[^a-zA-Z0-9_-]", "-", session_id[:20]) or "claude"
        claude_cmd = f"claude --resume {session_id}"
        if skip:
            claude_cmd += " --dangerously-skip-permissions"
        result = await launch_new_tmux_and_attach(safe_name, machine, cwd=cwd, command=claude_cmd)
    else:
        result = await launch_claude_session(cwd, session_id, machine, skip_permissions=skip)
    status = 200 if result.get("ok") else 500
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
    """Create a new tmux session."""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "invalid JSON body"}, status=400)
    machine = body.get("machine", "")
    name = body.get("name", "")
    if not machine or not name:
        return web.json_response({"ok": False, "error": "machine and name required"}, status=400)
    result = await create_tmux_session(machine, name, body.get("cwd"), body.get("command"))
    status = 200 if result.get("ok") else 500
    if result.get("ok"):
        local_machine = request.app["local_machine"]
        fleet = request.app["state"]["fleet"]
        tmux = await list_all_tmux(local_machine, fleet)
        request.app["state"]["tmux"] = tmux
        await _push_to_ws(request.app, "tmux", [t.to_dict() for t in tmux])
    return web.json_response(result, status=status)


async def handle_tmux_connect(request: web.Request) -> web.Response:
    """Connect to an existing tmux session (opens terminal)."""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "invalid JSON body"}, status=400)
    machine = body.get("machine", "")
    session_name = body.get("session_name", "")
    if not machine or not session_name:
        return web.json_response({"ok": False, "error": "machine and session_name required"}, status=400)
    result = await launch_tmux_attach(session_name, machine)
    status = 200 if result.get("ok") else 500
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
        local_machine = request.app["local_machine"]
        fleet = request.app["state"]["fleet"]
        tmux = await list_all_tmux(local_machine, fleet)
        request.app["state"]["tmux"] = tmux
        await _push_to_ws(request.app, "tmux", [t.to_dict() for t in tmux])
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
    # fallback: df -h
    import subprocess, re
    lines = subprocess.check_output(["df","-Pl"], text=True).splitlines()[1:]
    drives = []
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

    # Build the python one-liner
    if path:
        escaped = path.replace("'", "\\'")
        py_expr = f"pathlib.Path('{escaped}')"
    else:
        py_expr = "pathlib.Path.home()"

    py_script = (
        "import json,pathlib,sys;"
        f"p={py_expr}.expanduser().resolve();"
        "assert p.exists() and p.is_dir();"
        "dirs=sorted([{'name':d.name,'path':str(d)} for d in p.iterdir() if d.is_dir() and not d.name.startswith('.')],key=lambda x:x['name'])[:200];"
        "drive='/';"
        "try:\n"
        " import psutil\n"
        " mps=sorted([pt.mountpoint for pt in psutil.disk_partitions(all=False)],key=len,reverse=True)\n"
        " drive=next((m for m in mps if str(p).startswith(m)),'/') \n"
        "except Exception: pass\n"
        "print(json.dumps({'path':str(p),'parent':str(p.parent),'drive':drive,'dirs':dirs}))"
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
        return web.json_response({"ok": False, "error": err or "SSH command failed"}, status=500)

    try:
        result = json.loads(stdout.decode().strip())
        result["ok"] = True
        return web.json_response(result)
    except Exception as exc:
        return web.json_response({"ok": False, "error": f"Parse error: {exc}"}, status=500)


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
# WebSocket handler
# ---------------------------------------------------------------------------

async def handle_ws(request: web.Request) -> web.WebSocketResponse:
    ws = web.WebSocketResponse()
    await ws.prepare(request)

    ws._subscribed_channels: set[str] = set()  # type: ignore[attr-defined]
    request.app["state"]["ws_clients"].add(ws)

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

            elif msg.type in (web.WSMsgType.ERROR, web.WSMsgType.CLOSE):
                break
    finally:
        request.app["state"]["ws_clients"].discard(ws)

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
    app = web.Application(middlewares=[cors_middleware])

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
    }

    # Lifecycle hooks
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)

    # REST routes
    app.router.add_get("/health", handle_health)
    app.router.add_get("/api/sessions", handle_sessions_all)
    app.router.add_get("/api/sessions/{machine}", handle_sessions_machine)
    app.router.add_post("/api/sessions/scan", handle_sessions_scan)
    app.router.add_post("/api/sessions/launch", handle_sessions_launch)
    app.router.add_post("/api/sessions/pin", handle_sessions_pin)
    app.router.add_post("/api/sessions/unpin", handle_sessions_unpin)
    app.router.add_post("/api/sessions/archive", handle_sessions_archive)
    app.router.add_post("/api/sessions/unarchive", handle_sessions_unarchive)
    app.router.add_get("/api/fleet", handle_fleet)
    app.router.add_get("/api/tmux", handle_tmux)
    app.router.add_get("/api/tmux/{machine}", handle_tmux_machine)
    app.router.add_post("/api/tmux/create", handle_tmux_create)
    app.router.add_post("/api/tmux/connect", handle_tmux_connect)
    app.router.add_post("/api/tmux/connect-remote", handle_tmux_connect_remote)
    app.router.add_post("/api/tmux/kill", handle_tmux_kill)
    app.router.add_post("/api/browse", handle_browse)
    app.router.add_post("/api/drives", handle_drives)
    app.router.add_post("/api/mkdir", handle_mkdir)
    app.router.add_get("/api/preferences", handle_preferences_get)
    app.router.add_post("/api/preferences", handle_preferences_post)

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
