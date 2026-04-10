# Architecture

## Component Diagram

```
┌─────────────────────────────────────────────────────────────────┐
│                        claude-manager                            │
│                                                                 │
│  ┌──────────────┐  ┌──────────────────┐  ┌───────────────────┐ │
│  │  TUI         │  │  Web UI          │  │  Desktop GUI      │ │
│  │  (Textual)   │  │  (React SPA)     │  │  (pywebview)      │ │
│  │  src/tui/    │  │  src/web/        │  │  src/desktop.py   │ │
│  └──────┬───────┘  └────────┬─────────┘  └─────────┬─────────┘ │
│         │  direct calls     │  HTTP/WS              │  HTTP/WS  │
│         │                   └───────────────────────┘           │
│         │                             │                         │
│         │              ┌──────────────▼──────────────┐          │
│         │              │   REST + WebSocket API       │          │
│         │              │   src/server.py              │          │
│         │              │   aiohttp, port 44740        │          │
│         │              │   CORS: * (all origins)      │          │
│         │              └──────────────┬───────────────┘          │
│         │                             │ shared state             │
│         └─────────────────────────────┤                         │
│                                       │                         │
│                    ┌──────────────────┼──────────────────┐      │
│                    │                  │                  │      │
│          ┌─────────▼──────┐  ┌────────▼───────┐  ┌──────▼────┐ │
│          │  src/scanner.py│  │src/tmux_manager│  │src/fleet  │ │
│          │  Claude session│  │  tmux / psmux  │  │  .py      │ │
│          │  discovery     │  │  management    │  │  health   │ │
│          └────────┬───────┘  └───────┬────────┘  └──────┬────┘ │
│                   │                  │                  │      │
│          ┌────────▼──────────────────▼──────────────────▼────┐ │
│          │            src/launcher.py                        │ │
│          │   Cross-platform terminal launcher                │ │
│          │   macOS: iTerm2 / Terminal.app (osascript)        │ │
│          │   Linux: x-terminal-emulator / gnome-terminal     │ │
│          │   Windows: PowerShell                             │ │
│          └───────────────────────────────────────────────────┘ │
│                                                                 │
│          ┌────────────────────────────┐                         │
│          │  src/command_adapter.py    │                         │
│          │  CommandAdapter — OS-aware │                         │
│          │  command builder           │                         │
│          └────────────────────────────┘                         │
│                                                                 │
│          ┌─────────────────────────────────────────────────┐   │
│          │            src/mux_parser.py                    │   │
│          │  Universal tmux/psmux output format parser      │   │
│          └─────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────┘
                    │                  │                │
                    ▼                  ▼                ▼
             Local ~/.claude/    tmux/psmux        HTTP :44730
             + SSH remote        local + SSH       (claude-dispatch)
             scan script         attach/create     SSH fallback
```

## Data Flow: Session Scanning

Session discovery runs in three tiers, tried in order per machine:

```
scan_all() called
      │
      ├── Local machine ──────────────────────────────────────────┐
      │   scan_local()                                            │
      │   1. Enumerate ~/.claude/projects/*/                      │
      │   2. Parse each *.jsonl (first 50 lines for metadata)     │
      │   3. Cross-reference ~/.claude/sessions/*.json for PIDs   │
      │   4. psutil.Process(pid) to check alive + CPU %           │
      │   5. Status: working (CPU>5% or mtime<15s) / active / idle│
      │                                                           │
      └── Remote machines (parallel asyncio.gather) ─────────────┘
            │
            ├── Try: HTTP GET http://<ip>:44730/sessions
            │        (claude-dispatch daemon, fast, no SSH)
            │        ← Returns JSON list of session dicts
            │
            └── Fallback: SSH pipe → python3 -
                     REMOTE_SCAN_SCRIPT piped via stdin
                     (stdlib-only, runs on Python 3.6+)
                     ← Returns JSON via stdout
```

The remote scan script is entirely self-contained (no third-party imports). It is piped via `stdin` to avoid shell quoting issues with complex inline scripts.

