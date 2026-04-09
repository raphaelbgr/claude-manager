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
