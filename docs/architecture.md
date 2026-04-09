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

## Project Folder Name Encoding

Claude Code stores session files under `~/.claude/projects/<encoded-path>/`. The encoding rules (implemented in `decode_project_folder()`):

- **Unix paths:** leading `/` becomes a leading `-`, each `/` becomes `-`
  - `/Users/rbgnr/git/foo` → `-Users-rbgnr-git-foo`
- **Windows paths:** drive letter + `:` becomes `<letter>--`, each `\` becomes `-`
  - `C:\Users\rbgnr\git\foo` → `C--Users-rbgnr-git-foo`

The decoder prefers the `cwd` field recorded in the JSONL over the decoded folder name, since the dash-to-slash mapping is ambiguous for project names containing hyphens (e.g. `my-project` would be indistinguishable from a path component).
