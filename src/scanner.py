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

log = logging.getLogger("claude_manager.scanner")


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
) -> ClaudeSession:
    """
    Parse a single JSONL session file, reading at most the first 50 lines
    for metadata extraction.

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

    with file_path.open("r", encoding="utf-8", errors="replace") as fh:
        all_lines = fh.readlines()

    line_count = sum(1 for ln in all_lines if ln.strip())

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
    return ClaudeSession(
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

    projects_dir = claude_home / "projects"
    if not projects_dir.is_dir():
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
    found = 0
    for jf, project_path, folder_name in all_jsonl:
        found += 1
        if on_progress:
            on_progress(machine, found, total_files, str(jf.name))
        try:
            sess = parse_session(jf, project_path, folder_name, machine=machine)
            # Prefer cwd from JSONL as the authoritative path — it avoids the
            # lossy dash-to-slash decode ambiguity (e.g. "claude-manager" → "claude/manager").
            if sess.cwd:
                sess.project_path = sess.cwd
            all_sessions.append(sess)
        except Exception:
            continue

    _mark_active_sessions(all_sessions, active_pids, session_names)
    # Detect git remotes and commit counts for each unique cwd (memoized per scan)
    _remote_cache: dict[str, str] = {}
    _commits_cache: dict[str, int] = {}
    for sess in all_sessions:
        cwd_key = sess.cwd or sess.project_path
        if not cwd_key:
            continue
        if cwd_key not in _remote_cache:
            try:
                result = subprocess.run(
                    ["git", "-C", cwd_key, "config", "--get", "remote.origin.url"],
                    capture_output=True, text=True, timeout=2,
                    **_win32_kwargs(),
                )
                _remote_cache[cwd_key] = result.stdout.strip() if result.returncode == 0 else ""
            except Exception:
                _remote_cache[cwd_key] = ""
        sess.git_remote = _remote_cache[cwd_key]
        if cwd_key not in _commits_cache:
            try:
                result = subprocess.run(
                    ["git", "-C", cwd_key, "rev-list", "--count", "HEAD"],
                    capture_output=True, text=True, timeout=2,
                    **_win32_kwargs(),
                )
                _commits_cache[cwd_key] = int(result.stdout.strip()) if result.returncode == 0 else 0
            except Exception:
                _commits_cache[cwd_key] = 0
        sess.git_commits = _commits_cache[cwd_key]

    # Detect README presence per unique cwd (memoized)
    _readme_cache: dict[str, str] = {}
    _README_NAMES = ("README.md", "README.MD", "README", "readme.md")
    for sess in all_sessions:
        cwd_key = sess.cwd or sess.project_path
        if not cwd_key:
            continue
        if cwd_key not in _readme_cache:
            found = ""
            try:
                cwd_p = Path(cwd_key)
                for name in _README_NAMES:
                    candidate = cwd_p / name
                    if candidate.is_file():
                        found = str(candidate)
                        break
            except Exception:
                pass
            _readme_cache[cwd_key] = found
        sess.readme_path = _readme_cache[cwd_key]

    all_sessions.sort(key=lambda s: s.modified, reverse=True)
    log.info("scan_local: found %d sessions", len(all_sessions))
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
            pr = _sp.run(['git', '-C', cwd_key, 'config', '--get', 'remote.origin.url'],
                         capture_output=True, text=True, timeout=2, **_win_kw)
            _remote_cache[cwd_key] = pr.stdout.strip() if pr.returncode == 0 else ''
        except Exception:
            _remote_cache[cwd_key] = ''
    r['git_remote'] = _remote_cache[cwd_key]
    if cwd_key not in _commits_cache:
        try:
            pr = _sp.run(['git', '-C', cwd_key, 'rev-list', '--count', 'HEAD'],
                         capture_output=True, text=True, timeout=2, **_win_kw)
            _commits_cache[cwd_key] = int(pr.stdout.strip()) if pr.returncode == 0 else 0
        except Exception:
            _commits_cache[cwd_key] = 0
    r['git_commits'] = _commits_cache[cwd_key]

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
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=3)) as resp:
                if resp.status != 200:
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
                    )
                    sessions.append(s)
                log.info("scan_remote(%s): %d sessions via api", machine_name, len(sessions))
                return sessions
    except Exception as exc:
        log.warning("scan_remote(%s): api failed: %s", machine_name, exc)
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
    try:
        rc, stdout, stderr = await executor.exec_shell(
            "python3 -",
            timeout=30,
            input=script.encode("utf-8"),
        )
    except asyncio.TimeoutError:
        log.warning("scan_remote(%s): SSH timed out", machine_name)
        return []
    except Exception as exc:
        log.warning("scan_remote(%s): failed: %s", machine_name, exc)
        return []

    if rc != 0:
        log.warning("scan_remote(%s): SSH exited %d", machine_name, rc)
        return []

    try:
        raw_list: list[dict] = json.loads(stdout.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        log.warning("scan_remote(%s): JSON parse error", machine_name)
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
            )
            sessions.append(sess)
        except (KeyError, TypeError):
            continue

    log.info("scan_remote(%s): %d sessions via ssh", machine_name, len(sessions))
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
        if dispatch_port:
            async def _api_with_ssh_fallback(
                _name: str = name,
                _ip: str = info["ip"],
                _port: int = dispatch_port,
                _ssh_alias: str = info["ssh_alias"],
            ) -> list[ClaudeSession]:
                await _call_progress(_name, 0, 0, "querying API...")
                sessions = await scan_remote_via_api(_name, _ip, _port)
                if not sessions:
                    # API failed or returned empty — fall back to SSH
                    await _call_progress(_name, 0, 0, "connecting...")
                    sessions = await scan_remote(_name, _ssh_alias)
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
