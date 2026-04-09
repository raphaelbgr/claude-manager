"""
Unit tests for src/fleet.py

All network I/O (aiohttp, asyncio subprocesses) is mocked.
"""
from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock

import pytest
import pytest_asyncio

from src.fleet import check_machine_health, discover_fleet
from src.config import FLEET_MACHINES, SSH_TIMEOUT


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _machine(name: str, port: int | None = 44730) -> tuple[str, dict]:
    """Return a (name, info) pair for a fake machine."""
    return name, {
        "ip": "192.168.7.99",
        "os": "linux",
        "ssh_alias": name,
        "mux": "tmux",
        "dispatch_port": port,
    }


def _mock_aiohttp_ok(json_data: dict) -> MagicMock:
    """
    Build a context-manager mock that simulates a successful aiohttp GET
    returning status=200 and json_data.
    """
    resp = AsyncMock()
    resp.status = 200
    resp.json = AsyncMock(return_value=json_data)
    resp.text = AsyncMock(return_value="")

    # async context manager for session.get(url)
    get_cm = AsyncMock()
    get_cm.__aenter__ = AsyncMock(return_value=resp)
    get_cm.__aexit__ = AsyncMock(return_value=False)

    session = AsyncMock()
    session.get = MagicMock(return_value=get_cm)

    # async context manager for aiohttp.ClientSession(...)
    session_cm = AsyncMock()
    session_cm.__aenter__ = AsyncMock(return_value=session)
    session_cm.__aexit__ = AsyncMock(return_value=False)

    return session_cm


def _mock_aiohttp_error(exc: Exception) -> MagicMock:
    """Simulate aiohttp raising exc during session.get()."""
    get_cm = AsyncMock()
    get_cm.__aenter__ = AsyncMock(side_effect=exc)
    get_cm.__aexit__ = AsyncMock(return_value=False)

    session = AsyncMock()
    session.get = MagicMock(return_value=get_cm)

    session_cm = AsyncMock()
    session_cm.__aenter__ = AsyncMock(return_value=session)
    session_cm.__aexit__ = AsyncMock(return_value=False)

    return session_cm


def _mock_ssh_ok() -> MagicMock:
    """Simulate SSH subprocess returning returncode=0 and stdout=b'ok'."""
    proc = AsyncMock()
    proc.returncode = 0
    proc.communicate = AsyncMock(return_value=(b"ok\n", b""))
    return proc


def _mock_ssh_fail() -> MagicMock:
    """Simulate SSH subprocess returning returncode=1."""
    proc = AsyncMock()
    proc.returncode = 1
    proc.communicate = AsyncMock(return_value=(b"", b"ssh: connect to host ..."))
    return proc


# ---------------------------------------------------------------------------
# check_machine_health — HTTP success
# ---------------------------------------------------------------------------

class TestCheckMachineHealthHTTP:

    @pytest.mark.asyncio
    async def test_online_true_when_http_200(self):
        name, info = _machine("test-box")
        health_payload = {"status": "ok", "jobs": 0}
        session_cm = _mock_aiohttp_ok(health_payload)

        with patch("src.fleet.aiohttp.ClientSession", return_value=session_cm):
            result = await check_machine_health(name, info)

        assert result["online"] is True

    @pytest.mark.asyncio
    async def test_method_is_http_on_success(self):
        name, info = _machine("test-box")
        session_cm = _mock_aiohttp_ok({"status": "ok"})

        with patch("src.fleet.aiohttp.ClientSession", return_value=session_cm):
            result = await check_machine_health(name, info)

        assert result["method"] == "http"

    @pytest.mark.asyncio
    async def test_health_data_populated_from_json(self):
        name, info = _machine("test-box")
        payload = {"status": "ok", "version": "1.2.3"}
        session_cm = _mock_aiohttp_ok(payload)

        with patch("src.fleet.aiohttp.ClientSession", return_value=session_cm):
            result = await check_machine_health(name, info)

        assert result["health_data"] == payload

    @pytest.mark.asyncio
    async def test_name_and_ip_preserved(self):
        name, info = _machine("test-box")
        session_cm = _mock_aiohttp_ok({})

        with patch("src.fleet.aiohttp.ClientSession", return_value=session_cm):
            result = await check_machine_health(name, info)

        assert result["name"] == name
        assert result["ip"] == info["ip"]
        assert result["os"] == info["os"]

    @pytest.mark.asyncio
    async def test_http_url_uses_dispatch_port(self):
        """Verify the URL is constructed from the machine's IP and dispatch_port."""
        name, info = _machine("test-box", port=44730)
        captured_url: list[str] = []

        # Build a response mock
        resp = AsyncMock()
        resp.status = 200
        resp.json = AsyncMock(return_value={})

        # get_cm is a context manager returned by session.get(url)
        # We intercept the call via a side_effect on the MagicMock so we can
        # capture the URL while still returning the proper context manager.
        get_cm = MagicMock()
        get_cm.__aenter__ = AsyncMock(return_value=resp)
        get_cm.__aexit__ = AsyncMock(return_value=False)

        def _get(url, *args, **kwargs):
            captured_url.append(url)
            return get_cm

        session = MagicMock()
        session.get = _get

        session_cm = AsyncMock()
        session_cm.__aenter__ = AsyncMock(return_value=session)
        session_cm.__aexit__ = AsyncMock(return_value=False)

        with patch("src.fleet.aiohttp.ClientSession", return_value=session_cm):
            await check_machine_health(name, info)

        assert len(captured_url) == 1
        assert captured_url[0] == f"http://{info['ip']}:44730/health"


