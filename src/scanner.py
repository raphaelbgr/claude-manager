"""
Claude session scanner for claude-manager.

Ported from air-code/packages/was/src/routes/claude-sessions.ts.

Scans ~/.claude/projects/*/  for JSONL session files, decodes project
folder names to filesystem paths, and extracts lightweight metadata
(slug, cwd, first message, message count, modified time, PID status).

Supports local scan (uses psutil) and remote scan (SSHes a self-contained
Python script that uses only stdlib).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import psutil

from .subprocess_utils import run_with_timeout, _win32_kwargs
from .executor import SSHExecutor
from .tracking import tl

log = logging.getLogger("claude_manager.scanner")


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------

# Case-insensitive prefix markers for OS temp directories — any session whose
# cwd resolves under one of these is a throwaway that pollutes the Project tab.
# Unix covers /tmp, /var/tmp, macOS's /private/tmp and the per-user TemporaryItems
# tree. Windows covers the system temp, per-user Local\Temp, and the %TEMP%/%TMP%
# env-resolved paths. Check on normalized (forward-slash, lowercase) strings.
_TMP_PREFIXES_UNIX = (
    "/tmp/",
    "/var/tmp/",
    "/private/tmp/",
    "/private/var/folders/",
)
_TMP_PREFIXES_WIN = (
    "c:/windows/temp/",
    # Per-user: C:\Users\<name>\AppData\Local\Temp\ — match by substring below.
)


def _is_tmp_path(cwd: str) -> bool:
    """True if `cwd` looks like an OS temp directory on any supported OS.

    Called at scan time to drop sessions whose working directory is a
    throwaway location — short-lived runs, ad-hoc /tmp editing, Windows
    installer scratch dirs. These sessions can never be resumed usefully
    and just clutter the Project tab.
    """
    if not cwd:
        return False
    norm = cwd.replace("\\", "/").lower()
    for p in _TMP_PREFIXES_UNIX:
        if norm.startswith(p):
            return True
    for p in _TMP_PREFIXES_WIN:
        if norm.startswith(p):
            return True
    # Per-user Windows temp — appears anywhere under Local\Temp
    if "/appdata/local/temp/" in norm:
        return True
    # Env-resolved %TEMP% / %TMP% fallback. Only apply on Windows (on Unix,
    # TEMP/TMP may be set to non-temp paths; we've already covered /tmp etc.).
    if sys.platform == "win32":
        for var in ("TEMP", "TMP"):
            env_val = os.environ.get(var, "")
            if env_val:
                env_norm = env_val.replace("\\", "/").lower().rstrip("/") + "/"
                if norm.startswith(env_norm):
                    return True
    return False


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class ClaudeSession:
    session_id: str
    machine: str
    project_folder: str       # raw encoded folder name, e.g. "-Users-rbgnr-git-foo"
    project_path: str         # decoded filesystem path, e.g. "/Users/rbgnr/git/foo"
    cwd: str                  # working directory recorded in the JSONL
    slug: str                 # session slug from JSONL
    summary: str              # first user message, truncated to 120 chars
    messages: int             # total non-empty lines in JSONL
    modified: str             # ISO-8601 mtime
    status: str               # "working" | "active" | "idle"
    pid: int | None           # PID if active/working, else None
    file_size: int = 0        # file size in bytes of the .jsonl file
    tokens: int = 0           # sum of input + output tokens from assistant messages
    name: str = ""            # session name set by /rename
    cpu_percent: float = 0.0  # CPU usage if active (0.0 if idle/not measured)
    git_branch: str = ""      # git branch from JSONL gitBranch field
    subprocess_count: int = 0 # number of child processes (recursive)
    git_remote: str = ""      # raw git remote.origin.url (empty if not a git repo)
    git_commits: int = 0      # total commit count in the repo (rev-list --count HEAD)
    last_user_message: str = ""  # last user prompt in the session (160 char max)
    readme_path: str = ""        # absolute path to README.md (or variant) in cwd; empty if absent
    # Phase B — git freshness vs. upstream. All None when not determinable
    # (not a repo, no upstream configured, or subprocess failed). No git fetch
    # is performed during scans; ahead/behind reflect last-fetched state.
    git_upstream: str | None = None  # e.g. "origin/master"; None if no upstream
    git_ahead: int | None = None     # commits HEAD has that upstream doesn't
    git_behind: int | None = None    # commits upstream has that HEAD doesn't
    git_dirty: bool | None = None    # True if tracked files modified; None if status failed

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Path decoding
# ---------------------------------------------------------------------------

def decode_project_folder(folder: str) -> str:
    """
    Decode a Claude project folder name to an absolute filesystem path.

    Rules (ported 1-to-1 from the TypeScript original):
      - If position 1 is '-' AND position 0 is a letter (a-z / A-Z):
          treat position 0 as a Windows drive letter, emit "<letter>:",
          then start consuming from position 2.
      - Otherwise: consume from position 0.
      - Every '-' in the remaining characters becomes '/' (on non-Windows)
        or '\\' (on Windows).

    Examples:
      "-Users-rbgnr-git-air-code"  → "/Users/rbgnr/git/air-code"   (Unix)
      "C--Users-rbgnr-git-foo"     → "C:/Users/rbgnr/git/foo"       (Windows)
    """
    sep = "\\" if sys.platform == "win32" else "/"
    result = ""
    i = 0

    if len(folder) >= 2 and folder[1] == "-" and folder[0].isalpha():
        result += folder[0] + ":"
        i = 2

    while i < len(folder):
        result += sep if folder[i] == "-" else folder[i]
        i += 1

    return result


# ---------------------------------------------------------------------------
# JSONL session parser
# ---------------------------------------------------------------------------

def parse_session(
    file_path: Path,
    project_path: str,
    folder_name: str,
    machine: str = "local",
    prev: dict | None = None,
) -> ClaudeSession:
    """
    Parse a single JSONL session file, reading at most the first 50 lines
    for metadata extraction.

    If ``prev`` is supplied (from the incremental cache), it must have
    ``last_size`` ≤ current file size. We then seek to ``last_size`` and
    parse ONLY the new tail bytes for token/message-count updates,
    reusing the previously-extracted metadata fields. This turns a
    231 MB re-parse (~50s) into a 50ms tail read when a single message
    has appended to the live session. Setting ``prev=None`` does a full
    parse from byte 0 (cold-cache path).

    Raises OSError / json.JSONDecodeError if the file cannot be read.
    """
    session_id = file_path.stem
    stat = file_path.stat()
    modified = datetime.fromtimestamp(stat.st_mtime).astimezone().isoformat()
    file_size = stat.st_size

    slug = ""
    cwd = ""
    git_branch = ""
    first_message = ""
    last_user_message = ""
    line_count = 0
    tokens = 0
    metadata_found = False

    # Incremental path: file grew (or stayed the same size — same content).
    # Reuse the cached metadata + token total and only read the new tail.
    start_offset = 0
    if prev is not None and prev.get("last_size", -1) <= file_size:
        start_offset = prev["last_size"]
        slug = prev.get("slug", "") or slug
        cwd = prev.get("cwd", "") or cwd
        git_branch = prev.get("git_branch", "") or git_branch
        first_message = prev.get("first_message", "") or first_message
        last_user_message = prev.get("last_user_message", "") or last_user_message
        line_count = prev.get("line_count", 0)
        tokens = prev.get("tokens", 0)
        metadata_found = bool(slug and first_message)

    with file_path.open("r", encoding="utf-8", errors="replace") as fh:
        if start_offset > 0:
            fh.seek(start_offset)
        all_lines = fh.readlines()

    line_count += sum(1 for ln in all_lines if ln.strip())

    # Single pass: metadata from first ~50 lines, tokens from all assistant messages
    for idx, raw in enumerate(all_lines):
        raw = raw.strip()
        if not raw:
            continue
        try:
            d = json.loads(raw)
        except json.JSONDecodeError:
            continue

        # Track the LAST user prompt across the whole file (skip tool_result / meta blocks)
        if d.get("type") == "user":
            content = d.get("message", {}).get("content", "")
            text = ""
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        text = block.get("text", "") or ""
                        break
            elif isinstance(content, str):
                text = content
            text = text.strip()
            # Skip tool results, command-stdout, and empty/meta prompts
            if text and not text.startswith("<") and "tool_use_id" not in text:
                last_user_message = text[:160]

        # Sum tokens from assistant messages (every line)
        if d.get("type") == "assistant":
            msg = d.get("message")
            if isinstance(msg, dict):
                usage = msg.get("usage") or {}
                if isinstance(usage, dict):
                    tokens += int(usage.get("input_tokens", 0) or 0)
                    tokens += int(usage.get("output_tokens", 0) or 0)
                    tokens += int(usage.get("cache_creation_input_tokens", 0) or 0)
                    tokens += int(usage.get("cache_read_input_tokens", 0) or 0)

        # Metadata — only scan first 50 lines
        if not metadata_found and idx < 50:
            if d.get("type") == "user" and d.get("sessionId"):
                if not slug:
                    slug = d.get("slug", "")
                if not cwd:
                    cwd = d.get("cwd", "")
                if not git_branch and d.get("gitBranch"):
                    git_branch = d.get("gitBranch", "")

            if not first_message and d.get("type") == "user":
                content = d.get("message", {}).get("content", "")
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            text = block.get("text", "") or ""
                            first_message = text[:120]
                            break
                elif isinstance(content, str):
                    first_message = content[:120]

            if slug and first_message:
                metadata_found = True

    log.debug("parse_session(%s): %d messages, ~%d tokens", file_path.name, line_count, tokens)
    sess = ClaudeSession(
        session_id=session_id,
        machine=machine,
        project_folder=folder_name,
        project_path=project_path,
        cwd=cwd,
        slug=slug,
        summary=first_message,
        messages=line_count,
        modified=modified,
        status="idle",  # enriched later by _mark_active_sessions
        pid=None,
        file_size=file_size,
        tokens=tokens,
        git_branch=git_branch,
        last_user_message=last_user_message,
    )
    # Stash incremental-parse breadcrumbs on the session object. The scan
    # loop stores these in its cache so the next call can seek past the
    # already-parsed bytes instead of re-reading the whole file.
    sess._parse_breadcrumbs = {  # type: ignore[attr-defined]
        "last_size": file_size,
        "slug": slug,
        "cwd": cwd,
        "git_branch": git_branch,
        "first_message": first_message,
        "last_user_message": last_user_message,
        "line_count": line_count,
        "tokens": tokens,
    }
    return sess


# ---------------------------------------------------------------------------
# PID / active session detection (local)
# ---------------------------------------------------------------------------

def _load_active_pids(claude_home: Path) -> tuple[dict[str, int], dict[str, str]]:
    """
    Read ~/.claude/sessions/*.json and return:
      - mapping session_id → pid for sessions whose process is still alive
      - mapping session_id → name for sessions that have a /rename name set
    """
    sessions_dir = claude_home / "sessions"
    active: dict[str, int] = {}
    names: dict[str, str] = {}
    if not sessions_dir.is_dir():
        return active, names

    for jf in sessions_dir.glob("*.json"):
        try:
            data = json.loads(jf.read_text(encoding="utf-8"))
            pid = data.get("pid")
            session_id = data.get("sessionId") or jf.stem
            name = data.get("name", "")
            if name:
                names[session_id] = name
            if pid and isinstance(pid, int):
                try:
                    proc = psutil.Process(pid)
                    if proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE:
                        active[session_id] = pid
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
        except Exception:
            continue

    return active, names


def _mark_active_sessions(
    sessions: list[ClaudeSession],
    active_pids: dict[str, int],
    names: dict[str, str] | None = None,
) -> None:
    """Mutate sessions in-place: set status, pid, cpu_percent, and name for live sessions.

    Status logic:
    - 'working' — PID alive AND (JSONL modified within last 15s OR CPU > 5%)
    - 'active'  — PID alive but not currently working (waiting for user input)
    - 'idle'    — PID not alive or session not in active_pids
    """
    for sess in sessions:
        pid = active_pids.get(sess.session_id)
        if pid:
            sess.pid = pid
            # Determine working vs active using JSONL mtime + CPU
            is_working = False
            # Check 1: JSONL file modified recently (within 15 seconds)
            try:
                mtime_ts = datetime.fromisoformat(sess.modified).timestamp()
                age = time.time() - mtime_ts
                if age < 15:
                    is_working = True
            except Exception:
                pass
            # Check 2: CPU usage (if psutil available)
            cpu = 0.0
            try:
                proc = psutil.Process(pid)
                cpu = proc.cpu_percent(interval=0.1)
                sess.cpu_percent = round(cpu, 1)
                if cpu > 5.0:
                    is_working = True
                sess.subprocess_count = len(proc.children(recursive=True))
            except Exception:
                pass
            sess.status = "working" if is_working else "active"
        if names:
            n = names.get(sess.session_id, "")
            if n:
                sess.name = n


# ---------------------------------------------------------------------------
# Git state collection (Phase B) — shared by scan_local + consumed by Pull API
# ---------------------------------------------------------------------------

def _collect_git_state(cwd: str) -> dict:
    """Collect per-cwd git freshness: upstream ref, ahead/behind, dirty flag.

    Returns a dict with keys:
      git_upstream: str | None   "origin/master" or None if no upstream / not a repo
      git_ahead:    int  | None  commits HEAD has beyond upstream, or None
      git_behind:   int  | None  commits upstream has beyond HEAD, or None
      git_dirty:    bool | None  True if tracked files modified, None if status failed

    Does NOT run `git fetch` — ahead/behind reflects last-fetched remote state.
    Uses --untracked-files=no on status so scratch notes / build artefacts
    don't falsely mark the tree dirty (we only care about changes that block
    a fast-forward pull).

    All four fields remain None when the cwd is not a git working tree.
    """
    state = {
        "git_upstream": None,
        "git_ahead": None,
        "git_behind": None,
        "git_dirty": None,
    }
    if not cwd:
        return state

    _kw = _win32_kwargs()

    # Dirty first — succeeds on any working tree, including ones with no
    # upstream configured. None stays as "couldn't determine".
    try:
        r = subprocess.run(
            ["git", "-C", cwd, "status", "--porcelain", "--untracked-files=no"],
            capture_output=True, text=True, timeout=2, **_kw,
        )
        if r.returncode == 0:
            state["git_dirty"] = bool(r.stdout.strip())
    except Exception:
        pass

    # Upstream ref — fails when no upstream configured (exit 128). Silent.
    try:
        r = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"],
            capture_output=True, text=True, timeout=2, **_kw,
        )
        if r.returncode == 0:
            up = r.stdout.strip()
            if up:
                state["git_upstream"] = up
    except Exception:
        pass

    # Ahead/behind only computable when upstream is known.
    if state["git_upstream"]:
        try:
            r = subprocess.run(
                ["git", "-C", cwd, "rev-list", "--left-right", "--count",
                 f"{state['git_upstream']}...HEAD"],
                capture_output=True, text=True, timeout=2, **_kw,
            )
            if r.returncode == 0:
                parts = r.stdout.strip().split()
                if len(parts) == 2:
                    state["git_behind"] = int(parts[0])
                    state["git_ahead"] = int(parts[1])
        except Exception:
            pass

    return state


# ---------------------------------------------------------------------------
# Persisted session-parse cache
#
# scan_local stores per-file ClaudeSession + incremental-parse breadcrumbs in
# an in-memory dict so warm scans skip the per-line json.loads loop. That
# dict goes away on process restart — and the FIRST scan of a new process
# has to re-read every JSONL from byte 0, including the live conversation's
# 200+ MB file. Persisting the cache to disk lets a fresh process start
# already-warm; we only re-parse files whose size/mtime changed since the
# last shutdown.
#
# Format: {"version": 2, "entries": {<path>: {"sess": <dict>,
#          "last_size": int, "last_mtime_ns": int,
#          "breadcrumbs": {...}}}}
# Path: ~/.claude-manager/scan-cache.json
# ---------------------------------------------------------------------------

def _persisted_cache_path() -> Path:
    return Path.home() / ".claude-manager" / "scan-cache.json"


def _load_persisted_cache() -> dict:
    """Return the in-memory cache dict, populated from disk if available.

    Cache values are tuples (sess, last_size, last_mtime_ns) where ``sess``
    carries the breadcrumbs attribute. Missing or malformed disk cache is
    silently treated as empty — a slow first scan, not an error.
    """
    cache: dict = {}
    path = _persisted_cache_path()
    if not path.is_file():
        return cache
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("version") != 2:
            return cache
        for p, entry in (data.get("entries") or {}).items():
            try:
                sess_d = entry["sess"]
                sess = ClaudeSession(**sess_d)
                bc = entry.get("breadcrumbs") or {}
                if bc:
                    sess._parse_breadcrumbs = bc  # type: ignore[attr-defined]
                cache[p] = (sess, int(entry["last_size"]), int(entry["last_mtime_ns"]))
            except Exception:
                continue
    except Exception as exc:
        log.debug("scan cache load failed (%s): %s", path, exc)
    if cache:
        log.info("scan cache: loaded %d entries from %s", len(cache), path)
    return cache


def _save_persisted_cache(cache: dict) -> None:
    """Atomically write the cache to disk via temp+rename."""
    path = _persisted_cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    out = {"version": 2, "entries": {}}
    for p, (sess, last_size, last_mtime) in cache.items():
        try:
            out["entries"][p] = {
                "sess": asdict(sess),
                "last_size": int(last_size),
                "last_mtime_ns": int(last_mtime),
                "breadcrumbs": getattr(sess, "_parse_breadcrumbs", {}),
            }
        except Exception:
            continue
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# Local scan
# ---------------------------------------------------------------------------

def scan_local(
    claude_home: Path | None = None,
    machine: str = "local",
    on_progress: Callable | None = None,
) -> list[ClaudeSession]:
    """
    Scan ~/.claude/projects/ for JSONL session files.

    - Skips folders that don't look like encoded paths (no '-' at position 1
      for drive-style OR not starting with '-' for Unix-style).
    - Reads at most 20 most-recent sessions per project folder.
    - Cross-references ~/.claude/sessions/*.json for live PIDs via psutil.
    - Returns all sessions sorted by modified descending.
    """
    if claude_home is None:
        claude_home = Path.home() / ".claude"

    _scan_t0 = time.monotonic()

    projects_dir = claude_home / "projects"
    if not projects_dir.is_dir():
        tl.event("cm.scan.local.ok",
                 machine=machine, sessions=0,
                 elapsed_ms=int((time.monotonic() - _scan_t0) * 1000),
                 reason="no_projects_dir")
        return []

    active_pids, session_names = _load_active_pids(claude_home)
    all_sessions: list[ClaudeSession] = []

    # Collect all (jf, project_path, folder_name) tuples first so we know total_files
    all_jsonl: list[tuple[Path, str, str]] = []
    for entry in projects_dir.iterdir():
        if not entry.is_dir():
            continue
        folder_name = entry.name

        # Filter: must look like an encoded path.
        # Unix style: starts with '-'
        # Windows style: letter then '--'
        is_unix_style = folder_name.startswith("-")
        is_win_style = (
            len(folder_name) >= 3
            and folder_name[0].isalpha()
            and folder_name[1] == "-"
            and folder_name[2] == "-"
        )
        if not is_unix_style and not is_win_style:
            continue

        project_path = decode_project_folder(folder_name)

        # Collect JSONL files, sorted by mtime desc, cap at 20
        jsonl_files = list(entry.glob("*.jsonl"))
        if not jsonl_files:
            continue

        jsonl_files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        jsonl_files = jsonl_files[:20]

        for jf in jsonl_files:
            all_jsonl.append((jf, project_path, folder_name))

    total_files = len(all_jsonl)
    _phase_parse_start = time.monotonic()
    # Per-file cache keyed on PATH (not size/mtime). Each entry holds the
    # last full ClaudeSession AND parse breadcrumbs (last_size, accumulated
    # tokens, line_count, metadata fields). On rescan:
    #
    #   * unchanged file (size + mtime match cached) → return cached sess
    #   * grew (live session appended N lines)        → seek to last_size,
    #     read ONLY the new tail, add tokens, return updated sess. Crucial
    #     for the 231 MB active-conversation file that previously cost
    #     ~50s on every scan because each new assistant message invalidated
    #     the (path, mtime, size) cache key.
    #   * shrunk (file rotated/truncated)             → full reparse
    #
    # Threading was tried and lost: parse_session is GIL-bound (json.loads
    # + dict iteration), ThreadPoolExecutor added context-switch overhead
    # without parallelism.
    _cache = getattr(scan_local, "_session_cache", None)
    if _cache is None:
        _cache = _load_persisted_cache()
        scan_local._session_cache = _cache  # type: ignore[attr-defined]

    found = 0
    _cache_hits = 0
    _cache_incremental = 0
    for jf, project_path, folder_name in all_jsonl:
        found += 1
        if on_progress:
            on_progress(machine, found, total_files, str(jf.name))
        try:
            st = jf.stat()
            path_key = str(jf)
            cached = _cache.get(path_key)
            sess = None
            if cached is not None:
                cached_sess, prev_size, prev_mtime = cached
                if prev_mtime == st.st_mtime_ns and prev_size == st.st_size:
                    # Identical bytes — full cache hit, no I/O at all.
                    sess = cached_sess
                    _cache_hits += 1
                elif prev_size <= st.st_size:
                    # File grew — incremental parse from prev_size.
                    breadcrumbs = getattr(cached_sess, "_parse_breadcrumbs", None)
                    if breadcrumbs:
                        sess = parse_session(jf, project_path, folder_name,
                                             machine=machine, prev=breadcrumbs)
                        if sess.cwd:
                            sess.project_path = sess.cwd
                        _cache_incremental += 1
            if sess is None:
                # Cold path or rotated file — full reparse.
                sess = parse_session(jf, project_path, folder_name, machine=machine)
                if sess.cwd:
                    sess.project_path = sess.cwd
            _cache[path_key] = (sess, st.st_size, st.st_mtime_ns)
            if _is_tmp_path(sess.cwd or sess.project_path):
                continue
            all_sessions.append(sess)
        except Exception:
            continue
    # Prune cache: drop entries for paths not seen this cycle.
    _live_paths = {str(jf) for jf, _, _ in all_jsonl}
    for k in list(_cache.keys()):
        if k not in _live_paths:
            _cache.pop(k, None)
    _phase_parse_ms = int((time.monotonic() - _phase_parse_start) * 1000)

    _phase_pid_start = time.monotonic()
    _mark_active_sessions(all_sessions, active_pids, session_names)
    _phase_pid_ms = int((time.monotonic() - _phase_pid_start) * 1000)

    # Per-cwd git + README info, collected IN PARALLEL via a thread pool.
    # Before: 5 sequential `git` subprocess calls per unique cwd × ~100ms
    # Windows spawn cost × ~130 projects = ~40s wall time. After: same
    # work submitted concurrently with 16 workers — ~3-5s wall time.
    # No locks needed because each task writes to a dedicated future's
    # result; we don't share dicts across threads.
    _README_NAMES = ("README.md", "README.MD", "README", "readme.md")

    def _gather_cwd_info(cwd: str) -> dict:
        info = {"git_remote": "", "git_commits": 0, "readme_path": ""}
        info.update(_collect_git_state(cwd))
        _kw = _win32_kwargs()
        try:
            r = subprocess.run(
                ["git", "-C", cwd, "config", "--get", "remote.origin.url"],
                capture_output=True, text=True, timeout=2, **_kw,
            )
            if r.returncode == 0:
                info["git_remote"] = r.stdout.strip()
        except Exception:
            pass
        try:
            r = subprocess.run(
                ["git", "-C", cwd, "rev-list", "--count", "HEAD"],
                capture_output=True, text=True, timeout=2, **_kw,
            )
            if r.returncode == 0:
                info["git_commits"] = int(r.stdout.strip())
        except Exception:
            pass
        try:
            cwd_p = Path(cwd)
            for name in _README_NAMES:
                candidate = cwd_p / name
                if candidate.is_file():
                    info["readme_path"] = str(candidate)
                    break
        except Exception:
            pass
        return info

    _phase_git_start = time.monotonic()
    unique_cwds = {(s.cwd or s.project_path) for s in all_sessions}
    unique_cwds.discard("")
    unique_cwds.discard(None)

    # Per-cwd git+readme cache keyed on the cwd's .git dir mtime_ns. When the
    # user commits / pulls / checks out, .git's mtime advances → cache miss →
    # we re-run git for that one cwd. Static cwds (most projects, most of the
    # time) are cache hits and skip all 5 subprocess.run calls per cycle.
    # First cycle warms the cache; subsequent cycles drop git phase from
    # ~5s to <100ms for unchanged projects.
    _git_cache = getattr(scan_local, "_git_cache", None)
    if _git_cache is None:
        _git_cache = {}
        scan_local._git_cache = _git_cache  # type: ignore[attr-defined]

    _cwd_to_key: dict[str, tuple[str, int]] = {}
    _to_fetch: list[str] = []
    for c in unique_cwds:
        try:
            git_dir = Path(c) / ".git"
            mt = git_dir.stat().st_mtime_ns if git_dir.exists() else 0
        except Exception:
            mt = 0
        key = (c, mt)
        _cwd_to_key[c] = key
        if key not in _git_cache:
            _to_fetch.append(c)

    _git_hits = len(unique_cwds) - len(_to_fetch)
    _cwd_info: dict[str, dict] = {}
    if _to_fetch:
        from concurrent.futures import ThreadPoolExecutor
        # 16 workers is a good Windows sweet spot: subprocess spawn is the
        # dominant cost (CPU-light) so we over-subscribe modest cores; more
        # than 24 starts contending on Windows process-creation locks.
        with ThreadPoolExecutor(max_workers=16, thread_name_prefix="scan-git") as pool:
            future_to_cwd = {pool.submit(_gather_cwd_info, c): c for c in _to_fetch}
            for fut in future_to_cwd:
                cwd = future_to_cwd[fut]
                info = fut.result()
                _git_cache[_cwd_to_key[cwd]] = info
    for c in unique_cwds:
        _cwd_info[c] = _git_cache.get(_cwd_to_key[c], {})

    # Prune stale cache entries (cwds no longer present, or whose .git mtime
    # advanced — the old key won't be looked up again, so drop it).
    _live_keys = set(_cwd_to_key.values())
    for k in list(_git_cache.keys()):
        if k not in _live_keys:
            _git_cache.pop(k, None)
    _phase_git_ms = int((time.monotonic() - _phase_git_start) * 1000)

    for sess in all_sessions:
        cwd_key = sess.cwd or sess.project_path
        info = _cwd_info.get(cwd_key, {})
        sess.git_remote = info.get("git_remote", "")
        sess.git_commits = info.get("git_commits", 0)
        sess.git_upstream = info.get("git_upstream")
        sess.git_ahead = info.get("git_ahead")
        sess.git_behind = info.get("git_behind")
        sess.git_dirty = info.get("git_dirty")
        sess.readme_path = info.get("readme_path", "")

    all_sessions.sort(key=lambda s: s.modified, reverse=True)
    # Persist the cache to disk every scan so a fresh process restart starts
    # warm. Even on a 100%-hit cycle we save: the previous run may have
    # crashed before persisting, leaving an out-of-date disk file. ~50ms
    # for 400 entries — not worth a more elaborate dirty-bit guard.
    if total_files > 0:
        try:
            _save_persisted_cache(_cache)
        except Exception as exc:
            log.debug("scan cache persist failed: %s", exc)
    _total_ms = int((time.monotonic() - _scan_t0) * 1000)
    log.info(
        "scan_local: %d sessions in %dms "
        "(parse=%dms[hit=%d incr=%d cold=%d of %d] pid=%dms git=%dms[%d/%d cached] cwds=%d)",
        len(all_sessions), _total_ms, _phase_parse_ms,
        _cache_hits, _cache_incremental,
        total_files - _cache_hits - _cache_incremental, total_files,
        _phase_pid_ms, _phase_git_ms, _git_hits, len(unique_cwds), len(unique_cwds),
    )
    tl.event("cm.scan.local.ok",
             machine=machine, sessions=len(all_sessions),
             elapsed_ms=_total_ms, parse_ms=_phase_parse_ms,
             pid_ms=_phase_pid_ms, git_ms=_phase_git_ms,
             cwds=len(unique_cwds), files=total_files,
             parse_cache_hits=_cache_hits,
             parse_incremental=_cache_incremental,
             git_cache_hits=_git_hits)
    return all_sessions


# ---------------------------------------------------------------------------
# Remote scan script (self-contained, stdlib only)
# ---------------------------------------------------------------------------

REMOTE_SCAN_SCRIPT = r"""
import json, os, sys, pathlib, datetime

def decode(folder):
    sep = '\\\\' if sys.platform == 'win32' else '/'
    result = ''
    i = 0
    if len(folder) >= 2 and folder[1] == '-' and folder[0].isalpha():
        result += folder[0] + ':'
        i = 2
    while i < len(folder):
        result += sep if folder[i] == '-' else folder[i]
        i += 1
    return result

def is_tmp(cwd):
    # Mirror of scanner._is_tmp_path - inlined because this script ships raw
    # and cannot import from the host.
    if not cwd:
        return False
    norm = cwd.replace('\\', '/').lower()
    unix = ('/tmp/', '/var/tmp/', '/private/tmp/', '/private/var/folders/')
    for p in unix:
        if norm.startswith(p):
            return True
    if norm.startswith('c:/windows/temp/'):
        return True
    if '/appdata/local/temp/' in norm:
        return True
    if sys.platform == 'win32':
        for var in ('TEMP', 'TMP'):
            v = os.environ.get(var, '')
            if v:
                ev = v.replace('\\', '/').lower().rstrip('/') + '/'
                if norm.startswith(ev):
                    return True
    return False

def pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False

claude_home = pathlib.Path.home() / '.claude'
projects_dir = claude_home / 'projects'
sessions_dir = claude_home / 'sessions'

active_pids = {}
session_names = {}
if sessions_dir.is_dir():
    for jf in sessions_dir.glob('*.json'):
        try:
            d = json.loads(jf.read_text())
            pid = d.get('pid')
            sid = d.get('sessionId') or jf.stem
            name = d.get('name', '')
            if name:
                session_names[sid] = name
            if pid and pid_alive(int(pid)):
                active_pids[sid] = int(pid)
        except Exception:
            pass

results = []
if projects_dir.is_dir():
    for entry in projects_dir.iterdir():
        if not entry.is_dir():
            continue
        fn = entry.name
        is_unix = fn.startswith('-')
        is_win = len(fn) >= 3 and fn[0].isalpha() and fn[1] == '-' and fn[2] == '-'
        if not is_unix and not is_win:
            continue
        proj_path = decode(fn)
        jsonls = sorted(entry.glob('*.jsonl'), key=lambda p: p.stat().st_mtime, reverse=True)[:20]
        for jf in jsonls:
            try:
                stat = jf.stat()
                mod = datetime.datetime.fromtimestamp(stat.st_mtime).astimezone().isoformat()
                slug = ''; cwd = ''; git_branch = ''; summary = ''; line_count = 0; tokens = 0
                last_user_msg = ''
                meta_done = False
                with open(jf, encoding='utf-8', errors='replace') as fh:
                    all_lines = fh.readlines()
                line_count = sum(1 for l in all_lines if l.strip())
                for i, raw in enumerate(all_lines):
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        d = json.loads(raw)
                    except Exception:
                        continue
                    # Track last user prompt (skip tool results / meta blocks)
                    if d.get('type') == 'user':
                        c = d.get('message', {}).get('content', '')
                        t = ''
                        if isinstance(c, list):
                            for b in c:
                                if isinstance(b, dict) and b.get('type') == 'text':
                                    t = b.get('text', '') or ''
                                    break
                        elif isinstance(c, str):
                            t = c
                        t = t.strip()
                        if t and not t.startswith('<') and 'tool_use_id' not in t:
                            last_user_msg = t[:160]
                    # Tokens — every line
                    if d.get('type') == 'assistant':
                        u = d.get('message', {}).get('usage', {}) or {}
                        tokens += int(u.get('input_tokens', 0) or 0)
                        tokens += int(u.get('output_tokens', 0) or 0)
                        tokens += int(u.get('cache_creation_input_tokens', 0) or 0)
                        tokens += int(u.get('cache_read_input_tokens', 0) or 0)
                    # Metadata — first 50 lines
                    if not meta_done and i < 50:
                        if d.get('type') == 'user' and d.get('sessionId'):
                            if not slug: slug = d.get('slug', '')
                            if not cwd: cwd = d.get('cwd', '')
                            if not git_branch and d.get('gitBranch'): git_branch = d.get('gitBranch', '')
                        if not summary and d.get('type') == 'user':
                            c = d.get('message', {}).get('content', '')
                            if isinstance(c, list):
                                for b in c:
                                    if isinstance(b, dict) and b.get('type') == 'text':
                                        summary = (b.get('text') or '')[:120]; break
                            elif isinstance(c, str):
                                summary = c[:120]
                        if slug and summary:
                            meta_done = True
                sid = jf.stem
                pid = active_pids.get(sid)
                # Drop throwaway sessions in OS temp dirs — same rule as
                # scanner._is_tmp_path on the local side.
                if is_tmp(cwd or proj_path):
                    continue
                results.append({
                    'session_id': sid,
                    'project_folder': fn,
                    'project_path': cwd if cwd else proj_path,
                    'cwd': cwd,
                    'slug': slug,
                    'summary': summary,
                    'messages': line_count,
                    'modified': mod,
                    'status': 'active' if pid else 'idle',
                    'pid': pid,
                    'file_size': stat.st_size,
                    'tokens': tokens,
                    'name': session_names.get(sid, ''),
                    'git_branch': git_branch,
                    'last_user_message': last_user_msg,
                    'readme_path': '',
                    'git_upstream': None,
                    'git_ahead': None,
                    'git_behind': None,
                    'git_dirty': None,
                })
            except Exception:
                pass

results.sort(key=lambda s: s['modified'], reverse=True)

# Detect git remotes and commit counts (memoized per cwd)
import subprocess as _sp
# Windows: suppress conhost popups per git.exe spawn. Without these flags every
# _sp.run(['git', ...]) allocates a new console because the parent python was
# started by sshd without an inherited console — the windows accumulate visibly.
_win_kw = {}
if sys.platform == 'win32':
    _si = _sp.STARTUPINFO()
    _si.dwFlags |= 0x00000001  # STARTF_USESHOWWINDOW
    _si.wShowWindow = 0         # SW_HIDE
    _win_kw = {'creationflags': 0x08000000, 'startupinfo': _si}  # CREATE_NO_WINDOW

# Resolve the full path to git.exe on Windows. The sshd-inherited PATH is
# stripped and doesn't include C:\Program Files\Git\cmd, so bare 'git' would
# FileNotFoundError and leave git_remote=''. An empty remote breaks the
# cross-machine project-id merge in project_identity.py — /streams-android
# on mac-mini (with normalized github.com/.../streams-android id) then
# renders as a separate card from \streams-android on avell-i7 (which falls
# back to basename-only id). shutil.which first; fall back to the two
# well-known install locations.
import shutil as _shutil
_GIT = 'git'
if sys.platform == 'win32':
    _candidate = (_shutil.which('git')
                  or _shutil.which('git.exe')
                  or r'C:\Program Files\Git\cmd\git.exe'
                  or r'C:\Program Files\Git\bin\git.exe')
    for _p in (_shutil.which('git'),
               _shutil.which('git.exe'),
               r'C:\Program Files\Git\cmd\git.exe',
               r'C:\Program Files\Git\bin\git.exe'):
        if _p and os.path.isfile(_p):
            _GIT = _p
            break

_remote_cache = {}
_commits_cache = {}
for r in results:
    cwd_key = r.get('cwd') or r.get('project_path', '')
    if not cwd_key:
        r['git_remote'] = ''
        r['git_commits'] = 0
        continue
    if cwd_key not in _remote_cache:
        try:
            pr = _sp.run([_GIT, '-C', cwd_key, 'config', '--get', 'remote.origin.url'],
                         capture_output=True, text=True, timeout=2, **_win_kw)
            _remote_cache[cwd_key] = pr.stdout.strip() if pr.returncode == 0 else ''
        except Exception:
            _remote_cache[cwd_key] = ''
    r['git_remote'] = _remote_cache[cwd_key]
    if cwd_key not in _commits_cache:
        try:
            pr = _sp.run([_GIT, '-C', cwd_key, 'rev-list', '--count', 'HEAD'],
                         capture_output=True, text=True, timeout=2, **_win_kw)
            _commits_cache[cwd_key] = int(pr.stdout.strip()) if pr.returncode == 0 else 0
        except Exception:
            _commits_cache[cwd_key] = 0
    r['git_commits'] = _commits_cache[cwd_key]

# Phase B — git freshness state per cwd: upstream, ahead/behind, dirty.
# Never runs `git fetch`; ahead/behind is measured against last-fetched
# upstream. --untracked-files=no so scratch / build output don't mark dirty.
_state_cache = {}
for r in results:
    cwd_key = r.get('cwd') or r.get('project_path', '')
    if not cwd_key:
        continue
    if cwd_key not in _state_cache:
        gs = {'git_upstream': None, 'git_ahead': None, 'git_behind': None, 'git_dirty': None}
        try:
            pr = _sp.run([_GIT, '-C', cwd_key, 'status', '--porcelain', '--untracked-files=no'],
                         capture_output=True, text=True, timeout=2, **_win_kw)
            if pr.returncode == 0:
                gs['git_dirty'] = bool(pr.stdout.strip())
        except Exception:
            pass
        try:
            pr = _sp.run([_GIT, '-C', cwd_key, 'rev-parse', '--abbrev-ref',
                          '--symbolic-full-name', '@{upstream}'],
                         capture_output=True, text=True, timeout=2, **_win_kw)
            if pr.returncode == 0:
                up = pr.stdout.strip()
                if up:
                    gs['git_upstream'] = up
        except Exception:
            pass
        if gs['git_upstream']:
            try:
                pr = _sp.run([_GIT, '-C', cwd_key, 'rev-list', '--left-right', '--count',
                              gs['git_upstream'] + '...HEAD'],
                             capture_output=True, text=True, timeout=2, **_win_kw)
                if pr.returncode == 0:
                    parts = pr.stdout.strip().split()
                    if len(parts) == 2:
                        gs['git_behind'] = int(parts[0])
                        gs['git_ahead'] = int(parts[1])
            except Exception:
                pass
        _state_cache[cwd_key] = gs
    gs = _state_cache[cwd_key]
    r['git_upstream'] = gs['git_upstream']
    r['git_ahead'] = gs['git_ahead']
    r['git_behind'] = gs['git_behind']
    r['git_dirty'] = gs['git_dirty']

_readme_cache = {}
_README_NAMES = ('README.md', 'README.MD', 'README', 'readme.md')
for r in results:
    cwd_key = r.get('cwd') or r.get('project_path', '')
    if not cwd_key:
        r['readme_path'] = ''
        continue
    if cwd_key not in _readme_cache:
        found = ''
        for name in _README_NAMES:
            candidate = os.path.join(cwd_key, name)
            if os.path.isfile(candidate):
                found = candidate
                break
        _readme_cache[cwd_key] = found
    r['readme_path'] = _readme_cache[cwd_key]

print(json.dumps(results))
"""


# ---------------------------------------------------------------------------
# Remote scan via dispatch daemon API
# ---------------------------------------------------------------------------

async def scan_remote_via_api(machine_name: str, ip: str, dispatch_port: int) -> list[ClaudeSession]:
    """Query the dispatch daemon's /sessions endpoint instead of SSH."""
    import aiohttp
    url = f"http://{ip}:{dispatch_port}/sessions"
    _t0 = time.monotonic()
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=3)) as resp:
                if resp.status != 200:
                    tl.event("cm.scan.remote.err",
                             machine=machine_name, transport="api",
                             err=f"http {resp.status}",
                             elapsed_ms=int((time.monotonic() - _t0) * 1000))
                    return []
                data = await resp.json()
                sessions = []
                for item in data:
                    s = ClaudeSession(
                        session_id=item.get("session_id", ""),
                        machine=machine_name,
                        project_folder=item.get("project_folder", ""),
                        project_path=item.get("project_path", ""),
                        cwd=item.get("cwd", ""),
                        slug=item.get("slug", ""),
                        summary=item.get("summary", ""),
                        messages=item.get("messages", 0),
                        modified=item.get("modified", ""),
                        status=item.get("status", "idle"),
                        pid=item.get("pid"),
                        file_size=item.get("file_size", 0),
                        tokens=item.get("tokens", 0),
                        name=item.get("name", ""),
                        git_branch=item.get("git_branch", ""),
                        subprocess_count=item.get("subprocess_count", 0),
                        git_remote=item.get("git_remote", ""),
                        git_commits=item.get("git_commits", 0),
                        last_user_message=item.get("last_user_message", ""),
                        readme_path=item.get("readme_path", ""),
                        git_upstream=item.get("git_upstream"),
                        git_ahead=item.get("git_ahead"),
                        git_behind=item.get("git_behind"),
                        git_dirty=item.get("git_dirty"),
                    )
                    sessions.append(s)
                log.info("scan_remote(%s): %d sessions via api", machine_name, len(sessions))
                tl.event("cm.scan.remote.ok",
                         machine=machine_name, sessions=len(sessions),
                         transport="api",
                         elapsed_ms=int((time.monotonic() - _t0) * 1000))
                return sessions
    except Exception as exc:
        log.warning("scan_remote(%s): api failed: %s", machine_name, exc)
        tl.event("cm.scan.remote.err",
                 machine=machine_name, transport="api",
                 err=str(exc)[:200],
                 elapsed_ms=int((time.monotonic() - _t0) * 1000))
        return []


# ---------------------------------------------------------------------------
# Remote scan via SSH
# ---------------------------------------------------------------------------

async def scan_remote(
    machine_name: str,
    ssh_alias: str,
) -> list[ClaudeSession]:
    """
    Run REMOTE_SCAN_SCRIPT on a remote machine via SSH and parse results.

    Returns a (possibly empty) list of ClaudeSession objects tagged with
    the remote machine name.
    """
    script = REMOTE_SCAN_SCRIPT.strip()
    executor = SSHExecutor(machine_name)
    _t0 = time.monotonic()
    try:
        rc, stdout, stderr = await executor.exec_shell(
            "python3 -",
            timeout=30,
            input=script.encode("utf-8"),
        )
    except asyncio.TimeoutError:
        log.warning("scan_remote(%s): SSH timed out", machine_name)
        tl.event("cm.scan.remote.err",
                 machine=machine_name, transport="ssh", err="timeout",
                 elapsed_ms=int((time.monotonic() - _t0) * 1000))
        return []
    except Exception as exc:
        log.warning("scan_remote(%s): failed: %s", machine_name, exc)
        tl.event("cm.scan.remote.err",
                 machine=machine_name, transport="ssh",
                 err=str(exc)[:200],
                 elapsed_ms=int((time.monotonic() - _t0) * 1000))
        return []

    if rc != 0:
        log.warning("scan_remote(%s): SSH exited %d", machine_name, rc)
        tl.event("cm.scan.remote.err",
                 machine=machine_name, transport="ssh",
                 err=f"rc={rc}",
                 elapsed_ms=int((time.monotonic() - _t0) * 1000))
        return []

    try:
        raw_list: list[dict] = json.loads(stdout.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        log.warning("scan_remote(%s): JSON parse error", machine_name)
        tl.event("cm.scan.remote.err",
                 machine=machine_name, transport="ssh", err="json_decode",
                 elapsed_ms=int((time.monotonic() - _t0) * 1000))
        return []

    sessions: list[ClaudeSession] = []
    for item in raw_list:
        try:
            sess = ClaudeSession(
                session_id=item["session_id"],
                machine=machine_name,
                project_folder=item["project_folder"],
                project_path=item["project_path"],
                cwd=item.get("cwd", ""),
                slug=item.get("slug", ""),
                summary=item.get("summary", ""),
                messages=item.get("messages", 0),
                modified=item.get("modified", ""),
                status=item.get("status", "idle"),
                pid=item.get("pid"),
                file_size=item.get("file_size", 0),
                tokens=item.get("tokens", 0),
                name=item.get("name", ""),
                git_branch=item.get("git_branch", ""),
                subprocess_count=item.get("subprocess_count", 0),
                git_remote=item.get("git_remote", ""),
                git_commits=item.get("git_commits", 0),
                last_user_message=item.get("last_user_message", ""),
                readme_path=item.get("readme_path", ""),
                git_upstream=item.get("git_upstream"),
                git_ahead=item.get("git_ahead"),
                git_behind=item.get("git_behind"),
                git_dirty=item.get("git_dirty"),
            )
            sessions.append(sess)
        except (KeyError, TypeError):
            continue

    log.info("scan_remote(%s): %d sessions via ssh", machine_name, len(sessions))
    tl.event("cm.scan.remote.ok",
             machine=machine_name, sessions=len(sessions),
             transport="ssh",
             elapsed_ms=int((time.monotonic() - _t0) * 1000))
    return sessions


# ---------------------------------------------------------------------------
# Combined scan: local + all online remote machines
# ---------------------------------------------------------------------------

async def scan_all(
    local_machine: str | None,
    fleet: dict[str, dict[str, Any]],
    on_progress: Callable | None = None,
) -> list[ClaudeSession]:
    """
    Run local scan and remote scans in parallel via asyncio.gather.

    Args:
        local_machine: Fleet machine name for this host, or None.
        fleet:         Fleet health dict from fleet.discover_fleet().
                       Only machines with online=True are scanned remotely.
        on_progress:   Optional async or sync callback(machine, found, total, current_file).
                       Called as files are parsed (local) or at start/end (remote).

    Returns all sessions sorted by modified descending.
    """
    from .config import FLEET_MACHINES

    loop = asyncio.get_running_loop()

    async def _call_progress(machine: str, found: int, total: int, current_file: str) -> None:
        if on_progress is None:
            return
        result = on_progress(machine, found, total, current_file)
        if asyncio.iscoroutine(result):
            await result

    tasks: list[asyncio.Task] = []
    labels: list[str] = []

    # Local scan (run in executor to avoid blocking event loop on large dirs)
    # Progress callback is sync inside executor; we bridge via thread-safe call.
    def _sync_progress(machine: str, found: int, total: int, current_file: str) -> None:
        if on_progress is None:
            return
        result = on_progress(machine, found, total, current_file)
        if asyncio.iscoroutine(result):
            # Schedule on event loop from executor thread
            asyncio.run_coroutine_threadsafe(result, loop)

    local_label = local_machine or "local"

    async def _local() -> list[ClaudeSession]:
        await _call_progress(local_label, 0, 0, "scanning...")
        sessions = await loop.run_in_executor(
            None,
            lambda: scan_local(machine=local_label, on_progress=_sync_progress),
        )
        await _call_progress(local_label, len(sessions), len(sessions), "done")
        return sessions

    tasks.append(asyncio.ensure_future(_local()))
    labels.append("__local__")

    # Remote scans for online machines that are not this machine
    for name, health in fleet.items():
        if name == local_machine:
            continue
        if not health.get("online"):
            continue
        info = FLEET_MACHINES[name]
        dispatch_port = info.get("dispatch_port")
        # Windows OpenSSH spawns a fresh PowerShell with an attached ConPTY
        # for every exec channel (ControlMaster doesn't exist on Windows, so
        # the asyncssh pool can dedupe TCP but not shell spawns). Each of
        # those PowerShell children briefly embeds in Windows Terminal —
        # causing the visible window pileup seen 2026-04-22 on avell-i7.
        # Mitigation: on Windows targets, SSH fallback is only used when
        # claude-dispatch is UNREACHABLE (no dispatch_port). When the API
        # is configured but degraded (returns empty / times out on /sessions),
        # skip the SSH fallback and accept empty — better to show nothing
        # than to storm the host's desktop.
        is_windows = info.get("os") == "win32"
        if dispatch_port:
            async def _api_with_ssh_fallback(
                _name: str = name,
                _ip: str = info["ip"],
                _port: int = dispatch_port,
                _ssh_alias: str = info["ssh_alias"],
                _is_win: bool = is_windows,
            ) -> list[ClaudeSession]:
                await _call_progress(_name, 0, 0, "querying API...")
                sessions = await scan_remote_via_api(_name, _ip, _port)
                if not sessions and not _is_win:
                    # API failed or returned empty — fall back to SSH for
                    # non-Windows targets only (SSH on Unix is cheap).
                    await _call_progress(_name, 0, 0, "connecting...")
                    sessions = await scan_remote(_name, _ssh_alias)
                elif not sessions and _is_win:
                    log.info(
                        "scan_remote(%s): Windows + claude-dispatch API empty/degraded; "
                        "skipping SSH fallback to avoid ConPTY popup storm",
                        _name,
                    )
                await _call_progress(_name, len(sessions), len(sessions), "done")
                return sessions
            tasks.append(asyncio.ensure_future(_api_with_ssh_fallback()))
        else:
            async def _ssh_only(
                _name: str = name,
                _ssh_alias: str = info["ssh_alias"],
            ) -> list[ClaudeSession]:
                await _call_progress(_name, 0, 0, "connecting...")
                sessions = await scan_remote(_name, _ssh_alias)
                await _call_progress(_name, len(sessions), len(sessions), "done")
                return sessions
            tasks.append(asyncio.ensure_future(_ssh_only()))
        labels.append(name)

    results = await asyncio.gather(*tasks, return_exceptions=True)

    all_sessions: list[ClaudeSession] = []
    for result in results:
        if isinstance(result, Exception):
            continue
        if isinstance(result, list):
            all_sessions.extend(result)

    all_sessions.sort(key=lambda s: s.modified, reverse=True)

    # Update the on-disk project identity cache
    try:
        from .project_identity import project_id as _pid, project_display_name as _pdn, normalize_remote as _nr
        _update_project_cache(all_sessions, _pid, _pdn)
    except Exception as _exc:
        log.debug("project cache update failed: %s", _exc)

    return all_sessions


# ---------------------------------------------------------------------------
# Project identity cache (written to temp dir, read by /api/projects)
# ---------------------------------------------------------------------------

import os
import tempfile

_PROJECT_CACHE_DIR = Path(tempfile.gettempdir()) / "claude-manager"
_PROJECT_CACHE_FILE = _PROJECT_CACHE_DIR / "project-cache.json"


def _load_project_cache() -> dict:
    """Load the project cache from disk. Returns empty dict on missing/corrupt."""
    try:
        if _PROJECT_CACHE_FILE.is_file():
            data = json.loads(_PROJECT_CACHE_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("version") == 1:
                return data
    except Exception as exc:
        log.warning("project cache load failed (resetting): %s", exc)
    return {"version": 1, "updated": "", "projects": {}}


def _save_project_cache(cache: dict) -> None:
    """Save the project cache atomically."""
    try:
        _PROJECT_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = _PROJECT_CACHE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(cache, indent=2), encoding="utf-8")
        os.replace(str(tmp), str(_PROJECT_CACHE_FILE))
    except Exception as exc:
        log.warning("project cache save failed: %s", exc)


def _update_project_cache(
    sessions: list[ClaudeSession],
    pid_fn: Any,
    pdn_fn: Any,
) -> None:
    """Update the project cache with the current session list."""
    now = datetime.now(timezone.utc).isoformat()
    cache = _load_project_cache()
    projects = cache.setdefault("projects", {})

    # Collect info per project_id
    seen: dict[str, dict] = {}
    for sess in sessions:
        pid = pid_fn(sess)
        if pid not in seen:
            seen[pid] = {
                "display_name": pdn_fn(pid),
                "git_remote": sess.git_remote or "",
                "machines": set(),
                "last_seen": sess.modified or now,
                "first_seen": sess.modified or now,
            }
        entry = seen[pid]
        entry["machines"].add(sess.machine)
        if sess.modified and sess.modified > entry["last_seen"]:
            entry["last_seen"] = sess.modified
        if sess.modified and sess.modified < entry["first_seen"]:
            entry["first_seen"] = sess.modified

    # Merge into cache
    for pid, info in seen.items():
        existing = projects.get(pid, {})
        projects[pid] = {
            "display_name": info["display_name"],
            "git_remote": info["git_remote"] or existing.get("git_remote", ""),
            "first_seen": existing.get("first_seen") or info["first_seen"],
            "last_seen": info["last_seen"],
            "machines": sorted(info["machines"]),
        }

    cache["updated"] = now
    _save_project_cache(cache)
