# Fleet Setup Guide

How to add your computers to claude-manager.

## Prerequisites

- SSH access between machines (key-based, no password)
- Python 3.11+ on each machine
- Claude Code installed on each machine

## Step 1: SSH Key Setup

claude-manager connects to remote machines via SSH. You need passwordless key-based auth.

### Generate a key (if you don't have one)

```bash
ssh-keygen -t ed25519 -C "claude-manager"
```

### Copy your key to each machine

```bash
ssh-copy-id user@machine-ip
```

### Test the connection

```bash
ssh machine-alias echo "OK"
```

### SSH Config (recommended)

Add aliases to `~/.ssh/config` on the machine running claude-manager:

```
Host my-desktop
    HostName 192.168.1.100
    User myuser
    IdentityFile ~/.ssh/id_ed25519
    ServerAliveInterval 30

Host my-linux-box
    HostName 192.168.1.101
    User myuser
    IdentityFile ~/.ssh/id_ed25519
    ServerAliveInterval 30

Host my-windows-pc
    HostName 192.168.1.102
    User myuser
    IdentityFile ~/.ssh/id_ed25519
```

Test each entry:

```bash
ssh my-desktop echo ok
ssh my-linux-box echo ok
ssh my-windows-pc echo ok
```

All three must respond without prompting for a password.

## Step 2: Configure Fleet Machines

Edit `src/config.py` and add your machines to `FLEET_MACHINES`:

```python
FLEET_MACHINES: dict[str, dict] = {
    "my-mac": {
        "ip": "192.168.1.100",
        "os": "darwin",        # darwin, linux, or win32
        "ssh_alias": "my-mac", # matches ~/.ssh/config Host
        "mux": "tmux",         # tmux (macOS/Linux) or psmux (Windows)
        "dispatch_port": None,  # 44730 if running claude-dispatch daemon
    },
    "my-linux": {
        "ip": "192.168.1.101",
        "os": "linux",
        "ssh_alias": "my-linux",
        "mux": "tmux",
        "dispatch_port": None,
    },
    "my-windows": {
        "ip": "192.168.1.102",
        "os": "win32",
        "ssh_alias": "my-windows",
        "mux": "psmux",
        "dispatch_port": None,
    },
}
```

### Required fields

| Field | Description | Values |
|-------|-------------|--------|
| `ip` | LAN IP address | e.g. `192.168.1.100` |
| `os` | Operating system | `darwin`, `linux`, `win32` |
| `ssh_alias` | SSH config alias or hostname | matches `Host` in `~/.ssh/config` |
| `mux` | Terminal multiplexer | `tmux` (macOS/Linux), `psmux` (Windows) |
| `dispatch_port` | claude-dispatch daemon port | `44730` or `None` |

### Local machine detection

claude-manager automatically detects which fleet machine it is running on by comparing the system hostname and local IP addresses against the `FLEET_MACHINES` keys and IPs. It skips SSH for the local machine and reads session files directly from disk.

You do not need to add a special entry for the local machine — detection is automatic. If detection fails (the hostname or IP does not match any entry), the local machine is treated as unrecognised, and all scanning is done via SSH.

## Step 3: Install Dependencies on Each Remote Machine

Each remote machine needs Python 3 for the SSH-based scan fallback. No pip packages are required for the remote scan script — it uses stdlib only.

```bash
# macOS
brew install python3 tmux

# Ubuntu/Debian
sudo apt install python3 tmux

# Windows (Git Bash + psmux)
# Install Git for Windows: https://git-scm.com/downloads
# Install psmux: https://github.com/psmux/psmux
```

Verify Python is reachable over SSH:

```bash
ssh my-linux-box python3 --version
ssh my-windows-pc python3 --version   # requires Python in Git Bash PATH
```

## Step 4: Optional — claude-dispatch Daemon

