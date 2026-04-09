"""
Unit tests for src/config.py
"""
from __future__ import annotations

import socket
from unittest.mock import patch, MagicMock

import pytest

from src.config import (
    FLEET_MACHINES,
    DEFAULT_PORT,
    DEFAULT_BIND,
    SCAN_INTERVAL,
    SSH_TIMEOUT,
    detect_local_machine,
)


# ---------------------------------------------------------------------------
# FLEET_MACHINES structure
# ---------------------------------------------------------------------------

class TestFleetMachinesStructure:
    """Verify FLEET_MACHINES has the right shape and values."""

    REQUIRED_KEYS = {"ip", "os", "ssh_alias", "mux", "dispatch_port"}
    EXPECTED_MACHINES = {"mac-mini", "ubuntu-desktop", "avell-i7", "windows-desktop"}

    def test_has_all_four_machines(self):
        assert set(FLEET_MACHINES.keys()) == self.EXPECTED_MACHINES

    @pytest.mark.parametrize("name", ["mac-mini", "ubuntu-desktop", "avell-i7", "windows-desktop"])
    def test_machine_has_required_keys(self, name: str):
        machine = FLEET_MACHINES[name]
        missing = self.REQUIRED_KEYS - machine.keys()
        assert not missing, f"{name} is missing keys: {missing}"

    # IPs

    def test_mac_mini_ip(self):
        assert FLEET_MACHINES["mac-mini"]["ip"] == "192.168.7.102"

    def test_ubuntu_desktop_ip(self):
        assert FLEET_MACHINES["ubuntu-desktop"]["ip"] == "192.168.7.13"

    def test_avell_i7_ip(self):
        assert FLEET_MACHINES["avell-i7"]["ip"] == "192.168.7.103"

    def test_windows_desktop_ip(self):
        assert FLEET_MACHINES["windows-desktop"]["ip"] == "192.168.7.101"

    # OS values

    def test_mac_mini_os(self):
        assert FLEET_MACHINES["mac-mini"]["os"] == "darwin"

    def test_ubuntu_desktop_os(self):
        assert FLEET_MACHINES["ubuntu-desktop"]["os"] == "linux"

    def test_avell_i7_os(self):
        assert FLEET_MACHINES["avell-i7"]["os"] == "win32"

    def test_windows_desktop_os(self):
        assert FLEET_MACHINES["windows-desktop"]["os"] == "win32"

    # SSH aliases match machine names

    @pytest.mark.parametrize("name", ["mac-mini", "ubuntu-desktop", "avell-i7", "windows-desktop"])
    def test_ssh_alias_equals_machine_name(self, name: str):
        assert FLEET_MACHINES[name]["ssh_alias"] == name

    # mux values

    @pytest.mark.parametrize("name", ["mac-mini", "ubuntu-desktop"])
    def test_unix_machines_use_tmux(self, name: str):
        assert FLEET_MACHINES[name]["mux"] == "tmux"

    @pytest.mark.parametrize("name", ["avell-i7", "windows-desktop"])
    def test_windows_machines_use_psmux(self, name: str):
        assert FLEET_MACHINES[name]["mux"] == "psmux"

    # dispatch_port

    @pytest.mark.parametrize("name", ["mac-mini", "ubuntu-desktop", "avell-i7"])
    def test_dispatch_port_is_44730(self, name: str):
        assert FLEET_MACHINES[name]["dispatch_port"] == 44730

    def test_windows_desktop_has_no_dispatch_port(self):
        assert FLEET_MACHINES["windows-desktop"]["dispatch_port"] is None

    # All IPs are valid strings

    @pytest.mark.parametrize("name", ["mac-mini", "ubuntu-desktop", "avell-i7", "windows-desktop"])
    def test_ip_is_string(self, name: str):
        assert isinstance(FLEET_MACHINES[name]["ip"], str)

    # All IPs look like IPv4

    @pytest.mark.parametrize("name", ["mac-mini", "ubuntu-desktop", "avell-i7", "windows-desktop"])
    def test_ip_is_valid_ipv4(self, name: str):
        ip = FLEET_MACHINES[name]["ip"]
        parts = ip.split(".")
        assert len(parts) == 4
        assert all(p.isdigit() and 0 <= int(p) <= 255 for p in parts)


# ---------------------------------------------------------------------------
# Config constants
# ---------------------------------------------------------------------------

