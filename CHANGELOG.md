# Changelog

All notable changes to this project will be documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

## [Unreleased] - 2026-05-20

### Performance

- **scan_local: incremental + 4-layer cache** (~50s cold → ~3s warm, ~7s cold-restart).
  Per-file in-memory cache keyed on `(path, mtime_ns, size)`; on growth, `parse_session`
  seeks past `last_size` and parses only the new tail bytes (turns a 231 MB live-
  conversation re-read into a 50ms append-read). Both the session cache and the
  per-cwd git cache are persisted to disk (`~/.claude-manager/scan-cache.json`,
  `git-cache.json`) so a fresh process starts already warm.
- **scan_progress broadcast throttle** — was firing per parsed JSONL file (~300+
  WS broadcasts per scan), dominating wall time. Now capped at ≤20 emits/sec;
  first and last calls always fire. ~20× reduction in broadcast count.
- **Batched `cpu_percent` sampling** in `_mark_active_sessions` — was sleeping
  100ms per active session (N×100ms total). Now primes every Process up front,
  sleeps ONCE, reads every Process. Pid phase 2.9s → 0.4s with N≈25.
- **POST /api/sessions/scan parallel legs** — was `fleet → scan → tmux` serial;
  now runs fleet first then `scan || tmux` concurrently with progressive WS push
  so tmux lands as soon as it's ready instead of waiting on the slower session scan.
- **`handle_restart` keeps prior state** — pre-fix wiped sessions/fleet/tmux and
  awaited cancellation unbounded; UI went blank for 30s+. Now uses
  `asyncio.wait_for(asyncio.shield(bg), 0.5)` so the HTTP response returns in
  <600ms and the UI keeps showing the prior snapshot until the new cycle replaces it.

### Cross-shell quoting (bug-class closure)

- **POSIX `'\''` quote escapes no longer leak into PowerShell parse contexts.**
  `CommandAdapter` Windows-target builders (`cd_command_ssh`,
  `build_session_command_ssh`, `build_new_session_command_ssh`, `build_pane_command`)
  now emit PS-double-quoted paths via `_ps_double_quote` instead of PS-single-quoted.
  The result tokenises identically under bash POSIX-quote AND PowerShell single-
  quote-literal wrapping by the caller. Closes the "psmux: sessions should be
  nested with care, unset PSMUX_SESSION to force" defect on Windows→Windows attaches.
- **WindowsTerminalAdapter + `_launch_windows` use `-EncodedCommand`** (base64
  UTF-16-LE) instead of `-Command "<escaped>"`. wt.exe's argument parser treats
  `;` as a tab separator even inside quoted -Command values — pre-fix a single
  SSH command with `;` produced a pwsh tab plus 2-3 stray cmd tabs. EncodedCommand
  sidesteps every quoting and parsing layer.
- **`launch_tmux_attach` / `launch_claude_session`** skip the inner PS title-set
  for Windows targets — the outer `wt --title` already labels the tab and the
  inner emit was a source of POSIX-quote pollution.
- **`_ensure_claude_running` local-Windows path** uses `mux_send_keys_ps` (PS-
  native single-quote escape via doubling) when `sys.platform == "win32"`,
  since `asyncio.create_subprocess_shell` invokes cmd.exe which can't parse
  POSIX shlex.quote output.

### Cross-platform

- **`os.getuid()` Windows guard** in `_ssh_control_path` — was `AttributeError`
  on Windows whenever code forced the subprocess SSH fallback path (tests
  surfaced this). Now falls back to a stable per-user hash of `$USERNAME`.

### Tracelink instrumentation

- New `src/tracking/` package with soft-import `tl` (no-op stub when tracelink
  is absent — every other fleet machine). Declarations in
  `src/tracking/declarations.py`. Strategic emit points across `server.py`
  (per-API trace middleware, scan cycle, scan-button phases), `scanner.py`,
  `fleet.py`, `executor.py`, `ssh_pool.py`, `tmux_manager.py`,
  `command_adapter.py`, `state_store.py`, and `src/terminals/*` (16 adapters).
  Sink: `$TEMP/tracelink/claude-manager-<run_id>.jsonl`.
- Bug found in agent-written instrumentation: `tl.event("...", name=val)`
  collided with `event(name: str, **data)`'s positional parameter. All sites
  renamed to `session=val`. See GOTCHAS §11.

### UI

- **Session cards `flex-wrap: wrap`** by default + `@media (max-width: 900px)`
  rule that promotes the `.compact-spacer` to 100% width. Action buttons
  reflow to a 2nd row instead of overflowing silently between 600-900px
  viewports. Verified: 0 overflowing cards at 950px (was 7).
- **`Update → v?` badge** rendering fixed — was literal-rendering `?` when
  the GitHub commits API returned `latest` without a `version` int.

### Tests

- 1080 → 1139 passing (+59 net). Added `tests/test_scan_cache.py` (11 tests:
  incremental parser, in-memory cache, disk persistence, batched cpu_percent)
  and `tests/test_server_perf.py` (7 tests: WT EncodedCommand transport,
  scan_progress throttle, restart-keeps-state). Updated
  `tests/test_command_adapter.py` and `tests/test_launcher.py` assertions
  to match the new PS-double-quote / EncodedCommand shape.
- 6 pre-existing failures remain in `test_tmux_manager.py` (detect_local_machine
  patching mismatch) and `test_config.py` — present on clean master before
  this session's work; not regressions.

### Documentation

- **`docs/GOTCHAS.md`** — 15 hard-won pieces of knowledge from the debugging
  arc, each one a real shipped bug with the failure shape and the rule that
  prevents recurrence.

## [Unreleased] - 2026-05-17

### Documentation

- Added `PENDING.md` at repo root: real-code-derived pending work items prioritised P1–P3.
- Added `docs/bmad/prd.md`: BMAD v6 product requirements document covering problem statement, goals, user personas, features, constraints, and success metrics — derived from actual source code.
- Added `docs/bmad/architecture.md`: BMAD v6 architecture document covering system context, component decomposition, data models, API contracts, infrastructure requirements, and technology decisions — derived from actual source code.
- Updated `CHANGELOG.md` with this maintenance entry.

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
