# Product Requirements Document — claude-manager

**BMAD v6 PRD | Status: Brownfield — Documenting Existing Product**
**Date:** 2026-05-17
**Version:** 1.0.1 (VERSION v179)

---

## 1. Problem Statement

Software engineers who operate multi-machine fleets (macOS, Linux, Windows) running Claude Code lose context when switching between machines: they cannot quickly locate which machine holds the relevant session, what working directory it was in, or whether it is still live. For engineers with ADHD or heavy context-switching workloads the cognitive cost of rediscovering sessions is disproportionately high and frequently results in duplicated work or abandoned sessions.

**Specific pain points addressed:**

- No unified view across machines for Claude Code sessions (`~/.claude/projects/*/`)
- No live status — working / active / idle — without SSH-ing into each machine individually
- No cross-platform terminal launcher: resuming a session on a Windows machine from a macOS host requires manually composing SSH + psmux commands
- tmux and psmux sessions are siloed per machine; correlating them with their Claude Code session requires manual inspection

---

## 2. Goals and Non-Goals

### Goals

| ID | Goal |
|----|------|
| G1 | Provide a single interface that shows every Claude Code session across all fleet machines simultaneously |
| G2 | Surface accurate live status (working / active / idle) using PID tracking, CPU sampling, and file mtime heuristics |
| G3 | Enable one-action resume of any session in a native terminal window appropriate to the host OS |
| G4 | Manage tmux (Linux/macOS) and psmux (Windows) sessions from the same interface, locally and remotely |
| G5 | Support three rendering modes — Web UI, TUI, native desktop window — driven by a single API server |
| G6 | Integrate with the `claude-dispatch` daemon for fast scanning, with automatic SSH fallback |
| G7 | Work on macOS, Linux, and Windows without requiring different tools on each |

### Non-Goals

| ID | Non-Goal |
|----|----|
| NG1 | Replace Claude Code itself or modify its session storage format |
| NG2 | Provide a general-purpose SSH/terminal multiplexer |
| NG3 | Manage Docker, VM, or non-Claude processes |
| NG4 | Sync or replicate session content across machines |

---

## 3. User Personas

### Primary: Multi-Machine Power User ("Raphael")

- Runs Claude Code simultaneously on 4–5 machines (macOS, Linux, Windows)
- Has 300+ historical sessions across machines; dozens may be live at once
- Operates via multiple terminal types: iTerm2 (macOS), GNOME terminal (Linux), Windows Terminal / PowerShell (Windows)
- Uses tmux and psmux extensively to persist shell sessions across logins
- Needs to quickly switch context without losing track of prior work

### Secondary: Fleet Administrator

- Needs a health dashboard showing which machines are online/offline
- Wants to see which sessions are consuming CPU and which are idle
- May access claude-manager headlessly (API-only mode) from CI or a monitoring script

---

## 4. Functional Requirements

### FR-1: Session Discovery

| ID | Requirement |
|----|------------|
| FR-1.1 | Scan the local machine's `~/.claude/projects/*/` directory to discover session JSONL files |
| FR-1.2 | For each remote fleet machine, first attempt HTTP `GET :<dispatch_port>/sessions`; on failure, fall back to SSH + piped Python script |
| FR-1.3 | Cross-reference `~/.claude/sessions/*.json` for PID; use `psutil` to determine if PID is alive and measure CPU % |
| FR-1.4 | Classify each session as `working` (PID alive AND CPU > 5% OR mtime < 15 s), `active` (PID alive, not working), or `idle` (no live PID) |
| FR-1.5 | Extract session metadata: summary (first user message, ≤ 120 chars), message count, file size, git branch, last modified time |

### FR-2: Session Operations

| ID | Requirement |
|----|------------|
| FR-2.1 | Resume a session by opening a native terminal window pre-populated with `cd <cwd> && claude --resume <uuid>` |
| FR-2.2 | Support per-session `--dangerously-skip-permissions` toggle, persisted in `.claude-manager-prefs.json` |
| FR-2.3 | Allow pinning sessions to top and archiving (hiding) sessions without deletion |
| FR-2.4 | Allow renaming sessions (equivalent to Claude Code `/rename`) stored in session metadata |

### FR-3: tmux / psmux Management

| ID | Requirement |
|----|------------|
| FR-3.1 | List all tmux sessions (Linux/macOS) and psmux sessions (Windows) across fleet, locally and via SSH |
| FR-3.2 | Create new detached mux sessions with `POST /api/tmux/create` |
| FR-3.3 | Attach to a local mux session in the current terminal; open remote mux in a new terminal window |
| FR-3.4 | Kill a named mux session with `POST /api/tmux/kill` |
| FR-3.5 | Link mux panes to their active Claude Code session by matching `pane_current_path` to `session.cwd` on the same machine |

