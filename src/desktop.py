"""
Native desktop application for claude-manager.

Uses pywebview to render a native window (WebKit on macOS, WebView2 on Windows,
GTK WebKit on Linux) with the web UI as content. The API server runs in a
background thread. System tray icon provides quick access when minimized.

Launch: python -m src.main --enable-gui
"""
import json
import os
import sys
import threading
import time
import urllib.request
from pathlib import Path


def _server_is_ours(port: int) -> bool:
    """Check if a claude-manager server is already running on the port."""
    try:
        import json
        resp = urllib.request.urlopen(f"http://localhost:{port}/health", timeout=2)
        data = json.loads(resp.read())
        return data.get("status") == "ok"
    except Exception:
        return False


def run_desktop(bind: str = "0.0.0.0", port: int = 44740):
    """Launch the native desktop GUI with embedded API server."""
    try:
        import webview
    except ImportError:
        print("Desktop GUI requires: pip install pywebview")
        print("Optional tray icon: pip install pystray Pillow")
        sys.exit(1)

    base_url = f"http://{bind}:{port}"
    local_url = f"http://localhost:{port}"

    # --- Ensure API server is available ---
    # Strategy: if already running, reuse it. Otherwise start a new one.
    if _server_is_ours(port):
        print(f"Connecting to existing server on port {port}")
    else:
        server_thread = threading.Thread(
            target=_run_server, args=(bind, port), daemon=True
        )
        server_thread.start()
        # Wait for it to be ready
        _wait_for_server(port, timeout=15)

    # --- System tray (Linux/Windows only — macOS needs main thread for webview) ---
    if sys.platform != "darwin":
        tray_thread = threading.Thread(
            target=_run_tray, args=(base_url,), daemon=True
        )
        tray_thread.start()

    # --- Open native window ---
    print(f"claude-manager — {local_url}")

    loading_html = f"""
    <html style="background:#0d1117;color:#e6edf3;font-family:-apple-system,system-ui,sans-serif">
    <body style="display:flex;align-items:center;justify-content:center;height:100vh;margin:0">
    <div style="text-align:center">
        <div style="font-size:2rem;margin-bottom:16px;animation:spin 1s linear infinite;display:inline-block">↻</div>
        <div style="font-size:1.1rem;font-weight:600">claude-manager</div>
        <div style="font-size:0.8rem;color:#8b949e;margin-top:8px">Loading...</div>
    </div>
    <style>@keyframes spin{{from{{transform:rotate(0)}}to{{transform:rotate(360deg)}}}}</style>
    <script>
        (async function() {{
            for (let i = 0; i < 40; i++) {{
                try {{
                    const r = await fetch('{local_url}/health');
                    if (r.ok) {{ window.location = '{local_url}'; return; }}
                }} catch(e) {{}}
                await new Promise(r => setTimeout(r, 500));
            }}
            document.body.innerHTML = '<div style="text-align:center;margin-top:40vh;color:#f85149">Server failed to start on port {port}<br><span style="font-size:0.8rem;color:#8b949e;margin-top:8px;display:block">Try: kill $(lsof -ti:{port})</span></div>';
        }})();
    </script>
    </body></html>
    """

    window = webview.create_window(
        title="claude-manager",
        html=loading_html,
        width=1320,
        height=880,
        min_size=(900, 600),
        text_select=True,
        zoomable=True,
        background_color="#0d1117",
    )

    window.events.closed += lambda: os._exit(0)

    import warnings
    warnings.filterwarnings("ignore", category=RuntimeWarning)

    webview.start(
        debug=("--debug" in sys.argv),
        private_mode=False,
    )


def _run_server(bind: str, port: int):
    """Run the aiohttp API server in a background thread."""
    import asyncio
    import logging
    from aiohttp import web
    from .server import create_app

    logging.getLogger("asyncio").setLevel(logging.CRITICAL)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    app = create_app(port=port, bind=bind)

    runner = web.AppRunner(app)
    loop.run_until_complete(runner.setup())
    site = web.TCPSite(runner, bind, port)
    try:
        loop.run_until_complete(site.start())
    except OSError as e:
        if "address already in use" in str(e).lower() or getattr(e, "errno", 0) == 48:
            # Another process grabbed the port between our check and bind.
            # If it's a claude-manager server, just reuse it silently.
            if _server_is_ours(port):
                return
            print(f"Port {port} in use. Kill it: kill $(lsof -ti:{port})")
            os._exit(1)
        raise
    loop.run_forever()


