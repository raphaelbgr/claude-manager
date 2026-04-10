#!/usr/bin/env bash
set -euo pipefail
# claude-manager installer for Linux
# Usage: curl -fsSL .../install-linux.sh | bash

echo "Installing claude-manager..."

# Check/install Python 3.11+
NEED_PYTHON=false
if ! command -v python3 &>/dev/null; then
    NEED_PYTHON=true
else
    PY_VER=$(python3 -c 'import sys; print(sys.version_info.minor)')
    if [ "$PY_VER" -lt 11 ] || [ "$PY_VER" -ge 14 ]; then
        NEED_PYTHON=true
    fi
fi

if $NEED_PYTHON; then
    if command -v apt &>/dev/null; then
        sudo apt update && sudo apt install -y python3 python3-venv python3-pip git
    elif command -v dnf &>/dev/null; then
        sudo dnf install -y python3 python3-pip git
    else
        echo "Please install Python 3.11-3.13 manually"
        exit 1
    fi
fi

INSTALL_DIR="$HOME/.claude-manager"
if [ -d "$INSTALL_DIR" ]; then
    cd "$INSTALL_DIR" && git pull
else
    git clone https://github.com/raphaelbgr/claude-manager.git "$INSTALL_DIR"
fi
cd "$INSTALL_DIR"

python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip

# Base dependencies (always works)
pip install -e "."
echo "  Base dependencies installed."

# Desktop extras (pywebview + Pillow)
if pip install -e ".[desktop]" 2>/dev/null; then
    echo "  Desktop extras installed (native GUI available)."
else
    echo "  Desktop extras failed — GUI will fall back to browser mode."
fi

# TUI extras
if pip install -e ".[tui]" 2>/dev/null; then
    echo "  TUI extras installed (--tui mode available)."
else
    echo "  TUI extras failed — --tui mode unavailable."
fi

# System tray extras (pystray)
if pip install pystray Pillow 2>/dev/null; then
    echo "  System tray extras installed."
else
    echo "  System tray extras failed — tray icon unavailable."
fi

# .desktop entry
ICON_PATH="utilities-terminal"
if [ -f "$INSTALL_DIR/assets/icon.png" ]; then
    ICON_PATH="$INSTALL_DIR/assets/icon.png"
fi

mkdir -p "$HOME/.local/share/applications"
cat > "$HOME/.local/share/applications/claude-manager.desktop" << EOF
[Desktop Entry]
Name=Claude Manager
Comment=Fleet Session Manager for Claude Code
Exec=$INSTALL_DIR/.venv/bin/python3 -m src.main
Path=$INSTALL_DIR
Icon=$ICON_PATH
Terminal=false
Type=Application
Categories=Development;
EOF

# systemd user service
mkdir -p "$HOME/.config/systemd/user"
cat > "$HOME/.config/systemd/user/claude-manager.service" << EOF
[Unit]
Description=Claude Manager API Server
After=network.target

[Service]
Type=simple
WorkingDirectory=$INSTALL_DIR
ExecStart=$INSTALL_DIR/.venv/bin/python3 -m src.main --api-only
Restart=on-failure

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable claude-manager

# Verify SSH is available
if command -v ssh &>/dev/null; then
    echo "  SSH found — fleet scanning will work."
else
    echo "  WARNING: SSH not found — install with: sudo apt install openssh-client"
fi

# Verify tmux is available
if command -v tmux &>/dev/null; then
    echo "  tmux found: $(tmux -V)"
else
    echo "  WARNING: tmux not found — install with: sudo apt install tmux"
fi

echo ""
echo "claude-manager installed!"
echo "  Launch: search 'Claude Manager' in app launcher"
echo "  CLI: ~/.claude-manager/.venv/bin/python3 -m src.main"
echo "  Service: systemctl --user start claude-manager"
