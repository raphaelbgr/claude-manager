"""Tests for src/main.py — CLI entry point."""
from __future__ import annotations

import io
import sys
from unittest.mock import MagicMock, patch

import pytest

from src.main import build_parser, print_banner, main


# ---------------------------------------------------------------------------
# build_parser tests
# ---------------------------------------------------------------------------

class TestBuildParser:
    def test_default_bind(self):
        parser = build_parser()
        args = parser.parse_args([])
        assert args.bind == "0.0.0.0"

    def test_default_port(self):
        parser = build_parser()
        args = parser.parse_args([])
        assert args.port == 44740

    def test_tui_flag(self):
        parser = build_parser()
        args = parser.parse_args(["--tui"])
        assert args.tui is True

    def test_enable_web_default_true(self):
        parser = build_parser()
        args = parser.parse_args([])
        assert args.enable_web is True

    def test_no_web_flag(self):
        parser = build_parser()
        args = parser.parse_args(["--no-web"])
        assert args.no_web is True

    def test_enable_gui_flag(self):
        parser = build_parser()
        args = parser.parse_args(["--enable-gui"])
        assert args.enable_gui is True

    def test_api_only_flag(self):
        parser = build_parser()
        args = parser.parse_args(["--api-only"])
        assert args.api_only is True

    def test_custom_bind_and_port(self):
        parser = build_parser()
        args = parser.parse_args(["--bind", "127.0.0.1", "--port", "8080"])
        assert args.bind == "127.0.0.1"
        assert args.port == 8080


# ---------------------------------------------------------------------------
# print_banner tests
# ---------------------------------------------------------------------------

class TestPrintBanner:
    def test_banner_includes_url(self, capsys):
        print_banner("127.0.0.1", 44740)
        captured = capsys.readouterr()
        assert "http://127.0.0.1:44740" in captured.out

    def test_banner_shows_lan_ip_on_0000(self, capsys):
        import socket as _socket

        fake_sock = MagicMock()
        fake_sock.getsockname.return_value = ("192.168.7.99", 0)

        with patch("socket.socket", return_value=fake_sock):
            print_banner("0.0.0.0", 44740)

        captured = capsys.readouterr()
        assert "192.168.7.99" in captured.out

    def test_banner_no_lan_on_localhost(self, capsys):
        print_banner("127.0.0.1", 44740)
        captured = capsys.readouterr()
        # LAN URL line is only injected when bind == "0.0.0.0"
        assert "LAN URL" not in captured.out


# ---------------------------------------------------------------------------
# main() tests
# ---------------------------------------------------------------------------

class TestMain:
    def test_tui_mode_launches_app(self):
        mock_app = MagicMock()
        fake_app_cls = MagicMock(return_value=mock_app)

        with patch("src.main.build_parser") as mock_parser:
            mock_args = MagicMock()
            mock_args.daemon = False
            mock_args.tui = True
            mock_args.api_only = False
            mock_parser.return_value.parse_args.return_value = mock_args

            # Inject a fake src.tui.app module so the dynamic import inside main() resolves
            import sys as _sys
            import types as _types
            fake_tui_mod = _types.ModuleType("src.tui.app")
            fake_tui_mod.ClaudeManagerApp = fake_app_cls
            fake_tui_pkg = _types.ModuleType("src.tui")

            with patch.dict(_sys.modules, {
                "src.tui": fake_tui_pkg,
                "src.tui.app": fake_tui_mod,
            }):
                main([])

        fake_app_cls.assert_called_once()
        mock_app.run.assert_called_once()

    def test_api_only_mode_runs_server(self):
        mock_run_server = MagicMock()
        with patch("src.main.build_parser") as mock_parser:
            mock_args = MagicMock()
            mock_args.daemon = False
            mock_args.tui = False
            mock_args.api_only = True
            mock_args.bind = "0.0.0.0"
            mock_args.port = 44740
            mock_parser.return_value.parse_args.return_value = mock_args

            fake_server_mod = MagicMock(run_server=mock_run_server)
            with patch.dict("sys.modules", {"src.server": fake_server_mod}), \
                 patch("src.main.print_banner"):
                main([])

        mock_run_server.assert_called_once_with(port=44740, bind="0.0.0.0")

    def test_default_mode_tries_desktop(self):
        mock_run_desktop = MagicMock()
        with patch("src.main.build_parser") as mock_parser:
            mock_args = MagicMock()
            mock_args.daemon = False
            mock_args.tui = False
            mock_args.api_only = False
            mock_args.bind = "0.0.0.0"
            mock_args.port = 44740
            mock_parser.return_value.parse_args.return_value = mock_args

            fake_desktop_mod = MagicMock(run_desktop=mock_run_desktop)
            with patch.dict("sys.modules", {"src.desktop": fake_desktop_mod}):
                main([])

        mock_run_desktop.assert_called_once_with("0.0.0.0", 44740)

    def test_fallback_to_server_when_desktop_unavailable(self):
        """When desktop raises ImportError, fall back to run_server."""
        mock_run_server = MagicMock()

        with patch("src.main.build_parser") as mock_parser:
            mock_args = MagicMock()
            mock_args.daemon = False
            mock_args.tui = False
            mock_args.api_only = False
            mock_args.bind = "127.0.0.1"
            mock_args.port = 44740
            mock_parser.return_value.parse_args.return_value = mock_args

            fake_server_mod = MagicMock(run_server=mock_run_server)

            # Force the desktop import to fail by making run_desktop raise ImportError
            def _raise_import(*a, **kw):
                raise ImportError("pywebview not installed")

            fake_desktop_mod = MagicMock(run_desktop=_raise_import)
            with patch.dict("sys.modules", {
                "src.desktop": fake_desktop_mod,
                "src.server": fake_server_mod,
            }), patch("src.main.print_banner"):
                main([])

        mock_run_server.assert_called_once_with(port=44740, bind="127.0.0.1")

    def test_main_no_argv(self):
        """main(None) should parse sys.argv without crashing when mocked."""
        with patch("sys.argv", ["claude-manager", "--api-only"]), \
             patch("src.main.print_banner"), \
             patch("src.server.run_server"):
            # Should not raise
            try:
                main(None)
            except SystemExit:
                pass  # argparse may call sys.exit; that's fine here
