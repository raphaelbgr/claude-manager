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
    parser.add_argument(
        "--daemon",
        action="store_true",
        help="Run as background daemon (detach from terminal)",
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


def _daemonize(bind: str, port: int) -> None:
    """Fork to background and run the API server as a daemon (no terminal needed).

    On Windows, re-launches with pythonw.exe (no console window).
    On Unix, double-forks to detach from the terminal.
    """
    import os
    pid_file = os.path.join(os.path.expanduser("~"), ".claude-manager.pid")

    if sys.platform == "win32":
        # Re-launch with pythonw.exe (no console) if not already
        pythonw = sys.executable.replace("python.exe", "pythonw.exe")
        if not os.path.exists(pythonw):
            pythonw = sys.executable
        import subprocess
        proc = subprocess.Popen(
            [pythonw, "-m", "src", "--api-only", "--bind", bind, "--port", str(port)],
            creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW,
            close_fds=True,
        )
        with open(pid_file, "w") as f:
            f.write(str(proc.pid))
        print(f"claude-manager daemon started (PID {proc.pid})")
        print(f"  Web: http://localhost:{port}")
        print(f"  Stop: claude-manager --stop")
        return

    # Unix: double-fork to detach from terminal
    pid = os.fork()
    if pid > 0:
        # Parent — wait briefly for child to be ready, then exit
        print(f"claude-manager daemon started (PID {pid})")
        print(f"  Web: http://localhost:{port}")
        print(f"  Stop: kill $(cat {pid_file})")
        return

    # Child: new session, second fork
    os.setsid()
    pid2 = os.fork()
    if pid2 > 0:
        os._exit(0)

    # Grandchild: redirect stdio, write PID, run server
    sys.stdin = open(os.devnull, "r")
    sys.stdout = open(os.devnull, "w")
    sys.stderr = open(os.devnull, "w")

    with open(pid_file, "w") as f:
        f.write(str(os.getpid()))

    from .server import run_server
    run_server(port=port, bind=bind)


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.daemon:
        _daemonize(args.bind, args.port)
        return

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
