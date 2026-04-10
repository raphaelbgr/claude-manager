# Deployment

## Version

Current release: **1.0.0**

## Requirements

- Python 3.11 or 3.12
- SSH key-based authentication configured for each remote fleet machine (`~/.ssh/config` entries recommended)
- tmux installed on Linux/macOS machines; psmux on Windows machines

## Quick Install (curl one-liner)

### macOS

```bash
curl -fsSL https://raw.githubusercontent.com/raphaelbgr/claude-manager/master/installers/install-macos.sh | bash
```

### Linux

```bash
curl -fsSL https://raw.githubusercontent.com/raphaelbgr/claude-manager/master/installers/install-linux.sh | bash
```

### Windows (PowerShell)

```powershell
irm https://raw.githubusercontent.com/raphaelbgr/claude-manager/master/installers/install-windows.ps1 | iex
```

The platform-specific installers in `installers/` clone the repository, run `setup.sh`, and register the system service in one step. For manual control, follow the steps below.

## setup.sh

`setup.sh` handles the full local installation:

```bash
./setup.sh
```

What it does:
1. Detects Python 3.12 / 3.11 / 3 in order
2. Creates a `.venv` virtual environment if one doesn't exist
3. Runs `pip install -e ".[all]"` (installs core + TUI + desktop extras)

After setup, activate the environment and run:

```bash
source .venv/bin/activate
claude-manager --enable-web --bind 0.0.0.0
```

### Dependency extras

| Extra | Packages | When to use |
|-------|----------|-------------|
| *(none)* | `aiohttp`, `psutil` | API server only |
| `tui` | `textual>=3.0` | TUI mode (`--tui`) |
| `desktop` | `pystray`, `pywebview`, `Pillow` | Native window (`--enable-gui`) |
| `all` | all of the above | Full install (`setup.sh` default) |

Install a specific extra:
```bash
pip install -e ".[tui]"
pip install -e ".[desktop]"
```

## Running as a Background Daemon

`install.sh` registers claude-manager as a system service that starts automatically on boot.

```bash
# Run setup.sh first, then:
./install.sh
```

### macOS — launchd

`install.sh` writes `~/Library/LaunchAgents/com.claude-manager.plist` and loads it immediately.

```bash
# Uninstall / stop
launchctl unload ~/Library/LaunchAgents/com.claude-manager.plist
rm ~/Library/LaunchAgents/com.claude-manager.plist

# View logs
tail -f ~/Library/Logs/claude-manager.log
tail -f ~/Library/Logs/claude-manager.err

# Reload after config change
launchctl unload ~/Library/LaunchAgents/com.claude-manager.plist
launchctl load ~/Library/LaunchAgents/com.claude-manager.plist
```

### Linux — systemd user service

`install.sh` writes `~/.config/systemd/user/claude-manager.service` and enables + starts it.

```bash
# Check status
systemctl --user status claude-manager

# View logs
journalctl --user -u claude-manager -f

# Stop / restart
systemctl --user stop claude-manager
systemctl --user restart claude-manager

# Uninstall
systemctl --user disable --now claude-manager
rm ~/.config/systemd/user/claude-manager.service
```

Note: user systemd services require `loginctl enable-linger <user>` to run without an active login session.

### Windows — Task Scheduler

`install.sh` prints the command to register manually. In PowerShell (as administrator or via Task Scheduler GUI):

```powershell
$python = "C:\path\to\claude-manager\.venv\Scripts\python.exe"
$script = "C:\path\to\claude-manager"

$action = New-ScheduledTaskAction `
    -Execute $python `
    -Argument "-m src.main --enable-web --bind 0.0.0.0" `
    -WorkingDirectory $script

$trigger = New-ScheduledTaskTrigger -AtLogon

Register-ScheduledTask `
    -TaskName "claude-manager" `
    -Action $action `
    -Trigger $trigger `
    -RunLevel Highest
```

## System Tray (Desktop GUI)

The `--enable-gui` flag opens a native desktop window backed by the same API server. On Linux and Windows, a system tray icon is also created (requires `pystray` and `Pillow`, both installed by the `desktop` extra).

```bash
claude-manager --enable-gui
```

The tray icon provides:
- **Open Web UI** — opens `http://localhost:44740` in the default browser
- **Running Sessions** — lists all active/working sessions grouped by machine; clicking one launches a terminal for that session
- **Tmux / Psmux Sessions** — lists mux sessions; clicking one attaches
- **Force Scan** — triggers an immediate rescan
- **Exit** — calls `POST /api/exit` to shut down the server cleanly, then closes the tray icon

On macOS, pywebview requires the main thread for its AppKit integration, so the tray icon is not available on macOS. Use the Web UI or TUI instead.

### Exiting

From the Web UI: click the exit button (top-right menu).
From the system tray: click **Exit**.
Via API: `curl -X POST http://localhost:44740/api/exit`

`/api/exit` gives the server 0.5 seconds to respond, then calls `os._exit(0)`. The launchd/systemd service will restart the process automatically if `KeepAlive` / `Restart=always` is set.

To exit without restarting (e.g. for maintenance), stop the service first:

```bash
# macOS
launchctl unload ~/Library/LaunchAgents/com.claude-manager.plist

# Linux
systemctl --user stop claude-manager
```

## LAN Access Configuration

To make the Web UI accessible from other machines on your network:

```bash
# Bind to all interfaces
claude-manager --enable-web --bind 0.0.0.0

# Custom port
claude-manager --enable-web --bind 0.0.0.0 --port 44740
```

The startup banner prints the detected LAN IP:
```
  API server  →  http://0.0.0.0:44740
  LAN URL     →  http://192.168.1.10:44740
```

Open `http://192.168.1.10:44740` from any machine on your network.

## Fleet Machine Setup

See [Fleet Setup Guide](fleet-setup.md) for a full walkthrough covering SSH key setup, `src/config.py` configuration, per-OS dependency installation, and troubleshooting.

Quick reference for the most common steps:

### 1. SSH access

Add entries to `~/.ssh/config` on the machine running claude-manager:

```
Host mac-mini
    HostName 192.168.1.10
    User rbgnr
    IdentityFile ~/.ssh/id_rsa
    ServerAliveInterval 30

Host ubuntu-desktop
    HostName 192.168.1.11
    User rbgnr
    IdentityFile ~/.ssh/id_rsa
```

Test connectivity:
```bash
ssh mac-mini echo ok
ssh ubuntu-desktop echo ok
```

### 2. Python 3 (for SSH scan fallback)

The SSH fallback script requires only Python 3 stdlib — no pip packages needed:

```bash
ssh ubuntu-desktop python3 --version
```

### 3. tmux (Linux/macOS) or psmux (Windows)

```bash
# macOS
brew install tmux

# Ubuntu/Debian
sudo apt install tmux

# Windows — install psmux from https://github.com/psmux/psmux
```

### 4. claude-dispatch daemon (optional, recommended)

If you run the [claude-dispatch](https://github.com/raphaelbgr/claude-dispatch) daemon on a machine (port 44730), claude-manager will use its HTTP API instead of SSH for session/tmux queries. This is faster and more reliable than SSH polling.

Set `dispatch_port: 44730` for that machine in `src/config.py`.

If the daemon is not running or unreachable, claude-manager automatically falls back to SSH.

## Port Reference

| Port | Service |
|------|---------|
| 44740 | claude-manager API + Web UI |
| 44730 | claude-dispatch daemon (optional, on remote machines) |

## Environment Variables

No environment variables are required. Configuration is in `src/config.py`.
