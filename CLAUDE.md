# claude-manager

Multi-interface fleet session manager for Claude Code and tmux.

## Quick Start

```
./setup.sh                              # Install deps
source .venv/bin/activate
claude-manager --tui                    # TUI mode
claude-manager --enable-web             # API + Web UI on :44740
claude-manager --enable-web --bind 0.0.0.0  # LAN accessible
claude-manager --enable-gui             # System tray mode
```

## Architecture

- `src/server.py` — aiohttp REST+WS API (port 44740)
- `src/scanner.py` — Claude session scanning (local + SSH remote)
- `src/tmux_manager.py` — Tmux/psmux session management
- `src/launcher.py` — Cross-platform terminal launcher
- `src/fleet.py` — Fleet discovery via claude-dispatch
- `src/tui/` — Textual TUI (3-tab: Sessions, Tmux, Fleet)
- `src/web/index.html` — React SPA (served by API server)
- `src/desktop.py` — System tray (pystray + pywebview)

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | /health | Daemon health |
| GET | /api/sessions | All Claude sessions |
| GET | /api/tmux | All tmux sessions |
| GET | /api/fleet | Fleet status |
| POST | /api/sessions/launch | Launch terminal for session |
| POST | /api/sessions/scan | Force rescan |
| POST | /api/tmux/create | Create tmux session |
| POST | /api/tmux/connect | Connect to tmux session |
| WS | /ws | Live updates (subscribe to sessions/tmux/fleet) |

## Fleet

Scans mac-mini (192.168.7.102), ubuntu-desktop (192.168.7.13), avell-i7 (192.168.7.103), windows-desktop (192.168.7.101) via SSH.

## Dev

```
python -m src.main --enable-web          # dev server
python -m src --tui                      # TUI dev
```
