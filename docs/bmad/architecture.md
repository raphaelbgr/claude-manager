# Architecture Document — claude-manager

**BMAD v6 Architecture | Status: Brownfield — Documenting Existing System**
**Date:** 2026-05-17
**Version:** 1.0.1 (VERSION v179)

---

## 1. System Context

claude-manager is a single-process Python daemon. It exposes an HTTP/WebSocket API on port 44740 (loopback by default, LAN-accessible via `--bind 0.0.0.0`). Three rendering clients consume that API: a React SPA served by the same process, a Textual TUI that makes direct Python calls, and a pywebview native window.

```
┌───────────────────── claude-manager process ─────────────────────┐
│                                                                   │
│  [TUI/Textual]  [Web UI/React SPA]  [Desktop/pywebview]          │
│       │               │                    │                      │
│       └───────────────┼────────────────────┘                      │
│                       │ REST + WebSocket :44740                   │
│              ┌────────▼────────────┐                              │
│              │  src/server.py      │                              │
│              │  StateStore         │                              │
│              └────────┬────────────┘                              │
│         ┌─────────────┼────────────────────┐                      │
│  [scanner.py]  [tmux_manager.py]  [fleet.py]                      │
│         └─────────────┼────────────────────┘                      │
│              ┌────────▼────────────┐                              │
│              │  executor.py        │                              │
│              │  LocalExecutor      │                              │
│              │  SSHExecutor        │                              │
│              │  ssh_pool.py        │                              │
│              └─────────────────────┘                              │
└───────────────────────────────────────────────────────────────────┘

External targets:
  Local:   ~/.claude/projects/*/  (session JSONL files)
           ~/.claude/sessions/*.json  (PID records)
  Remote:  HTTP :44730 (claude-dispatch) OR SSH → piped Python
  Apps:    iTerm2 / Terminal.app / gnome-terminal / Windows Terminal
```

---

## 2. Component Decomposition

### 2.1 API Server (`src/server.py`)

- **Framework:** aiohttp (async)
- **Port:** 44740
- **Startup:** `on_startup` hook creates StateStore, starts the `_background_scan` asyncio task, registers SSH pool shutdown on `on_cleanup`
- **State:** shared `app["state"]` dict wrapped by StateStore — sessions list, tmux list, fleet dict, WebSocket client set, pane_streams dict
- **Auth middleware:** checks `Authorization: Bearer <token>` on non-loopback requests when auth is enabled; loopback always bypassed
- **CORS:** wildcard (`*`) — all origins allowed

Key route groups:

| Group | Routes |
|-------|--------|
| Health | `GET /health` |
| Sessions | `GET /api/sessions`, `GET /api/sessions/{machine}`, `POST /api/sessions/scan`, `POST /api/sessions/launch` |
| Tmux | `GET /api/tmux`, `GET /api/tmux/{machine}`, `POST /api/tmux/create`, `POST /api/tmux/connect`, `POST /api/tmux/connect-remote`, `POST /api/tmux/kill` |
| Fleet | `GET /api/fleet` |
| Projects | `GET /api/projects` |
| Preferences | `GET /api/preferences`, `POST /api/preferences` |
| Logs | `GET /api/logs` |
| Auth | `GET /api/auth/config`, `POST /api/auth/config` |
| WebSocket | `GET /ws` |
| Static | `GET /` (serves `src/web/index.html`) |

### 2.2 StateStore (`src/state_store.py`)

Centralised, lock-protected wrapper around `app["state"]`. All mutations go through atomic setters that immediately fan-out WebSocket notifications. Provides:

- `update_sessions(sessions)` — replaces session list and pushes `sessions` channel
- `update_tmux(tmux)` — replaces tmux list, enriches with session links via `session_link.enrich_tmux_dicts`, pushes `tmux` channel
- `update_fleet(fleet)` — replaces fleet dict and pushes `fleet` channel
- Pane stream bookkeeping: `subscribe_pane` / `unsubscribe_pane` / `unsubscribe_pane_all` with asyncio.Lock to prevent subscribe/unsubscribe races

### 2.3 Scanner (`src/scanner.py`)

Claude Code session discovery. Produces `ClaudeSession` objects.

**Local scan:**
1. Enumerate `~/.claude/projects/*/` — each subdirectory is an encoded project folder
2. For each project folder, scan `*.jsonl` files (first 50 lines for metadata extraction)
3. Cross-reference `~/.claude/sessions/*.json` for PID field
4. psutil.Process(pid) to check alive and CPU %
5. Status classification: `working` / `active` / `idle`