## Data Flow: Tmux Scanning

```
list_all_tmux() called
      │
      ├── Local: tmux list-sessions -F '#{name}|#{created}|...'
      │          parse_mux_output() → list[TmuxSession]
      │
      └── Remote (parallel):
            ├── Try: HTTP GET http://<ip>:44730/tmux
            │        (claude-dispatch daemon)
            └── Fallback: SSH → tmux list-sessions -F '...'
                          or psmux list-sessions (Windows)
                          parse_mux_output() handles both formats
```

## Data Flow: Fleet Health

```
discover_fleet() called
      │
      └── Per machine (parallel asyncio.gather):
            ├── Try: HTTP GET http://<ip>:44730/health (3s timeout)
            │        method = "http"
            └── Fallback: SSH echo ok (SSH_TIMEOUT + 1s)
                          method = "ssh" | "unreachable"
```

## Background Scan Loop

The API server runs a single asyncio background task (`_background_scan`) that loops forever:

```
on_startup() → asyncio.ensure_future(_background_scan())

_background_scan():
  while True:
    1. discover_fleet()
    2. scan_all()
    3. list_all_tmux()
    4. Update app["state"]
    5. Push to all WebSocket subscribers:
         {"type": "update", "channel": "sessions"|"fleet"|"tmux",
          "data": [...], "action": "refresh"}
    6. await asyncio.sleep(SCAN_INTERVAL)  # default: 30 seconds
```

Scans triggered by `POST /api/sessions/scan` also push to WebSocket subscribers.

## WebSocket Protocol

Connect to `ws://host:44740/ws`.

### Client → Server

**Subscribe to a channel:**
```json
{"type": "subscribe", "channel": "sessions"}
{"type": "subscribe", "channel": "fleet"}
{"type": "subscribe", "channel": "tmux"}
```

**Unsubscribe:**
```json
{"type": "unsubscribe", "channel": "sessions"}
```

### Server → Client

**Immediate snapshot** (sent on subscribe):
```json
{
  "type": "snapshot",
  "channel": "sessions",
  "data": [ ...session objects... ]
}
```

**Live update** (sent every SCAN_INTERVAL or after forced scan):
```json
{
  "type": "update",
  "channel": "sessions",
  "data": [ ...session objects... ],
  "action": "refresh"
}
```

**Error:**
```json
{"type": "error", "message": "invalid JSON"}
```

## Session Data Model

`ClaudeSession` (defined in `src/scanner.py`):

| Field | Type | Description |
|-------|------|-------------|
| `session_id` | `str` | JSONL filename stem (UUID-style) |
| `machine` | `str` | Fleet machine name (key in FLEET_MACHINES) |
| `project_folder` | `str` | Raw encoded folder name, e.g. `-Users-rbgnr-git-foo` |
| `project_path` | `str` | Decoded filesystem path, e.g. `/Users/rbgnr/git/foo` |
| `cwd` | `str` | Working directory recorded in the JSONL (preferred over decoded path) |
| `slug` | `str` | Session slug from JSONL metadata |
| `summary` | `str` | First user message, truncated to 120 characters |
| `messages` | `int` | Total non-empty lines in the JSONL file |
| `modified` | `str` | ISO-8601 UTC mtime of the JSONL file |
| `status` | `str` | `"working"` / `"active"` / `"idle"` |
| `pid` | `int \| None` | Process ID if active/working, `null` if idle |
| `file_size` | `int` | Size of the JSONL file in bytes |
| `name` | `str` | Session name set by `/rename` (empty string if unset) |
| `cpu_percent` | `float` | CPU usage at time of scan (0.0 if idle or not measured) |

**Status rules:**
- `working` — PID is alive AND (JSONL modified within last 15 seconds OR CPU > 5%)
- `active` — PID is alive but not working (waiting for user input)
- `idle` — No live PID found

## Tmux Data Model

`TmuxSession` (defined in `src/tmux_manager.py`):

