# Handoff: Fleet-Watchdog Deployment Across mac-mini + ubuntu-desktop + avell-i5

**Session origin:** claude-manager session `macmini/claude-manager` on 2026-04-15/16. Context exhausted — starting fresh here.

## The goal (stated by user)

> "the watchdog must self update and update all registered apps"

fleet-watchdog's auto-update loop should Just Work across the whole fleet, for all 3 registered apps (`claude-dispatch-daemon`, `claude-manager`, `personal-cloud`), on all 4 machines (avell-i7, mac-mini, ubuntu-desktop, avell-i5).

## What's already DONE — do NOT redo

### Fixed and pushed this session

- **claude-manager** (3 commits, master pushed to `github.com:raphaelbgr/claude-manager`):
  - `b64e222` SSH pool for ad-hoc API calls, new `/api/logs/tail` + `/api/update/watchdog` endpoints, sshd-leak fix.
  - `7fcb3db` `sendBeacon` for the Exit button so pywebview doesn't drop the shutdown POST.
  - `b3eb59c` pathlib scope fix inside `create_app`.
- **fleet-watchdog** (1 commit, master pushed to `github.com:raphaelbgr/fleet-watchdog`):
  - `b08b7a7` `_default_branch()` resolves `origin/HEAD` in `supervisor.py::git_fetch/git_pull_ff` — replaces the hardcoded `"main"` that caused `fatal: couldn't find remote ref main` for repos tracking `master`.
- **claude-kb-vault** (1 commit, master pushed):
  - `b39eb42` global memory M69 "stale ControlSocket gotcha" + full postmortem file.

### Live fixes on avell-i7 (no commit needed)

- Deleted stale `~/.ssh/cm-git@github.com:22` socket.
- Appended `Host github.com` override to `~/.ssh/config` with `ControlMaster no` + `ControlPath none`.
- `~/.fastsoftware-apps/fleet-watchdog` pulled to `c1ffdc5` (includes the default-branch fix).
- Restarted `FleetWatchdogSupervisor` + `FleetWatchdogTray` via Task Scheduler — supervisor no longer in restart loop.
- Deleted the dead `claude-dispatch-watchdog` scheduled task (was Disabled, pointed at a non-existent `updater.py`).
- Disabled `PersonalCloud` and `npcapwatchdog` scheduled tasks — "only watchdog owns lifecycle" policy.
- Removed both `cuda-crack-sim` and `cuda_packet_crack` entries from `~/git/claude-dispatch/managed_projects.json` per user ("no cuda crack for now on the watchdog"). Backup saved next to it with timestamp suffix.
- Killed orphan `personal_cloud.daemon` (PID 7204) and `daemon.py` (PID 17336) — **these are currently dead on avell-i7** until something restarts them, which nothing will until this handoff work lands.

## The architecture gap (the real work)

fleet-watchdog has **two separate registries** that don't agree with reality:

| Registry | Path | Consumer | Schema | Does what |
|---|---|---|---|---|
| **Legacy** | `~/git/claude-dispatch/managed_projects.json` (gitignored) | `supervisor.py` → `managed.py` | `{name, repo_dir, module, pid_file, health_url, extra_start_args, linux_start_args, leak_limits?}` | Start/lifecycle/leak-pause |
| **App-level** | `~/.config/fleet-watchdog/registry.json` | `apps_api.py::_auto_update_loop` (300s tick) | `{name, repo, branch, install_dir, manifest_url, auto_update, scope}` | Git pull + update against `install_dir` |

`registry.json` is **identical on all 3 reachable machines** and already registers all 3 apps with `auto_update: true`. The registered `install_dir` values are all `~/.fastsoftware-apps/<name>/`.

**But on every machine, the daemons are running from `~/git/<name>/` (dev checkouts), not from `~/.fastsoftware-apps/<name>/`.** Consequently:

