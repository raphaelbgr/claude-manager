#!/usr/bin/env bash
set -euo pipefail
# claude-manager installer for Linux
# Usage: curl -fsSL .../install-linux.sh | bash

echo "Installing claude-manager..."

# Check/install Python 3.11+
if ! command -v python3 &>/dev/null; then
    if command -v apt &>/dev/null; then
        sudo apt update && sudo apt install -y python3 python3-venv python3-pip git
    elif command -v dnf &>/dev/null; then
        sudo dnf install -y python3 python3-pip git
    else
        echo "Please install Python 3.11+ manually"
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
pip install -e ".[all]"

# .desktop entry
mkdir -p "$HOME/.local/share/applications"
cat > "$HOME/.local/share/applications/claude-manager.desktop" << EOF
[Desktop Entry]
Name=Claude Manager
Comment=Fleet Session Manager for Claude Code
Exec=$INSTALL_DIR/.venv/bin/python3 -m src.main
Path=$INSTALL_DIR
Icon=utilities-terminal
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

echo ""
echo "claude-manager installed!"
echo "  Launch: search 'Claude Manager' in app launcher"
echo "  CLI: ~/.claude-manager/.venv/bin/python3 -m src.main"
echo "  Service: systemctl --user start claude-manager"
