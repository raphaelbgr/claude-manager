"""Tmux/psmux session management across fleet machines."""
import asyncio
import logging
import shlex
from dataclasses import dataclass, asdict
from .command_adapter import get_adapter
from .config import FLEET_MACHINES, detect_local_machine, SSH_TIMEOUT
from .mux_parser import parse_mux_output

log = logging.getLogger("claude_manager.tmux_manager")

# PATH prefix for SSH commands to Unix machines — ensures tmux is found in
# non-interactive shells (macOS Homebrew at /opt/homebrew/bin, etc.)
# NOT used for Windows targets (PowerShell doesn't understand 'export').
_SSH_PATH_PREFIX_UNIX = "export PATH=/opt/homebrew/bin:/usr/local/bin:/snap/bin:$PATH; "


def _ssh_path_prefix(machine: str) -> str:
    """Return PATH prefix for SSH commands, empty for Windows targets."""
    info = FLEET_MACHINES.get(machine, {})
    if info.get("os") == "win32":
        return ""
    return _SSH_PATH_PREFIX_UNIX


@dataclass
class TmuxSession:
    name: str
    machine: str
    created: str   # ISO 8601 string
    windows: int
    attached: bool
    is_local: bool
    cwd: str = ""  # pane current directory (empty if unavailable)

    def to_dict(self) -> dict:
        return asdict(self)


def _dicts_to_sessions(parsed: list[dict], machine: str, is_local: bool) -> list[TmuxSession]:
    """Convert parse_mux_output dicts into TmuxSession objects."""
    sessions = []
    for d in parsed:
        sessions.append(TmuxSession(
            name=d["name"],
            machine=machine,
            created=d.get("created") or "",
            windows=d.get("windows", 0),
            attached=d.get("attached", False),
            is_local=is_local,
            cwd=d.get("cwd") or "",
        ))
    return sessions


async def list_local_tmux() -> list[TmuxSession]:
    """List all tmux sessions on the local machine."""
    machine = detect_local_machine()
    fmt = "#{session_name}|#{session_created}|#{session_windows}|#{session_attached}|#{pane_current_path}"
    try:
        proc = await asyncio.create_subprocess_exec(
            "tmux", "list-sessions", "-F", fmt,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)
    except (FileNotFoundError, asyncio.TimeoutError):
        return []

    if proc.returncode != 0:
        # "no server running" or similar — not an error we surface
        return []

    return _dicts_to_sessions(parse_mux_output(stdout.decode()), machine, is_local=True)


async def list_remote_tmux_via_api(machine_name: str, ip: str, dispatch_port: int) -> list[TmuxSession]:
    """Query dispatch daemon's /tmux endpoint."""
    import aiohttp
    url = f"http://{ip}:{dispatch_port}/tmux"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json()
                return [TmuxSession(
                    name=item.get("name", ""),
                    machine=machine_name,
                    created=item.get("created", ""),
                    windows=item.get("windows", 0),
                    attached=item.get("attached", False),
                    is_local=False,
                ) for item in data]
    except Exception as exc:
        log.warning("list_remote_tmux(%s): api failed: %s", machine_name, exc)
        return []


