"""
Fleet configuration and local machine detection for claude-manager.
"""
from __future__ import annotations

import socket
import os

# Configure your fleet machines below.
# These are example LAN IPs — update to match your network.
FLEET_MACHINES: dict[str, dict] = {
    "mac-mini": {
        "ip": "192.168.7.102",
        "os": "darwin",
        "ssh_alias": "mac-mini",
        "mux": "tmux",
        "dispatch_port": 44730,
    },
    "ubuntu-desktop": {
        "ip": "192.168.7.13",
        "os": "linux",
        "ssh_alias": "ubuntu-desktop",
        "mux": "tmux",
        "dispatch_port": 44730,
    },
    "avell-i7": {
        "ip": "192.168.7.103",
        "os": "win32",
        "ssh_alias": "avell-i7",
        "mux": "psmux",
        "dispatch_port": 44730,
    },
    "windows-desktop": {
        "ip": "192.168.7.101",
        "os": "win32",
        "ssh_alias": "windows-desktop",
        "mux": "psmux",
        "dispatch_port": None,
    },
}

DEFAULT_PORT: int = 44740
DEFAULT_BIND: str = "0.0.0.0"
SCAN_INTERVAL: int = 30
SSH_TIMEOUT: int = 3


def detect_local_machine() -> str | None:
    """
    Detect which fleet machine this process is running on.

    Strategy (in order):
    1. Compare local IP addresses against fleet IPs (most reliable)
    2. Check hostname / COMPUTERNAME against fleet names (partial match)

    Returns the fleet machine name (key in FLEET_MACHINES) or None
    if the local host is not a recognised fleet member.
    """
    # ── Step 1: IP-based detection (most reliable) ──────────────────────────
    try:
        local_addrs: set[str] = set()
        # UDP connect trick — gets the LAN IP without actually sending data
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("192.168.7.1", 80))
            local_addrs.add(s.getsockname()[0])
            s.close()
        except OSError:
            pass
        # gethostbyname gives primary IP
        try:
            local_addrs.add(socket.gethostbyname(socket.gethostname()))
        except OSError:
            pass
        # getaddrinfo gives all bound addresses
        try:
            for _family, _type, _proto, _canonname, sockaddr in socket.getaddrinfo(
                socket.gethostname(), None
            ):
                local_addrs.add(sockaddr[0])
        except OSError:
            pass

        for name, info in FLEET_MACHINES.items():
            if info["ip"] in local_addrs:
                return name
    except Exception:
        pass

    # ── Step 2: Hostname-based detection (fallback) ─────────────────────────
    hostnames: set[str] = set()
    try:
        hostnames.add(socket.gethostname().lower())
    except OSError:
        pass
    # Windows COMPUTERNAME (often different from socket.gethostname)
    for env_var in ("COMPUTERNAME", "HOSTNAME"):
        val = os.environ.get(env_var, "").lower()
        if val:
            hostnames.add(val)

    for name in FLEET_MACHINES:
        for h in hostnames:
            # Exact match or fleet name appears as a word boundary in hostname
            # e.g. "avell-i7" matches hostname "avell-i7", "AVELL-C62MOB" won't
            #       match "mac-mini"
            if name == h or name in h:
                return name

    return None