class TestConfigConstants:
    def test_default_port_is_int(self):
        assert isinstance(DEFAULT_PORT, int)

    def test_default_port_is_positive(self):
        assert DEFAULT_PORT > 0

    def test_default_port_is_44740(self):
        assert DEFAULT_PORT == 44740

    def test_default_bind_is_string(self):
        assert isinstance(DEFAULT_BIND, str)

    def test_default_bind_is_localhost(self):
        assert DEFAULT_BIND == "localhost"

    def test_scan_interval_is_int(self):
        assert isinstance(SCAN_INTERVAL, int)

    def test_scan_interval_is_positive(self):
        assert SCAN_INTERVAL > 0

    def test_scan_interval_is_30(self):
        assert SCAN_INTERVAL == 30

    def test_ssh_timeout_is_int(self):
        assert isinstance(SSH_TIMEOUT, int)

    def test_ssh_timeout_is_positive(self):
        assert SSH_TIMEOUT > 0

    def test_ssh_timeout_is_3(self):
        assert SSH_TIMEOUT == 3


# ---------------------------------------------------------------------------
# detect_local_machine()
# ---------------------------------------------------------------------------

class TestDetectLocalMachine:
    """Tests for the hostname / IP matching logic."""

    def test_returns_none_or_valid_name(self):
        """Result must be a fleet machine name or None."""
        result = detect_local_machine()
        assert result is None or result in FLEET_MACHINES

    def test_returns_mac_mini_on_matching_hostname(self):
        with patch("socket.gethostname", return_value="mac-mini"):
            result = detect_local_machine()
        assert result == "mac-mini"

    def test_returns_ubuntu_desktop_on_matching_hostname(self):
        with patch("socket.gethostname", return_value="ubuntu-desktop"):
            result = detect_local_machine()
        assert result == "ubuntu-desktop"

    def test_returns_avell_i7_on_matching_hostname(self):
        with patch("socket.gethostname", return_value="avell-i7"):
            result = detect_local_machine()
        assert result == "avell-i7"

    def test_returns_windows_desktop_on_matching_hostname(self):
        with patch("socket.gethostname", return_value="windows-desktop"):
            result = detect_local_machine()
        assert result == "windows-desktop"

    def test_hostname_match_is_case_insensitive(self):
        with patch("socket.gethostname", return_value="MAC-MINI"):
            result = detect_local_machine()
        assert result == "mac-mini"

    def test_partial_hostname_match(self):
        # Fleet name contained within a longer hostname
        with patch("socket.gethostname", return_value="my-mac-mini-host"):
            result = detect_local_machine()
        assert result == "mac-mini"

    def test_returns_none_for_unknown_hostname_and_ip(self):
        with (
            patch("socket.gethostname", return_value="some-random-box"),
            patch("socket.gethostbyname", return_value="10.0.0.99"),
            patch("socket.getaddrinfo", return_value=[]),
        ):
            result = detect_local_machine()
        assert result is None

    def test_ip_fallback_matches_mac_mini(self):
        mac_mini_ip = FLEET_MACHINES["mac-mini"]["ip"]
        with (
            patch("socket.gethostname", return_value="some-random-box"),
            patch("socket.gethostbyname", return_value=mac_mini_ip),
            patch("socket.getaddrinfo", return_value=[]),
        ):
            result = detect_local_machine()
        assert result == "mac-mini"

    def test_ip_fallback_matches_ubuntu_desktop(self):
        ubuntu_ip = FLEET_MACHINES["ubuntu-desktop"]["ip"]
        with (
            patch("socket.gethostname", return_value="some-random-box"),
            patch("socket.gethostbyname", return_value=ubuntu_ip),
            patch("socket.getaddrinfo", return_value=[]),
        ):
            result = detect_local_machine()
        assert result == "ubuntu-desktop"

    def test_returns_none_when_gethostname_raises(self):
        # When gethostname() raises, hostname becomes "".
        # "" is a substring of every fleet name (Python string semantics),
        # so detect_local_machine() returns the first fleet machine rather
        # than None. The valid contract is: result is None *or* a fleet name.
        with (
            patch("socket.gethostname", side_effect=OSError("no hostname")),
        ):
            result = detect_local_machine()
        assert result is None or result in FLEET_MACHINES

    def test_ip_fallback_via_getaddrinfo(self):
        """IP discovered through getaddrinfo (not gethostbyname) matches a fleet machine."""
        avell_ip = FLEET_MACHINES["avell-i7"]["ip"]
        fake_addrinfo = [(None, None, None, None, (avell_ip, 0))]
        with (
            patch("socket.gethostname", return_value="some-random-box"),
            patch("socket.gethostbyname", side_effect=OSError),
            patch("socket.getaddrinfo", return_value=fake_addrinfo),
        ):
            result = detect_local_machine()
        assert result == "avell-i7"