**Remote scan (per machine, parallel):**
- Tier 1: HTTP GET to the claude-dispatch daemon's `/sessions` endpoint
- Tier 2 fallback: SSH, pipe a stdlib-only Python script via stdin, read JSON from stdout

**Project folder decoding** (`decode_project_folder`):
- Unix: leading `-` becomes `/`; each `-` becomes `/`
- Windows: `<letter>--` becomes `<letter>:\`; `-` becomes `\`
- Prefers `cwd` from JSONL over decoded folder name (avoids ambiguity with hyphens in project names)

### 2.4 TmuxManager (`src/tmux_manager.py`)

Manages tmux (Linux/macOS) and psmux (Windows) sessions.

- `list_all_tmux()`: parallel async gather across all machines; HTTP daemon first, SSH fallback
- `create_session(machine, name)`: runs the mux new-session command via executor
- `kill_session(machine, name)`: kills by name
- Uses `executor.get_executor(machine)` for all subprocess dispatch

### 2.5 Fleet (`src/fleet.py`)

Machine health probing.

- `discover_fleet()`: parallel async gather; HTTP GET `/health` on dispatch port (3 s timeout); SSH `echo ok` fallback
- Returns per-machine dict: `{status, method, os, ip, dispatch_port, ...hardware_stats}`

### 2.6 Executor (`src/executor.py`)

Unified command-execution abstraction eliminating local/remote branching across scanner, tmux_manager, and fleet.

```python
LocalExecutor.exec(cmd, timeout) -> (rc, stdout, stderr)
SSHExecutor.exec(cmd, timeout)   -> (rc, stdout, stderr)   # via ssh_pool first, subprocess fallback
```

`SSHExecutor` injects a PATH prefix for Unix targets to ensure tools are found in non-interactive shells. Windows (PowerShell) targets receive no prefix.

`LocalExecutor` uses `_augmented_local_env()` which builds an env with fallback PATH entries for Homebrew, snap, `~/.local/bin`, Windows PowerShell paths, etc. The result is cached in `_CACHED_LOCAL_ENV`.

### 2.7 SSH Pool (`src/ssh_pool.py`)

App-scoped persistent SSH connection pool using asyncssh.

- One `_MachineConn` instance per fleet machine, lazily created on first use
- Each `_MachineConn` holds one `asyncssh.SSHClientConnection`, reconnects with exponential backoff (1→2→4→8→16→30 s cap) on failure
- `SSHPool.run(machine, cmd, timeout)` returns `(rc, stdout, stderr)` — same shape as `subprocess_utils.run_with_timeout`
- `shutdown_default()` called from aiohttp `on_cleanup` to close all connections cleanly
- Disabled gracefully if asyncssh is not installed — SSHExecutor falls through to subprocess ssh

### 2.8 CommandAdapter (`src/command_adapter.py`)

OS-aware command builder. Every place that needs shell commands goes through here.

Construction: `CommandAdapter(target_os, mux_type)` — `target_shell` is `"cmd"` for psmux/Windows, `"bash"` for everything else.

Key handling for Windows paths sent via SSH: `_win_path_to_bash(path)` converts `C:\Users\...` to `/c/users/...` because SSH into Windows lands in Git Bash.

`get_adapter(machine_name)` returns the adapter for a fleet machine without looking up config manually.

### 2.9 MuxParser (`src/mux_parser.py`)

Universal tmux/psmux output parser. `parse_mux_output(output)` auto-detects format by inspecting the first line:

1. **Pipe-delimited** — `name|created_epoch|windows|attached[|pane_cwd]` (tmux `-F` format, 4 or 5 fields)
2. **Plain text** — `name: N windows (created DATE) (attached)` (psmux default)
3. **Name-per-line** — last-resort fallback

All three return `list[dict]` with keys: `name`, `created` (ISO-8601 or empty string), `windows`, `attached`, `cwd`.

### 2.10 Launcher (`src/launcher.py`) and Terminals (`src/terminals/`)

Cross-platform terminal launching. `launch_session(session, prefs)` delegates to a platform-specific backend:

| Module | Platform |
|--------|----------|
| `terminals/darwin.py` | iTerm2 (AppleScript), Terminal.app (fallback) |
| `terminals/linux.py` | gnome-terminal, x-terminal-emulator, xterm |
| `terminals/windows.py` | Windows Terminal (`wt`), PowerShell |

Each backend implements `TerminalBase` from `terminals/base.py`.

### 2.11 SessionLink (`src/session_link.py`)

Links tmux panes to Claude Code sessions by matching `pane_current_path` (normalized) to `session.cwd` on the same machine. Shell panes (bash, zsh, pwsh, etc.) are excluded to avoid mislabeling.

`enrich_tmux_dicts(tmux_list, sessions)` merges `claude_session_id` and `claude_session_name` into each tmux dict — called by `StateStore.update_tmux`.

### 2.12 ProjectIdentity (`src/project_identity.py`)

Maps sessions to canonical project identifiers for cross-machine grouping in `/api/projects`.

- `project_id(session)`: prefers normalized git remote URL (`github.com/owner/repo`); falls back to cwd basename
- `normalize_remote(url)`: handles SSH and HTTPS git URL forms; generic fallback for self-hosted
- `canonical_basename(session)`: bare directory basename used for two-pass consolidation

### 2.13 Auth (`src/auth.py`)

SSH-key-derived bearer token authentication.

- Token: `sha256(pubkey_file_contents).hexdigest()[:32]`
- Config persisted at `~/.claude-manager/auth.json`
- `AuthConfig` loaded at startup and cached in `app["auth"]`
- `is_loopback(remote_addr)` bypasses auth for loopback addresses

### 2.14 TUI (`src/tui/`)

Textual application with three tabs:

| Tab | Widget | Description |
|-----|--------|-------------|
| Sessions | `session_card.py` | Session cards with status, metadata, resume button |
| Tmux | `tmux_card.py` | Mux session list with attach/kill |
| Fleet | (inline) | Machine health rows |

`header_bar.py` provides the filter input and tab bar. `screens/new_tmux.py` is the modal for creating a new mux session.

### 2.15 Desktop GUI (`src/desktop.py`)

`run_desktop(bind, port)` starts the native window mode:

- Thread layout: main thread runs `webview.start()` (AppKit on macOS); background threads run the API server and tray icon (Linux/Windows only)
- Server reuse: if `/health` already returns `{"status": "ok"}` on the target port, no new server thread is started
- Tray menu rebuilt dynamically from `/api/sessions` and `/api/tmux` on each refresh loop tick

---

## 3. Data Models

### ClaudeSession

Defined in `src/scanner.py`.

| Field | Type | Notes |
|-------|------|-------|
| `session_id` | `str` | JSONL filename stem (UUID-style) |
| `machine` | `str` | Fleet machine name |
| `project_folder` | `str` | Raw encoded folder name |
| `project_path` | `str` | Decoded filesystem path |
| `cwd` | `str` | Working directory from JSONL (preferred) |
| `slug` | `str` | Session slug from JSONL metadata |
| `summary` | `str` | First user message, max 120 chars |
| `messages` | `int` | Total non-empty JSONL lines |
| `modified` | `str` | ISO-8601 UTC mtime |
| `status` | `str` | `"working"` / `"active"` / `"idle"` |
| `pid` | `int or None` | Live PID or null |
| `file_size` | `int` | JSONL size in bytes |
| `name` | `str` | User-assigned rename (empty if unset) |
| `cpu_percent` | `float` | CPU % at scan time |
| `git_remote` | `str` | Git remote URL (for project_identity) |

**Status rules:**
- `working`: PID alive AND (mtime within 15 s OR CPU above 5%)
- `active`: PID alive, not working
- `idle`: no live PID

### TmuxSession

Defined in `src/tmux_manager.py`.

| Field | Type | Notes |
|-------|------|-------|
| `name` | `str` | Session name |
| `machine` | `str` | Fleet machine name |
| `created` | `str` | ISO-8601 UTC or empty string |
| `windows` | `int` | Window count |
| `attached` | `bool` | Client attached? |
| `is_local` | `bool` | Running on local machine? |
| `cwd` | `str` | Pane current path (if available from dispatcher) |
| `pane_current_command` | `str` | Current command in pane (for shell detection) |
| `claude_session_id` | `str` | Linked Claude session UUID (enriched by session_link) |
| `claude_session_name` | `str` | Display name of linked session |

### AuthConfig

Defined in `src/auth.py`.

| Field | Type | Notes |
|-------|------|-------|
| `enabled` | `bool` | Whether auth is active |
| `key_path` | `Path or None` | Path to SSH public key file |
| `token` | `str or None` | Computed bearer token (not exposed via API) |

---

## 4. API Contracts

### WebSocket Protocol (`/ws`)

**Client to Server:**

```json
{"type": "subscribe", "channel": "sessions"}
{"type": "subscribe", "channel": "fleet"}
{"type": "subscribe", "channel": "tmux"}
{"type": "unsubscribe", "channel": "sessions"}
```

**Server to Client (on subscribe):**

```json
{"type": "snapshot", "channel": "sessions", "data": [...]}
```

**Server to Client (on scan):**

```json
{"type": "update", "channel": "sessions", "data": [...], "action": "refresh"}
```

**Server to Client (error):**

```json
{"type": "error", "message": "invalid JSON"}
```

### Key REST Endpoints

```
GET  /health
     Returns: {"status": "ok", "machines": N, "sessions": N, "last_scan": ISO8601}

