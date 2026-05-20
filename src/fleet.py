"""
Fleet health discovery for claude-manager.

Checks each machine via HTTP /health endpoint (claude-dispatch daemon)
and falls back to SSH echo if the daemon is unreachable.
"""
from __future__ import annotations

import asyncio
import logging
import subprocess
import sys
import time
from typing import Any

import aiohttp

from .config import FLEET_MACHINES, SSH_TIMEOUT
from .executor import SSHExecutor
from .subprocess_utils import run_with_timeout
from .tracking import tl

log = logging.getLogger("claude_manager.fleet")


async def check_machine_health(name: str, info: dict[str, Any]) -> dict[str, Any]:
    """
    Check health of a single fleet machine.

    Strategy:
      1. HTTP GET http://<ip>:44730/health  (3 s timeout)
      2. If dispatch_port is None or HTTP fails → SSH `echo ok`

    Returns a dict:
      {
        "name":        str,
        "online":      bool,
        "os":          str,
        "ip":          str,
        "method":      "http" | "ssh" | "unreachable",
        "health_data": dict | None,
      }
    """
    base: dict[str, Any] = {
        "name": name,
        "online": False,
        "os": info["os"],
        "ip": info["ip"],
        "method": "unreachable",
        "health_data": None,
    }

    port = info.get("dispatch_port")
    _t0 = time.monotonic()

    # --- HTTP probe ---
    if port is not None:
        url = f"http://{info['ip']}:{port}/health"
        try:
            timeout = aiohttp.ClientTimeout(total=SSH_TIMEOUT)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url) as resp:
                    if resp.status == 200:
                        try:
                            data = await resp.json(content_type=None)
                        except Exception:
                            data = {"raw": await resp.text()}
                        base.update(
                            online=True,
                            method="http",
                            health_data=data,
                        )
                        log.info("check_machine_health(%s): online=True via http", name)
                        tl.event("cm.fleet.health.ok",
                                 machine=name, transport="http",
                                 elapsed_ms=int((time.monotonic() - _t0) * 1000))
                        return base
        except Exception:
            pass  # fall through to SSH

    # --- SSH probe ---
    try:
        executor = SSHExecutor(name)
        rc, stdout, _ = await executor.exec(["echo", "ok"], timeout=SSH_TIMEOUT + 2)
        if rc == 0 and b"ok" in stdout:
            base.update(online=True, method="ssh", health_data={"ssh": "ok"})
            log.info("check_machine_health(%s): online=True via ssh", name)
            tl.event("cm.fleet.health.ok",
                     machine=name, transport="ssh",
                     elapsed_ms=int((time.monotonic() - _t0) * 1000))
            return base
    except Exception as exc:
        tl.event("cm.fleet.health.err",
                 machine=name, transport="ssh",
                 err=str(exc)[:200],
                 elapsed_ms=int((time.monotonic() - _t0) * 1000))
        log.info("check_machine_health(%s): online=False", name)
        return base

    log.info("check_machine_health(%s): online=False", name)
    tl.event("cm.fleet.health.err",
             machine=name, transport="none", err="unreachable",
             elapsed_ms=int((time.monotonic() - _t0) * 1000))
    return base


async def discover_fleet(
    machines: dict[str, dict[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    """
    Parallel health check for all fleet machines.

    Args:
        machines: Fleet config dict (defaults to FLEET_MACHINES).

    Returns:
        dict mapping machine name → health result dict.
    """
    if machines is None:
        machines = FLEET_MACHINES

    tl.event("cm.fleet.discover.start", total=len(machines))
    _t0 = time.monotonic()

    tasks = [
        check_machine_health(name, info) for name, info in machines.items()
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    fleet_status: dict[str, dict[str, Any]] = {}
    for name, result in zip(machines.keys(), results):
        if isinstance(result, Exception):
            fleet_status[name] = {
                "name": name,
                "online": False,
                "os": machines[name]["os"],
                "ip": machines[name]["ip"],
                "method": "unreachable",
                "health_data": None,
                "error": str(result),
            }
        else:
            fleet_status[name] = result  # type: ignore[assignment]

    online = sum(1 for v in fleet_status.values() if v.get("online"))
    total = len(fleet_status)
    log.info("discover_fleet: %d/%d machines online", online, total)
    tl.event("cm.fleet.discover.done",
             online_count=online, total=total,
             elapsed_ms=int((time.monotonic() - _t0) * 1000))
    return fleet_status
