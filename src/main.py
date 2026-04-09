"""
claude-manager entry point.

CLI:
    claude-manager [--bind HOST] [--port PORT] [--tui] [--enable-web] [--enable-gui]

Default mode: start the REST/WebSocket API server.
"""
from __future__ import annotations

import argparse
import sys


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="claude-manager",
        description="Manage Claude Code sessions and tmux sessions across fleet machines.",
    )
    parser.add_argument(
        "--bind",
        default="0.0.0.0",
        metavar="HOST",
        help="Bind address for the API server (default: 0.0.0.0)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=44740,
        metavar="PORT",
        help="Port for the API server (default: 44740)",
    )
    parser.add_argument(
        "--tui",
        action="store_true",
        help="Launch the Textual TUI",
    )
    parser.add_argument(
        "--enable-web",
        action="store_true",
        default=True,
        help="Serve the web UI at / (default: enabled)",
    )
    parser.add_argument(
        "--no-web",
        action="store_true",
        help="Disable the web UI (API only)",
    )
    parser.add_argument(
        "--enable-gui",
        action="store_true",
        help="Launch the native desktop GUI window",
    )
    parser.add_argument(
        "--api-only",
        action="store_true",
        help="Run API server only (no GUI, no web UI)",
    )
    return parser


def print_banner(bind: str, port: int) -> None:
    url = f"http://{bind}:{port}"
    lines = [
        "",
        "  claude-manager",
        f"  API server  →  {url}",
    ]
    if bind == "0.0.0.0":
        # Show actual LAN IP for convenience
        import socket
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("192.168.7.1", 80))
            lan_ip = s.getsockname()[0]
            s.close()
            lines.append(f"  LAN URL     →  http://{lan_ip}:{port}")
        except Exception:
            pass
    lines += [
        f"  Health      →  {url}/health",
        f"  Sessions    →  {url}/api/sessions",
        f"  Fleet       →  {url}/api/fleet",
        f"  WebSocket   →  ws://{bind}:{port}/ws",
        "",
        "  Press Ctrl+C to stop.",
        "",
    ]
    print("\n".join(lines))


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.tui:
        from .tui.app import ClaudeManagerApp
        app = ClaudeManagerApp()
        app.run()
        return

    if args.api_only:
        from .server import run_server
        print_banner(args.bind, args.port)
        run_server(port=args.port, bind=args.bind)
        return

    # Default: launch native desktop GUI with embedded API server
    # Use --api-only to skip the GUI and run headless
    try:
        from .desktop import run_desktop
        run_desktop(args.bind, args.port)
    except (ImportError, Exception) as e:
        # If pywebview not available, fall back to API server + print LAN URL
        print(f"Desktop GUI unavailable ({e}), starting API server...")
        from .server import run_server
        print_banner(args.bind, args.port)
        run_server(port=args.port, bind=args.bind)


if __name__ == "__main__":
    main()
