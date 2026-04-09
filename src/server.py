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
from datetime import datetime, timezone
from typing import Any

from aiohttp import web

from .config import DEFAULT_BIND, DEFAULT_PORT, SCAN_INTERVAL, detect_local_machine
from .fleet import discover_fleet
from .scanner import ClaudeSession, scan_all

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
            app["state"]["fleet"] = fleet
            app["state"]["sessions"] = sessions
            app["state"]["last_scan"] = _now_iso()

            # Push to WebSocket subscribers
            await _push_to_ws(app, "sessions", [s.to_dict() for s in sessions])
            await _push_to_ws(app, "fleet", fleet)
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
        app["state"]["fleet"] = fleet
        app["state"]["sessions"] = sessions
        app["state"]["last_scan"] = _now_iso()
        await _push_to_ws(app, "sessions", [s.to_dict() for s in sessions])
        await _push_to_ws(app, "fleet", fleet)
        return web.json_response(
            {
                "ok": True,
                "sessions": [s.to_dict() for s in sessions],
                "last_scan": app["state"]["last_scan"],
            }
        )
    except Exception as exc:
        log.exception("forced scan failed")
        return web.json_response({"ok": False, "error": str(exc)}, status=500)


async def handle_sessions_launch(request: web.Request) -> web.Response:
    """Stub — Phase 2 will implement terminal launcher."""
    return web.json_response(
        {"ok": False, "error": "launch not implemented in Phase 1"},
        status=501,
    )


async def handle_fleet(request: web.Request) -> web.Response:
    fleet = request.app["state"]["fleet"]
    return web.json_response(fleet)


async def handle_tmux(request: web.Request) -> web.Response:
    """Stub — Phase 2 will implement tmux session listing."""
    return web.json_response([])


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
                        snap = []
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
    app.router.add_get("/api/fleet", handle_fleet)
    app.router.add_get("/api/tmux", handle_tmux)

    # WebSocket
    app.router.add_get("/ws", handle_ws)

    # Static web UI
    import pathlib
    web_dir = pathlib.Path(__file__).parent / "web"
    if web_dir.is_dir():
        app.router.add_static("/", web_dir, show_index=True)

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