async def list_remote_tmux(machine_name: str, ssh_alias: str, mux: str) -> list[TmuxSession]:
    """List tmux/psmux sessions on a remote machine via SSH."""
    fmt = "#{session_name}|#{session_created}|#{session_windows}|#{session_attached}|#{pane_current_path}"
    ssh_base = [
        "ssh",
        "-o", f"ConnectTimeout={SSH_TIMEOUT}",
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=no",
        ssh_alias,
    ]

    async def _run_remote(cmd_str: str) -> tuple[int, str]:
        try:
            proc = await asyncio.create_subprocess_exec(
                *ssh_base, _ssh_path_prefix(machine_name) + cmd_str,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
            return proc.returncode, stdout.decode()
        except (asyncio.TimeoutError, OSError):
            return -1, ""

    # Try with -F format string first (works on tmux; psmux ignores it but may still list)
    rc, out = await _run_remote(f"{mux} list-sessions -F '{fmt}'")
    parsed = parse_mux_output(out) if rc == 0 else []

    # If empty and this is psmux, retry without -F (psmux plain-text output)
    if not parsed and mux == "psmux":
        rc, out = await _run_remote("psmux list-sessions")
        if rc != 0 or not out.strip():
            return []
        parsed = parse_mux_output(out)

    sessions = _dicts_to_sessions(parsed, machine_name, is_local=False)
    log.info("list_remote_tmux(%s): %d sessions", machine_name, len(sessions))
    return sessions


async def list_all_tmux(local_machine: str, fleet_status: dict) -> list[TmuxSession]:
    """
    List tmux sessions from local machine and all online remotes in parallel.

    Args:
        local_machine: Name of the local machine (as in FLEET_MACHINES keys).
        fleet_status: Dict of machine_name → {"online": bool, ...} from fleet health check.

    Returns:
        Sorted list of TmuxSession objects (by machine, then name).
    """
    tasks: list[asyncio.coroutine] = [list_local_tmux()]

    for machine_name, info in FLEET_MACHINES.items():
        if machine_name == local_machine:
            continue
        status = fleet_status.get(machine_name, {})
        if not status.get("online", False):
            continue
        dispatch_port = info.get("dispatch_port")
        ssh_alias = info.get("ssh_alias", machine_name)
        mux = info.get("mux", "tmux")
        if dispatch_port:
            # API first, SSH fallback if empty/failed
            async def _api_with_ssh_fallback(
                _name=machine_name, _ip=info["ip"], _port=dispatch_port,
                _alias=ssh_alias, _mux=mux,
            ):
                sessions = await list_remote_tmux_via_api(_name, _ip, _port)
                if not sessions:
                    sessions = await list_remote_tmux(_name, _alias, _mux)
                return sessions
            tasks.append(_api_with_ssh_fallback())
        else:
            tasks.append(list_remote_tmux(machine_name, ssh_alias, mux))

    results = await asyncio.gather(*tasks, return_exceptions=True)

    all_sessions: list[TmuxSession] = []
    for result in results:
        if isinstance(result, list):
            all_sessions.extend(result)
        # Silently skip exceptions — individual machine failures are non-fatal

    all_sessions.sort(key=lambda s: (s.machine, s.name))
    return all_sessions


async def create_tmux_session(
    machine: str,
    name: str,
    cwd: str | None = None,
    command: str | None = None,
) -> dict:
    """
    Create a new detached tmux/psmux session.

    Returns:
        {"ok": True} on success, {"ok": False, "error": str} on failure.
    """
    info = FLEET_MACHINES.get(machine, {})
    mux = info.get("mux", "tmux")
    ssh_alias = info.get("ssh_alias", machine)
    local_machine = detect_local_machine()
    is_local = (machine == local_machine)

    adapter = get_adapter(machine)

    try:
        if is_local:
            # Step 1: Create detached session (no -c, psmux doesn't support it)
            cmd_parts = [mux, "new-session", "-d", "-s", name]
            proc = await asyncio.create_subprocess_exec(
                *cmd_parts,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
            if proc.returncode != 0:
                err = stderr.decode().strip()
                log.error("create_tmux_session(%s, %s): %s", machine, name, err)
                return {"ok": False, "error": err}

            # Step 2: cd into the working directory via send-keys
            if cwd:
                cd_cmd = adapter.cd_command(cwd)
                sk = [mux, "send-keys", "-t", name, cd_cmd, "Enter"]
                proc = await asyncio.create_subprocess_exec(
                    *sk, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                )
                await asyncio.wait_for(proc.communicate(), timeout=10)

            # Step 3: Send the command via send-keys
            if command:
                sk = [mux, "send-keys", "-t", name, command, "Enter"]
                proc = await asyncio.create_subprocess_exec(
                    *sk, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                )
                await asyncio.wait_for(proc.communicate(), timeout=10)

            log.info("create_tmux_session(%s, %s): ok (local, cwd=%s)", machine, name, cwd)
            return {"ok": True}
        else:
            # Remote: use create + send-keys approach (avoids quoting hell over SSH).
            ssh_base = [
                "ssh",
                "-o", f"ConnectTimeout={SSH_TIMEOUT}",
                "-o", "BatchMode=yes",
                "-o", "StrictHostKeyChecking=no",
                ssh_alias,
            ]

            async def _ssh_run(cmd_str: str) -> tuple[int, str]:
                p = await asyncio.create_subprocess_exec(
                    *ssh_base, _ssh_path_prefix(machine) + cmd_str,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                )
                _, se = await asyncio.wait_for(p.communicate(), timeout=15)
                return p.returncode, se.decode().strip()

            # Step 1: Create empty detached session.
            # NOTE: psmux returns rc=0 even when the session already exists
            # (only writes "session 'name' already exists" to stderr). Detect
            # that case explicitly and return an error so the UI sees it.
            rc, err = await _ssh_run(adapter.mux_create_session(name))
            err_lower = err.lower() if err else ""
            if rc != 0 or "already exists" in err_lower or "duplicate" in err_lower:
                log.error("create_tmux_session(%s, %s): %s", machine, name, err or "(no stderr)")
                return {"ok": False, "error": err or f"failed to create session '{name}'"}

            # Step 2: cd into working directory via send-keys
            if cwd:
                cd_cmd = adapter.cd_command(cwd)
                await _ssh_run(adapter.mux_send_keys(name, cd_cmd))

            # Step 3: Send the command via send-keys
            if command:
                await _ssh_run(adapter.mux_send_keys(name, command))

            log.info("create_tmux_session(%s, %s): ok (remote, cwd=%s)", machine, name, cwd)
            return {"ok": True, "machine": machine, "session": name}
    except asyncio.TimeoutError:
        log.error("create_tmux_session(%s, %s): timed out", machine, name)
        return {"ok": False, "error": "Timed out"}
    except OSError as exc:
        log.error("create_tmux_session(%s, %s): %s", machine, name, exc)
        return {"ok": False, "error": str(exc)}


import re as _re

# Matches non-color ANSI: cursor movement, OSC, charset switching — but NOT SGR (color) codes
_ANSI_CONTROL_RE = _re.compile(
    r'\x1b\[[0-9;]*[A-HJKSTfhln]'   # cursor movement, erase, scroll (NOT 'm' = SGR/color)
    r'|\x1b\].*?\x07'                # OSC sequences (title, etc.)
    r'|\x1b[()][A-Z0-9]'             # charset switching
    r'|\x1b\[[\?][0-9;]*[hl]'        # DEC private mode set/reset
)


def _clean_pane_output(text: str) -> str:
    """Remove non-color control codes, keep SGR (color) sequences. Trim trailing blanks."""
    cleaned = _ANSI_CONTROL_RE.sub('', text)
    lines = [line.rstrip() for line in cleaned.splitlines()]
    while lines and not lines[-1]:
        lines.pop()
    return '\n'.join(lines)


async def capture_pane(machine: str, session_name: str, lines: int = 50) -> str:
    """Capture current pane content without TTY allocation.

    Strategy: API-first (dispatch daemon), SSH fallback, local exec for local machine.
    API runs locally on the target machine so it has full PATH and -e flag support.
    """
    local_machine = detect_local_machine()
    is_local = (machine == local_machine)

    # --- Local machine: direct exec ---
    if is_local:
        return await _capture_pane_local(machine, session_name, lines)

    # --- Remote machine: try API first (dispatch daemon) ---
    info = FLEET_MACHINES.get(machine, {})
    dispatch_port = info.get("dispatch_port")
    ip = info.get("ip", "")

    if dispatch_port and ip:
        result = await _capture_pane_via_api(ip, dispatch_port, session_name, lines)
        if result:
            return result
        log.debug("capture_pane(%s, %s): API failed, trying SSH", machine, session_name)

    # --- SSH fallback ---
    return await _capture_pane_via_ssh(machine, session_name, lines)


async def _capture_pane_via_api(ip: str, port: int, session_name: str, lines: int) -> str:
    """Capture pane via dispatch daemon API (POST /tmux/capture-pane)."""
    import aiohttp
    url = f"http://{ip}:{port}/tmux/capture-pane"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url,
                json={"session_name": session_name, "lines": lines},
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data.get("ok") and data.get("content"):
                        return _clean_pane_output(data["content"])
    except Exception as exc:
        log.debug("_capture_pane_via_api(%s:%d, %s): %s", ip, port, session_name, exc)
    return ""


async def _capture_pane_local(machine: str, session_name: str, lines: int) -> str:
    """Capture pane on the local machine via direct exec."""
    adapter = get_adapter(machine)
    if adapter.mux_type == "tmux":
        cmd = ["tmux", "capture-pane", "-t", session_name, "-e", "-p", "-S", f"-{lines}"]
    else:
        cmd = ["psmux", "capture-pane", "-t", session_name, "-p"]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        if proc.returncode == 0:
            return _clean_pane_output(stdout.decode("utf-8", errors="replace"))
    except (asyncio.TimeoutError, OSError) as exc:
        log.warning("_capture_pane_local(%s, %s): %s", machine, session_name, exc)
    return ""


async def _capture_pane_via_ssh(machine: str, session_name: str, lines: int) -> str:
    """Capture pane via SSH (fallback when API unavailable)."""
    adapter = get_adapter(machine)
    info = FLEET_MACHINES.get(machine, {})
    ssh_alias = info.get("ssh_alias", machine)

    if adapter.mux_type == "tmux":
        cmd_str = f"tmux capture-pane -t {shlex.quote(session_name)} -e -p -S -{lines}"
    else:
        cmd_str = f"psmux capture-pane -t {shlex.quote(session_name)} -p"

    ssh_cmd = [
        "ssh",
        "-o", f"ConnectTimeout={SSH_TIMEOUT}",
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=no",
        ssh_alias,
        _ssh_path_prefix(machine) + cmd_str,
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *ssh_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        if proc.returncode == 0:
            return _clean_pane_output(stdout.decode("utf-8", errors="replace"))
    except (asyncio.TimeoutError, OSError) as exc:
        log.warning("_capture_pane_via_ssh(%s, %s): %s", machine, session_name, exc)
    return ""


async def start_pipe_pane(session_name: str, output_path: str) -> bool:
    """Start tmux pipe-pane for LOCAL tmux session only.

    Pipes pane output to a file for real-time streaming.
    Returns True on success.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "tmux", "pipe-pane", "-t", session_name,
            f"cat >> {shlex.quote(output_path)}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await asyncio.wait_for(proc.communicate(), timeout=5)
        return proc.returncode == 0
    except (asyncio.TimeoutError, OSError) as exc:
        log.warning("start_pipe_pane(%s): %s", session_name, exc)
        return False


async def stop_pipe_pane(session_name: str) -> bool:
    """Stop tmux pipe-pane for LOCAL tmux session.

    Running pipe-pane with no command stops the pipe.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "tmux", "pipe-pane", "-t", session_name,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await asyncio.wait_for(proc.communicate(), timeout=5)
        return proc.returncode == 0
    except (asyncio.TimeoutError, OSError) as exc:
        log.warning("stop_pipe_pane(%s): %s", session_name, exc)
        return False


async def kill_tmux_session(machine: str, name: str) -> dict:
    """
    Kill a tmux/psmux session by name.

    Returns:
        {"ok": True} on success, {"ok": False, "error": str} on failure.
    """
    info = FLEET_MACHINES.get(machine, {})
    mux = info.get("mux", "tmux")
    ssh_alias = info.get("ssh_alias", machine)
    local_machine = detect_local_machine()
    is_local = (machine == local_machine)

    kill_cmd = [mux, "kill-session", "-t", name]

    try:
        if is_local:
            proc = await asyncio.create_subprocess_exec(
                *kill_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)
            if proc.returncode != 0:
                err = stderr.decode().strip()
                log.warning("kill_tmux_session(%s, %s): %s", machine, name, err)
                return {"ok": False, "error": err}
            return {"ok": True}
        else:
            adapter = get_adapter(machine)
            remote_cmd = _ssh_path_prefix(machine) + adapter.mux_kill_session(name)
            ssh_cmd = [
                "ssh",
                "-o", f"ConnectTimeout={SSH_TIMEOUT}",
                "-o", "BatchMode=yes",
                "-o", "StrictHostKeyChecking=no",
                ssh_alias,
                remote_cmd,
            ]
            proc = await asyncio.create_subprocess_exec(
                *ssh_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
            if proc.returncode != 0:
                err = stderr.decode().strip()
                log.warning("kill_tmux_session(%s, %s): %s", machine, name, err)
                return {"ok": False, "error": err}
            return {"ok": True}
    except asyncio.TimeoutError:
        log.warning("kill_tmux_session(%s, %s): timed out", machine, name)
        return {"ok": False, "error": "Timed out"}
    except OSError as exc:
        log.error("kill_tmux_session(%s, %s): %s", machine, name, exc)
        return {"ok": False, "error": str(exc)}
