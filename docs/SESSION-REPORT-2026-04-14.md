# Session report — 2026-04-14

## Scope
Fix local Resume/tmux buttons, rework pin semantics, stop Windows SSH session
storms, label spawned terminal windows, add a pluggable terminal picker per
machine/OS.

## What shipped (on `master`)

| Commit | Task | Summary |
|---|---|---|
| `446c981` | #1 | Native-OS PATH fallbacks for LocalExecutor — fixes `[Errno 2] No such file or directory: 'tmux'` when the daemon is launched from a GUI/LaunchAgent with a stripped env. |
| `2170e57` | #2, #3 | Removed redundant "Resume in terminal/tmux" buttons from the expanded card view. Mobile: compact-row spacer collapses below 600px so action buttons wrap flush-left instead of being shoved off-screen. |
| `f00513d` | #4, #5, #8, #9 | **Pin-per-project** (backend `/api/projects/{pin,unpin}` + prefs `pinned_projects`; session-level pin button removed; pinned section iterates projects, collapsible via `localStorage.pinnedSectionCollapsed`). **asyncssh pool** — 1 persistent connection per machine for the app lifecycle, lock-serialised reconnect with 1→30s backoff, `SSHExecutor.exec_shell` prefers the pool and silently falls back to subprocess ssh on failure. **Cross-OS window titles** via `build_window_title` + `title_prefix_for` (ANSI OSC 0 on Unix, `$Host.UI.RawUI.WindowTitle` on PowerShell), wrapped in try/catch so title-set never breaks a launch. |
| `f85154d` | #6 | **SOLID terminal adapter architecture**: `src/terminals/{base,darwin,linux,windows,__init__}.py`. Each terminal is a subclass with `probe_shell()` and `launch(command, title)`; registry indexed by `(os, id)`. Covers iTerm2 / Terminal.app / Alacritty / kitty / Ghostty / gnome-terminal / konsole / xfce4-terminal / xterm / Windows Terminal / pwsh / PowerShell / cmd / Git Bash. `/api/machines/{machine}/terminals` probes via runner protocol (local subprocess or asyncssh pool — same code path for local and remote). Split-button dropdown on Resume + tmux carets; 5-min server-side cache. |
| `a46fd68` | test infra | pytest conftest disables asyncssh pool during tests so suite runs offline. |
| `c4e4919` | docs | Preparatory plan for a terminal-scripts refactor (next-session work). |

### Task index

- [x] #1 E2E fix local resume/tmux buttons (`446c981`)
- [x] #2 Remove redundant expanded-view buttons (`2170e57`)
- [x] #3 Mobile vertical layout (`2170e57`)
- [x] #4 PIN section above all — subsumed by #9 (`f00513d`)
- [x] #5 Persistent asyncssh SSH pool (`f00513d`)
- [x] #6 Terminal dropdown disclosure — SOLID adapter architecture (`f85154d`)
- [x] #7 command-ranker integration — dev-time only, no runtime code (see section below)
- [x] #8 Cross-OS window titles (`f00513d`)
- [x] #9 Pin per-project + collapsible pinned section (`f00513d`)

## Still open (MUST address next session)

### Blocker-class

1. **Conceptual bug in `/api/machines/{machine}/terminals`.** The endpoint
   probes the TARGET machine, but `launch_terminal()` always runs on the
   DAEMON HOST. For remote sessions the dropdown advertises the wrong
   terminals. The picker should default to probing the origin (daemon host)
   when the launch happens there, and only use the target-machine set when
   the intent is `launch_remote_terminal` (terminal opened on the remote's
   display). Fix: add a second endpoint `/api/terminals/local` OR have the
   launch handler resolve which side to probe based on mode.
2. **UI never rendered in a browser.** TerminalPicker, split-button CSS,
   dropdown open/close, pick-then-launch — none of this was observed in
   Chrome/Safari/Firefox. Manual UAT required.

### Deep-test matrix (nothing here is covered yet)

| Path | Status |
|---|---|
| darwin × iTerm2 × terminal mode (local) | manual curl only |
| darwin × Terminal.app × terminal mode (local) | manual curl only |
| darwin × iTerm2 × tmux mode (local) | manual curl only |
| linux × gnome-terminal × any mode (local) | NEVER launched |
| linux × gnome-terminal × tmux-remote (origin mac) | NEVER launched |
| win32 × Windows Terminal × any mode | NEVER launched |
| win32 × PowerShell × psmux | NEVER launched |
| Git Bash / mintty path | NEVER launched |
| asyncssh pool reconnect after server restart | NEVER observed |
| asyncssh pool behavior when target drops mid-run | NEVER observed |
| terminal_id pointing at uninstalled adapter → silent fallback | NEVER exercised |
| pinned project whose project_id is no longer in /api/projects | NEVER exercised |
| 5-min terminal cache expiry | NEVER exercised |
| PinnedSection collapse state persistence across reload | NEVER exercised |

