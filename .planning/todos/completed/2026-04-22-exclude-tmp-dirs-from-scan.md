---
created: 2026-04-22T12:20:00-03:00
title: Exclude /tmp paths from session scanner on all OSes
area: scanner
files:
  - src/scanner.py (scan_local, REMOTE_SCAN_SCRIPT)
---

## Problem

User wants session scanning to skip any Claude session whose `cwd` points inside an OS temp directory — `/tmp/**` on macOS/Linux, `%TEMP%` / `%TMP%` / `C:\Windows\Temp\**` / `C:\Users\*\AppData\Local\Temp\**` on Windows. Temp-path sessions clutter the Project tab with short-lived/throwaway work that's meaningless cross-session and can never be resumed usefully.

Applies to BOTH the local scan path (`scan_local`) and the REMOTE_SCAN_SCRIPT that runs on avell-i7 / ubuntu-desktop via SSH — so filtering must be duplicated in both places (the remote script ships as a raw string and can't `import subprocess_utils`).

## Solution

Add a helper `is_tmp_path(cwd: str) -> bool` that checks:
- Unix: cwd starts with `/tmp/`, `/var/tmp/`, `/private/tmp/`, or `/private/var/folders/` (macOS TemporaryItems)
- Windows: cwd starts with `C:\Windows\Temp\`, matches `C:\Users\*\AppData\Local\Temp\`, or matches `%TEMP%` / `%TMP%` env-resolved paths (case-insensitive)

Apply the filter in `scan_local` and inline the same check in REMOTE_SCAN_SCRIPT (self-contained — stdlib only, platform-detected via `sys.platform`). Filter at the `results.append(...)` site so we never enrich git/readme metadata for sessions we're about to drop anyway.

Acceptance: any session whose cwd resolves to an OS temp dir is excluded from `/api/sessions` responses. Unit test: scan a fixture with a temp-cwd session and a normal-cwd session; assert only the normal one appears. Verify on live fleet that temp-path count drops to zero in the UI.
