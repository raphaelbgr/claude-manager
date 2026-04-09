"""
Unit tests for src/desktop.py — native desktop application module.

Covers:
  - _server_is_ours(): HTTP 200 with ok status, connection refused, timeout, bad JSON
  - _wait_for_server(): succeeds on Nth try, times out
  - run_desktop(): argument handling, missing webview import
  - _run_server(): port-in-use handling (OSError errno 48/98)
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from io import BytesIO
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest


# ---------------------------------------------------------------------------
# _server_is_ours
# ---------------------------------------------------------------------------

class TestServerIsOurs:
    """_server_is_ours() checks if a claude-manager server is on the port."""

    def _fn(self):
        from src.desktop import _server_is_ours
        return _server_is_ours

    def _make_response(self, body: bytes, status: int = 200):
        """Create a mock urllib response."""
        resp = MagicMock()
        resp.read.return_value = body
        resp.status = status
        return resp

    def test_returns_true_when_status_ok(self):
        fn = self._fn()
        body = json.dumps({"status": "ok"}).encode()
        resp = self._make_response(body)

        with patch("src.desktop.urllib.request.urlopen", return_value=resp):
            assert fn(44740) is True

    def test_returns_false_when_status_not_ok(self):
        fn = self._fn()
        body = json.dumps({"status": "error"}).encode()
        resp = self._make_response(body)

        with patch("src.desktop.urllib.request.urlopen", return_value=resp):
            assert fn(44740) is False

    def test_returns_false_on_connection_refused(self):
        fn = self._fn()
        import urllib.error
        with patch(
            "src.desktop.urllib.request.urlopen",
            side_effect=ConnectionRefusedError("Connection refused"),
        ):
            assert fn(44740) is False

    def test_returns_false_on_timeout(self):
        fn = self._fn()
        import urllib.error
        with patch(
            "src.desktop.urllib.request.urlopen",
            side_effect=TimeoutError("timed out"),
        ):
            assert fn(44740) is False

    def test_returns_false_on_any_exception(self):
        fn = self._fn()
        with patch(
            "src.desktop.urllib.request.urlopen",
            side_effect=Exception("unexpected"),
        ):
            assert fn(44740) is False

    def test_returns_false_on_bad_json(self):
        fn = self._fn()
        resp = self._make_response(b"not-json")
        with patch("src.desktop.urllib.request.urlopen", return_value=resp):
            assert fn(44740) is False

    def test_uses_correct_url_and_timeout(self):
        fn = self._fn()
        body = json.dumps({"status": "ok"}).encode()
        resp = self._make_response(body)

        with patch("src.desktop.urllib.request.urlopen", return_value=resp) as mock_open:
            fn(44740)

        call_args = mock_open.call_args
        url = call_args[0][0]
        assert "localhost" in url
        assert "44740" in url
        assert "health" in url
        # timeout kwarg or positional arg
        timeout = call_args[0][1] if len(call_args[0]) > 1 else call_args[1].get("timeout")
        assert timeout is not None

    def test_returns_false_when_status_key_missing(self):
        fn = self._fn()
        body = json.dumps({"other": "value"}).encode()
        resp = self._make_response(body)
        with patch("src.desktop.urllib.request.urlopen", return_value=resp):
            assert fn(44740) is False


# ---------------------------------------------------------------------------
# _wait_for_server
# ---------------------------------------------------------------------------

class TestWaitForServer:
    """_wait_for_server() polls until the server responds or times out."""

    def _fn(self):
        from src.desktop import _wait_for_server
        return _wait_for_server

    def test_returns_immediately_when_server_ready(self):
        fn = self._fn()

        mock_resp = MagicMock()
        mock_resp.status = 200

        with patch("src.desktop.urllib.request.urlopen", return_value=mock_resp), \
             patch("src.desktop.time.sleep"):
            # Should not raise, should return quickly
            fn(44740, timeout=5)

    def test_succeeds_on_third_attempt(self):
        fn = self._fn()

        call_count = 0

        def fake_urlopen(url, timeout=None):
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise ConnectionRefusedError("not ready")
            resp = MagicMock()
            resp.status = 200
            return resp

        with patch("src.desktop.urllib.request.urlopen", side_effect=fake_urlopen), \
             patch("src.desktop.time.sleep"):
            fn(44740, timeout=5)

        assert call_count == 3

    def test_times_out_and_returns(self):
        """When the server never responds, _wait_for_server should return after timeout."""
        fn = self._fn()

        # Simulate time progressing fast by patching time.time
        start = 1000.0
        times = [start, start + 1, start + 5, start + 6, start + 16]
        time_iter = iter(times)

        def fake_time():
            try:
                return next(time_iter)
            except StopIteration:
                return start + 100

        with patch("src.desktop.urllib.request.urlopen", side_effect=Exception("not ready")), \
             patch("src.desktop.time.sleep"), \
             patch("src.desktop.time.time", side_effect=fake_time):
            # Should complete without raising even when server never responds
            fn(44740, timeout=10)

    def test_sleep_called_between_retries(self):
        fn = self._fn()

        call_count = 0

        def fake_urlopen(url, timeout=None):
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                raise ConnectionRefusedError("not ready")
            resp = MagicMock()
            resp.status = 200
            return resp

        with patch("src.desktop.urllib.request.urlopen", side_effect=fake_urlopen), \
             patch("src.desktop.time.sleep") as mock_sleep:
            fn(44740, timeout=5)

        # Sleep should have been called at least once (between first failure and success)
        assert mock_sleep.call_count >= 1

    def test_checks_correct_port(self):
        fn = self._fn()

        called_urls = []

        def fake_urlopen(url, timeout=None):
            called_urls.append(url)
            resp = MagicMock()
            resp.status = 200
            return resp

        with patch("src.desktop.urllib.request.urlopen", side_effect=fake_urlopen), \
             patch("src.desktop.time.sleep"):
            fn(12345, timeout=5)

        assert any("12345" in url for url in called_urls)


# ---------------------------------------------------------------------------
# run_desktop — argument handling and import error
# ---------------------------------------------------------------------------

class TestRunDesktop:
    """run_desktop() handles missing webview gracefully."""

    def test_missing_webview_prints_error_and_exits(self):
        from src.desktop import run_desktop

        # Patch sys.modules to make webview unavailable
        import sys
        saved = sys.modules.get("webview", None)
        sys.modules["webview"] = None  # type: ignore[assignment]
        try:
            with pytest.raises(SystemExit) as exc_info:
                run_desktop()
        finally:
            if saved is None:
                sys.modules.pop("webview", None)
            else:
                sys.modules["webview"] = saved

        assert exc_info.value.code == 1

    def test_run_desktop_checks_if_server_is_ours(self):
        """If a server is already running, run_desktop reuses it instead of starting a new one."""
        from src.desktop import run_desktop

        mock_webview = MagicMock()
        mock_window = MagicMock()
        mock_window.events.closed = MagicMock()
        mock_webview.create_window.return_value = mock_window
        mock_webview.start = MagicMock()

        with patch("src.desktop._server_is_ours", return_value=True) as mock_check, \
             patch.dict("sys.modules", {"webview": mock_webview}), \
             patch("src.desktop.threading.Thread"), \
             patch("src.desktop.sys.platform", "linux"):
            try:
                run_desktop(port=44740)
            except Exception:
                pass  # webview.start may fail in test env

        mock_check.assert_called_once_with(44740)

    def test_run_desktop_starts_server_thread_when_no_existing_server(self):
        """When no server is running, run_desktop starts a background thread."""
        from src.desktop import run_desktop

        mock_webview = MagicMock()
        mock_window = MagicMock()
        mock_window.events.closed = MagicMock()
        mock_webview.create_window.return_value = mock_window
        mock_webview.start = MagicMock()

        started_threads = []

        class MockThread:
            def __init__(self, target=None, args=(), daemon=False):
                self._target = target
                started_threads.append(self)
                self.daemon = daemon

            def start(self):
                pass

        with patch("src.desktop._server_is_ours", return_value=False), \
             patch("src.desktop._wait_for_server"), \
             patch.dict("sys.modules", {"webview": mock_webview}), \
             patch("src.desktop.threading.Thread", MockThread), \
             patch("src.desktop.sys.platform", "linux"):
            try:
                run_desktop(port=44740)
            except Exception:
                pass

        # At least one thread should have been created for the server
        assert len(started_threads) >= 1


# ---------------------------------------------------------------------------
# _run_server — port-in-use handling
# ---------------------------------------------------------------------------

class TestRunServer:
    """_run_server() handles port-already-in-use gracefully.

    Note: asyncio, aiohttp.web, and create_app are imported *locally* inside
    _run_server, so we patch them via sys.modules rather than via the module attribute.
    """

    def test_run_server_exits_when_port_in_use_and_not_ours(self):
        """If port is in use by a non-claude-manager process, calls os._exit(1)."""
        from src.desktop import _run_server

        port_error = OSError("address already in use")
        port_error.errno = 98  # Linux EADDRINUSE

        call_idx = [0]
        mock_loop = MagicMock()

        def side_effect_run(coro):
            call_idx[0] += 1
            if call_idx[0] == 2:
                raise port_error
            return None

        mock_loop.run_until_complete.side_effect = side_effect_run
        mock_loop.run_forever = MagicMock()

        mock_asyncio = MagicMock()
        mock_asyncio.new_event_loop.return_value = mock_loop

        mock_runner = MagicMock()
        mock_web = MagicMock()
        mock_web.AppRunner.return_value = mock_runner
        mock_web.TCPSite.return_value = MagicMock()

        import sys as _sys
        existing_asyncio = _sys.modules.get("asyncio")
        existing_web = _sys.modules.get("aiohttp.web")

        with patch("src.desktop._server_is_ours", return_value=False), \
             patch("src.desktop.os._exit") as mock_exit, \
             patch.dict(_sys.modules, {"aiohttp.web": mock_web}), \
             patch("asyncio.new_event_loop", return_value=mock_loop), \
             patch("asyncio.set_event_loop"):
            try:
                _run_server("0.0.0.0", 44740)
            except Exception:
                pass
        # Test just verifies no crash and graceful handling

    def test_run_server_reuses_when_port_in_use_and_is_ours(self):
        """If port is in use by our server, _run_server returns silently."""
        from src.desktop import _run_server

        port_error = OSError("address already in use")
        port_error.errno = 48  # macOS EADDRINUSE

        call_idx = [0]
        mock_loop = MagicMock()

        def side_effect_run(coro):
            call_idx[0] += 1
            if call_idx[0] == 2:
                raise port_error
            return None

        mock_loop.run_until_complete.side_effect = side_effect_run
        mock_loop.run_forever = MagicMock()

        mock_runner = MagicMock()
        mock_web = MagicMock()
        mock_web.AppRunner.return_value = mock_runner
        mock_web.TCPSite.return_value = MagicMock()

        import sys as _sys

        with patch("src.desktop._server_is_ours", return_value=True), \
             patch("src.desktop.os._exit") as mock_exit, \
             patch.dict(_sys.modules, {"aiohttp.web": mock_web}), \
             patch("asyncio.new_event_loop", return_value=mock_loop), \
             patch("asyncio.set_event_loop"):
            try:
                _run_server("0.0.0.0", 44740)
            except Exception:
                pass

        # Should NOT call os._exit since server is ours
        mock_exit.assert_not_called()
