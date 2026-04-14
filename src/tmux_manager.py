"""Tmux/psmux session management across fleet machines."""
import asyncio
import logging
import shlex
from dataclasses import dataclass, asdict
from .command_adapter import get_adapter
from .config import FLEET_MACHINES, detect_local_machine, SSH_TIMEOUT
from .executor import get_executor, SSHExecutor
from .mux_parser import parse_mux_output
from .subprocess_utils import run_with_timeout

log = logging.getLogger("claude_manager.tmux_manager")


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
        rc, stdout, _ = await run_with_timeout(
            ["tmux", "list-sessions", "-F", fmt],
            timeout=10,
        )
    except (FileNotFoundError, asyncio.TimeoutError):
        return []

    if rc != 0:
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
                    cwd=item.get("cwd", "") or "",
                ) for item in data]
    except Exception as exc:
        log.warning("list_remote_tmux(%s): api failed: %s", machine_name, exc)
        return []


async def list_remote_tmux(machine_name: str, ssh_alias: str, mux: str) -> list[TmuxSession]:
    """List tmux/psmux sessions on a remote machine via SSH."""
    fmt = "#{session_name}|#{session_created}|#{session_windows}|#{session_attached}|#{pane_current_path}"
    executor = SSHExecutor(machine_name)

    async def _run_remote(cmd_str: str) -> tuple[int, str]:
        try:
            rc, stdout, _ = await executor.exec_shell(cmd_str, timeout=15)
            return rc, stdout.decode()
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

    # psmux ignores -F on list-sessions but honors display-message, so the
    # cwd field is never populated via the list call. Enrich each session
    # with a single batched SSH call that runs display-message per session.
    if parsed and mux == "psmux" and any(not p.get("cwd") for p in parsed):
        names = [p["name"] for p in parsed]
        # Sentinel between outputs so we can re-split reliably even if a
        # path contains unusual characters. display-message prints the path
        # followed by a newline, so the sentinel ends up on its own line.
        sentinel = "__PSMUX_CWD_END__"
        parts = [
            f"psmux display-message -p -t {shlex.quote(n)} '#{{pane_current_path}}'; echo {sentinel}"
            for n in names
        ]
        rc2, out2 = await _run_remote("; ".join(parts))
        if rc2 == 0 and out2:
            chunks = out2.split(sentinel)
            for sess, chunk in zip(parsed, chunks):
                sess["cwd"] = chunk.strip().splitlines()[-1].strip() if chunk.strip() else ""

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
    adapter = get_adapter(machine)
    executor = get_executor(machine)

    try:
        if executor.is_local:
            # Step 1: Create detached session (no -c, psmux doesn't support it)
            _rc_create, _, stderr = await executor.exec(
                [mux, "new-session", "-d", "-s", name],
                timeout=15,
            )
            if _rc_create != 0:
                err = stderr.decode().strip()
                log.error("create_tmux_session(%s, %s): %s", machine, name, err)
                return {"ok": False, "error": err}

            # Step 2: cd into the working directory via send-keys
            if cwd:
                cd_cmd = adapter.cd_command(cwd)
                await executor.exec(
                    [mux, "send-keys", "-t", name, cd_cmd, "Enter"],
                    timeout=10,
                )

            # Step 3: Send the command via send-keys
            if command:
                await executor.exec(
                    [mux, "send-keys", "-t", name, command, "Enter"],
                    timeout=10,
                )

            log.info("create_tmux_session(%s, %s): ok (local, cwd=%s)", machine, name, cwd)
            return {"ok": True}
        else:
            assert isinstance(executor, SSHExecutor)

            async def _ssh_run(cmd_str: str) -> tuple[int, str]:
                _rc, _, _se = await executor.exec_shell(cmd_str, timeout=15)
                return _rc, _se.decode().strip()

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
        rc, stdout, _ = await run_with_timeout(cmd, timeout=5)
        if rc == 0:
            return _clean_pane_output(stdout.decode("utf-8", errors="replace"))
    except (asyncio.TimeoutError, OSError) as exc:
        log.warning("_capture_pane_local(%s, %s): %s", machine, session_name, exc)
    return ""


async def _capture_pane_via_ssh(machine: str, session_name: str, lines: int) -> str:
    """Capture pane via SSH (fallback when API unavailable)."""
    adapter = get_adapter(machine)
    executor = SSHExecutor(machine)

    if adapter.mux_type == "tmux":
        cmd_str = f"tmux capture-pane -t {shlex.quote(session_name)} -e -p -S -{lines}"
    else:
        cmd_str = f"psmux capture-pane -t {shlex.quote(session_name)} -p"

    try:
        rc, stdout, _ = await executor.exec_shell(cmd_str, timeout=10)
        if rc == 0:
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
        rc, _, _ = await run_with_timeout(
            ["tmux", "pipe-pane", "-t", session_name, f"cat >> {shlex.quote(output_path)}"],
            timeout=5,
        )
        return rc == 0
    except (asyncio.TimeoutError, OSError) as exc:
        log.warning("start_pipe_pane(%s): %s", session_name, exc)
        return False


async def stop_pipe_pane(session_name: str) -> bool:
    """Stop tmux pipe-pane for LOCAL tmux session.

    Running pipe-pane with no command stops the pipe.
    """
    try:
        rc, _, _ = await run_with_timeout(
            ["tmux", "pipe-pane", "-t", session_name],
            timeout=5,
        )
        return rc == 0
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
    executor = get_executor(machine)

    try:
        if executor.is_local:
            rc, _, stderr = await executor.exec(
                [mux, "kill-session", "-t", name],
                timeout=10,
            )
            if rc != 0:
                err = stderr.decode().strip()
                log.warning("kill_tmux_session(%s, %s): %s", machine, name, err)
                return {"ok": False, "error": err}
            return {"ok": True}
        else:
            assert isinstance(executor, SSHExecutor)
            adapter = get_adapter(machine)
            rc, _, stderr = await executor.exec_shell(
                adapter.mux_kill_session(name),
                timeout=15,
            )
            if rc != 0:
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
