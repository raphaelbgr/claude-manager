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
from typing import Any

import aiohttp

from .config import FLEET_MACHINES, SSH_TIMEOUT

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
                        return base
        except Exception:
            pass  # fall through to SSH

    # --- SSH probe ---
    ssh_alias = info.get("ssh_alias", name)
    try:
        proc = await asyncio.wait_for(
            asyncio.create_subprocess_exec(
                "ssh",
                "-o", "BatchMode=yes",
                "-o", f"ConnectTimeout={SSH_TIMEOUT}",
                "-o", "StrictHostKeyChecking=no",
                ssh_alias,
                "echo ok",
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            ),
            timeout=SSH_TIMEOUT + 1,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=SSH_TIMEOUT + 2)
        if proc.returncode == 0 and b"ok" in stdout:
            base.update(online=True, method="ssh", health_data={"ssh": "ok"})
            log.info("check_machine_health(%s): online=True via ssh", name)
            return base
    except Exception:
        pass

    log.info("check_machine_health(%s): online=False", name)
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
    return fleet_status
