# Next-session prompt — paste this verbatim

```
Read docs/SESSION-REPORT-2026-04-14.md first — it has the full status from
the prior session. Your job is to close out two blockers and then drive the
whole thing through a real test matrix before we ship.

Primary objectives, in order:

1) Fix the conceptual bug in the terminal picker. `/api/machines/{machine}/terminals`
   today probes the TARGET machine, but `launch_terminal()` runs on the
   DAEMON HOST. For any remote session the dropdown advertises the wrong
   set. Two clean ways to fix it — pick one and justify:
     a) Add `/api/terminals/local` that probes the daemon host and have the
        Resume/tmux dropdowns call that endpoint (target-machine probes stay
        available for a future `launch_remote_terminal` picker).
     b) Teach `handle_machine_terminals` to return the daemon-host set by
        default, with an explicit `?remote=1` query flag to opt into the
        target-machine probe.
   Write it up as a two-paragraph decision in docs/architecture.md before
   touching code. Then implement and delete the wrong-axis code paths.

2) Render the UI in a real browser end-to-end. Start the daemon with
     env -i PATH=/usr/bin:/bin:/usr/sbin:/sbin HOME=$HOME USER=$USER \
       .venv/bin/python3 -m src.main --enable-web --bind 127.0.0.1
   (that stripped-env launch is how we catch the PATH-fallback class of
   bug — do not run it with your interactive shell's PATH). Open Chrome,
   load http://127.0.0.1:44740/, and manually walk the Resume/tmux split
   buttons + dropdown on:
     - a LOCAL mac-mini session
     - a REMOTE ubuntu-desktop session
     - a REMOTE windows session IF avell/windows-desktop is online this time
   For each: pick every terminal in the dropdown, confirm the correct
   terminal actually opens on your screen, confirm the window title is
   "origin → [dest →] [mux →] project", and confirm the daemon log line
   `launch_terminal: terminal_id=X` matches your pick.

3) Run the existing test suite + add coverage. `pytest -q` must stay green.
   Add tests for:
     - `src/terminals/__init__.py::list_available` with a fake runner that
       returns rc=0 for 2 adapters and rc=1 for the rest → verifies
       priority-desc order.
     - `src/terminals/{darwin,linux,windows}.py` — one probe_shell string
       test per adapter class.
     - `src/ssh_pool.py::_MachineConn` backoff: induce 2 consecutive connect
       failures, assert _fail_backoff doubles 1→2→4 and get() raises
       ConnectionError during the backoff window.
     - `src/launcher.py::build_window_title` — all 8 combinations of
       origin/dest/mux/project presence.
     - `src/launcher.py::title_prefix_for` — win32 branch returns the
       PowerShell form, darwin/linux return ANSI OSC 0, unknown os returns
       "".
   After green: run the live probe matrix from the SESSION-REPORT's
   "Deep-test matrix" table and fill in every row. Keep a file
   `docs/TEST-MATRIX.md` with pass/fail notes.

4) Documentation pass:
     - README.md: add a "Pin projects" section and a "Terminal picker"
       section with a short screenshot/gif reference.
     - CHANGELOG.md: bullet entries for commits 446c981, 2170e57, f00513d,
       f85154d, a46fd68.
     - docs/architecture.md: the terminal-adapter diagram + asyncssh pool
       note.
     - docs/api.md: `/api/projects/{pin,unpin}`,
       `/api/machines/{machine}/terminals`, and the `terminal_id` field in
       `/api/sessions/launch`. Include a curl example for each.
     - docs/development.md: the "daemon launched from GUI has stripped
       PATH" gotchya with the env-i reproduction recipe.

5) Push master and open a PR that marshals the whole delta since
   `f00513d^` for review. Use `gh pr create` with a body that cross-links
   the session report.

Non-negotiable rules for this session:
- Do NOT ship code paths you have not actually executed. "Reading looks
  right" is not verification. Every claim of "done" needs a run block with
  command + observed output.
- If command-ranker's PreToolUse hook leaks unrelated project suggestions
  again (we saw polymarket-bot / streams-android / crypto-sweeper leaks
  last session), file a gotcha in /Users/rbgnr/git/command-ranker/data/
  gotchas.jsonl via the CLI — don't silently ignore.
- Commit atomically per objective (1 per numbered bullet above). Don't
  bundle objectives 1 and 2 into one commit.
- When editing src/web/index.html, check git status first — the file has
  historically carried user WIP that must not get bundled into unrelated
  commits.
```
