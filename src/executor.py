"""
RemoteExecutor abstraction: run a command locally or via SSH with a
unified interface.  Eliminates the local/remote branching boilerplate
that was duplicated across tmux_manager, scanner, and fleet.
"""
from __future__ import annotations

import shlex
from typing import Protocol

from .config import FLEET_MACHINES, SSH_TIMEOUT, detect_local_machine
from .subprocess_utils import run_with_timeout

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
        return await run_with_timeout(cmd, timeout=timeout, input=input)


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

    def _build_ssh_cmd(self, remote_cmd: list[str]) -> list[str]:
        if self._is_windows:
            # PowerShell: space-join, no PATH prefix
            remote_str = " ".join(remote_cmd)
        else:
            # Unix: shlex-quote each token, prepend PATH export
            remote_str = self._path_prefix + " ".join(shlex.quote(t) for t in remote_cmd)
        return [
            "ssh",
            "-o", "BatchMode=yes",
            "-o", f"ConnectTimeout={SSH_TIMEOUT}",
            "-o", "StrictHostKeyChecking=no",
            self._ssh_alias,
            remote_str,
        ]

    def _build_ssh_cmd_raw(self, remote_shell_str: str) -> list[str]:
        """Build SSH command from a pre-assembled shell string (no per-token quoting).

        Use this when the caller has already composed the full remote command
        string (e.g. 'tmux list-sessions -F ...' or a compound ; chain).
        The PATH prefix is still prepended for Unix targets.
        """
        remote_str = self._path_prefix + remote_shell_str if not self._is_windows else remote_shell_str
        return [
            "ssh",
            "-o", "BatchMode=yes",
            "-o", f"ConnectTimeout={SSH_TIMEOUT}",
            "-o", "StrictHostKeyChecking=no",
            self._ssh_alias,
            remote_str,
        ]

    async def exec(
        self,
        cmd: list[str],
        *,
        timeout: float,
        input: bytes | None = None,
    ) -> tuple[int, bytes, bytes]:
        ssh_cmd = self._build_ssh_cmd(cmd)
        return await run_with_timeout(ssh_cmd, timeout=timeout, input=input)

    async def exec_shell(
        self,
        remote_shell_str: str,
        *,
        timeout: float,
        input: bytes | None = None,
    ) -> tuple[int, bytes, bytes]:
        """Run a pre-assembled shell command string on the remote machine."""
        ssh_cmd = self._build_ssh_cmd_raw(remote_shell_str)
        return await run_with_timeout(ssh_cmd, timeout=timeout, input=input)


def get_executor(machine: str) -> LocalExecutor | SSHExecutor:
    """Return a LocalExecutor if machine is the local host, else SSHExecutor."""
    local = detect_local_machine()
    if machine == local:
        return LocalExecutor()
    return SSHExecutor(machine)
