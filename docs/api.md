# API Reference

The claude-manager API server runs on port **44740** by default. All endpoints return JSON.

## CORS

All responses include:
```
Access-Control-Allow-Origin: *
Access-Control-Allow-Methods: GET, POST, OPTIONS
Access-Control-Allow-Headers: Content-Type, Authorization
```

Preflight `OPTIONS` requests return `204 No Content`.

## Error Handling

Errors return a JSON body with an `error` field:
```json
{"ok": false, "error": "description of the problem"}
```

HTTP status codes used:
- `200` — success
- `400` — bad request (missing or invalid fields)
- `500` — internal error

---

## GET /health

Server liveness and summary stats.

**Response:**
```json
{
  "status": "ok",
  "port": 44740,
  "machines": 4,
  "sessions": 23,
  "last_scan": "2026-04-09T12:00:00+00:00"
}
```

---

## GET /api/sessions

All Claude Code sessions across the fleet, grouped by machine then project folder.

**Response:**
```json
{
  "mac-mini": [
    {
      "project_folder": "-Users-rbgnr-git-myproject",
      "sessions": [
        {
          "session_id": "abc123",
          "machine": "mac-mini",
          "project_folder": "-Users-rbgnr-git-myproject",
          "project_path": "/Users/rbgnr/git/myproject",
          "cwd": "/Users/rbgnr/git/myproject",
          "slug": "myproject",
          "summary": "Add authentication to the API",
          "messages": 142,
          "modified": "2026-04-09T11:58:00+00:00",
          "status": "working",
          "pid": 12345,
          "file_size": 98304,
          "name": "",
          "cpu_percent": 12.5
        }
      ]
    }
  ],
  "ubuntu-desktop": [ ... ]
}
```

**Session `status` values:**
- `"working"` — process alive, actively generating (CPU > 5% or JSONL modified < 15s ago)
- `"active"` — process alive, waiting for input
- `"idle"` — no live process

---

## GET /api/sessions/{machine}

Sessions for one machine, same structure as the per-machine entry in `/api/sessions`.

**Path parameter:** `machine` — name from `FLEET_MACHINES` (e.g. `mac-mini`)

