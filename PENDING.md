# PENDING — claude-manager

**Project purpose:** Fleet-wide Claude Code session manager. Discovers, organizes, and resumes Claude Code sessions across all fleet machines (mac-mini, ubuntu-desktop, avell-i7, windows-desktop) from a single interface — Web UI (React SPA), TUI (Textual), or native desktop window (pywebview). REST + WebSocket API at port 44740 drives all interfaces.

**Current state (2026-05-20):** Stable. VERSION v195+. Working tree on `master`. 1139 of 1145 tests pass (6 pre-existing failures unrelated to recent work). Major perf + cross-shell quoting rework landed across `src/scanner.py`, `src/command_adapter.py`, `src/launcher.py`, `src/terminals/windows.py`, and `src/server.py`. See `docs/GOTCHAS.md` for the hard-won knowledge.

**Cumulative scan perf** (231 MB live JSONL, 399 files, 74 cwds):
- Cold first scan: 50-60s → **~10s** (disk-persisted caches load at startup)
- Warm in-process scan: 21s parse → **~0.1s parse** (mtime cache + incremental tail reads)
- Scan-button (POST /api/sessions/scan): now runs fleet → (sessions || tmux) parallel with incremental WS push

**Quoting bug class closed:** the POSIX-`'\''`-inside-PowerShell tokenisation hazard that produced `psmux: sessions should be nested with care, unset PSMUX_SESSION to force` and the "extra cmd tabs on tmux attach" defects has been eliminated structurally — see GOTCHAS §1-3.

---

## Prioritised Pending Work

### P1 — High value, low risk

1. **Auth not documented in README / docs/api.md** — `src/auth.py` and the `POST /api/auth/config` + `GET /api/auth/config` API surface exist but are absent from `docs/api.md` and only briefly mentioned in README. Any user binding to `0.0.0.0` won't know to enable auth.

2. **`--enable-gui` alias deprecation notice missing** — `main.py` accepts `--enable-gui` as a deprecated alias for `--enable-desktop` but README usage block only shows `--enable-desktop`. A note in the Usage section would prevent user confusion.

3. **`/api/projects` endpoint undocumented** — `project_identity.py` powers a projects consolidation view, but `docs/api.md` has no entry for `/api/projects`. If the endpoint exists in `server.py` it needs a docs row; if not yet wired it's a pending implementation item.

4. **SSH pool connection health not surfaced via `/health`** — `ssh_pool.py` tracks per-machine connection state and backoff windows, but `/health` only returns top-level status. Exposing `pool_status` per machine would help diagnose connectivity issues from the Web UI without SSH access.

5. **`pane_streams` WebSocket channel not documented** — `state_store.py` implements pane streaming (subscribe/unsubscribe pane streams), but the WebSocket protocol section in `docs/architecture.md` does not list the pane-stream message types or the subscribe payload format.

### P2 — Medium value

6. **Version badge in README still reads 1.0.0** — `pyproject.toml` says `version = "1.0.1"` and `VERSION.json` is at `v179`. The shield badge in README still shows `version-1.0.0-blue`.

7. **Windows `_ssh_control_path` uses `/tmp/`** — `executor.py` builds ControlPath as `/tmp/cm-ssh-{uid}-{hash}` which is a Unix path; on Windows targets this path is irrelevant (ControlMaster is ignored), but if claude-manager itself runs on Windows the path would be wrong. Should resolve to `%TEMP%` on win32.

8. **No graceful shutdown for pane streams on WS disconnect** — `state_store.py`'s `unsubscribe_pane_all` must be called on WS close; if server.py misses this call, pane polling tasks leak until the app restarts.

### P3 — Nice to have

9. **Test count drift** — CHANGELOG says 588 tests across 12 files; repo now has 19 test files. Test count in README and CHANGELOG should be updated to match reality.

10. **`graphify-out/` committed to repo** — The knowledge-graph artefacts (hundreds of Obsidian `.md` stubs) are checked in, inflating the repo. A `.gitignore` entry for `graphify-out/` would clean the tree.