# ---------------------------------------------------------------------------
# check_machine_health — HTTP fails, SSH fallback
# ---------------------------------------------------------------------------

class TestCheckMachineHealthSSHFallback:

    @pytest.mark.asyncio
    async def test_falls_back_to_ssh_when_http_fails(self):
        name, info = _machine("test-box")
        # HTTP raises a generic exception
        err_cm = _mock_aiohttp_error(ConnectionError("refused"))
        proc = _mock_ssh_ok()

        with (
            patch("src.fleet.aiohttp.ClientSession", return_value=err_cm),
            patch("asyncio.create_subprocess_exec", return_value=proc),
            patch("asyncio.wait_for", side_effect=_wrap_wait_for(proc)),
        ):
            result = await check_machine_health(name, info)

        assert result["online"] is True
        assert result["method"] == "ssh"
        assert result["health_data"] == {"ssh": "ok"}

    @pytest.mark.asyncio
    async def test_skips_http_when_dispatch_port_is_none(self):
        """When dispatch_port is None, should go straight to SSH."""
        name, info = _machine("no-daemon", port=None)
        proc = _mock_ssh_ok()

        with (
            patch("src.fleet.aiohttp.ClientSession") as mock_cs,
            patch("asyncio.create_subprocess_exec", return_value=proc),
            patch("asyncio.wait_for", side_effect=_wrap_wait_for(proc)),
        ):
            result = await check_machine_health(name, info)

        # aiohttp.ClientSession should NOT have been called
        mock_cs.assert_not_called()
        assert result["online"] is True
        assert result["method"] == "ssh"

    @pytest.mark.asyncio
    async def test_offline_when_both_fail(self):
        name, info = _machine("test-box")
        err_cm = _mock_aiohttp_error(ConnectionError("refused"))

        async def _raise_timeout(*args, **kwargs):
            raise asyncio.TimeoutError()

        with (
            patch("src.fleet.aiohttp.ClientSession", return_value=err_cm),
            patch("asyncio.wait_for", side_effect=_raise_timeout),
        ):
            result = await check_machine_health(name, info)

        assert result["online"] is False
        assert result["method"] == "unreachable"
        assert result["health_data"] is None

    @pytest.mark.asyncio
    async def test_ssh_fail_returncode_nonzero_gives_offline(self):
        name, info = _machine("test-box", port=None)
        proc = _mock_ssh_fail()

        with (
            patch("asyncio.create_subprocess_exec", return_value=proc),
            patch("asyncio.wait_for", side_effect=_wrap_wait_for(proc)),
        ):
            result = await check_machine_health(name, info)

        assert result["online"] is False
        assert result["method"] == "unreachable"


# ---------------------------------------------------------------------------
# discover_fleet
# ---------------------------------------------------------------------------

