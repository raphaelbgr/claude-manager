"""Tmux/psmux session management across fleet machines."""
import asyncio
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from .config import FLEET_MACHINES, detect_local_machine, SSH_TIMEOUT


@dataclass
class TmuxSession:
    name: str
    machine: str
    created: str   # ISO 8601 string
    windows: int
    attached: bool
    is_local: bool


def _unix_to_iso(ts_str: str) -> str:
    """Convert Unix timestamp string to ISO 8601 UTC string."""
    try:
        ts = int(ts_str)
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
    except (ValueError, OSError):
        return ts_str


def _parse_tmux_lines(output: str, machine: str, is_local: bool) -> list[TmuxSession]:
    """Parse tmux list-sessions output into TmuxSession objects."""
    sessions = []
    for line in output.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("|")
        if len(parts) != 4:
            continue
        name, created_raw, windows_raw, attached_raw = parts
        try:
            windows = int(windows_raw)
        except ValueError:
            windows = 0
        sessions.append(TmuxSession(
            name=name,
            machine=machine,
            created=_unix_to_iso(created_raw),
            windows=windows,
            attached=(attached_raw.strip() == "1"),
            is_local=is_local,
        ))
    return sessions


async def list_local_tmux() -> list[TmuxSession]:
    """List all tmux sessions on the local machine."""
    machine = detect_local_machine()
    fmt = "#{session_name}|#{session_created}|#{session_windows}|#{session_attached}"
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

    return _parse_tmux_lines(stdout.decode(), machine, is_local=True)


async def list_remote_tmux(machine_name: str, ssh_alias: str, mux: str) -> list[TmuxSession]:
    """List tmux/psmux sessions on a remote machine via SSH."""
    fmt = "#{session_name}|#{session_created}|#{session_windows}|#{session_attached}"
    ssh_base = [
        "ssh",
        f"-o ConnectTimeout={SSH_TIMEOUT}",
        "-o BatchMode=yes",
        "-o StrictHostKeyChecking=no",
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

    if mux == "psmux":
        # Try same tmux-compatible format first, fall back to plain list
        rc, out = await _run_remote(f"psmux list-sessions -F '{fmt}'")
        if rc != 0 or not out.strip():
            rc, out = await _run_remote("psmux list-sessions")
            if rc != 0:
                return []
            # Plain psmux output: just session names, one per line
            sessions = []
            for line in out.strip().splitlines():
                line = line.strip()
                if line:
                    sessions.append(TmuxSession(
                        name=line,
                        machine=machine_name,
                        created="",
                        windows=0,
                        attached=False,
                        is_local=False,
                    ))
            return sessions
    else:
        rc, out = await _run_remote(f"tmux list-sessions -F '{fmt}'")
        if rc != 0:
            return []

    return _parse_tmux_lines(out, machine_name, is_local=False)


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
        ssh_alias = info.get("ssh_alias", machine_name)
        mux = info.get("mux", "tmux")
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

    cmd_parts = [mux, "new-session", "-d", "-s", name]
    if cwd:
        cmd_parts += ["-c", cwd]
    if command:
        cmd_parts.append(command)

    is_local = (machine == local_machine)

    try:
        if is_local:
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
            remote_cmd = " ".join(
                f"'{p}'" if " " in p else p for p in cmd_parts
            )
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
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=20)
            if proc.returncode != 0:
                return {"ok": False, "error": stderr.decode().strip()}
            return {"ok": True}
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
