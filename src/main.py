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
        default="localhost",
        metavar="HOST",
        help="Bind address for the API server (default: localhost)",
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
        help="Also serve the web UI at / (enabled automatically when running the server)",
    )
    parser.add_argument(
        "--enable-gui",
        action="store_true",
        help="Launch the desktop GUI (Phase 4, not yet implemented)",
    )
    return parser


def print_banner(bind: str, port: int) -> None:
    url = f"http://{bind}:{port}"
    lines = [
        "",
        "  claude-manager",
        f"  API server  →  {url}",
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

    if args.enable_gui:
        print("Desktop GUI not yet implemented (Phase 4). Run without --enable-gui to start the API server.")
        sys.exit(1)

    # Default: start API server
    from .server import run_server
    print_banner(args.bind, args.port)
    run_server(port=args.port, bind=args.bind)


if __name__ == "__main__":
    main()