GET  /api/sessions
     Returns: {"machine_name": {"sessions": [...], "stats": {...}}, ...}

POST /api/sessions/scan
     Body: {}
     Returns: {"status": "scanning"}

POST /api/sessions/launch
     Body: {"session_id": "...", "machine": "...", "cwd": "...", "skip_permissions": bool}
     Returns: {"status": "launched"} on success, {"error": "..."} on failure

POST /api/tmux/create
     Body: {"machine": "...", "name": "...", "cwd": "..."}
     Returns: {"status": "created", "name": "..."}

POST /api/tmux/kill
     Body: {"machine": "...", "name": "..."}
     Returns: {"status": "killed"}

GET  /api/preferences
     Returns: {"skip_permissions": bool}

POST /api/preferences
     Body: {"skip_permissions": bool}
     Returns: {"skip_permissions": bool}
```

---

## 5. Background Scan Loop

```
app startup -> asyncio.ensure_future(_background_scan())

_background_scan():
  while True:
    fleet    = await discover_fleet()         # src/fleet.py
    sessions = await scan_all()               # src/scanner.py
    tmux     = await list_all_tmux()          # src/tmux_manager.py
    await state_store.update_fleet(fleet)     # pushes WS "fleet" channel
    await state_store.update_sessions(sessions)  # pushes WS "sessions" channel
    await state_store.update_tmux(tmux)       # enriches + pushes WS "tmux" channel
    state_store.set_last_scan(now_iso())
    await asyncio.sleep(SCAN_INTERVAL)        # default: 30 seconds