- Auto-update tick reads `_read_git_version(install_dir)` → `None` (path doesn't exist) → `installed_version is None` → `continue` (skips).
- `apps_api.py` does not bootstrap-install — it only updates installed apps. So the auto-update loop has literally nothing to do on any machine.
- `~/.fastsoftware-apps/` doesn't even exist on mac-mini or ubuntu-desktop. On avell-i7 only `fleet-watchdog` itself is there.

## Current running state per machine

### avell-i7 (192.168.7.103)
- fleet-watchdog: **daemon + tray running** via Task Scheduler (`FleetWatchdogSupervisor`, `FleetWatchdogTray`). Installed at `~/.fastsoftware-apps/fleet-watchdog`. HTTP on `127.0.0.1:44732` (auth required).
- `claude-dispatch daemon.py`: **DEAD** (killed by this session).
- `personal_cloud.daemon`: **DEAD** (killed by this session).
- Supervisor log (`%LOCALAPPDATA%\fleet-watchdog\updater.log`) shows clean `"No new commits"` ticks every 2 min since 20:20:08 on 2026-04-15.

### mac-mini (192.168.7.131 / 102)
- fleet-watchdog: **only tray** running (PID 92926 at `~/git/fleet-watchdog/.venv/bin/python -m fleet_watchdog --tray`). No daemon. `~/.fastsoftware-apps/fleet-watchdog` does NOT exist.
- `claude-dispatch daemon.py`: running (PID 5254) from `~/git/claude-dispatch/.venv`, managed by launchd job `com.rbgnr.claude-dispatch`. Port 44730.
- `personal_cloud.daemon`: running (PID 675) from `/opt/homebrew/Cellar/python@3.13/...` (not a venv!), management unclear — probably a launchd agent. Not at `~/.fastsoftware-apps/personal-cloud`.
- Legacy launchd agent `com.rbgnr.claude-dispatch-watchdog` (PID 673) still active — superseded by fleet-watchdog, needs disabling.
- `managed_projects.json` here only has `cuda-crack-sim` + `cuda_packet_crack` entries (user wants these GONE from any machine).

### ubuntu-desktop (192.168.7.13)
- fleet-watchdog: **NOT running at all**. No daemon, no tray.
- `claude-dispatch daemon.py`: **NOT running**.
- `personal_cloud.daemon`: **NOT running**.
- Only unrelated process: an old `mcp_server.py` from `~/claude-dispatch-install/` (stale path, ignore).
- `managed_projects.json` has cuda entries.

### avell-i5 (192.168.7.134)
- **Unreachable** (SSH timeout 2026-04-15 ~17:50). Was probably offline/asleep. Retry at start of next session.

## What this next session needs to do

Think of it as three tracks that compose:

### Track A — install fleet-watchdog daemon everywhere

avell-i7 is the reference install. Mirror it on mac-mini, ubuntu-desktop, avell-i5:

1. Clone `github.com:raphaelbgr/fleet-watchdog` to `~/.fastsoftware-apps/fleet-watchdog/` on each machine.
2. Create venv + `pip install -e .` (or whatever its `pyproject.toml` installs).
3. Wire up auto-start per OS:
   - macOS: launchd plist in `~/Library/LaunchAgents/com.fastsoftware.fleet-watchdog-supervisor.plist` and `…-tray.plist`.
   - Linux: systemd user unit or a dedicated systemd service (ubuntu-desktop has a graphical session, so a `systemctl --user` unit is fine).
   - Windows (avell-i5): Task Scheduler entries `FleetWatchdogSupervisor` + `FleetWatchdogTray`, same pattern as avell-i7.
4. Verify HTTP `127.0.0.1:44732/health` responds on each.

### Track B — bootstrap-install each registered app

For each app in `registry.json` (`claude-dispatch-daemon`, `claude-manager`, `personal-cloud`) on each machine:

1. Clone `github.com/raphaelbgr/<name>` to `~/.fastsoftware-apps/<name>/` at the registered branch.
2. Create venv, install, create VERSION.json if missing (use `scripts/gen_version.py` pattern from claude-dispatch).
3. Start the daemon from the new install path.
4. Migrate any existing OS-level autostart job (launchd / Task Scheduler / systemd) to point at the new path, OR disable it and let fleet-watchdog own startup.

**Open design question to resolve first:** should fleet-watchdog itself bootstrap missing installs (add logic to `apps_api.py` so a `None` `installed_version` triggers initial clone instead of skipping), or should the bootstrap be a separate one-shot install script per machine? The first is cleaner long-term; the second unblocks everything today.

### Track C — clean up legacy lifecycle mechanisms

Per-machine residue to kill once Track B is stable:

- **mac-mini**: disable launchd agents `com.rbgnr.claude-dispatch`, `com.rbgnr.claude-dispatch-watchdog`, whatever started PID 675 (`personal_cloud.daemon` from homebrew python) — find with `launchctl print pid/675` and `launchctl list | grep -i personal`.
- **ubuntu-desktop**: nothing running now, but check `~/claude-dispatch-install/` — that old checkout has its own autostart? Delete the dir if stale.
- **avell-i7**: already done. Just verify.
- **avell-i5**: TBD.

## Known traps — waste zero time re-discovering these

1. **Windows OpenSSH ControlMaster is a silent killer.** Any new Windows machine you touch, immediately: delete `~/.ssh/cm-*` sockets and add `Host github.com\n  ControlMaster no\n  ControlPath none` to `~/.ssh/config`. Otherwise git fetch/pull will hang with `mux_client_request_session: read from master failed` which masquerades as `couldn't find remote ref <branch>`. See global memory M69. Set `ControlMaster no` for ALL hosts on Windows if you want to be safe.
2. **Windows OpenSSH server also ignores ControlMaster.** Each subprocess `ssh avell-i7 …` call spawns a fresh sshd on the remote side. The claude-manager daemon now routes through `asyncssh` pool (single persistent channel) — see `src/ssh_pool.py`. Non-daemon callers (shell commands you run from Bash tool) should add `-o ControlMaster=no -o ControlPath=none` or accept the leak.
3. **PowerShell quoting over SSH is hell.** Use base64 `-EncodedCommand` pattern: `python3 -c "import base64,sys; print(base64.b64encode(sys.argv[1].encode('utf-16le')).decode())" "$PS"` then `ssh host "powershell -NoProfile -EncodedCommand $B64"`. Don't try to inline heredocs or escape `"` manually — PS5.1 will break your soul.
4. **`managed_projects.json` is gitignored.** Each machine has its own local copy. Don't try to "sync" it via git.
5. **Don't blind-kill daemons on mac-mini without first disabling their launchd agents** — launchd will relaunch them within seconds. `launchctl disable gui/501/com.rbgnr.claude-dispatch` first, then kill.
6. **cuda_packet_crack's visible console popups** are caused by `multiprocessing.set_start_method('spawn')` + fleet-watchdog spawning it from `python.exe` (not `pythonw.exe`). User's other AI on avell-i7 is addressing this in the `~/git/Immunefi/code/cuda_packet_crack/` repo. Not in scope here, but don't be surprised by it.
7. **User removed cuda entries from avell-i7's `managed_projects.json` and wants them gone from all machines.** Remove cuda from mac-mini's + ubuntu-desktop's copies too if you edit them.
8. **personal-cloud is crash-looping on mac-mini (macOS 26.3.1).** Python 3.13 `NSApplication.run()` called from a non-main thread → `EXC_BREAKPOINT` via `___NSAssertMainEventQueueIsCurrentEventQueue_block_invoke`. Parent chain is zsh→Python (launchd-respawned every ~8s). Fix in `personal-cloud` repo: ensure the tray/GUI event loop (pystray `icon.run()` or pywebview `webview.start()`) runs on the main thread, with async/background work pushed to daemon threads — NOT the other way around. macOS 26 made this assertion hard-fatal. Before Track B bootstrap installs personal-cloud anywhere else, fix this OR the crash propagates fleet-wide.

## Start-of-session checklist

1. Read `~/.claude/projects/C--Users-rbgnr/memory/MEMORY.md` — M37, M48, M64, M65, M66, M67, M68, M69 are directly relevant.
2. Read `~/git/fleet-watchdog/fleet_watchdog/apps_api.py::_auto_update_tick` (line ~908) and `_run_update` (line 591) to understand the exact update semantics.
3. `ssh avell-i7` and verify `~/.fastsoftware-apps/fleet-watchdog` matches the tip of `master` on GitHub.
4. Check avell-i5 reachability: `ssh -o ConnectTimeout=5 avell-i5 hostname`. If still down, defer that machine to a later pass.
5. Decide: bootstrap-in-apps_api.py or one-shot script. Write that decision down before touching anything.

## Do NOT do in the next session

- **Do NOT touch claude-manager** — its current fixes are correct and deployed. The only reason to edit it would be if fleet-watchdog starts pointing its registered `install_dir` (`~/.fastsoftware-apps/claude-manager`) at a checkout, and we want the running daemon (mac-mini PID 93300 on port 44740) to migrate there — that's a Track C cleanup, not an active dev task.
- **Do NOT re-register apps in `registry.json`** — they're already there on all machines.
- **Do NOT try to install fleet-watchdog on raspberry-pi** — per fleet rules it's Tor/relay only.
- **Do NOT touch streams-android, Immunefi/**, or any cuda_* code — employer/out-of-scope.

## Exit criteria for the next session

- `curl 127.0.0.1:44732/apps/status` on every machine returns all 3 apps as INSTALLED, running, and version-current (ignoring auth — either disable auth for that endpoint or pass the token).
- A git push to any of the 3 app repos' tracked branch triggers auto-update on all machines within 300s.
- `systemctl --user status fleet-watchdog` (ubuntu), `launchctl list | grep fleet-watchdog` (mac), `Get-ScheduledTask FleetWatchdog*` (avell-i7/i5) all show healthy.
- avell-i7's `updater.log` shows no error lines in the last hour.
