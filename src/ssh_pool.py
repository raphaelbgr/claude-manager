"""Persistent asyncssh connection pool — one connection per fleet machine
for the lifetime of the app.

Why: the previous subprocess-`ssh` approach spawned a new ssh process (and on
Windows a new sshd-session) on every scan tick / tmux list / command run.
That flooded Windows targets with hundreds of sshd processes per hour and
burned reconnect latency on every poll. Unix mitigates it with ControlMaster
sockets, but Windows OpenSSH ignores ControlMaster entirely.

This module keeps exactly one asyncssh connection open per machine, reuses
it for every non-interactive command, auto-reconnects with backoff when the
link drops, and shuts down cleanly on app exit so nothing leaks.

Interactive terminal launches (the user-facing `ssh host -t …` windows) are
NOT routed through this pool — those intentionally spawn their own subprocess
so the user's terminal emulator owns the PTY.
"""
from __future__ import annotations

import asyncio
import logging
import pathlib
import shlex
import time
from typing import Optional

try:
    import asyncssh
except ImportError:  # pragma: no cover — dep is pinned in pyproject.toml
    asyncssh = None  # type: ignore

from .config import FLEET_MACHINES, SSH_TIMEOUT
from .tracking import tl

log = logging.getLogger("claude_manager.ssh_pool")


