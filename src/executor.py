"""
RemoteExecutor abstraction: run a command locally or via SSH with a
unified interface.  Eliminates the local/remote branching boilerplate
that was duplicated across tmux_manager, scanner, and fleet.
"""
from __future__ import annotations

import hashlib
import os
import shlex
import sys
import time
from typing import Protocol

from .config import FLEET_MACHINES, SSH_TIMEOUT, detect_local_machine
from .subprocess_utils import run_with_timeout, _win32_kwargs
from .tracking import tl, span


# Hardcoded native-OS PATH fallbacks so the daemon finds tmux/psmux/claude/git
# regardless of how it was started (GUI, LaunchAgent, systemd user service,
# Task Scheduler). Minimal launchd envs like PATH=/usr/bin:/bin:/usr/sbin:/sbin
# would otherwise break every local subprocess that relies on Homebrew / snap
# / user-site installs.
_LOCAL_PATH_FALLBACKS: dict[str, list[str]] = {
    "darwin": [
        "/opt/homebrew/bin",
        "/opt/homebrew/sbin",
        "/usr/local/bin",
        "/usr/local/sbin",
        "/usr/bin",
        "/bin",
        "/usr/sbin",
        "/sbin",
    ],
    "linux": [
        "/usr/local/bin",
        "/usr/local/sbin",
        "/usr/bin",
        "/bin",
        "/usr/sbin",
        "/sbin",
        "/snap/bin",
        os.path.expanduser("~/.local/bin"),
    ],
    "win32": [
        r"C:\Windows\System32",
        r"C:\Windows",
        r"C:\Windows\System32\WindowsPowerShell\v1.0",
        r"C:\Program Files\Git\bin",
        r"C:\Program Files\Git\cmd",
        r"C:\Program Files\PowerShell\7",
        os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WindowsApps"),
        os.path.expandvars(r"%USERPROFILE%\.local\bin"),
    ],
}


def _augmented_local_env() -> dict[str, str]:
    """Return a copy of os.environ with PATH augmented by native-OS fallbacks.

    Preserves the user's existing PATH entries (they take precedence) and
    appends the hardcoded fallbacks so missing Homebrew / snap / PowerShell
    paths are recovered when the daemon inherits a stripped launchd env.
    """
    env = os.environ.copy()
    key = sys.platform if sys.platform in _LOCAL_PATH_FALLBACKS else (
        "linux" if sys.platform.startswith("linux") else None
    )
    if not key:
        return env
    sep = ";" if key == "win32" else ":"
    current = env.get("PATH", "")
    existing = [p for p in current.split(sep) if p]
    seen = set(existing)
    for p in _LOCAL_PATH_FALLBACKS[key]:
        if p and p not in seen:
            existing.append(p)
            seen.add(p)
    env["PATH"] = sep.join(existing)
    return env


_CACHED_LOCAL_ENV: dict[str, str] | None = None


def local_env() -> dict[str, str]:
    """Cached accessor for the augmented local env (built once per process)."""
    global _CACHED_LOCAL_ENV
    if _CACHED_LOCAL_ENV is None:
        _CACHED_LOCAL_ENV = _augmented_local_env()
    return _CACHED_LOCAL_ENV


def _ssh_control_path(machine: str) -> str:
    """Return a stable, short, user-isolated ControlPath socket path.

    macOS enforces a 104-char limit on Unix socket paths; hashing the machine
    name keeps the path well under that limit regardless of alias length.
    """
    h = hashlib.sha256(machine.encode()).hexdigest()[:10]
    return f"/tmp/cm-ssh-{os.getuid()}-{h}"

# PATH prefix injected before commands on Unix SSH targets so that
# Homebrew (/opt/homebrew/bin), snap (/snap/bin), etc. are found in
# non-interactive shells.  Windows (PowerShell) targets get an empty
# string — 'export' is not a valid PowerShell keyword.
_SSH_PATH_PREFIX_UNIX = "export PATH=/opt/homebrew/bin:/usr/local/bin:/snap/bin:$PATH; "


def _path_prefix_for(machine: str) -> str:
    info = FLEET_MACHINES.get(machine, {})
    if info.get("os") == "win32":
        return ""
    return _SSH_PATH_PREFIX_UNIX


class RemoteExecutor(Protocol):
    machine: str
    is_local: bool

    async def exec(
        self,
        cmd: list[str],
        *,
        timeout: float,
        input: bytes | None = None,
    ) -> tuple[int, bytes, bytes]: ...


class LocalExecutor:
    """Runs cmd directly via run_with_timeout (no SSH wrapper)."""

    def __init__(self) -> None:
        self.machine: str = detect_local_machine() or "local"
        self.is_local: bool = True

    async def exec(
        self,
        cmd: list[str],
        *,
        timeout: float,
        input: bytes | None = None,
    ) -> tuple[int, bytes, bytes]:
        return await run_with_timeout(cmd, timeout=timeout, input=input, env=local_env())


