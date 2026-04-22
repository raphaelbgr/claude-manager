---
created: 2026-04-22T12:40:00-03:00
title: fleet-watchdog auto-pull must fall back to git ls-remote when VERSION.json is unchanged
area: fleet-watchdog
files:
  - ~/git/fleet-watchdog/fleet_watchdog/apps_api.py:1300 (_auto_update_tick)
  - ~/git/fleet-watchdog/fleet_watchdog/apps_api.py:706 (_run_update)
  - ~/git/fleet-watchdog/tests/test_apps_api.py
---

## Problem

`_auto_update_tick` (`apps_api.py:1300-1340`) is the sole gate for fleet-wide auto-update. It compares `remote.version` (fetched from raw.githubusercontent.com/<repo>/<branch>/VERSION.json) against `installed.version` (from `_read_git_version(install_dir)`). If `remote.version <= installed.version`, it skips — silently.

Two failure modes this creates:

1. **Stale VERSION.json on origin:** if contributors push code without regenerating VERSION.json, origin's manifest says v151 forever. Installed watchdogs at v151 see "no update" and never pull the new commits. Happened 2026-04-22 — claude-manager VERSION.json was stale at v151/b4d3a68 (Apr 16) while actual HEAD was at v155. Ubuntu-desktop's fleet-watchdog didn't pull today's conhost-popup + SSH-pool fixes until the user manually intervened. Mac-mini pulled only because something bumped its install VERSION.json out of band.

2. **Manifest fetch failure:** `_fetch_json_cached(_remote_version_url(...))` can fail (network, rate limit, CDN cache miss). Log on ubuntu 12:10:04 showed `Remote fetch .../personal-cloud/main/VERSION.json failed` — the tick silently `continue`s without any backup signal. App stays at stale version forever until the raw.githubusercontent URL happens to succeed AND someone has meanwhile bumped VERSION.json.

The upstream claude-manager side added `tests/test_version_manifest.py` to catch case 1 in CI. That closes the hole for this one repo but the underlying fleet-watchdog design still fails open on any other app that forgets to bump or any transient manifest outage.

## Solution

In `_auto_update_tick`, add a secondary signal using `git ls-remote` (or `git fetch --dry-run` on the install dir):

```python
# After the VERSION.json comparison, before `continue`:
if installed_version is None or remote_version <= installed_version:
    # Fallback: maybe remote VERSION.json is stale but code has moved.
    # Compare git HEAD of install_dir vs origin/<branch> via ls-remote.
    if install_dir_path.is_dir():
        try:
            local_head = _git("rev-parse", "HEAD", cwd=install_dir_path)
            remote_head = _git("ls-remote", origin_url, f"refs/heads/{branch}")
            remote_sha = remote_head.split()[0] if remote_head else None
            if remote_sha and remote_sha != local_head:
                log.info("auto-update (ls-remote fallback): %s %s -> %s",
                         name, local_head[:7], remote_sha[:7])
                self._inflight_updates.add(name)
                asyncio.create_task(self._run_update_tracked(name, app))
                continue
        except Exception as exc:
            log.debug("ls-remote fallback failed for %s: %s", name, exc)
    continue
```

Also cache `ls-remote` for ~60s to avoid hammering GitHub.

## Tests to add in fleet-watchdog/tests/test_apps_api.py

Edge cases for `_auto_update_tick`:

1. **happy path** — remote VERSION > installed → queues `_run_update`.
2. **no update needed** — remote VERSION == installed → skips.
3. **auto_update=false** — never queues, regardless of diff.
4. **manifest fetch returns None** — falls back to git ls-remote; if remote SHA != local, queues update.
5. **manifest fetch returns dict without 'version' key** — same fallback.
6. **manifest version field non-int** — same fallback.
7. **manifest version ≤ installed BUT ls-remote shows new commits** — queues update (case that broke 2026-04-22).
8. **install_dir missing** — logs + skips, does not crash.
9. **install_dir exists but not a git repo** — logs + skips.
10. **git ls-remote times out** — logs + skips, does not crash the whole tick loop.
11. **watchdog self-update path** — recognises install_dir == self_repo_dir and defers to supervisor.
12. **inflight dedup** — if an update is already running for an app, second tick does not double-queue.
13. **registry reload mid-tick** — tick iterates over a snapshot.

Integration test using tmp git repo + local "origin" bare repo:

14. Create bare repo, clone to install_dir, add commits on origin, run tick, assert install_dir now at origin's HEAD — even if VERSION.json in the tmp repo was never regenerated.

## Acceptance

- All 14 unit tests pass against `_auto_update_tick`.
- Live smoke on ubuntu-desktop: set origin VERSION.json to match an outdated installed version, advance origin HEAD via a no-VERSION.json commit, wait one tick (≤300s), confirm install_dir advances to origin HEAD.
- No regressions on the 13 existing tests in `test_apps_api.py`.
