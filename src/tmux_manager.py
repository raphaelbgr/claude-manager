"""Tmux/psmux session management across fleet machines."""
import asyncio
import shlex
from dataclasses import dataclass, asdict
from .config import FLEET_MACHINES, detect_local_machine, SSH_TIMEOUT
from .mux_parser import parse_mux_output


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
    except Exception:
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
                *ssh_base, cmd_str,
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

    return _dicts_to_sessions(parsed, machine_name, is_local=False)


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

    try:
        if is_local:
            # Local: create session with -c and command as exec args
            cmd_parts = [mux, "new-session", "-d", "-s", name]
            if cwd:
                cmd_parts += ["-c", cwd]
            if command:
                cmd_parts.append(command)
            proc = await asyncio.create_subprocess_exec(
                *cmd_parts,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
            if proc.returncode != 0:
                return {"ok": False, "error": stderr.decode().strip()}
            return {"ok": True}
        else:
            # Remote: use create + send-keys approach (avoids quoting hell over SSH).
            # Step 1: Create empty detached session
            ssh_base = [
                "ssh",
                "-o", f"ConnectTimeout={SSH_TIMEOUT}",
                "-o", "BatchMode=yes",
                "-o", "StrictHostKeyChecking=no",
                ssh_alias,
            ]
            create_cmd = f"{mux} new-session -d -s {shlex.quote(name)}"
            proc = await asyncio.create_subprocess_exec(
                *ssh_base, create_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
            if proc.returncode != 0:
                err = stderr.decode().strip()
                if err:
                    return {"ok": False, "error": err}

            # Step 2: Send the command via send-keys (reliable, no quoting issues)
            if command:
                remote_os = info.get("os", "")
                cmd_to_send = command

                if remote_os == "win32":
                    # psmux on Windows runs cmd.exe inside sessions.
                    # - Don't convert paths (cmd uses C:\path, not /c/path)
                    # - Remove POSIX single quotes around paths (cmd doesn't use them)
                    # - Replace 'path' quoting with "path" for cmd.exe
                    import re
                    cmd_to_send = re.sub(r"'([^']*)'", r'"\1"', cmd_to_send)

                keys_cmd = f"{mux} send-keys -t {shlex.quote(name)} {shlex.quote(cmd_to_send)} Enter"
                proc = await asyncio.create_subprocess_exec(
                    *ssh_base, keys_cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                await asyncio.wait_for(proc.communicate(), timeout=15)

            return {"ok": True, "machine": machine, "session": name}
    except asyncio.TimeoutError:
        return {"ok": False, "error": "Timed out"}
    except OSError as exc:
        return {"ok": False, "error": str(exc)}


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
                return {"ok": False, "error": stderr.decode().strip()}
            return {"ok": True}
        else:
            remote_cmd = f"{mux} kill-session -t '{name}'"
            ssh_cmd = [
                "ssh",
                f"-o ConnectTimeout={SSH_TIMEOUT}",
                "-o BatchMode=yes",
                "-o StrictHostKeyChecking=no",
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
                return {"ok": False, "error": stderr.decode().strip()}
            return {"ok": True}
    except asyncio.TimeoutError:
        return {"ok": False, "error": "Timed out"}
    except OSError as exc:
        return {"ok": False, "error": str(exc)}
