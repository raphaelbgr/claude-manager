# Development

## Project Structure

```
claude-manager/
├── setup.sh                    # Install deps, create venv
├── install.sh                  # Register system service (launchd/systemd/Task Scheduler)
├── pyproject.toml              # Package metadata, entry point, optional deps
├── LICENSE
├── README.md
├── docs/
│   ├── api.md                  # Full API reference
│   ├── architecture.md         # Component diagrams, data models, flow diagrams
│   ├── deployment.md           # Setup, daemon, fleet machine config
│   └── development.md          # This file
└── src/
    ├── __init__.py
    ├── __main__.py             # Enables `python -m src`
    ├── main.py                 # CLI entry point, argparse, print_banner()
    ├── config.py               # FLEET_MACHINES dict, constants, detect_local_machine()
    ├── server.py               # aiohttp app factory, REST handlers, WebSocket handler
    ├── scanner.py              # Claude session discovery (local + remote)
    ├── tmux_manager.py         # tmux/psmux list, create, kill
    ├── mux_parser.py           # Universal tmux/psmux output parser
    ├── fleet.py                # Fleet health checks (HTTP + SSH)
    ├── launcher.py             # Cross-platform terminal launcher
    ├── desktop.py              # pywebview native window + pystray tray
    ├── web/
    │   └── index.html          # React SPA (single file, CDN imports)
    └── tui/
        ├── __init__.py
        ├── app.py              # Textual app, 3-tab layout, key bindings
        ├── styles/
        │   └── app.tcss        # Textual CSS stylesheet
        ├── screens/
        │   ├── __init__.py
        │   └── new_tmux.py     # Modal screen: create new tmux session
        └── widgets/
            ├── __init__.py
            ├── header_bar.py   # StatusBar widget
            ├── session_card.py # format_session_row() for DataTable
            └── tmux_card.py    # format_tmux_row() for DataTable
```

## Running in Development

```bash
# Activate virtualenv first
source .venv/bin/activate

# API server + web UI (reloads on file save with --reload if using uvicorn, but aiohttp has none)
python -m src.main --enable-web --bind 0.0.0.0

# TUI
python -m src --tui

# API only (no web UI)
python -m src.main --api-only
```

## Running Tests

```bash
pip install pytest pytest-asyncio
pytest
```

`pyproject.toml` sets `asyncio_mode = "auto"`, so async test functions are discovered automatically.

## Adding a Fleet Machine

1. Edit `src/config.py` and add an entry to `FLEET_MACHINES`:

```python
FLEET_MACHINES: dict[str, dict] = {
    ...
    "my-new-machine": {
        "ip": "192.168.1.50",       # LAN IP — used for HTTP probes
        "os": "linux",              # "darwin" | "linux" | "win32"
        "ssh_alias": "my-new-machine",  # must match ~/.ssh/config Host entry
        "mux": "tmux",              # "tmux" for Linux/macOS, "psmux" for Windows
        "dispatch_port": 44730,     # set to None if no claude-dispatch daemon
    },
}
```

2. Add the SSH alias to `~/.ssh/config`:

```
Host my-new-machine
    HostName 192.168.1.50
    User yourusername
    IdentityFile ~/.ssh/id_rsa
```

3. Test SSH access: `ssh my-new-machine echo ok`

That's all. No code changes beyond `config.py` are needed. `discover_fleet()`, `scan_all()`, and `list_all_tmux()` all iterate `FLEET_MACHINES` dynamically.

## How the Universal Mux Parser Works

`src/mux_parser.py` handles both tmux and psmux output from a single `parse_mux_output(output)` call.

**Format detection (tried in order):**

1. **Pipe-delimited** — tmux with `-F '#{session_name}|#{session_created}|#{session_windows}|#{session_attached}'`
   - Detected by: first line contains 3+ `|` characters
   - `session_created` is a Unix timestamp → converted to ISO-8601
   - `session_attached` is `"0"` (not attached) or `"1"` (attached)

2. **Structured plain text** — psmux default output: `name: N windows (created DATE) (attached)`
   - Detected by: regex `^(.+?):\s+(\d+)\s+windows?`
   - Handles optional `(created DATE)` and `(attached)` groups

3. **One-name-per-line fallback** — last resort when neither format matches
   - Returns sessions with `windows=0`, `attached=False`, `created=None`

The same parser handles both formats because psmux was designed to mimic tmux's interface, so both tools use `list-sessions` and share similar semantics.

**Why pipe-delimited first?** It's the most information-rich format and unambiguous. Plain text parsing is attempted only if no `|` characters are found.

## Code Patterns

### aiohttp handlers

All REST handlers follow this pattern:

```python
async def handle_example(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "invalid JSON body"}, status=400)

    value = body.get("required_field", "")
    if not value:
        return web.json_response({"ok": False, "error": "required_field required"}, status=400)

    result = await do_something(value)
    status = 200 if result.get("ok") else 500
    return web.json_response(result, status=status)
```

### asyncio.gather for parallel I/O

All remote scans run in parallel via `asyncio.gather(*tasks, return_exceptions=True)`. Exceptions from individual machines are caught and silently skipped — a single unreachable machine never blocks results from others:

```python
results = await asyncio.gather(*tasks, return_exceptions=True)
for result in results:
    if isinstance(result, Exception):
        continue  # skip failed machines
    all_items.extend(result)
```

### Dataclasses for data models

`ClaudeSession` and `TmuxSession` are `@dataclass` with a `to_dict()` method that delegates to `dataclasses.asdict()`. This keeps serialization trivial and avoids custom JSON encoders.

### Local scan in executor

`scan_local()` reads many files synchronously (psutil, stat, file reads). To avoid blocking the aiohttp event loop, it runs in a thread pool executor:

```python
loop = asyncio.get_running_loop()
sessions = await loop.run_in_executor(None, lambda: scan_local(...))
```

### SSH scan via stdin pipe

The remote scan script (`REMOTE_SCAN_SCRIPT`) is piped to `python3 -` via stdin rather than passed as `-c "..."`. This avoids shell quoting issues with special characters in the script:

```python
proc = await asyncio.create_subprocess_exec(
    "ssh", ssh_alias, "python3", "-",
    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
)
stdout, _ = await proc.communicate(input=script.encode("utf-8"))
```

### WebSocket channel subscription

Each WebSocket connection tracks its subscribed channels in a set attribute:

```python
ws._subscribed_channels: set[str] = set()
```

`_push_to_ws(app, channel, data)` iterates all connected clients, checks their subscription set, and sends only to subscribers. Dead connections are collected and removed after each push.