For faster scanning (HTTP API instead of SSH), install the
[claude-dispatch](https://github.com/raphaelbgr/claude-dispatch) daemon on each machine.

| Method | Speed | Requirement |
|--------|-------|-------------|
| SSH fallback | ~3s per machine | Python 3, SSH key |
| HTTP (dispatch daemon) | ~200ms per machine | claude-dispatch running on port 44730 |

With the daemon running, set `dispatch_port: 44730` for that machine:

```python
"my-linux": {
    "ip": "192.168.1.101",
    "os": "linux",
    "ssh_alias": "my-linux",
    "mux": "tmux",
    "dispatch_port": 44730,   # enables HTTP fast path
},
```

If the daemon is unreachable, claude-manager automatically falls back to SSH.

## Step 5: Verify

Start claude-manager and check the fleet bar:

```bash
source .venv/bin/activate
claude-manager --enable-web --bind 0.0.0.0
```

Open `http://localhost:44740` and look at the bottom bar. Each machine shows green (online) or red (offline). You can also query the fleet status directly:

```bash
curl http://localhost:44740/api/fleet
```

Each entry has `"online": true` or `"online": false` and a `"method"` field (`"http"`, `"ssh"`, or `"unreachable"`).

## Step 6: Auto-Update via Watchdog (Recommended)

claude-manager integrates with the [claude-dispatch](https://github.com/raphaelbgr/claude-dispatch) watchdog system for automatic updates and crash recovery across the fleet.

### Architecture — The Mesh

```
┌─────────────────────────────────────────────────────────────┐
│                     Each Fleet Machine                       │
│                                                              │
│  ┌──────────────────┐  ┌──────────────────┐                 │
│  │ claude-dispatch   │  │ claude-dispatch   │                │
│  │ daemon.py :44730  │  │ updater.py :44731 │ ← Watchdog    │
│  │ (job queue +      │  │ (git poll, test,  │                │
│  │  /sessions,       │  │  restart daemon,  │                │
│  │  /tmux,           │  │  sidecar monitor) │                │
│  │  /browse,         │  │                   │                │
│  │  /drives,         │  │  Monitors:        │                │
│  │  /tmux/capture)   │  │  - dispatch daemon│                │
│  └──────────────────┘  │  - claude-manager  │                │
│                         │  - personal-cloud  │                │
│  ┌──────────────────┐  └──────────────────┘                 │
│  │ claude-manager    │                                       │
│  │ :44740 (API+GUI)  │  ← Uses dispatch as infrastructure   │
│  │ REST+WS+Desktop   │                                       │
│  └──────────────────┘                                       │
└─────────────────────────────────────────────────────────────┘
```

### Related Repositories

| Repo | Purpose | Port |
|------|---------|------|
| [claude-dispatch](https://github.com/raphaelbgr/claude-dispatch) | Fleet mesh daemon + watchdog | 44730 (daemon), 44731 (watchdog) |
| [claude-manager](https://github.com/raphaelbgr/claude-manager) | Session manager (this project) | 44740 |
| [personal-cloud](https://github.com/raphaelbgr/personal-cloud) | Clipboard/screenshot daemon | — |

### How Auto-Update Works

The watchdog (`updater.py` on port 44731) runs on each machine and:

1. **Polls git** every 120 seconds for new commits on `main`/`master`
2. **Pulls changes** (`git pull --ff-only`)
3. **Runs tests** (5 min timeout) to verify the update is safe
4. **Restarts the daemon** if tests pass
5. **Auto-rollbacks** if tests fail or health checks fail after restart
6. **Writes failures** to `~/git/claude-kb-vault/pending-actions/` for manual review

### Enable Watchdog Monitoring for claude-manager

The dispatch watchdog can monitor claude-manager as a **sidecar service**. It will:
- Probe `http://localhost:44740/health` every 30s
- Auto-restart claude-manager if 3 consecutive health checks fail
- Expose management via `GET/POST /services/claude-manager`

To register claude-manager with the watchdog, the dispatch daemon's sidecar config detects services with a `/health` endpoint on known ports. Since claude-manager runs on `:44740`, it's automatically discovered.

### Boot Services

The installers (`installers/install-*.sh`) create boot services:

| OS | Daemon Service | Location |
|----|---------------|----------|
| macOS | launchd | `~/Library/LaunchAgents/com.rbgnr.claude-manager.plist` |
| Linux | systemd user | `~/.config/systemd/user/claude-manager.service` |
| Windows | Task Scheduler | "Claude Manager" (runs at logon) |

### Manual Fleet Deploy

Push code to GitHub → the watchdog on each machine auto-pulls within 120s. No SSH needed.

To force immediate deploy on a specific machine:

```bash
# Via watchdog API (if available)
curl -X POST http://192.168.7.102:44731/watchdog/deploy -d '{"commit": "main"}'

# Via SSH (direct)
ssh mac-mini "cd ~/git/claude-manager && git pull && systemctl --user restart claude-manager"
```

### Verifying the Mesh

```bash
# Check dispatch daemon health on all machines
curl http://192.168.7.102:44730/health   # mac-mini
curl http://192.168.7.13:44730/health    # ubuntu-desktop
curl http://192.168.7.103:44730/health   # avell-i7

# Check watchdog status
curl http://192.168.7.102:44731/watchdog/health

# Check claude-manager health
curl http://192.168.7.102:44740/health

# Check sidecar services
curl http://192.168.7.102:44731/services
```

## Troubleshooting

### Machine shows offline

Check SSH connectivity first:

```bash
# Test with strict batch mode (no password prompt)
ssh -o BatchMode=yes my-desktop echo ok

# Check the IP is reachable
ping 192.168.1.100
```

Check the SSH config alias matches exactly what is in `FLEET_MACHINES`:

```bash
grep "ssh_alias" src/config.py
grep "^Host " ~/.ssh/config
```

### No sessions found on remote machine

Verify Claude Code is installed and session files exist:

```bash
ssh my-desktop which claude
ssh my-desktop "ls ~/.claude/projects/ | head -10"
```

Verify the SSH fallback scan script can run:

```bash
ssh my-desktop python3 -c "import json, os, pathlib; print('ok')"
```

If using the claude-dispatch daemon, verify it is healthy:

```bash
curl http://192.168.1.100:44730/health
```

### Windows SSH issues

Windows machines must use **PowerShell as the default SSH shell** (not Git Bash). Git Bash causes terminal scroll corruption over SSH.

Set PowerShell as default (run on each Windows machine):
```powershell
New-ItemProperty -Path 'HKLM:\SOFTWARE\OpenSSH' -Name DefaultShell -Value 'C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe' -PropertyType String -Force
```

Key points:

- SSH lands in PowerShell — claude-manager handles this automatically
- Git Bash is available if needed: `bash -c 'command'` from PowerShell
- The `export PATH=...` prefix is NOT sent to Windows targets (PowerShell syntax)
- psmux sessions use cmd.exe internally; the CommandAdapter translates commands correctly

Check the Windows SSH server is running:

```powershell
Get-Service sshd
Start-Service sshd
```

Test from your local machine:

```bash
ssh -o BatchMode=yes my-windows-pc echo ok
```

### Session rename shows "No active PID file found"

Session renaming requires an active PID file in `~/.claude/sessions/<pid>.json`. The file is only present while the Claude Code process is running. You can only rename active or working sessions (status `active` or `working`), not idle ones.

### Ports blocked by firewall

If using the claude-dispatch daemon (port 44730), ensure the port is open:

```bash
# Linux
sudo ufw allow 44730

# macOS — add a rule in System Settings > Firewall, or:
sudo pfctl -e
```

No firewall changes are needed for the SSH fallback — it uses standard SSH (port 22).
