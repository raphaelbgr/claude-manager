"""
Native desktop application for claude-manager.

Uses pywebview to render a native window (WebKit on macOS, WebView2 on Windows,
GTK WebKit on Linux) with the web UI as content. The API server runs in a
background thread. System tray icon provides quick access when minimized.

Launch: python -m src.main --enable-gui
"""
import os
import sys
import threading
import time
import urllib.request
from pathlib import Path


def run_desktop(bind: str = "localhost", port: int = 44740):
    """Launch the native desktop GUI with embedded API server."""
    try:
        import webview
    except ImportError:
        print("Desktop GUI requires: pip install pywebview")
        print("Optional tray icon: pip install pystray Pillow")
        sys.exit(1)

    base_url = f"http://{bind}:{port}"

    # --- Start API server in background thread ---
    server_thread = threading.Thread(
        target=_run_server, args=(bind, port), daemon=True
    )
    server_thread.start()

    # Wait for server to be ready (poll health endpoint)
    _wait_for_server(base_url, timeout=10)

    # --- Start system tray in background (optional, non-blocking) ---
    # On macOS, both pywebview and pystray need the main thread (AppKit).
    # pywebview wins since it's the primary UI. Tray only runs on Linux/Windows.
    if sys.platform != "darwin":
        tray_thread = threading.Thread(
            target=_run_tray, args=(base_url,), daemon=True
        )
        tray_thread.start()

    # --- Open native window (this is the main event loop) ---
    print(f"claude-manager native window — API at {base_url}")

    window = webview.create_window(
        title="claude-manager",
        url=base_url,
        width=1320,
        height=880,
        min_size=(900, 600),
        text_select=True,
        zoomable=True,
    )

    # When the window closes, exit the app
    def on_closed():
        os._exit(0)

    window.events.closed += on_closed

    # Start the native webview event loop (blocks until window closes)
    webview.start(
        debug=("--debug" in sys.argv),
        private_mode=False,
    )


def _run_server(bind: str, port: int):
    """Run the aiohttp API server in a background thread."""
    import asyncio
    from aiohttp import web
    from .server import create_app

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    app = create_app(port=port, bind=bind)

    runner = web.AppRunner(app)
    loop.run_until_complete(runner.setup())
    site = web.TCPSite(runner, bind, port)
    loop.run_until_complete(site.start())
    loop.run_forever()


def _wait_for_server(base_url: str, timeout: int = 10):
    """Poll the health endpoint until the server is ready."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            req = urllib.request.Request(f"{base_url}/health")
            resp = urllib.request.urlopen(req, timeout=2)
            if resp.status == 200:
                return
        except Exception:
            pass
        time.sleep(0.3)
    print(f"Warning: server not responding after {timeout}s, opening window anyway")


def _run_tray(base_url: str):
    """Run system tray icon (optional, fails silently if deps missing)."""
    try:
        import pystray
        from PIL import Image, ImageDraw
    except ImportError:
        return  # Tray is optional

    # Generate icon: blue circle with "CM"
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse([4, 4, 60, 60], fill=(88, 166, 255, 255))
    try:
        from PIL import ImageFont
        if sys.platform == "darwin":
            font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 24)
        else:
            font = ImageFont.load_default()
    except Exception:
        font = ImageFont.load_default()
    draw.text((14, 16), "CM", fill=(13, 17, 23, 255), font=font)

    def open_browser(icon, item):
        import webbrowser
        webbrowser.open(base_url)

    def force_scan(icon, item):
        try:
            req = urllib.request.Request(f"{base_url}/api/sessions/scan", method="POST")
            urllib.request.urlopen(req, timeout=10)
        except Exception:
            pass

    def open_tui(icon, item):
        import subprocess
        project_dir = str(Path(__file__).parent.parent)
        cmd = f"cd {project_dir} && python3 -m src --tui"
        if sys.platform == "darwin":
            subprocess.Popen(["osascript", "-e",
                f'tell application "iTerm2"\n'
                f'  activate\n'
                f'  set newWindow to (create window with default profile)\n'
                f'  tell current session of newWindow\n'
                f'    write text "{cmd}"\n'
                f'  end tell\n'
                f'end tell'])
        elif sys.platform == "win32":
            subprocess.Popen(["cmd", "/c", "start", "powershell", "-NoExit", "-Command", cmd])
        else:
            subprocess.Popen(["x-terminal-emulator", "-e", "bash", "-c", f"{cmd}; exec bash"])

    def quit_app(icon, item):
        icon.stop()
        os._exit(0)

    menu = pystray.Menu(
        pystray.MenuItem("Open in Browser", open_browser, default=True),
        pystray.MenuItem("Open TUI", open_tui),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Force Scan", force_scan),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(f"API: {base_url}", None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit", quit_app),
    )

    icon = pystray.Icon("claude-manager", img, "claude-manager", menu)
    icon.run()
