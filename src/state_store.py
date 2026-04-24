"""
StateStore — centralised, lock-protected wrapper around app["state"].

All mutations go through atomic setters that immediately fan-out WS
notifications.  Getters return copies so HTTP handlers cannot mutate
shared state accidentally.  Pane-stream bookkeeping uses an asyncio.Lock
to prevent subscribe/unsubscribe races with the poll loop's subscriber check.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Callable

log = logging.getLogger("claude_manager.state_store")


class StateStore:
    def __init__(self, app) -> None:
        self._app = app
        self._state: dict = app["state"]
        self._pane_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Atomic setters — replace list/dict, then push WS notifications
    # ------------------------------------------------------------------

    async def update_sessions(self, sessions: list) -> None:
        self._state["sessions"] = sessions
        await self._push("sessions", [s.to_dict() for s in sessions])

    async def update_tmux(self, tmux: list) -> None:
        self._state["tmux"] = tmux
        from .session_link import enrich_tmux_dicts
        await self._push("tmux", enrich_tmux_dicts(tmux, self._state.get("sessions", [])))

    async def update_fleet(self, fleet: dict) -> None:
        self._state["fleet"] = fleet
        await self._push("fleet", fleet)

    def set_last_scan(self, ts: str) -> None:
        self._state["last_scan"] = ts

    def last_scan(self) -> str | None:
        return self._state.get("last_scan")

    # ------------------------------------------------------------------
    # Snapshot getters — return copies so callers can't mutate shared state
    # ------------------------------------------------------------------

    def sessions(self) -> list:
        return list(self._state["sessions"])

    def tmux(self) -> list:
        return list(self._state["tmux"])

    def fleet(self) -> dict:
        return dict(self._state["fleet"])

    # ------------------------------------------------------------------
    # WS client bookkeeping
    # ------------------------------------------------------------------

    def add_ws(self, ws) -> None:
        self._state["ws_clients"].add(ws)

    def remove_ws(self, ws) -> None:
        self._state["ws_clients"].discard(ws)

    def iter_ws(self) -> list:
        """Return a snapshot list so callers can't mutate-during-iteration."""
        return list(self._state["ws_clients"])

    def ws_count(self) -> int:
        return len(self._state["ws_clients"])

    def has_ws_clients(self) -> bool:
        return bool(self._state["ws_clients"])

    # ------------------------------------------------------------------
    # Internal WS push helper
    # ------------------------------------------------------------------

    async def _push(self, channel: str, data: Any) -> None:
        """Send update message to all WS clients subscribed to channel."""
        payload = json.dumps({"type": "update", "channel": channel, "data": data, "action": "refresh"})
        dead = set()
        for ws in list(self._state["ws_clients"]):
            subs: set[str] = getattr(ws, "_subscribed_channels", set())
            if channel in subs:
                try:
                    await ws.send_str(payload)
                except Exception:
                    dead.add(ws)
        self._state["ws_clients"] -= dead

    async def push_raw(self, payload: str) -> None:
        """Broadcast a pre-serialised payload to ALL WS clients (e.g. scan_progress)."""
        dead = set()
        for ws in list(self._state["ws_clients"]):
            try:
                await ws.send_str(payload)
            except Exception:
                dead.add(ws)
        self._state["ws_clients"] -= dead

    async def push_to_channel(self, channel: str, data: Any) -> None:
        """Public alias for _push — used by server handlers after manual state updates."""
        await self._push(channel, data)

    # ------------------------------------------------------------------
    # Pane stream bookkeeping
    # ------------------------------------------------------------------

    async def subscribe_pane(
        self,
        machine: str,
        session_name: str,
        ws,
        loop_factory: Callable,
    ) -> None:
        """Subscribe ws to (machine, session_name). Starts loop if not running."""
        key = (machine, session_name)
        async with self._pane_lock:
            streams = self._state["pane_streams"]
            if key not in streams:
                streams[key] = {
                    "subscribers": set(),
                    "last_content": "",
                    "task": None,
                }
            streams[key]["subscribers"].add(ws)
            # Start loop task if not already running
            if streams[key]["task"] is None or streams[key]["task"].done():
                streams[key]["task"] = asyncio.ensure_future(
                    loop_factory(machine, session_name)
                )

    async def unsubscribe_pane(self, machine: str, session_name: str, ws) -> None:
        """Unsubscribe ws. Cancels loop task if no subscribers remain."""
        key = (machine, session_name)
        async with self._pane_lock:
            streams = self._state["pane_streams"]
            info = streams.get(key)
            if not info:
                return
            info["subscribers"].discard(ws)
            if not info["subscribers"]:
                if info["task"] and not info["task"].done():
                    info["task"].cancel()
                del streams[key]

    async def unsubscribe_pane_all(self, ws) -> None:
        """Remove ws from every pane stream it was subscribed to."""
        async with self._pane_lock:
            streams = self._state["pane_streams"]
            for key in list(streams):
                info = streams.get(key)
                if not info:
                    continue
                info["subscribers"].discard(ws)
                if not info["subscribers"]:
                    if info["task"] and not info["task"].done():
                        info["task"].cancel()
                    del streams[key]

    def pane_subscribers(self, machine: str, session_name: str) -> set:
        """Return subscriber set snapshot for a pane (empty if not streaming)."""
        key = (machine, session_name)
        info = self._state["pane_streams"].get(key)
        if not info:
            return set()
        return set(info["subscribers"])

    def pane_last_content(self, machine: str, session_name: str) -> str:
        key = (machine, session_name)
        info = self._state["pane_streams"].get(key)
        return info["last_content"] if info else ""

    def set_pane_last_content(self, machine: str, session_name: str, content: str) -> None:
        key = (machine, session_name)
        info = self._state["pane_streams"].get(key)
        if info:
            info["last_content"] = content

    def has_pane_subscribers(self, machine: str, session_name: str) -> bool:
        key = (machine, session_name)
        info = self._state["pane_streams"].get(key)
        return bool(info and info["subscribers"])

    def iter_pane_subscribers(self, machine: str, session_name: str) -> list:
        """Return snapshot list of pane subscribers."""
        key = (machine, session_name)
        info = self._state["pane_streams"].get(key)
        return list(info["subscribers"]) if info else []