| Field | Type | Description |
|-------|------|-------------|
| `name` | `str` | Session name |
| `machine` | `str` | Fleet machine name |
| `created` | `str` | ISO-8601 UTC creation time (empty string if unavailable) |
| `windows` | `int` | Number of windows in the session |
| `attached` | `bool` | Whether any client is currently attached |
| `is_local` | `bool` | `true` if running on the local machine |

## CommandAdapter Pattern

`src/command_adapter.py` — `CommandAdapter` — centralises all OS-specific command generation. Every place in the codebase that needs to build a shell command (cd, claude resume, tmux/psmux new-session, send-keys, attach, kill) goes through `CommandAdapter` rather than building shell strings inline.

### Construction

```python
adapter = CommandAdapter(target_os="win32", mux_type="psmux")
```

`target_os` is `"darwin"`, `"linux"`, or `"win32"`. `mux_type` is `"tmux"` or `"psmux"`. Together they determine `target_shell`: `"cmd"` for psmux (Windows), `"bash"` for everything else.

### Key methods

| Method | Returns |
|--------|---------|
| `quote_path(path)` | Path quoted for the target shell |
| `cd_command(path)` | `cd '/path'` (bash) or `cd /d "C:\path"` (cmd) |
| `cd_command_ssh(path)` | `cd '/path'` (bash) or `Set-Location 'C:\path'` (PowerShell) |
| `claude_resume_command(session_id, skip_permissions)` | `claude --resume <uuid> [--dangerously-skip-permissions]` |
| `build_session_command(cwd, session_id, skip_permissions)` | Full `cd && claude --resume` for the target shell |
| `build_session_command_ssh(cwd, session_id, skip_permissions)` | Same, but for SSH -t execution (Windows paths converted to Git Bash format) |
| `mux_create_session(name)` | `tmux new-session -d -s <name>` or `psmux new-session -d -s <name>` |
| `mux_send_keys(session_name, command)` | `tmux send-keys -t <name> <cmd> Enter` |
| `mux_attach(session_name)` | `tmux attach -t <name>` |
| `mux_kill_session(session_name)` | `tmux kill-session -t <name>` |
| `ssh_wrap(alias, command, allocate_tty)` | `ssh [-t] <alias> '<command>'` |
| `for_terminal(command, keep_open)` | Wraps with `; exec bash` to keep terminal open (bash only) |
| `generate_mux_session_name(machine, project, existing)` | Unique `machine_project-session-NN` name |

Use `get_adapter(machine_name)` to get the right adapter for a fleet machine without looking up its config manually:

```python
from src.command_adapter import get_adapter
adapter = get_adapter("avell-i7")   # returns CommandAdapter(target_os="win32", mux_type="psmux")
```

### Windows path conversion

When SSHing into a Windows machine, the connection lands in Git Bash. Windows paths (`C:\Users\...`) must be converted to Git Bash format (`/c/users/...`) for commands sent via SSH. `CommandAdapter._win_path_to_bash(path)` handles this conversion. Commands executed inside a psmux pane use cmd.exe syntax (built with `cd_command` / `mux_send_keys`), while SSH wrapper commands use Git Bash syntax.

---

## Universal Mux Parser

`src/mux_parser.py` — `parse_mux_output(output)` — handles all tmux and psmux output formats in one function. Auto-detects the format by inspecting the first line, then dispatches to the appropriate parser:

1. **Pipe-delimited** (`tmux -F '#{session_name}|#{session_created}|#{session_windows}|#{session_attached}[|#{pane_current_path}]'`) — primary format
2. **Plain text** (`name: N windows (created DATE) (attached)`) — psmux default output
3. **Name-per-line** — last-resort fallback

All three parsers return the same list-of-dict structure:

```python
[
  {
    "name": "claude-work",
    "created": "2026-04-09T08:00:00+05:30",  # ISO-8601 or None
    "windows": 3,
    "attached": True,
    "cwd": "/Users/rbgnr/git/myproject",      # only in pipe format; "" otherwise
  }
]
```