class SSHExecutor:
    """Runs cmd on a remote machine via SSH.

    For Unix targets the PATH prefix is prepended to the remote command
    string so that tmux / psmux are found in non-interactive shells.
    For Windows (PowerShell) targets no prefix is added.

    cmd is treated as a list of tokens for the *remote* command.  The
    tokens are joined with shlex for Unix targets and with spaces for
    Windows targets (callers are responsible for PowerShell-safe strings).
    """

    def __init__(self, machine: str) -> None:
        self.machine = machine
        self.is_local: bool = False
        info = FLEET_MACHINES.get(machine, {})
        self._ssh_alias: str = info.get("ssh_alias", machine)
        self._is_windows: bool = info.get("os") == "win32"
        self._path_prefix: str = _path_prefix_for(machine)

    def _ssh_base_opts(self) -> list[str]:
        """Return shared SSH options including ControlMaster multiplexing."""
        ctl = _ssh_control_path(self.machine)
        return [
            "-o", "BatchMode=yes",
            "-o", f"ConnectTimeout={SSH_TIMEOUT}",
            "-o", "StrictHostKeyChecking=no",
            "-o", "ControlMaster=auto",
            "-o", f"ControlPath={ctl}",
            "-o", "ControlPersist=60s",
        ]

    def _build_ssh_cmd(self, remote_cmd: list[str]) -> list[str]:
        if self._is_windows:
            # PowerShell: space-join, no PATH prefix
            remote_str = " ".join(remote_cmd)
        else:
            # Unix: shlex-quote each token, prepend PATH export
            remote_str = self._path_prefix + " ".join(shlex.quote(t) for t in remote_cmd)
        return ["ssh", *self._ssh_base_opts(), self._ssh_alias, remote_str]

    def _build_ssh_cmd_raw(self, remote_shell_str: str) -> list[str]:
        """Build SSH command from a pre-assembled shell string (no per-token quoting).

        Use this when the caller has already composed the full remote command
        string (e.g. 'tmux list-sessions -F ...' or a compound ; chain).
        The PATH prefix is still prepended for Unix targets.
        """
        remote_str = self._path_prefix + remote_shell_str if not self._is_windows else remote_shell_str
        return ["ssh", *self._ssh_base_opts(), self._ssh_alias, remote_str]

    @staticmethod
    def shutdown_connections(machine: str) -> None:
        """Force-close the ControlMaster multiplexer socket for a machine.

        Runs `ssh -O exit` against the control socket.  Safe to call even if
        no master is running (ssh exits non-zero but causes no side effects).
        """
        import subprocess
        ctl = _ssh_control_path(machine)
        info = FLEET_MACHINES.get(machine, {})
        alias = info.get("ssh_alias", machine)
        subprocess.run(
            ["ssh", "-O", "exit", "-o", f"ControlPath={ctl}", alias],
            capture_output=True,
            **_win32_kwargs(),
        )

    async def exec(
        self,
        cmd: list[str],
        *,
        timeout: float,
        input: bytes | None = None,
    ) -> tuple[int, bytes, bytes]:
        # Route through exec_shell so argv calls share the pool-first /
        # subprocess-ssh-fallback path — no extra sshd-session per call on
        # Windows targets. Join per target shell; exec_shell re-applies the
        # Unix PATH prefix itself.
        if self._is_windows:
            shell_str = " ".join(cmd)
        else:
            shell_str = " ".join(shlex.quote(t) for t in cmd)
        return await self.exec_shell(shell_str, timeout=timeout, input=input)

    async def exec_shell(
        self,
        remote_shell_str: str,
        *,
        timeout: float,
        input: bytes | None = None,
    ) -> tuple[int, bytes, bytes]:
        """Run a pre-assembled shell command string on the remote machine.

        Tries the persistent asyncssh pool first (single long-lived connection
        per machine → no sshd-session spam, no per-call reconnect latency).
        Falls back to subprocess `ssh` if the pool is unavailable or the
        connection is in its backoff window. PATH prefix is applied for Unix
        targets just like the subprocess path.
        """
        remote_str = (
            self._path_prefix + remote_shell_str
            if not self._is_windows else remote_shell_str
        )
        _t0 = time.monotonic()
        _cmd_head = (remote_shell_str or "")[:200]
        # Prefer the pool — reuses a single connection for the whole app lifecycle.
        try:
            from .ssh_pool import default_pool, asyncssh as _asyncssh
            if _asyncssh is not None:
                pool = default_pool()
                with span("cm.ssh.exec", machine=self.machine,
                          cmd_head=_cmd_head, transport="pool") as s:
                    rc, out, err = await pool.run(self.machine, remote_str, timeout=timeout, input=input)
                    s.update(rc=rc, elapsed_ms=int((time.monotonic() - _t0) * 1000))
                    return rc, out, err
        except Exception as exc:
            # Silent fallback to subprocess on any pool failure (asyncssh missing,
            # backoff window, auth failure, etc). Logged at debug so it doesn't
            # fill production logs when a machine is genuinely offline.
            import logging as _logging
            _logging.getLogger("claude_manager.executor").debug(
                "exec_shell(%s): pool unavailable (%s), falling back to subprocess", self.machine, exc
            )
        ssh_cmd = self._build_ssh_cmd_raw(remote_shell_str)
        with span("cm.ssh.exec", machine=self.machine,
                  cmd_head=_cmd_head, transport="subprocess") as s:
            rc, out, err = await run_with_timeout(ssh_cmd, timeout=timeout, input=input)
            s.update(rc=rc, elapsed_ms=int((time.monotonic() - _t0) * 1000))
            return rc, out, err


def get_executor(machine: str) -> LocalExecutor | SSHExecutor:
    """Return a LocalExecutor if machine is the local host, else SSHExecutor."""
    local = detect_local_machine()
    if machine == local:
        return LocalExecutor()
    return SSHExecutor(machine)