**Response:** array of project-group objects (same as the machine's array above)

---

## POST /api/sessions/scan

Force an immediate rescan of all machines. Blocks until the scan completes, then pushes results to WebSocket subscribers.

**Request body:** none required

**Response:**
```json
{
  "ok": true,
  "sessions": [ ...flat array of session objects... ],
  "tmux": [ ...flat array of tmux session objects... ],
  "last_scan": "2026-04-09T12:01:00+00:00"
}
```

---

## POST /api/sessions/launch

Open a terminal window and resume a specific Claude Code session.

For local sessions: opens a terminal with `claude --resume <session_id>`.
For remote sessions: opens a terminal with `ssh <alias> -t 'claude --resume ...'`.

**Request body:**
```json
{
  "session_id": "abc123",
  "cwd": "/Users/rbgnr/git/myproject",
  "machine": "mac-mini"
}
```

| Field | Required | Description |
|-------|----------|-------------|
| `session_id` | yes | The session UUID to resume |
| `cwd` | yes | Working directory to `cd` to before launching |
| `machine` | yes | Fleet machine name |

**Response:**
```json
{"ok": true}
```
or
```json
{"ok": false, "error": "osascript timed out"}
```

---

## GET /api/fleet

Fleet health status for all configured machines.

**Response:**
```json
{
  "mac-mini": {
    "name": "mac-mini",
    "online": true,
    "os": "darwin",
    "ip": "192.168.7.102",
    "method": "http",
    "health_data": {
      "status": "ok",
      "jobs_running": 0
    }
  },
  "ubuntu-desktop": {
    "name": "ubuntu-desktop",
    "online": true,
    "os": "linux",
    "ip": "192.168.7.13",
    "method": "ssh",
    "health_data": {"ssh": "ok"}
  },
  "offline-machine": {
    "name": "offline-machine",
    "online": false,
    "os": "win32",
    "ip": "192.168.7.101",
    "method": "unreachable",
    "health_data": null
  }
}
```

**`method` values:**
- `"http"` — reached via claude-dispatch HTTP daemon
- `"ssh"` — reached via SSH echo
- `"unreachable"` — all probes failed

---

## GET /api/tmux

All tmux/psmux sessions across the fleet.

**Response:**
```json
[
  {
    "name": "claude-work",
    "machine": "mac-mini",
    "created": "2026-04-09T08:00:00+00:00",
    "windows": 3,
    "attached": true,
    "is_local": true
  },
  {
    "name": "dev-session",
    "machine": "ubuntu-desktop",
    "created": "2026-04-09T09:30:00+00:00",
    "windows": 1,
    "attached": false,
    "is_local": false
  }
]
```

---

## GET /api/tmux/{machine}

Tmux sessions for one machine. Returns the same per-machine subset of the `/api/tmux` array.

---

## POST /api/tmux/create

Create a new detached tmux/psmux session on a fleet machine.

**Request body:**
```json
{
  "machine": "mac-mini",
  "name": "my-session",
  "cwd": "/Users/rbgnr/git/myproject",
  "command": "claude"
}
```

| Field | Required | Description |
|-------|----------|-------------|
| `machine` | yes | Fleet machine name |
| `name` | yes | Session name |
| `cwd` | no | Working directory for the session |
| `command` | no | Command to run in the session |

**Response:**
```json
{"ok": true}
```

---

## POST /api/tmux/connect

Open a **local** terminal window and attach to an existing tmux session (via SSH if remote).

**Request body:**
```json
{
  "machine": "mac-mini",
  "session_name": "my-session"
}
```

**Response:** `{"ok": true}` or `{"ok": false, "error": "..."}`

---

## POST /api/tmux/connect-remote

Open a terminal window **on the remote machine's own display** (not via SSH to your local display). Useful for machines with physical monitors or VNC.

- macOS remote: triggers `osascript` via SSH to open Terminal.app
- Linux remote: runs `DISPLAY=:0 x-terminal-emulator` via SSH
- Windows remote: runs `Start-Process powershell` via SSH

**Request body:**
```json
{
  "machine": "ubuntu-desktop",
  "session_name": "my-session"
}
```

**Response:** `{"ok": true}` or `{"ok": false, "error": "..."}`

---

## POST /api/tmux/kill

Kill a tmux/psmux session by name.

**Request body:**
```json
{
  "machine": "mac-mini",
  "name": "my-session"
}
```

**Response:** `{"ok": true}` or `{"ok": false, "error": "..."}`

---

## GET /api/logs

Return recent log entries from the in-memory ring buffer (last 500 entries max).

**Query parameters:**

| Parameter | Default | Description |
|-----------|---------|-------------|
| `limit` | `100` | Maximum number of entries to return |
| `level` | *(none)* | Filter by log level: `DEBUG`, `INFO`, `WARNING`, `ERROR` |

**Response:**
```json
{
  "logs": [
    {
      "timestamp": "2026-04-09T12:00:01+00:00",
      "level": "INFO",
      "module": "server",
      "message": "background_scan: 23 sessions, 4 tmux, 4 fleet machines in 1.23s"
    },
    {
      "timestamp": "2026-04-09T12:00:01+00:00",
      "level": "ERROR",
      "module": "scanner",
      "message": "SSH to ubuntu-desktop failed: Connection timed out"
    }
  ]
}
```

---

## POST /api/sessions/rename

Rename a Claude Code session by writing a `name` field into the active PID file (`~/.claude/sessions/<pid>.json`).

The session must be currently running (status `active` or `working`) — a PID file is only present while the Claude Code process is alive.

**Request body:**
```json
{
  "machine": "mac-mini",
  "session_id": "abc123",
  "pid": 24740,
  "name": "my-new-name"
}
```

| Field | Required | Description |
|-------|----------|-------------|
| `machine` | yes | Fleet machine name |
| `session_id` | yes | Session UUID |
| `pid` | no | Process ID — speeds up PID file lookup; falls back to scanning all `*.json` files |
| `name` | yes | New display name (non-empty string) |

**Response:**
```json
{"ok": true, "name": "my-new-name"}
```

---

## POST /api/sessions/archive

Add a session ID to the archived list (stored in `.claude-manager-prefs.json`). Archived sessions are hidden from the default view.

**Request body:**
```json
{"session_id": "abc123"}
```

**Response:**
```json
{"ok": true, "archived_sessions": ["abc123"]}
```

---

## POST /api/sessions/unarchive

Remove a session ID from the archived list.

**Request body:**
```json
{"session_id": "abc123"}
```

**Response:**
```json
{"ok": true, "archived_sessions": []}
```

---

## POST /api/sessions/pin

Add a session ID to the pinned list (stored in `.claude-manager-prefs.json`). Pinned sessions appear at the top of the session list.

**Request body:**
```json
{"session_id": "abc123"}
```

**Response:**
```json
{"ok": true, "pinned_sessions": ["abc123"]}
```

---

## POST /api/sessions/unpin

Remove a session ID from the pinned list.

**Request body:**
```json
{"session_id": "abc123"}
```

**Response:**
```json
{"ok": true, "pinned_sessions": []}
```

---

## POST /api/hardware

Collect CPU, GPU, and memory stats for a machine. Results are cached for 30 seconds.

**Request body:**
```json
{"machine": "ubuntu-desktop"}
```

Omit `machine` or set it to the local machine name to query the local host.

**Response:**
```json
{
  "ok": true,
  "cpu": {
    "name": "AMD Ryzen 7 5800X",
    "cores": 16,
    "usage_percent": 12.3,
    "temp_c": 54.0
  },
  "gpus": [
    {
      "name": "NVIDIA GeForce GTX 1060",
      "temp_c": 48.0,
      "usage_percent": 0.0,
      "memory_used_mb": 512.0,
      "memory_total_mb": 6144.0
    }
  ],
  "memory": {
    "total_gb": 32.0,
    "used_gb": 14.2,
    "percent": 44.4
  }
}
```

GPU data uses `nvidia-smi` when available. On macOS without NVIDIA hardware, GPU name is read from `system_profiler`; temperature and usage fields are `null`. `cpu_temp` requires `psutil.sensors_temperatures()` support (not available on macOS or Windows).

---

## POST /api/browse

List subdirectories at a given path on a machine. Used by the folder picker in the Web UI.

**Request body:**
```json
{
  "machine": "ubuntu-desktop",
  "path": "/home/myuser/git"
}
```

Omit `path` to start at the home directory. Omit `machine` for the local host.

**Response:**
```json
{
  "ok": true,
  "path": "/home/myuser/git",
  "parent": "/home/myuser",
  "drive": "/",
  "dirs": [
    {"name": "myproject", "path": "/home/myuser/git/myproject"},
    {"name": "other-repo", "path": "/home/myuser/git/other-repo"}
  ]
}
```

Hidden directories (names starting with `.`) are excluded. At most 200 entries are returned, sorted alphabetically.

---

## POST /api/drives

List disk drives/volumes on a machine. Used by the folder picker to show the root of each drive.

**Request body:**
```json
{"machine": "windows-desktop"}
```

Omit `machine` for the local host.

**Response:**
```json
{
  "ok": true,
  "drives": [
    {
      "path": "C:\\",
      "name": "C:",
      "label": "C:",
      "total_gb": 953.9,
      "free_gb": 412.1,
      "is_system": true
    },
    {
      "path": "D:\\",
      "name": "D:",
      "label": "D:",
      "total_gb": 1863.0,
      "free_gb": 900.5,
      "is_system": false
    }
  ]
}
```

Virtual, pseudo, and system filesystems (devfs, tmpfs, sysfs, /proc, /sys, /dev, /run, /snap) are excluded.

---

## POST /api/mkdir

Create a single new directory (no recursive parents) on a machine.

**Request body:**
```json
{
  "machine": "ubuntu-desktop",
  "path": "/home/myuser/git/new-project"
}
```

**Response:**
```json
{"ok": true, "path": "/home/myuser/git/new-project"}
```

**Error responses:**
- `400` — path is not absolute, or parent does not exist
- `403` — permission denied
- `409` — directory already exists
- `504` — SSH timeout (remote machines)

---

## POST /api/exit

Gracefully shut down the server. The server responds, then calls `os._exit(0)` after a 0.5-second delay.

**Request body:** none

**Response:**
```json
{"ok": true, "message": "Shutting down..."}
```

If the server is running under launchd (`KeepAlive: true`) or systemd (`Restart=always`), it will restart automatically. Stop the service first if you want a clean exit.

---

## POST /api/restart

Reset the background scan cycle without killing the server. Cancels the current background task, clears cached state, and restarts the scan loop.

**Request body:** none

**Response:**
```json
{"ok": true, "message": "Scan cycle restarted"}
```

---

## GET /api/preferences

Get current user preferences.

**Response:**
```json
{
  "skip_permissions": false
}
```

---

## POST /api/preferences

Update one or more preference keys. Merges with existing preferences.

**Request body:**
```json
{
  "skip_permissions": true
}
```

**Response:** the full preferences object after update.

---

## WebSocket: /ws

Connect to `ws://host:44740/ws` for live updates.

### Subscribe

```json
{"type": "subscribe", "channel": "sessions"}
{"type": "subscribe", "channel": "fleet"}
{"type": "subscribe", "channel": "tmux"}
```

On subscribe, the server immediately sends a `snapshot` with current data, then sends `update` messages every `SCAN_INTERVAL` seconds (default: 30) and after any forced scan.

### Unsubscribe

```json
{"type": "unsubscribe", "channel": "sessions"}
```

### Messages from server

**Snapshot** (sent immediately on subscribe):
```json
{
  "type": "snapshot",
  "channel": "sessions",
  "data": [ ...array of session objects... ]
}
```

**Update** (sent on each background scan or forced scan):
```json
{
  "type": "update",
  "channel": "tmux",
  "data": [ ...array of tmux session objects... ],
  "action": "refresh"
}
```

**Error:**
```json
{"type": "error", "message": "invalid JSON"}
```

Note: `sessions` channel data is a flat array of session objects (not the machine-grouped structure of `GET /api/sessions`).
