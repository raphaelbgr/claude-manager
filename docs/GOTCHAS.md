# Gotchas & Hard-Earned Knowledge

Compiled from a long debugging arc 2026-04 → 2026-05. Read this before changing
anything in `src/launcher.py`, `src/command_adapter.py`, `src/terminals/`,
`src/scanner.py`, or fleet-watchdog's tray. Every entry below is a real bug we
shipped and then fixed; the failure shape is the why.

## 1. POSIX `'\''` quote escape does not survive a PowerShell parse

`shlex.quote("abc'def")` returns `'abc'"'"'def'`. Bash tokenises that
correctly. PowerShell does not — PS only knows `''` (double the inner '). When
the local terminal host (wt + pwsh, classic powershell, pwsh 7) re-parses an
SSH command built with `shlex.quote`, any embedded `'` causes the parser to
split at the wrong point. Trailing parts after a `;` then run as separate
LOCAL statements.

Symptom shipped: clicking "tmux attach" on a Windows-target session opened a
pwsh window that errored with
`psmux: sessions should be nested with care, unset PSMUX_SESSION to force`
— because the trailing `psmux attach -t NAME` was being executed locally
inside the already-psmux'd shell instead of being sent over SSH.

**Rule:** when constructing a command that will be run by a local PowerShell
host, do not use POSIX `shlex.quote` on any segment that contains inner `'`.
Use `CommandAdapter._ps_double_quote(s)` — wraps in `"..."` and backtick-
escapes `` ` ``, `$`, `"`. Single quotes inside pass through literally
(unlike bash, PS double-quoted strings treat `'` as a literal).

**Coverage:** `tests/test_command_adapter.py::TestBuildSessionCommandSsh`,
`TestBuildPaneCommand`, `TestMuxSendKeysPowerShell`.

## 2. `wt.exe` parses `;` as a tab separator INSIDE quoted arguments

Even when the command is inside `-Command "..."`, Windows Terminal tokenises
`;` at the top of its parser and creates additional tabs in the default
profile (typically cmd.exe). One SSH command with `;` in it became one pwsh
tab + 2-3 stray cmd tabs containing fragments.

**Rule:** the WT adapter (`src/terminals/windows.py`) must use
`-EncodedCommand <base64-UTF-16-LE>` to pass the command. Base64 contains no
`;` so wt sees a single opaque blob and routes everything to one pwsh tab.
Same fix applies to `_launch_windows` legacy fallback.

**Coverage:** `tests/test_server_perf.py::TestWindowsTerminalAdapterEncodedCommand`
(asserts `-EncodedCommand` is used and the decoded payload survives intact).

## 3. PowerShell `Set-Location '...'` survives the SSH wrap; `Set-Location "..."` survives the SSH+PS wrap

When `build_session_command_ssh` emits the remote PS command, the caller wraps
the whole thing with `shlex.quote(...)` for the local SSH-wrapping shell.
Single quotes inside the PS command create the gotcha #1 chain. Switching
the PS quoting style to DOUBLE quotes eliminates the inner-single-quote
chain entirely:

```
'Set-Location "C:\path"; claude'      ← PS-double inside POSIX-single
```

Both bash and PowerShell parse this as a single string. Windows paths
contain no `$` or backticks so backtick-escaping is rarely needed (but
`_ps_double_quote` does it just in case).

## 4. claude-manager scan perf — the 4-layer cache

Cold scan was 50-60s; warm scan is now ~3s. The chain of caches:

1. **In-memory session cache** (`scan_local._session_cache`) — keyed by
   absolute path. Value: `(ClaudeSession, last_size, last_mtime_ns)`. Three
   outcomes per file on rescan:
   - identical bytes (mtime + size match) → return cached `sess`
   - file grew (size > last_size) → **incremental parse** (gotcha #5)
   - file shrunk → cold reparse (assume rotation)

2. **Incremental parser** — `parse_session(prev=breadcrumbs)` seeks to
   `prev["last_size"]` and reads ONLY the new tail bytes, adding tokens
   to the running total. Crucial for the live conversation's JSONL
   (200+ MB) where every assistant message invalidates the mtime but
   only appends a few KB.

3. **Disk-persisted cache** (`~/.claude-manager/scan-cache.json` v2,
   `~/.claude-manager/git-cache.json` v1). Loaded once on first scan,
   saved at end of every scan. Atomic temp+rename writes. A fresh
   process restart starts already-warm.

4. **Per-cwd git cache** (`scan_local._git_cache`) — keyed by
   `(cwd, .git/mtime_ns)`. Skips the 5 subprocess.run git calls per
   cwd when `.git` hasn't been touched. Hit rate stabilises around 50%
   on active dev machines because `.git/index` and `.git/FETCH_HEAD`
   mtimes advance on commit/fetch.

**Coverage:** `tests/test_scan_cache.py` exercises each layer.

## 5. Threading parse_session is the WRONG optimisation

Tried it. The per-line `json.loads` + dict iteration is GIL-bound; an
8-worker ThreadPoolExecutor regressed parse time from 22s to 97s. The
right answer is caching + incremental parsing (gotcha #4), not concurrency.

## 6. WebSocket broadcast flood

`scan_local` calls `on_progress(machine, found, total, file)` per file. The
server wraps this in `_emit_scan_progress` which JSON-serialises and pushes
to every WS client. 399 files = 300+ broadcasts in <10s — the broadcasts
themselves dominated wall time more than the actual scan.

**Rule:** every per-iteration broadcast must be throttled. Pattern:

```python
_PROGRESS_MIN_INTERVAL_S = 0.05
_last_emit = [0.0]
def emit(...):
    now = time.monotonic()
    is_first = found <= 1
    is_last = total > 0 and found >= total
    if not is_first and not is_last and (now - _last_emit[0]) < _PROGRESS_MIN_INTERVAL_S:
        return
    _last_emit[0] = now
    # ... actual broadcast
```

First and last calls ALWAYS fire. Intermediate calls are coalesced into 50ms
buckets (~20 emits/sec ceiling).

**Coverage:** `tests/test_server_perf.py::TestScanProgressThrottle`.

## 7. `handle_restart` must NOT clear UI state

Pre-fix: cancel bg_task, `await bg`, then `update_sessions([])`,
`update_fleet({})`, `update_tmux([])` — UI went blank for 30s+ until the
next scan replaced the cleared snapshot. Plus the unbounded `await bg` could
hang on a mid-flight long parse.

**Rule:** keep prior snapshot, bound the cancel wait with
`asyncio.wait_for(asyncio.shield(bg), 0.5)`. Response returns in <600ms;
UI keeps showing the last good data until the next cycle replaces it.

**Coverage:** `tests/test_server_perf.py::TestHandleRestartKeepsState`.

## 8. fleet-watchdog tray UI thread must not do HTTP

pystray invokes the menu callable on its UI thread on every right-click.
Anything synchronous in there freezes the menu open. Two offenders:

- `_build_menu_items` was doing `run_sync(api_client.list_apps_and_catalog())`
- Action callbacks (Launch / Restart / Update) were doing `run_sync(...)` —
  Launch holds for 5s+ during the spawn grace window.

**Rule:** the tooltip poll thread (already running every POLL_INTERVAL_S)
prefetches the apps+catalog bundle into module-level `_MENU_CACHE`.
`_build_menu_items` reads the cache, no HTTP. Action callbacks dispatch
the actual RPC to a daemon thread (`threading.Thread(target=_worker, daemon=True)`)
so the click returns immediately and the result notification fires when the
worker completes.

## 9. `psutil.cpu_percent(interval=0.1)` per-process sleeps

Each call sleeps 0.1s individually. With N=15-30 active claude sessions
that summed to ~3s on every scan cycle. Switch to the batched-prime
pattern: prime every Process up front (returns 0.0 baseline, no sleep),
sleep ONCE for 100ms, then read every Process's delta. Total cost drops
from N×100ms to a single 100ms.

**Coverage:** `tests/test_scan_cache.py::TestMarkActiveSessionsBatched`
(asserts exactly one sleep regardless of N).

## 10. `os.getuid()` doesn't exist on Windows

`AttributeError` whenever code paths assuming POSIX hit a Windows runtime
(tests forcing the subprocess SSH fallback path surfaced this). Guard with
`hasattr(os, 'getuid')` and use a stable per-user hash of `$USERNAME` as
the Windows fallback for socket-path uniqueness.

## 11. `tl.event(name=...)` collides with `event()`'s positional parameter

The agent-written instrumentation in `tmux_manager.py` emitted
`tl.event("cm.mux.kill", name=session_name, ...)`. Python raised
`TypeError: event() got multiple values for argument 'name'` because
`event(name: str, **data)` reserves `name` as the first positional. Use
`session=`, `mux_name=`, or any other key. **General rule:** when adding
data kwargs to `tl.event` or `tl.track`, don't reuse the function's own
parameter names (`name`, `point`).

## 12. fleet-watchdog API auth from a script

`POST /apps/<name>/launch`, `/restart`, etc. require RSA-PSS signed
requests. From a script:

```python
sys.path.insert(0, r'C:\Users\rbgnr\.fastsoftware-apps\fleet-watchdog')
from fleet_watchdog.auth import load_private_key, sign_request

headers = sign_request(load_private_key(), "POST", "/apps/claude-manager/launch", b"")
# attach headers to the request
```

Localhost still requires the signature (no Bearer bypass).

## 13. `tmux capture-pane` hot loop — don't trace inside it

`server.py:_pane_poll_loop` calls `tmux_manager.capture_pane` every 1.5-2.5s
per active pane. Adding tracelink emits there would explode the JSONL by
orders of magnitude. Top-level scan/launch operations are the right
instrumentation granularity; per-poll-tick reads are noise.

## 14. pytest `tmp_path` lives under `%TEMP%` — `_is_tmp_path` filters it

Sessions whose `cwd` resolves under the OS temp dir are intentionally
dropped from the scan (they clutter the Project tab and can never be
resumed). pytest's `tmp_path` fixture lives under `%TEMP%` on Windows,
so tests writing JSONLs there must inject a non-temp `cwd` via an
override field, or the scan will return zero sessions and the test
will look broken.

## 15. mac-mini worktree-agent branches are mostly re-implemented in master

A handful of `worktree-agent-*` branches existed on mac-mini's local
checkout pre-2026-05-20. Most were independently re-implemented in
master under different SHAs (project_identity.py, the cards-links-row UI,
the cross-machine grouping). The remaining one with truly unique work
is `worktree-agent-ad6e4068` (rename_registry + terminal-scripts catalog
refactor); it conflicts with the current EncodedCommand fix and was
intentionally deferred. Fetched as `mac-mini/<branch>` remote refs for
manual inspection.
