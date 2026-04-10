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

Windows SSH (via Git Bash) requires Git for Windows to be installed. The SSH server on Windows must be the OpenSSH Server included with Windows 10/11 or Git for Windows.

Key points:

- Install Git for Windows, which includes Git Bash and OpenSSH client/server
- The SSH session lands in Git Bash by default — claude-manager handles this automatically
- psmux sessions use cmd.exe internally; the CommandAdapter translates commands correctly for each shell

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