### Smaller follow-ups

- README + docs/architecture.md do NOT mention the terminal adapter layer,
  the asyncssh pool, pin-per-project, or the new endpoints. Update both.
- `docs/api.md` lists session-level pin endpoints but not
  `/api/projects/{pin,unpin}` or `/api/machines/{machine}/terminals`. Update.
- Launcher's per-callsite title prefixing is verbose (six sites, repeated
  sys.platform check). Extract a helper `resolve_local_os()` and a single
  `title_for_context(origin, dest, mux, project)` that callers reuse.
- `launch_remote_terminal` bypasses the adapter registry entirely — still
  does its own AppleScript/bash/PowerShell branching. Port it to adapters.

## Gotchyas learned this session

1. **Daemon launched from GUI (pywebview tray, LaunchAgent, Task Scheduler)
   inherits a stripped env** — on macOS typically
   `PATH=/usr/bin:/bin:/usr/sbin:/sbin`. Every subprocess call that relies on
   Homebrew/snap/WindowsApps/Git-Bash silently breaks. Symptom here:
   `[Errno 2] No such file or directory: 'tmux'`. Fix: hardcoded per-OS
   PATH fallbacks injected into LocalExecutor's `env=` kwarg. (commit
   `446c981`). Verify with `env -i PATH=/usr/bin:/bin python3 -m src.main`
   before shipping daemon-path changes.
2. **Windows OpenSSH ignores ControlMaster.** The Unix trick of sharing one
   master socket is a no-op on Windows — every `ssh host <cmd>` spawns a
   fresh sshd-session on the Windows side. A scan tick that runs
   `tmux ls`/`psmux ls` against four machines every 30 s burns hundreds of
   sshd processes per hour on Windows targets. Fix: asyncssh pool with one
   persistent connection per machine for the app lifecycle; subprocess ssh
   only for terminal-facing launches (where the subprocess is the user's
   window).
3. **`project_id` ≠ `project_folder`.** `/api/projects` returns
   `project_id: "github.com/<user>/<repo>"` (the git remote URL),
   while `/api/sessions[*].project_folder` is
   `"-Users-rbgnr-git-<repo>"` (the claude session directory name). Pinning
   keyed on `project_id` can't be filtered client-side against sessions;
   PinnedSection has to fetch `/api/projects` and cross-reference. Easy
   foot-gun.
4. **command-ranker PreToolUse hook scope leak.** The hook fired ~3× during
   this session (at commit time, at session-clear, at the search for
   "command-matcher") and each firing returned patterns from OTHER projects
   — polymarket-bot, streams-android, crypto/tor/weak-sweeper. Either the
   hook isn't scoping by `viewer_machine`+`target_machine`+`render_context`
   the way its README promises, or it's matching on a too-broad intent
   string. Needs investigation in the command-ranker repo, not here.
   `/Users/rbgnr/git/command-ranker/data/patterns.db` has 1435 rows;
   `hooks/ranker-suggest.py` is the culprit.
5. **Daemon does NOT hot-reload.** After editing server.py you MUST kill +
   relaunch. I wasted several minutes hitting new endpoints and getting
   `404 Not Found` before remembering this.
6. **Editing a 5000-line index.html file with uncommitted user WIP is
   dangerous.** Each new edit layers onto WIP that predates the session.
   Commit-bundle WIP alongside the first logical delta, don't stack.

## Documents to update (next session)

- `README.md` — add "Pin-per-project" and "Terminal picker" feature bullets.
- `CHANGELOG.md` — entries for 4 commits above.
- `docs/architecture.md` — new section "Terminal adapter layer (src/terminals)";
  diagram the registry + probe/launch flow; note the asyncssh pool on the
  SSH side.
- `docs/api.md` — document `/api/projects/pin`, `/api/projects/unpin`,
  `/api/machines/{machine}/terminals`, and the new `terminal_id` field in
  `/api/sessions/launch`.
- `docs/development.md` — gotchya #1 (stripped PATH) + how to simulate via
  `env -i`.

## command-ranker status

**Working** (as a data store): 1435 patterns, gotchas.jsonl actively
appended, ranker-suggest.py hook fires on PreToolUse.
**Broken** (as a targeted recommender in this project's context): returned
3 unrelated suggestions during this session. Scope filtering appears to be
too permissive or the intent-embedding is matching across unrelated tokens.
The user confirmed command-ranker is dev-time only (task #7) — no runtime
integration from claude-manager. Treat its runtime output as advisory only.