The pipe format supports an optional 5th field (`pane_current_path`) introduced in a later version of the format string. The parser handles both 4-field and 5-field variants transparently.

---

## Logging Architecture

### MemoryLogHandler

`MemoryLogHandler` (defined in `src/server.py`) is a `logging.Handler` subclass that stores log records in a `collections.deque` ring buffer (default: 500 entries). It is attached to the `claude_manager` logger hierarchy at app startup and never writes to disk or stdout — it exists solely to expose logs over the API.

```python
handler = MemoryLogHandler(max_entries=500)
logging.getLogger("claude_manager").addHandler(handler)
```

Each record stored in the buffer is a dict:

```python
{
    "timestamp": "2026-04-09T12:00:01+00:00",
    "level": "INFO",        # levelname from LogRecord
    "module": "scanner",    # last component of record.name
    "message": "...",       # formatted message
}
```

`GET /api/logs` calls `handler.get_logs(limit, level)` to retrieve entries. The `level` filter is case-insensitive; `limit` slices the most recent N entries from the buffer. Because `deque` is a ring buffer, old entries are evicted automatically when the buffer is full.

### Log hierarchy

All internal loggers use the `claude_manager.*` namespace:

| Logger | Module |
|--------|--------|
| `claude_manager.server` | `src/server.py` |
| `claude_manager.scanner` | `src/scanner.py` |
| `claude_manager.fleet` | `src/fleet.py` |
| `claude_manager.tmux_manager` | `src/tmux_manager.py` |
| `claude_manager.launcher` | `src/launcher.py` |
| `claude_manager.command_adapter` | `src/command_adapter.py` |

---

## System Tray Architecture

`src/desktop.py` — `run_desktop(bind, port)` — launches the native desktop GUI.

### Thread layout

```
Main thread          Background threads
────────────         ──────────────────
webview.start()  ←── _run_server()       (aiohttp API server)
                 ←── _run_tray()          (pystray tray icon, Linux/Windows only)
                 ←── _refresh_loop()      (inside _run_tray, polls API every 30s)
```

On macOS, pywebview's AppKit integration requires the main thread. The tray icon is therefore only available on Linux and Windows. On macOS, `_run_tray` is not started.

### Server reuse

Before starting the API server thread, `run_desktop` checks whether a claude-manager server is already running on the target port via `_server_is_ours(port)`. If the `/health` endpoint returns `{"status": "ok"}`, the existing server is reused and no new server thread is started. This allows the desktop GUI to attach to an already-running daemon without conflict.

### Tray menu

The tray menu is rebuilt dynamically by `_build_menu()` each time `_refresh_loop` fetches new data from `/api/sessions` and `/api/tmux`. The menu structure:

1. Header label (non-interactive)
2. **Open Web UI** (default action — opens browser)
3. Separator
4. Running Sessions (one item per session, grouped by machine)
5. Separator
6. Tmux / Psmux Sessions (one item per mux session)
7. Separator
8. Force Scan
9. API URL label (non-interactive)
10. Separator
11. **Exit** (calls `POST /api/exit`, then `icon.stop()`)

Clicking a session item calls `POST /api/sessions/launch`. Clicking a mux session item calls `POST /api/tmux/connect` (local) or `POST /api/tmux/connect-remote` (remote).

---

## Project Folder Name Encoding

Claude Code stores session files under `~/.claude/projects/<encoded-path>/`. The encoding rules (implemented in `decode_project_folder()`):

- **Unix paths:** leading `/` becomes a leading `-`, each `/` becomes `-`
  - `/Users/rbgnr/git/foo` → `-Users-rbgnr-git-foo`
- **Windows paths:** drive letter + `:` becomes `<letter>--`, each `\` becomes `-`
  - `C:\Users\rbgnr\git\foo` → `C--Users-rbgnr-git-foo`

The decoder prefers the `cwd` field recorded in the JSONL over the decoded folder name, since the dash-to-slash mapping is ambiguous for project names containing hyphens (e.g. `my-project` would be indistinguishable from a path component).
