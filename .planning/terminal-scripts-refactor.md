# Terminal Launch Script Catalog + Send-Keys Auth API — Plan

Picks up from session ending at commit after `f85154d`. All prior work (tasks
1-9) already shipped on master. This plan covers 3 new workstreams the user
approved at the end of that session.

## Context the fresh session needs

- Editable install (`pip install -e .`) — `~/git/claude-manager/.venv/bin/python`.
- Daemon launched by `~/Applications/claude-manager.app/Contents/MacOS/run` as
  `python -m src.main --bind 0.0.0.0 --port 44740` (desktop mode, pywebview).
- asyncssh pool exists at `src/ssh_pool.py` with a module-level
  `default_pool()` singleton. Used by `SSHExecutor.exec_shell`.
- Tests: `pytest tests/`. Autouse fixture in `tests/conftest.py` disables the
  pool so subprocess-ssh mocks work.
- Fleet machines currently reachable: **mac-mini (darwin)**, **avell-i7
  (win32)**, **ubuntu-desktop (linux)**. **windows-desktop is offline — do not
  test there.**

## Workstream A — Script catalog refactor (blocking)

**Goal:** Replace per-adapter Python command-building with pre-made launch
scripts on disk, one per `(target_os, action)` combo. Each terminal adapter
just opens its window and runs the appropriate script file.

### Layout

```
src/terminals/scripts/
  unix_resume.sh          # cd $CWD && exec claude --resume $SID [$SKIP]
  unix_fresh.sh           # cd $CWD && exec claude [$SKIP]
  unix_tmux_resume.sh     # tmux new-session -d -s $NAME "cd $CWD && claude --resume $SID"
                          # then attach. Runs the _ensure_claude_running probe
                          # INSIDE the script via a wrapper check.
  unix_tmux_attach.sh     # tmux attach -t $NAME
  win_resume.ps1          # Set-Location $CWD; claude --resume $SID [$SKIP]
  win_fresh.ps1
  win_psmux_resume.ps1    # psmux new -d -s $NAME ; psmux send-keys ... ; psmux attach
  win_psmux_attach.ps1
```

Parameters come through env vars (`$CWD`, `$SID`, `$NAME`, `$SKIP`) to avoid
shell-escape hell. Scripts live in the repo — user can `cat` them to debug.

### Preserve pane-capture recovery

Inside `unix_tmux_attach.sh` / `win_psmux_attach.ps1`, before attaching:
1. Capture the target pane with `tmux capture-pane -p -t $NAME` (last 15 lines)
2. Grep for claude TUI markers: `Welcome to Claude`, `Claude Code`, `╭`, `╰`, `│ >`
3. If none found AND the last line matches a shell prompt regex, send
   `claude [--dangerously-skip-permissions]` via `send-keys` then sleep 0.8s
4. Attach

This is exactly what `launcher._ensure_claude_running` does today. Moving it
into the script means the behavior survives even if someone invokes the script
manually outside the daemon.

### Launcher refactor

Replace the body of `launch_claude_session`, `launch_tmux_attach`,
`launch_new_tmux_and_attach` with:

```python
def render_script(name: str, **kwargs) -> str:
    """Return the script's content with $-style params substituted.
    For remote: returns the inline body. For local: returns the path to the
    script file directly so the terminal runs the real file (inspectable)."""
```

Callers pass the rendered command string to the same `launch_terminal(..., terminal_id=, title=)` plumbing as today — the TerminalPicker architecture from commit `f85154d` is unchanged.

### Deletes

- `launcher._ensure_claude_running` (moves into scripts)
- `launcher._looks_like_shell_prompt` + `_SHELL_PROMPT_RE` (script uses
  `egrep` directly)
- The `_launch_macos_multi` chained-AppleScript hack for Windows psmux — one
  script replaces the delay/send-keys dance.

### Tests to update

- `tests/test_launcher.py::TestShellInjectionSafety` — reassert on the rendered
  script params, not the inline string.
- `tests/test_launcher.py::TestLaunchNewTmuxAndAttach` — `terminal_id` kwarg.
- `tests/test_server.py::TestSessionsLaunchEndpoint::test_calls_launch_with_correct_args` — `terminal_id=None` now in the call signature.
- `tests/test_ui_integration.py::TestAPIEndpointReferences::test_{pin,unpin}_endpoint_referenced` — UI now calls `/api/projects/{pin,unpin}`. Update string asserts.

## Workstream A.1 — Post-init Claude session rename

**Goal:** After the spawned terminal's Claude Code instance finishes
initializing, send `/rename <pretty-name>` to Claude so the session shows a
human-friendly name in the Claude UI. The tmux/psmux session name stays
ASCII-safe (muxes reject `/`); the pretty name is Claude-only and uses `/`.

### Naming rules

```
<pretty-name> = [<mux-tag>] <machine>/<project-name>[-N]

mux-tag:        "tmux" | "psmux" | ""  (omit bracket if no mux)
machine:        session.machine — "mac-mini", "avell-i7", "ubuntu-desktop"
project-name:   basename of session.cwd — e.g. "claude-manager"
-N:             suffix appended ONLY when the exact name already exists in the
                rename-registry (-2, -3, ...)
```

Examples:
- `[tmux] mac-mini/claude-manager`
- `[psmux] avell-i7/polymarket-bot-2`
- `ubuntu-desktop/weak-sweeper` (plain terminal, no mux)

### Registry

A JSON file at `~/.claude-manager/rename-registry.json`:

