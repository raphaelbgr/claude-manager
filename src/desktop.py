"""Desktop system tray + webview for claude-manager."""
import sys
import threading
import time
import webbrowser
from pathlib import Path


def run_desktop(bind: str = "localhost", port: int = 44740):
    """Run claude-manager with system tray icon and webview window."""
    try:
        import pystray
        from PIL import Image, ImageDraw
    except ImportError:
        print("Desktop mode requires: pip install pystray Pillow pywebview")
        sys.exit(1)

    base_url = f"http://{bind}:{port}"
    project_dir = str(Path(__file__).parent.parent)

    # --- Icon generation ---
    def create_icon_image():
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        draw.ellipse([4, 4, 60, 60], fill=(88, 166, 255, 255))
        try:
            from PIL import ImageFont
            font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 24)
        except Exception:
            font = ImageFont.load_default()
        draw.text((14, 16), "CM", fill=(13, 17, 23, 255), font=font)
        return img

    # --- Menu actions ---
    def open_web_ui(icon, item):
        webbrowser.open(base_url)

    def open_webview(icon, item):
        try:
            import webview
            # Run in a separate thread so it doesn't block the tray
            t = threading.Thread(target=_open_webview_window, args=(base_url,), daemon=True)
            t.start()
        except ImportError:
            webbrowser.open(base_url)

    def open_tui(icon, item):
        import subprocess
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

    def force_scan(icon, item):
        import urllib.request
        try:
            req = urllib.request.Request(f"{base_url}/api/sessions/scan", method="POST")
            urllib.request.urlopen(req, timeout=10)
        except Exception:
            pass

    def quit_app(icon, item):
        icon.stop()
        # Force exit since aiohttp may keep the thread alive
        import os
        os._exit(0)

    # --- Start API server in background ---
    server_thread = threading.Thread(target=_run_server, args=(bind, port), daemon=True)
    server_thread.start()

    # Wait briefly for server to be ready
    time.sleep(1.5)

    # --- Create and run tray icon ---
    menu = pystray.Menu(
        pystray.MenuItem("Open in Browser", open_web_ui, default=True),
        pystray.MenuItem("Open Native Window", open_webview),
        pystray.MenuItem("Open TUI", open_tui),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Force Scan", force_scan),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(f"API: {base_url}", None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit", quit_app),
    )

    icon = pystray.Icon("claude-manager", create_icon_image(), "claude-manager", menu)

    print(f"claude-manager tray icon active — API at {base_url}")
    print(f"Double-click tray icon or right-click → Open in Browser")
    icon.run()


def _run_server(bind: str, port: int):
    """Run the aiohttp server in a background thread with its own event loop."""
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
    # Keep the loop running for background tasks (scan loop, WS)
    loop.run_forever()


def _open_webview_window(url: str):
    """Open a native webview window (runs in its own thread)."""
    import webview
    webview.create_window(
        "claude-manager",
        url,
        width=1280,
        height=860,
        min_size=(800, 500),
    )
    webview.start()
