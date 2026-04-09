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
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psutil


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
    name: str = ""            # session name set by /rename
    cpu_percent: float = 0.0  # CPU usage if active (0.0 if idle/not measured)

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
    modified = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat()
    file_size = stat.st_size

    slug = ""
    cwd = ""
    first_message = ""
    line_count = 0

    with file_path.open("r", encoding="utf-8", errors="replace") as fh:
        all_lines = fh.readlines()

    line_count = sum(1 for ln in all_lines if ln.strip())

    for raw in all_lines[:50]:
        raw = raw.strip()
        if not raw:
            continue
        try:
            d = json.loads(raw)
        except json.JSONDecodeError:
            continue

        if d.get("type") == "user" and d.get("sessionId"):
            if not slug:
                slug = d.get("slug", "")
            if not cwd:
                cwd = d.get("cwd", "")

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
            break

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
    all_sessions.sort(key=lambda s: s.modified, reverse=True)
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
                mod = datetime.datetime.fromtimestamp(stat.st_mtime, tz=datetime.timezone.utc).isoformat()
                slug = ''; cwd = ''; summary = ''; line_count = 0
                with open(jf, encoding='utf-8', errors='replace') as fh:
                    all_lines = fh.readlines()
                line_count = sum(1 for l in all_lines if l.strip())
                for raw in all_lines[:50]:
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        d = json.loads(raw)
                    except Exception:
                        continue
                    if d.get('type') == 'user' and d.get('sessionId'):
                        if not slug: slug = d.get('slug', '')
                        if not cwd: cwd = d.get('cwd', '')
                    if not summary and d.get('type') == 'user':
                        c = d.get('message', {}).get('content', '')
                        if isinstance(c, list):
                            for b in c:
                                if isinstance(b, dict) and b.get('type') == 'text':
                                    summary = (b.get('text') or '')[:120]; break
                        elif isinstance(c, str):
                            summary = c[:120]
                    if slug and summary:
                        break
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
                    'name': session_names.get(sid, ''),
                })
            except Exception:
                pass

results.sort(key=lambda s: s['modified'], reverse=True)
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
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
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
                        name=item.get("name", ""),
                    )
                    sessions.append(s)
                return sessions
    except Exception:
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
    try:
        # Pipe script via stdin to avoid shell quoting issues with SSH
        proc = await asyncio.wait_for(
            asyncio.create_subprocess_exec(
                "ssh",
                "-o", "BatchMode=yes",
                "-o", "ConnectTimeout=5",
                "-o", "StrictHostKeyChecking=no",
                ssh_alias,
                "python3", "-",
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            ),
            timeout=30,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(input=script.encode("utf-8")),
            timeout=30,
        )
    except asyncio.TimeoutError:
        return []
    except Exception:
        return []

    if proc.returncode != 0:
        return []

    try:
        raw_list: list[dict] = json.loads(stdout.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
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
                name=item.get("name", ""),
            )
            sessions.append(sess)
        except (KeyError, TypeError):
            continue

    return sessions


# ---------------------------------------------------------------------------
# Combined scan: local + all online remote machines
# ---------------------------------------------------------------------------

async def scan_all(
    local_machine: str | None,
    fleet: dict[str, dict[str, Any]],
) -> list[ClaudeSession]:
    """
    Run local scan and remote scans in parallel via asyncio.gather.

    Args:
        local_machine: Fleet machine name for this host, or None.
        fleet:         Fleet health dict from fleet.discover_fleet().
                       Only machines with online=True are scanned remotely.

    Returns all sessions sorted by modified descending.
    """
    from .config import FLEET_MACHINES

    tasks: list[asyncio.Task] = []
    labels: list[str] = []

    # Local scan (run in executor to avoid blocking event loop on large dirs)
    loop = asyncio.get_running_loop()

    async def _local() -> list[ClaudeSession]:
        return await loop.run_in_executor(
            None,
            lambda: scan_local(machine=local_machine or "local"),
        )

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
                sessions = await scan_remote_via_api(_name, _ip, _port)
                if not sessions:
                    # API failed or returned empty — fall back to SSH
                    sessions = await scan_remote(_name, _ssh_alias)
                return sessions
            tasks.append(asyncio.ensure_future(_api_with_ssh_fallback()))
        else:
            tasks.append(asyncio.ensure_future(scan_remote(name, info["ssh_alias"])))
        labels.append(name)

    results = await asyncio.gather(*tasks, return_exceptions=True)

    all_sessions: list[ClaudeSession] = []
    for result in results:
        if isinstance(result, Exception):
            continue
        if isinstance(result, list):
            all_sessions.extend(result)

    all_sessions.sort(key=lambda s: s.modified, reverse=True)
    return all_sessions
