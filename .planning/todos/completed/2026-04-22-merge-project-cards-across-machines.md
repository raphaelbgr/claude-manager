---
created: 2026-04-22T12:20:00-03:00
title: Merge project cards across machines under same project_id
area: scanner
files:
  - src/project_identity.py
  - src/scanner.py:437-615 (REMOTE_SCAN_SCRIPT)
  - src/scanner.py:572-614 (git_remote + git_commits block)
  - src/web/index.html (Project tab grouping)
---

## Problem

On the Project tab in claude-manager UI, the same repo shows as SEPARATE entries when sessions for it exist on multiple machines. Concrete case: `streams-android` appears twice — one row for mac-mini (20 sessions) and a second row for avell-i7 (17 sessions) — instead of one merged card with a per-machine breakdown.

The canonical-id logic in `src/project_identity.py` is supposed to group by normalized git remote URL so cross-OS path differences don't matter. It's not firing because the REMOTE_SCAN_SCRIPT shipped to avell-i7 returns `git_remote: ""` for every session there. Without a remote URL, the grouping falls back to the raw local path, and `C:\Users\rbgnr\git\streams-android` on Windows never matches `/Users/rbgnr/AndroidStudioProjects/streams-android` on macOS, so they render as two cards.

Root-cause hypothesis for the empty remote: the REMOTE_SCAN_SCRIPT runs under sshd on Windows which inherits a stripped PATH — `git config --get remote.origin.url` may not resolve `git.exe` without the full path, silently failing and returning empty string from the `except Exception: _remote_cache[cwd_key] = ''` branch.

Screenshot evidence: user attached UI screenshot 2026-04-22 showing the duplicate streams-android rows.

## Solution

Two complementary fixes:

1. **Make git_remote reliable on Windows remotes.** In REMOTE_SCAN_SCRIPT, resolve full git path before the subprocess.run calls: on Windows, try `shutil.which('git')` falling back to `C:\Program Files\Git\bin\git.exe`. Pass the resolved path as argv[0]. Alternatively, prepend `C:\Program Files\Git\bin;C:\Program Files\Git\cmd` to os.environ['PATH'] at the top of the script on Windows.

2. **Secondary fallback in project_identity.canonical_id():** when git_remote is empty on BOTH sides of a potential merge, fall back to a (last-path-segment + current-git-branch) signature. So `.../streams-android` on `main` on mac-mini and `...\streams-android` on `main` on avell-i7 collapse to one project.

Acceptance: Project tab shows exactly one `streams-android` card, with "20 sessions · mac-mini · 17 sessions · avell-i7" (or similar per-machine summary) — never two separate top-level cards for the same repo.
