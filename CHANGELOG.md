# Changelog

All notable changes to this project will be documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

## [1.0.0] - 2026-04-09

### Added

- **Multi-interface fleet session manager** — Web UI (React SPA), Terminal UI (Textual), and Native Desktop window (pywebview) all driven by a single REST + WebSocket API server on port 44740.
- **Session scanning** across macOS, Linux, and Windows via claude-dispatch daemon HTTP API with automatic SSH fallback for machines without a daemon.
- **Working / Active / Idle status detection** using PID tracking, CPU sampling (psutil), and file mtime analysis.
- **tmux/psmux session management** — list, create, attach, kill across all fleet machines locally and remotely.
- **Cross-platform terminal launcher** — iTerm2, Terminal.app, gnome-terminal, PowerShell, and Git Bash detection with automatic fallback.
- **OS-aware command adapter** — builds correct shell commands for bash, cmd.exe, PowerShell, and Git Bash depending on the target machine OS.
- **Collapsible session cards** with project grouping, collapsed view showing only status and path (no machine stats noise in collapsed state).
- **Pin and archive sessions** — pin important sessions to the top of the list, archive sessions to hide them without deleting.
- **Session rename** — equivalent to `/rename` in Claude Code, stored in session metadata.
- **Universal mux parser** — single parser handles both tmux pipe-delimited format and psmux plain-text output, with format auto-detection.
- **Git branch display** on session cards — shows the active branch when the session was created.
- **File size and message count** on session cards.
- **Hardware monitoring** — CPU, GPU, and RAM stats per machine in fleet view and expanded session cards.
- **Folder browser** — integrated directory picker with Windows drive selector and mkdir support for launching sessions in new locations.
- **Comprehensive filter bar** — filter by machine, status, time window, sort order, and free-text search across path, summary, and machine.
- **`--dangerously-skip-permissions` toggle** — exposed as a preference in Web UI and persisted in `.claude-manager-prefs.json`.
- **Preferences API** — `GET/POST /api/preferences` with JSON persistence, git-ignored preferences file.
- **WebSocket live updates** — background polling task pushes session, tmux, and fleet diffs to all subscribers every 30 seconds.
- **System tray icon** on Linux and Windows via pystray + Pillow — open browser, force scan, quit.
- **Native desktop GUI** via pywebview — WebKit on macOS, WebView2 on Windows, GTK WebKit on Linux.
- **Structured logging** with `/api/logs` endpoint for retrieving recent log entries.
- **Fleet health view** — online/offline indicator per machine with OS, IP, connectivity method (daemon vs SSH), and dispatch status.
- **Resume button variants** — Resume in local terminal, tmux, psmux, and SSH depending on session location and available mux.
- **Instant collapse toggle** — expand/collapse all session groups with one click.
- **Windows drive picker** — drive selector in folder browser handles Windows drive letters correctly.
- **`(local)` label** on session cards — clearly marks sessions running on the same machine as claude-manager.
- **Remote psmux session creation** using `send-keys` for reliable cross-platform tmux/psmux command delivery.
- **Machine stats** visible only in expanded session card view to keep collapsed cards clean.
- **psmux session visibility** — API-first scanning with SSH fallback ensures Windows psmux sessions appear reliably.
- **588 unit and integration tests** across 12 test files covering API endpoints, session scanning, tmux management, mux parsing, fleet discovery, launcher logic, and desktop integration.
