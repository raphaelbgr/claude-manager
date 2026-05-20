"""Trace-contract declarations for claude-manager.

Each `declare()` is a clause in the executable spec: it names an event,
states its legal predecessors, expected successors, and (optionally)
temporal SLOs and the screen it belongs to. The corresponding emit
sites in src/* must produce these events at runtime; the contract
validator catches drift (declared-but-never-emitted = dead clause;
emitted-but-never-declared = uncovered breach).

Naming convention: ``cm.<area>.<verb>[.<outcome>]``
  area  ∈ scan, fleet, adapter, launch, mux, api, ws, ui
  verb  ∈ start, request, dispatch, ok, err, ...
  outcome optional: ``ok`` / ``err`` / ``timeout``

Soft-imports so this module loads even on machines without tracelink.
"""
from __future__ import annotations

try:
    from tracelink.contract import declare as _declare
except ImportError:  # pragma: no cover — bare environments
    def _declare(*args, **kwargs): pass


# ----------------------------------------------------------------------
# Process lifecycle
# ----------------------------------------------------------------------

_declare("cm.proc.start", screen="Boot", must=True)
_declare("cm.proc.ready", after=["cm.proc.start"], screen="Boot", must=True)


# ----------------------------------------------------------------------
# Scanner (src/scanner.py)
# ----------------------------------------------------------------------

_declare("cm.scan.cycle.start", screen="Scan")
_declare("cm.scan.local.ok",    after=["cm.scan.cycle.start"], screen="Scan")
_declare("cm.scan.remote.ok",   after=["cm.scan.cycle.start"], screen="Scan")
_declare("cm.scan.remote.err",  after=["cm.scan.cycle.start"], screen="Scan")
_declare("cm.scan.cycle.done",  after=["cm.scan.cycle.start"], screen="Scan",
         within_ms_of=("cm.scan.cycle.start", 30000.0))


# ----------------------------------------------------------------------
# Fleet discovery (src/fleet.py)
# ----------------------------------------------------------------------

_declare("cm.fleet.discover.start", screen="Fleet")
_declare("cm.fleet.health.ok",      after=["cm.fleet.discover.start"], screen="Fleet")
_declare("cm.fleet.health.err",     after=["cm.fleet.discover.start"], screen="Fleet")
_declare("cm.fleet.discover.done",  after=["cm.fleet.discover.start"], screen="Fleet",
         within_ms_of=("cm.fleet.discover.start", 15000.0))


# ----------------------------------------------------------------------
# Terminal adapter spawn (src/launcher.py + src/terminals/*)
# ----------------------------------------------------------------------

_declare("cm.adapter.probe.start",   screen="Launch")
_declare("cm.adapter.probe.attempt", after=["cm.adapter.probe.start"], screen="Launch")
_declare("cm.adapter.probe.ok",      after=["cm.adapter.probe.start"], screen="Launch")
_declare("cm.adapter.pick",          after=["cm.adapter.probe.ok"], screen="Launch")

_declare("cm.adapter.launch.start",   after=["cm.adapter.pick"], screen="Launch")
_declare("cm.adapter.spawn",          after=["cm.adapter.launch.start"], screen="Launch")
_declare("cm.adapter.fallback",       after=["cm.adapter.launch.start"], screen="Launch")
_declare("cm.adapter.wt.host_picked", screen="Launch")
_declare("cm.adapter.launch.ok",      after=["cm.adapter.launch.start"], screen="Launch",
         within_ms_of=("cm.adapter.launch.start", 10000.0))
_declare("cm.adapter.launch.err",     after=["cm.adapter.launch.start"], screen="Launch",
         within_ms_of=("cm.adapter.launch.start", 10000.0))


# ----------------------------------------------------------------------
# Session/tmux launch high-level (src/launcher.py)
# ----------------------------------------------------------------------

_declare("cm.launch.session.start", screen="Launch")
_declare("cm.launch.session.done",  after=["cm.launch.session.start"], screen="Launch")
_declare("cm.launch.tmux.start",    screen="Launch")
_declare("cm.launch.tmux.done",     after=["cm.launch.tmux.start"], screen="Launch")
_declare("cm.launch.ensure_claude.run", screen="Launch")


# ----------------------------------------------------------------------
# Tmux manager (src/tmux_manager.py)
# ----------------------------------------------------------------------

_declare("cm.mux.list.start", screen="Mux")
_declare("cm.mux.list.ok",    after=["cm.mux.list.start"], screen="Mux")
_declare("cm.mux.list.err",   after=["cm.mux.list.start"], screen="Mux")
_declare("cm.mux.create.start", screen="Mux")
_declare("cm.mux.create.ok",  after=["cm.mux.create.start"], screen="Mux")
_declare("cm.mux.create.err", after=["cm.mux.create.start"], screen="Mux")
_declare("cm.mux.kill",  screen="Mux")
_declare("cm.mux.capture", screen="Mux")


# ----------------------------------------------------------------------
# HTTP API (src/server.py — request entry/exit)
# ----------------------------------------------------------------------

_declare("cm.api.request",  screen="Api")
_declare("cm.api.response", after=["cm.api.request"], screen="Api",
         within_ms_of=("cm.api.request", 30000.0))

# Scan-button (POST /api/sessions/scan) phase progress
_declare("cm.api.scan.start",         screen="Api")
_declare("cm.api.scan.fleet_done",    after=["cm.api.scan.start"], screen="Api")
_declare("cm.api.scan.sessions_done", after=["cm.api.scan.start"], screen="Api")
_declare("cm.api.scan.tmux_done",     after=["cm.api.scan.start"], screen="Api")
_declare("cm.api.scan.done",          after=["cm.api.scan.start"], screen="Api")


# ----------------------------------------------------------------------
# WebSocket (src/server.py — WS lifecycle)
# ----------------------------------------------------------------------

_declare("cm.ws.connect", screen="Ws")
_declare("cm.ws.broadcast", after=["cm.ws.connect"], screen="Ws")
_declare("cm.ws.disconnect", after=["cm.ws.connect"], screen="Ws")


# ----------------------------------------------------------------------
# State store (src/state_store.py)
# ----------------------------------------------------------------------

_declare("cm.state.sessions.set", screen="State")
_declare("cm.state.tmux.set", screen="State")
_declare("cm.state.fleet.set", screen="State")


# ----------------------------------------------------------------------
# Command adapter (src/command_adapter.py)
# ----------------------------------------------------------------------

_declare("cm.adapter.sanitize.name_changed", screen="Adapter")


# ----------------------------------------------------------------------
# SSH executor (src/executor.py — exec_shell unified entry)
# ----------------------------------------------------------------------

_declare("cm.ssh.exec", screen="Ssh")


# ----------------------------------------------------------------------
# SSH connection pool (src/ssh_pool.py — persistent asyncssh connections)
# ----------------------------------------------------------------------

_declare("cm.ssh.pool.connect.start", screen="Ssh")
_declare("cm.ssh.pool.connect.ok",  after=["cm.ssh.pool.connect.start"], screen="Ssh")
_declare("cm.ssh.pool.connect.err", after=["cm.ssh.pool.connect.start"], screen="Ssh")
_declare("cm.ssh.pool.backoff", screen="Ssh")
