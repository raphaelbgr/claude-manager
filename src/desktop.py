"""Desktop system tray + webview for claude-manager."""
import sys
import threading
import webbrowser


def run_desktop(bind: str = "localhost", port: int = 44740):
    """Run claude-manager with system tray icon and optional webview."""
    try:
        import pystray
        from PIL import Image, ImageDraw
    except ImportError:
        print("Desktop mode requires: pip install pystray Pillow pywebview")
        sys.exit(1)

    # Generate a simple tray icon (blue circle with "CM" text)
    def create_icon_image():
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        draw.ellipse([4, 4, 60, 60], fill=(88, 166, 255, 255))  # accent blue
        # Draw "CM" text (use default font, centered)
        try:
            from PIL import ImageFont
            font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 24)
        except Exception:
            font = ImageFont.load_default()
        draw.text((14, 16), "CM", fill=(13, 17, 23, 255), font=font)
        return img

    base_url = f"http://{bind}:{port}"

    def open_web_ui(icon, item):
        try:
            import webview
            webview.create_window("claude-manager", base_url, width=1200, height=800)
            webview.start()
        except ImportError:
            webbrowser.open(base_url)

    def open_tui(icon, item):
        import subprocess
        # Launch TUI in a new terminal
        if sys.platform == "darwin":
            subprocess.Popen(["osascript", "-e",
                f'tell application "Terminal" to do script "cd {_project_dir()} && python3 -m src --tui"'])
        elif sys.platform == "win32":
            subprocess.Popen(["cmd", "/c", "start", "python", "-m", "src", "--tui"])
        else:
            subprocess.Popen(["x-terminal-emulator", "-e", "python3", "-m", "src", "--tui"])

    def force_scan(icon, item):
        import urllib.request
        try:
            req = urllib.request.Request(f"{base_url}/api/sessions/scan", method="POST")
            urllib.request.urlopen(req, timeout=5)
        except Exception:
            pass

    def quit_app(icon, item):
        icon.stop()

    def _project_dir():
        from pathlib import Path
        return str(Path(__file__).parent.parent)

    # Start the API server in a background thread
    server_thread = threading.Thread(target=_run_server, args=(bind, port), daemon=True)
    server_thread.start()

    # Create tray icon
    menu = pystray.Menu(
        pystray.MenuItem("Open Web UI", open_web_ui, default=True),
        pystray.MenuItem("Open TUI", open_tui),
        pystray.MenuItem("Force Scan", force_scan),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(f"Running on {base_url}", None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit", quit_app),
    )

    icon = pystray.Icon("claude-manager", create_icon_image(), "claude-manager", menu)

    print(f"claude-manager tray icon active — API at {base_url}")
    icon.run()


def _run_server(bind: str, port: int):
    """Run the aiohttp server in a background thread."""
    import asyncio
    from .server import create_app
    from aiohttp import web

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    app = loop.run_until_complete(create_app())
    web.run_app(app, host=bind, port=port, print=None)