def _wait_for_server(port: int, timeout: int = 15):
    """Poll localhost health endpoint until the server responds."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            resp = urllib.request.urlopen(f"http://localhost:{port}/health", timeout=2)
            if resp.status == 200:
                return
        except Exception:
            pass
        time.sleep(0.3)


def _run_tray(base_url: str):
    """Run system tray icon with dynamic menu (optional, fails silently if deps missing).

    Only runs on Linux/Windows — macOS needs the main thread for AppKit/webview.
    Refreshes session/tmux data from the API every 30 seconds.
    """
    try:
        import pystray
        from PIL import Image, ImageDraw
    except ImportError:
        return

    # ── Tray icon image ──────────────────────────────────────────────────────
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse([4, 4, 60, 60], fill=(88, 166, 255, 255))
    try:
        from PIL import ImageFont
        font = ImageFont.load_default()
    except Exception:
        from PIL import ImageFont
        font = ImageFont.load_default()
    draw.text((14, 16), "CM", fill=(13, 17, 23, 255), font=font)

    # ── Shared state for dynamic menu ────────────────────────────────────────
    _state = {"sessions": [], "tmux": []}

    def _fetch_state():
        """Fetch /api/sessions and /api/tmux; update _state."""
        try:
            resp = urllib.request.urlopen(f"{base_url}/api/sessions", timeout=5)
            _state["sessions"] = json.loads(resp.read())
        except Exception:
            pass
        try:
            resp = urllib.request.urlopen(f"{base_url}/api/tmux", timeout=5)
            _state["tmux"] = json.loads(resp.read())
        except Exception:
            pass

    # ── Action callbacks ─────────────────────────────────────────────────────
    def open_browser(icon, item):
        import webbrowser
        webbrowser.open(base_url)

    def force_scan(icon, item):
        try:
            req = urllib.request.Request(f"{base_url}/api/sessions/scan", method="POST")
            urllib.request.urlopen(req, timeout=10)
        except Exception:
            pass

    def exit_app(icon, item):
        try:
            req = urllib.request.Request(f"{base_url}/api/exit", method="POST")
            urllib.request.urlopen(req, timeout=5)
        except Exception:
            pass
        icon.stop()

    def _make_session_callback(session_id: str, machine: str):
        def _attach(icon, item):
            try:
                data = json.dumps({"id": session_id, "machine": machine}).encode()
                req = urllib.request.Request(
                    f"{base_url}/api/sessions/launch",
                    data=data,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                urllib.request.urlopen(req, timeout=10)
            except Exception:
                pass
        return _attach

    def _make_tmux_callback(session_name: str, machine: str):
        def _attach(icon, item):
            try:
                data = json.dumps({"session": session_name, "machine": machine}).encode()
                endpoint = "connect-remote" if machine != "local" else "connect"
                req = urllib.request.Request(
                    f"{base_url}/api/tmux/{endpoint}",
                    data=data,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                urllib.request.urlopen(req, timeout=10)
            except Exception:
                pass
        return _attach

    # ── Dynamic menu builder ─────────────────────────────────────────────────
    def _build_menu():
        items = []

        # Header + Open Web UI
        items.append(pystray.MenuItem("claude-manager", None, enabled=False))
        items.append(pystray.MenuItem("Open Web UI", open_browser, default=True))
        items.append(pystray.Menu.SEPARATOR)

        # ── Running Sessions grouped by machine ──────────────────────────────
        sessions = _state.get("sessions", [])
        by_machine: dict[str, list] = {}
        for s in sessions:
            m = s.get("machine", "local")
            by_machine.setdefault(m, []).append(s)

        if by_machine:
            items.append(pystray.MenuItem("Running Sessions", None, enabled=False))
            for machine, machine_sessions in sorted(by_machine.items()):
                for s in machine_sessions:
                    name = s.get("name") or s.get("id", "?")
                    status = (s.get("status") or "idle").upper()
                    label = f"  {machine}: {name} ({status})"
                    cb = _make_session_callback(s.get("id", ""), machine)
                    items.append(pystray.MenuItem(label, cb))
            items.append(pystray.Menu.SEPARATOR)

        # ── Tmux / Psmux Sessions ────────────────────────────────────────────
        tmux = _state.get("tmux", [])
        if tmux:
            items.append(pystray.MenuItem("Tmux / Psmux Sessions", None, enabled=False))
            for t in tmux:
                machine = t.get("machine", "local")
                name = t.get("name") or t.get("session", "?")
                label = f"  {machine}: {name}"
                cb = _make_tmux_callback(name, machine)
                items.append(pystray.MenuItem(label, cb))
            items.append(pystray.Menu.SEPARATOR)

        # ── Footer actions ───────────────────────────────────────────────────
        items.append(pystray.MenuItem("Force Scan", force_scan))
        items.append(pystray.MenuItem(f"API: {base_url}", None, enabled=False))
        items.append(pystray.Menu.SEPARATOR)
        items.append(pystray.MenuItem("Exit", exit_app))

        return pystray.Menu(*items)

    # ── Background refresh loop ──────────────────────────────────────────────
    icon_ref: list = []  # mutable container so the thread closure can access the icon

    def _refresh_loop():
        while True:
            time.sleep(30)
            _fetch_state()
            if icon_ref:
                try:
                    icon_ref[0].menu = _build_menu()
                    icon_ref[0].update_menu()
                except Exception:
                    pass

    # Initial data fetch before showing the icon
    _fetch_state()

    icon = pystray.Icon("claude-manager", img, "claude-manager", _build_menu())
    icon_ref.append(icon)

    refresh_thread = threading.Thread(target=_refresh_loop, daemon=True)
    refresh_thread.start()

    icon.run()