```json
{
  "mac-mini_claude-manager-session-01": "[tmux] mac-mini/claude-manager",
  "avell-i7_polymarket-1":              "[psmux] avell-i7/polymarket-bot"
}
```

- **Key:** mux session name (ASCII-safe, no slashes — what tmux/psmux itself
  uses). For plain-terminal (no mux) launches, key is `terminal:<uuid>` so
  each window still has a unique entry.
- **Value:** pretty name actually sent to `/rename`.
- Lookup-before-rename prevents re-renaming on reconnect. The N suffix is
  computed by scanning this registry for existing values that match the
  base name.

### Where the rename runs

Append to `unix_tmux_resume.sh` / `win_psmux_resume.ps1` / `unix_resume.sh`
etc. at the END of the script, after the `claude` process is launched:

```bash
# After claude is started, wait for its TUI to draw then send /rename.
# Detection: poll `tmux capture-pane` every 500ms, up to 10s, for the
# "│ >" Claude prompt marker. Once found, `tmux send-keys` the /rename line.
```

For plain-terminal (no mux) launches, rename is done via the authenticated
send-keys API after the window opens — Workstream B dependency. Track it as
"best effort, skip if no mux" so plain terminals still work without B.

### Simplifications applied

1. **One script path, not two.** The rename logic lives at the tail of each
   launch script. No separate "post-launch step" orchestrated from Python.
2. **Registry is flat JSON, not a DB.** Read, compute, write atomically
   (`os.replace`). Max size ~1KB per entry, negligible.
3. **Mux name always ASCII.** Existing `sanitize_mux_name()` in
   `command_adapter.py` already strips `/`. Pretty name is the ONLY place
   where slashes appear, and it only goes to Claude's `/rename`.
4. **De-dupe is cheap.** Registry scan for prefix match → max N suffix + 1.
   No locking — the daemon is the only writer.

### Tests

- `tests/test_rename_registry.py`:
  - Empty registry → first name is unsuffixed
  - Existing `mac-mini/foo` + new request for `mac-mini/foo` → `foo-2`
  - Existing `foo` + `foo-2` + new → `foo-3`
  - Same mux session name re-requested → same pretty name returned (idempotent)
- `tests/test_launcher_rename.py`:
  - Script renders with `/rename` line appended
  - Plain terminal mode: script has no `/rename` (deferred to send-keys API)


## Workstream B — Authenticated send-keys API

**Goal:** Expose a `POST /api/terminal/send` endpoint so external tools can
inject keystrokes into tmux/psmux sessions using the user's existing
pubkey-derived bearer token (already built in `src/auth.py`).

### Contract

```
POST /api/terminal/send
Authorization: Bearer <token from auth.py compute_token()>
Body: { "machine": "mac-mini", "session": "foo", "keys": "claude --resume abc\n" }
→ 200 {"ok": true}
→ 401 if token missing/invalid
→ 400 if session not found on machine
```

### Wiring

- Reuse `auth.py`'s `extract_bearer_token` + `compute_token` via the middleware
  that already protects non-loopback requests.
- Dispatch through `ssh_pool.default_pool().run(machine, <tmux send-keys>)` for
  remote machines, `LocalExecutor.exec(...)` for the daemon's host.
- Quote the keys with `shlex.quote` for Unix targets, PowerShell-escape for
  Windows psmux.

### Tests

- `tests/test_api_endpoints.py::TestTerminalSend`:
  - 401 without token
  - 401 with wrong token
  - 200 + correct tmux-send-keys command on valid token
  - 400 when session doesn't exist (via mocked `list_all_tmux`)

## Workstream C — Cross-fleet verification

**Goal:** Probe + launch each terminal on each machine, capture the resulting
pane, confirm Claude Code actually came up.

### Matrix

| Machine | Terminals to verify |
|---------|---------------------|
| mac-mini (darwin) | iTerm2, Terminal.app, Alacritty*, kitty*, Ghostty* (* if installed) |
| ubuntu-desktop (linux) | gnome-terminal, konsole*, xfce4-terminal, alacritty*, kitty*, xterm |
| avell-i7 (win32) | wt, pwsh, powershell, cmd, git-bash |

Skip installations not detected by `GET /api/machines/:m/terminals`.

### Harness

`scripts/verify-terminals.py` — for each installed terminal on each machine:
1. Create a tmux/psmux session via the launch API (mode=tmux)
2. Wait 2s, then `tmux capture-pane -p -t <name>` via ssh_pool
3. Assert the pane contains at least one of `"Welcome to Claude"`,
   `"│ >"`, `"Claude Code"`
4. Kill the session
5. Report PASS/FAIL per combo to stdout

User does the final visual confirm on actual window appearance (color, title,
layout) — pane content is the automated gate.

## Order of operations

1. Workstream A first (scripts + launcher refactor) — self-contained, unblocks B.
2. Fix the 9 new test failures after A — they'll all be touching launcher.py /
   server.py anyway.
3. Workstream B (auth API) second.
4. Workstream C (verification harness) last — runs against the combined A+B
   state to validate end-to-end.

## What NOT to do

- Don't remove `/api/sessions/pin` / `/api/sessions/unpin` — kept for back-
  compat. UI already migrated to `/api/projects/*`.
- Don't touch `.claude-manager-prefs.json` — user-state.
- Don't test against windows-desktop (offline).
- Don't regress the 21 baseline test failures that predate task 1 (they
  already fail on commit `60ffc0b`).