class _MachineConn:
    """Single-machine connection holder with lock-serialised (re)connect."""

    def __init__(self, machine: str) -> None:
        self.machine = machine
        info = FLEET_MACHINES.get(machine, {})
        self._host: str = info.get("ssh_alias") or info.get("ip") or machine
        self._is_windows: bool = info.get("os") == "win32"
        self._conn: Optional["asyncssh.SSHClientConnection"] = None
        self._lock = asyncio.Lock()
        self._last_fail_ts: float = 0.0
        self._fail_backoff: float = 1.0  # seconds, doubles up to 30s on repeat failure

    async def _open(self) -> "asyncssh.SSHClientConnection":
        """Open a fresh asyncssh connection. Honours ~/.ssh/config via known_hosts None
        + agent forwarding. BatchMode-equivalent: no password prompts."""
        # known_hosts=None disables strict host key checking; matches the
        # existing subprocess-ssh flags (StrictHostKeyChecking=no). This is
        # LAN-only traffic on a home fleet, so acceptable. Upgrade to a known
        # hosts file later if public-net SSH is ever added.
        return await asyncio.wait_for(
            asyncssh.connect(
                self._host,
                known_hosts=None,
                client_keys=[str(pathlib.Path.home() / ".ssh" / "id_rsa")],
                connect_timeout=SSH_TIMEOUT,
            ),
            timeout=SSH_TIMEOUT + 2,
        )

    async def get(self) -> "asyncssh.SSHClientConnection":
        """Return a live connection, reconnecting if the previous one is dead.

        Backoff: doubles 1→2→4→8→16→30s on repeat failure to avoid hammering
        an offline machine during a scan storm.
        """
        async with self._lock:
            # Fast path: already connected and healthy.
            if self._conn is not None and not self._conn.is_closed():
                return self._conn

            now = asyncio.get_event_loop().time()
            if self._last_fail_ts and (now - self._last_fail_ts) < self._fail_backoff:
                tl.event("cm.ssh.pool.backoff",
                         machine=self.machine,
                         backoff_s=round(self._fail_backoff, 2))
                raise ConnectionError(
                    f"{self.machine}: in backoff window ({self._fail_backoff:.1f}s), skipping"
                )

            tl.event("cm.ssh.pool.connect.start", machine=self.machine, host=(self._host or "")[:120])
            _t0 = time.monotonic()
            try:
                self._conn = await self._open()
                self._fail_backoff = 1.0  # reset on success
                log.info("ssh_pool: opened connection to %s (%s)", self.machine, self._host)
                tl.event("cm.ssh.pool.connect.ok",
                         machine=self.machine,
                         elapsed_ms=int((time.monotonic() - _t0) * 1000))
                return self._conn
            except Exception as exc:
                self._last_fail_ts = now
                self._fail_backoff = min(self._fail_backoff * 2, 30.0)
                log.warning(
                    "ssh_pool: connect to %s (%s) failed: %s — next retry in %.1fs",
                    self.machine, self._host, exc, self._fail_backoff,
                )
                tl.event("cm.ssh.pool.connect.err",
                         machine=self.machine,
                         err=str(exc)[:200],
                         next_backoff_s=round(self._fail_backoff, 2),
                         elapsed_ms=int((time.monotonic() - _t0) * 1000))
                self._conn = None
                raise

    async def run(
        self,
        cmd: str,
        *,
        timeout: float = 15,
        input: bytes | None = None,
    ) -> tuple[int, bytes, bytes]:
        """Run a shell command on the remote machine, reusing the cached connection.

        Returns (returncode, stdout, stderr) — matches the shape returned by
        subprocess_utils.run_with_timeout so callers can drop-in swap.
        """
        conn = await self.get()
        try:
            result = await asyncio.wait_for(
                conn.run(
                    cmd,
                    input=input.decode() if isinstance(input, bytes) else input,
                    check=False,
                ),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            # Don't kill the whole connection for one slow command.
            raise
        except Exception as exc:
            # Connection dropped mid-run — invalidate so next call reconnects.
            log.warning("ssh_pool: run on %s failed, marking conn dead: %s", self.machine, exc)
            try:
                if self._conn is not None:
                    self._conn.close()
            except Exception:
                pass
            self._conn = None
            raise

        stdout = result.stdout.encode() if isinstance(result.stdout, str) else (result.stdout or b"")
        stderr = result.stderr.encode() if isinstance(result.stderr, str) else (result.stderr or b"")
        return (result.returncode or 0, stdout, stderr)

    async def close(self) -> None:
        async with self._lock:
            if self._conn is not None and not self._conn.is_closed():
                try:
                    self._conn.close()
                    await asyncio.wait_for(self._conn.wait_closed(), timeout=3)
                except Exception:
                    pass
            self._conn = None


class SSHPool:
    """App-scoped pool: 1 persistent SSH connection per fleet machine."""

    def __init__(self) -> None:
        self._conns: dict[str, _MachineConn] = {}
        self._enabled = asyncssh is not None

    @property
    def enabled(self) -> bool:
        return self._enabled

    def _get_holder(self, machine: str) -> _MachineConn:
        h = self._conns.get(machine)
        if h is None:
            h = _MachineConn(machine)
            self._conns[machine] = h
        return h

    async def run(
        self,
        machine: str,
        cmd: str,
        *,
        timeout: float = 15,
        input: bytes | None = None,
    ) -> tuple[int, bytes, bytes]:
        if not self._enabled:
            raise RuntimeError("asyncssh not installed; SSH pool disabled")
        return await self._get_holder(machine).run(cmd, timeout=timeout, input=input)

    async def shutdown(self) -> None:
        """Close every open connection. Called from aiohttp on_shutdown."""
        await asyncio.gather(
            *(h.close() for h in self._conns.values()),
            return_exceptions=True,
        )
        self._conns.clear()
        log.info("ssh_pool: all connections closed")


# Module-level singleton so SSHExecutor can reach the pool without threading
# an app handle through every call site. Populated on first use; closed via
# shutdown_default() from the app's on_cleanup hook.
_DEFAULT_POOL: SSHPool | None = None


def default_pool() -> SSHPool:
    global _DEFAULT_POOL
    if _DEFAULT_POOL is None:
        _DEFAULT_POOL = SSHPool()
    return _DEFAULT_POOL


async def shutdown_default() -> None:
    global _DEFAULT_POOL
    if _DEFAULT_POOL is not None:
        await _DEFAULT_POOL.shutdown()
        _DEFAULT_POOL = None


def build_remote_cmd(machine: str, cmd_tokens: list[str], path_prefix: str = "") -> str:
    """Assemble a remote shell string — Unix: PATH prefix + shlex-quoted tokens,
    Windows (PowerShell): space-joined tokens as-is. Mirrors SSHExecutor behaviour
    so callers can pass the same cmd shape through either transport."""
    info = FLEET_MACHINES.get(machine, {})
    if info.get("os") == "win32":
        return " ".join(cmd_tokens)
    return path_prefix + " ".join(shlex.quote(t) for t in cmd_tokens)
