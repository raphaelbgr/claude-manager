# claude-manager

> Manage Claude Code sessions and tmux/psmux sessions across multiple machines from a single interface.

![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-blue) ![License: MIT](https://img.shields.io/badge/License-MIT-green)

## Features

- **Session Scanner** — Discover all Claude Code sessions across your fleet (local + remote via daemon API or SSH fallback). Detects working/active/idle status using PID tracking and CPU sampling.
- **Tmux/Psmux Manager** — List, create, attach, and kill tmux (Linux/macOS) and psmux (Windows) sessions across every machine in your fleet.
- **Three Interfaces** — Terminal UI (Textual), Web UI (React SPA served by the API), Native Desktop (pywebview window with embedded API).
- **API-First** — A REST + WebSocket server drives all three interfaces. Any tool that speaks HTTP can integrate.
- **Fleet Integration** — Primary scanning path uses the [claude-dispatch](https://github.com/raphaelbgr/claude-dispatch) daemon HTTP API; SSH is the automatic fallback for machines without a daemon.
- **Cross-Platform** — macOS (iTerm2 / Terminal.app), Linux (common terminal emulators), Windows (PowerShell).
- **Live Status** — Polling background task pushes session/tmux/fleet diffs to all WebSocket subscribers every 30 seconds.
- **Universal Mux Parser** — Single parser handles both tmux pipe-delimited format and psmux plain-text output automatically.

## Quick Start

```bash
git clone https://github.com/raphaelbgr/claude-manager
cd claude-manager
./setup.sh
source .venv/bin/activate

# Terminal UI
claude-manager --tui

# Web UI + API server on localhost
claude-manager --enable-web

# Web UI + API server on LAN (accessible from other machines)
claude-manager --enable-web --bind 0.0.0.0

# Native desktop window (pywebview)
claude-manager --enable-gui

# API server only (no GUI)
claude-manager --api-only
```

After starting with `--enable-web`, open **http://localhost:44740** in your browser. When bound to `0.0.0.0`, the startup banner prints your LAN URL (e.g. `http://192.168.1.10:44740`).

## Architecture

```
┌─────────────────────────────────────────────────────┐
│                   claude-manager                     │
│                                                     │
│  ┌──────────┐  ┌──────────────┐  ┌──────────────┐  │
│  │  TUI     │  │  Web UI      │  │  Desktop GUI │  │
│  │(Textual) │  │ (React SPA)  │  │ (pywebview)  │  │
│  └────┬─────┘  └──────┬───────┘  └──────┬───────┘  │
│       │               │                 │           │
│       └───────────────┼─────────────────┘           │
│                       │                             │
│          ┌────────────▼────────────┐                │
│          │  REST + WebSocket API   │                │
│          │     (aiohttp :44740)    │                │
│          └────────────┬────────────┘                │
│                       │                             │
│       ┌───────────────┼───────────────┐             │
│       │               │               │             │
│  ┌────▼────┐   ┌──────▼──────┐  ┌────▼────┐        │
│  │ Scanner │   │TmuxManager  │  │  Fleet  │        │
│  │         │   │             │  │Discovery│        │
│  └────┬────┘   └──────┬──────┘  └────┬────┘        │
│       │               │               │             │
└───────┼───────────────┼───────────────┼─────────────┘
        │               │               │
        ▼               ▼               ▼
   Local FS +      tmux/psmux      HTTP /health
   ~/.claude/      local + SSH     (dispatch daemon)
   SSH remote      SSH remote      SSH fallback
```

**Components:**

| File | Role |
|------|------|
| `src/server.py` | aiohttp REST + WebSocket API server (port 44740) |
| `src/scanner.py` | Claude session discovery — local `~/.claude/` scan + SSH remote script |
| `src/tmux_manager.py` | tmux/psmux session listing, creation, and killing |
| `src/mux_parser.py` | Universal parser for tmux and psmux output formats |
| `src/fleet.py` | Fleet health discovery — HTTP ping then SSH fallback |
| `src/launcher.py` | Cross-platform terminal launcher (iTerm2, gnome-terminal, PowerShell) |
| `src/config.py` | Fleet machine definitions and global constants |
| `src/main.py` | CLI entry point and argument parsing |
| `src/tui/` | Textual TUI — 3-tab app (Sessions, Tmux, Fleet) |
| `src/web/index.html` | React SPA (CDN imports, single file, served at `/`) |
| `src/desktop.py` | pywebview native window + optional pystray system tray |

## Configuration

### Fleet Machines

Edit `src/config.py` to define your machines:

```python
FLEET_MACHINES: dict[str, dict] = {
    "my-server": {
        "ip": "192.168.1.10",       # LAN IP for HTTP probes
        "os": "linux",              # "darwin" | "linux" | "win32"
        "ssh_alias": "my-server",   # SSH config alias
        "mux": "tmux",              # "tmux" | "psmux"
        "dispatch_port": 44730,     # claude-dispatch daemon port, or None
    },
    "my-windows-pc": {
        "ip": "192.168.1.20",
        "os": "win32",
        "ssh_alias": "my-windows-pc",
        "mux": "psmux",
        "dispatch_port": None,      # No daemon — SSH-only fallback
    },
}
```

The machine running `claude-manager` is auto-detected by hostname or IP match. Remote machines are scanned in parallel.

### Preferences

User preferences are stored in `.claude-manager-prefs.json` (git-ignored) at the project root. Currently supports:

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `skip_permissions` | bool | `false` | Skip Claude Code permission prompts when launching sessions |

Preferences can be updated via the Web UI toggle or the `POST /api/preferences` endpoint.

## API Reference

The API server starts automatically with any mode except `--tui`.

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/health` | Server health, machine count, session count, last scan time |
| `GET` | `/api/sessions` | All sessions grouped by machine, then project folder |
| `GET` | `/api/sessions/{machine}` | Sessions for a specific machine |
| `POST` | `/api/sessions/scan` | Force immediate rescan; returns fresh data |
| `POST` | `/api/sessions/launch` | Open terminal and resume a Claude session |
| `GET` | `/api/tmux` | All tmux/psmux sessions across fleet |
| `GET` | `/api/tmux/{machine}` | Tmux sessions for a specific machine |
| `POST` | `/api/tmux/create` | Create a new detached tmux session |
| `POST` | `/api/tmux/connect` | Open local terminal attached to a tmux session |
| `POST` | `/api/tmux/connect-remote` | Open terminal on the remote machine's display |
| `POST` | `/api/tmux/kill` | Kill a tmux session by name |
| `GET` | `/api/preferences` | Get current preferences |
| `POST` | `/api/preferences` | Update preferences |
| `WS` | `/ws` | WebSocket — subscribe to live updates |

Full request/response documentation: [docs/api.md](docs/api.md)

## Web UI

The React SPA at `http://localhost:44740` provides:

- **Sessions tab** — Collapsible machine groups, each with collapsible project sections. Session cards show status badge (Working / Active / Idle), project path, first message summary, message count, and last modified time. Click a session to open it in a terminal.
- **Tmux tab** — Session cards per machine with window count, attached badge, and created time. Create, Attach, Remote Attach (opens on the remote machine's own display), and Kill buttons.
- **Fleet tab** — Online/offline indicator per machine with OS, IP, connectivity method (HTTP daemon vs SSH), and dispatch daemon status.
- **Filter bar** — Live text filter on session path, summary, status, or machine name.
- **Scan button** — Force a rescan from the UI at any time.
- **Live updates** — WebSocket keeps the UI in sync without manual refreshes.

## TUI

Launch with `claude-manager --tui`.

| Key | Action |
|-----|--------|
| `r` | Force rescan |
| `n` | New tmux session (Tmux tab only) |
| `/` | Show filter bar |
| `Esc` | Clear filter / close filter bar |
| `Enter` | Launch session / attach tmux session |
| `q` | Quit |
| `Tab` | Switch between Sessions / Tmux / Fleet tabs |

The TUI auto-refreshes every 30 seconds in the background.

## Desktop GUI

The native window mode (`--enable-gui`) uses **pywebview** to render the Web UI in a native window (WebKit on macOS, WebView2 on Windows, GTK WebKit on Linux). The API server runs in a background thread.

On Linux and Windows, an optional **system tray icon** (via `pystray` + `Pillow`) provides quick access:
- Open in browser
- Open TUI in a terminal
- Force scan
- Quit

On macOS, pywebview owns the main thread (AppKit requirement), so the system tray is skipped there.

Install desktop dependencies: `pip install ".[desktop]"`

## Integration with claude-dispatch

When a machine in `FLEET_MACHINES` has `dispatch_port` set, claude-manager uses the [claude-dispatch](https://github.com/raphaelbgr/claude-dispatch) daemon's HTTP API for faster, more reliable scanning:

- **Sessions:** `GET http://<ip>:<port>/sessions` — daemon returns pre-scanned session list
- **Tmux:** `GET http://<ip>:<port>/tmux` — daemon returns tmux session list
- **Health:** `GET http://<ip>:<port>/health` — used for fleet online/offline detection

If the daemon is unreachable or returns an empty response, claude-manager automatically falls back to running a self-contained Python scan script via SSH. Machines with `dispatch_port: None` always use the SSH path.

## Development

```bash
# Dev server (API + web UI)
python -m src.main --enable-web

# TUI dev
python -m src --tui

# Run tests
pip install pytest pytest-asyncio
pytest
```

See [docs/development.md](docs/development.md) for project structure, how to add fleet machines, and code patterns.

## License

MIT — see [LICENSE](LICENSE).