```

`POST /api/sessions/scan` triggers an immediate out-of-cycle scan and also pushes results to subscribers.

---

## 6. SSH Transport Architecture

```
SSHExecutor.exec_shell(cmd, machine)
      |
      +-- Try: ssh_pool.default_pool().run(machine, cmd)
      |        asyncssh persistent connection, 1 per machine
      |        exponential backoff (1 to 30 s) on failure
      |
      +-- Fallback: subprocess ssh with ControlMaster
                    (Unix: ControlPersist=60s socket)
                    (Windows: ControlMaster=auto is ignored by OpenSSH)
```

`shutdown_default()` is called from aiohttp `on_cleanup` to close all asyncssh connections before process exit.

---

## 7. Infrastructure Requirements

| Component | Requirement |
|-----------|------------|
| Python | >= 3.11, < 3.15 |
| SSH | Key auth (id_rsa or id_ed25519); no password prompts |
| Network | LAN connectivity to fleet machines; Tailscale for WAN |
| Ports | 44740 (API), 44730 (claude-dispatch daemon, optional) |
| Python on remotes | >= 3.6 (remote scan script is stdlib-only) |
| tmux on Linux/macOS remotes | Any version supporting `list-sessions -F` |
| psmux on Windows remotes | Compatible with `list-sessions` / `new-session` |

---

## 8. Technology Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| API framework | aiohttp | Async-native; handles concurrent WS clients and background scan loop in one process |
| TUI framework | Textual | Rich terminal UI with widgets, CSS-like layout, event system |
| Desktop window | pywebview | Wraps existing React SPA; one codebase for web and desktop |
| SSH transport | asyncssh (primary) + subprocess ssh (fallback) | asyncssh eliminates per-call SSH handshake; subprocess fallback keeps compatibility when asyncssh is absent |
| Mux abstraction | Universal parser + CommandAdapter | Single entry point handles tmux and psmux output/command differences |
| Auth scheme | SHA-256 of SSH public key | Zero extra secret management; key is already present on all fleet machines; loopback exempt |
| React SPA delivery | Single-file `src/web/index.html` (CDN imports) | No build step; served directly by aiohttp |
| State management | StateStore centralised wrapper | Prevents handler-level mutation of shared state; guarantees WS push on every mutation |

---

## 9. Known Architectural Gaps

| Gap | Location | Impact |
|-----|----------|--------|
| Pane streams WebSocket sub-protocol undocumented | `state_store.py` | External clients cannot implement pane streaming |
| `/api/projects` endpoint absent from `docs/api.md` | `server.py` | Undiscoverable feature |
| SSH pool status not in `/health` | `ssh_pool.py` | Cannot diagnose per-machine pool health via API |
| `_ssh_control_path` uses `/tmp/` unconditionally | `executor.py` | Wrong on Windows if claude-manager itself runs on Windows |
| `unsubscribe_pane_all` call on WS close unverified | `server.py` | Potential pane polling task leak on client disconnect |