### FR-4: Interfaces

| ID | Requirement |
|----|------------|
| FR-4.1 | Web UI: React SPA served at `/` by the aiohttp server; communicates via REST + WebSocket |
| FR-4.2 | TUI: Textual-based terminal application with Sessions, Tmux, and Fleet tabs; keyboard-driven |
| FR-4.3 | Desktop GUI: pywebview native window wrapping the Web UI; optional pystray tray icon on Linux/Windows |
| FR-4.4 | API-only headless mode for programmatic consumers |

### FR-5: Fleet Health

| ID | Requirement |
|----|------------|
| FR-5.1 | Probe each machine's dispatch daemon via HTTP first (3 s timeout); fall back to `ssh echo ok` |
| FR-5.2 | Report per-machine status: online/offline, connectivity method (http/ssh/unreachable), OS, IP |
| FR-5.3 | Surface hardware stats (CPU, GPU, RAM) where available |

### FR-6: Live Updates

| ID | Requirement |
|----|------------|
| FR-6.1 | Background asyncio task rescans all machines every 30 seconds (configurable `SCAN_INTERVAL`) |
| FR-6.2 | Push session, tmux, and fleet diffs to all WebSocket subscribers after each scan |
| FR-6.3 | Allow clients to subscribe per-channel: `sessions`, `tmux`, `fleet` |
| FR-6.4 | Forced rescan via `POST /api/sessions/scan` pushes to subscribers immediately |

### FR-7: Authentication

| ID | Requirement |
|----|------------|
| FR-7.1 | When binding to non-loopback addresses, support optional bearer-token authentication derived from the server's SSH public key |
| FR-7.2 | Auth config persisted to `~/.claude-manager/auth.json`; enable/disable via `POST /api/auth/config` |
| FR-7.3 | Loopback clients bypass auth checks regardless of auth configuration |

---

## 5. Non-Functional Requirements

| ID | Requirement |
|----|------------|
| NFR-1 | Fleet scan of 4 machines must complete within 10 seconds under normal LAN conditions |
| NFR-2 | API server must handle concurrent WebSocket clients without blocking the background scan loop |
| NFR-3 | Remote scan fallback Python script must be stdlib-only (no third-party imports) to run on any Python 3.6+ remote host |
| NFR-4 | claude-manager itself requires Python ≥ 3.11 |
| NFR-5 | No global installs required; all dependencies are project-local (venv) |
| NFR-6 | SSH pool must not spawn more than one persistent connection per fleet machine |

---

## 6. Constraints

- **Python 3.11–3.14** — runtime constraint from `pyproject.toml`
- **Port 44740** — fixed API server port; must not conflict with `claude-dispatch` (44730)
- **Windows SSH behaviour** — Windows OpenSSH ignores `ControlMaster`; the asyncssh persistent pool compensates
- **macOS pywebview** — requires the AppKit main thread; system tray is unavailable on macOS in desktop mode
- **No interactive SSH** — interactive terminal launches are spawned as standalone processes, not routed through the connection pool

---

## 7. Success Metrics

| Metric | Target |
|--------|--------|
| Session discovery latency (4-machine fleet, all online) | < 8 s per scan cycle |
| False-positive `working` status rate | < 5% of sessions over a 24-hour period |
| Fleet scan availability (at least one interface reachable) | 99% of scan cycles |
| Test coverage: unit + integration | ≥ 19 test files, ≥ 588 test cases |
| Time-to-resume (click to terminal open) | < 3 s on local machine, < 10 s remote |

---

## 8. Dependencies

| Dependency | Role | Version |
|-----------|------|---------|
| `aiohttp` | REST + WebSocket API server | ≥ 3.9 |
| `psutil` | PID tracking and CPU sampling | ≥ 5.9 |
| `asyncssh` | Persistent SSH connection pool | ≥ 2.14 |
| `textual` | TUI framework (optional, `tui` extra) | ≥ 3.0 |
| `pywebview` | Native desktop window (optional, `desktop` extra, non-Windows) | ≥ 5.0 |
| `pystray` + `Pillow` | System tray icon (optional, `desktop-tray` extra) | ≥ 0.19 / ≥ 10.0 |
| `claude-dispatch` daemon | Fast remote scanning (optional; SSH fallback always available) | compatible with `/sessions`, `/tmux`, `/health` API |

---

## 9. Open Questions

1. Should `/api/projects` (powered by `project_identity.py`) be promoted to a first-class documented endpoint? Currently present in the source but absent from `docs/api.md`.
2. Should SSH pool connection status be surfaced in `/health` to enable programmatic monitoring of per-machine connectivity?
3. Is the `pane_streams` WebSocket sub-protocol intended to be public API, or an internal implementation detail for the TUI pane-content view?