class TestDiscoverFleet:

    @pytest.mark.asyncio
    async def test_returns_dict_with_all_fleet_machines(self):
        """discover_fleet() with no arg uses FLEET_MACHINES and returns all keys."""
        async def _fake_check(name: str, info: dict) -> dict:
            return {
                "name": name,
                "online": True,
                "os": info["os"],
                "ip": info["ip"],
                "method": "http",
                "health_data": {},
            }

        with patch("src.fleet.check_machine_health", side_effect=_fake_check):
            result = await discover_fleet()

        assert set(result.keys()) == set(FLEET_MACHINES.keys())

    @pytest.mark.asyncio
    async def test_all_online_when_checks_succeed(self):
        async def _fake_check(name: str, info: dict) -> dict:
            return {
                "name": name,
                "online": True,
                "os": info["os"],
                "ip": info["ip"],
                "method": "http",
                "health_data": {},
            }

        with patch("src.fleet.check_machine_health", side_effect=_fake_check):
            result = await discover_fleet()

        for name, status in result.items():
            assert status["online"] is True, f"{name} should be online"

    @pytest.mark.asyncio
    async def test_handles_mixed_online_offline(self):
        online_set = {"mac-mini", "ubuntu-desktop"}

        async def _fake_check(name: str, info: dict) -> dict:
            return {
                "name": name,
                "online": name in online_set,
                "os": info["os"],
                "ip": info["ip"],
                "method": "http" if name in online_set else "unreachable",
                "health_data": {} if name in online_set else None,
            }

        with patch("src.fleet.check_machine_health", side_effect=_fake_check):
            result = await discover_fleet()

        assert result["mac-mini"]["online"] is True
        assert result["ubuntu-desktop"]["online"] is True
        assert result["avell-i7"]["online"] is False
        assert result["windows-desktop"]["online"] is False

    @pytest.mark.asyncio
    async def test_handles_exception_from_check(self):
        """If check_machine_health raises, discover_fleet should mark that machine offline."""
        async def _fake_check(name: str, info: dict) -> dict:
            if name == "mac-mini":
                raise RuntimeError("boom")
            return {
                "name": name,
                "online": True,
                "os": info["os"],
                "ip": info["ip"],
                "method": "http",
                "health_data": {},
            }

        with patch("src.fleet.check_machine_health", side_effect=_fake_check):
            result = await discover_fleet()

        # mac-mini should be offline with an error key
        assert result["mac-mini"]["online"] is False
        assert "error" in result["mac-mini"]

    @pytest.mark.asyncio
    async def test_accepts_custom_machines_dict(self):
        """discover_fleet(machines=...) should use the provided dict, not FLEET_MACHINES."""
        custom = {
            "custom-box": {
                "ip": "10.0.0.1",
                "os": "linux",
                "ssh_alias": "custom-box",
                "mux": "tmux",
                "dispatch_port": 44730,
            }
        }

        async def _fake_check(name: str, info: dict) -> dict:
            return {
                "name": name,
                "online": True,
                "os": info["os"],
                "ip": info["ip"],
                "method": "http",
                "health_data": {},
            }

        with patch("src.fleet.check_machine_health", side_effect=_fake_check):
            result = await discover_fleet(machines=custom)

        assert list(result.keys()) == ["custom-box"]
        assert result["custom-box"]["online"] is True

    @pytest.mark.asyncio
    async def test_all_offline_returns_full_dict(self):
        async def _fake_check(name: str, info: dict) -> dict:
            return {
                "name": name,
                "online": False,
                "os": info["os"],
                "ip": info["ip"],
                "method": "unreachable",
                "health_data": None,
            }

        with patch("src.fleet.check_machine_health", side_effect=_fake_check):
            result = await discover_fleet()

        assert len(result) == len(FLEET_MACHINES)
        assert all(not v["online"] for v in result.values())


# ---------------------------------------------------------------------------
# Timeout propagation in check_machine_health
# ---------------------------------------------------------------------------

class TestTimeoutHandling:

    @pytest.mark.asyncio
    async def test_ssh_timeout_returns_offline(self):
        """asyncio.TimeoutError during SSH should result in online=False."""
        name, info = _machine("slow-box", port=None)

        async def _timeout_wait_for(*args, **kwargs):
            raise asyncio.TimeoutError()

        with patch("asyncio.wait_for", side_effect=_timeout_wait_for):
            result = await check_machine_health(name, info)

        assert result["online"] is False
        assert result["method"] == "unreachable"

    @pytest.mark.asyncio
    async def test_http_timeout_falls_through_to_ssh(self):
        """An aiohttp timeout should trigger SSH fallback (not immediately offline)."""
        name, info = _machine("slow-box")
        err_cm = _mock_aiohttp_error(asyncio.TimeoutError())
        proc = _mock_ssh_ok()

        with (
            patch("src.fleet.aiohttp.ClientSession", return_value=err_cm),
            patch("asyncio.create_subprocess_exec", return_value=proc),
            patch("asyncio.wait_for", side_effect=_wrap_wait_for(proc)),
        ):
            result = await check_machine_health(name, info)

        assert result["online"] is True
        assert result["method"] == "ssh"


# ---------------------------------------------------------------------------
# Internal helper for wrapping wait_for around a fixed proc mock
# ---------------------------------------------------------------------------

def _wrap_wait_for(proc):
    """
    Returns a coroutine-compatible side_effect for asyncio.wait_for.

    The first call returns the proc (simulating create_subprocess_exec).
    The second call returns the communicate result (b"ok", b"").
    """
    call_count = [0]

    async def _inner(coro, timeout=None):
        call_count[0] += 1
        if call_count[0] == 1:
            # First wait_for wraps create_subprocess_exec → return the proc
            return proc
        else:
            # Second wait_for wraps proc.communicate()
            return (b"ok\n", b"")

    return _inner
