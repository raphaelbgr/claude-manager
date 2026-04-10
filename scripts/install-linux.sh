#!/bin/bash
# Install a claude-manager .desktop entry + icon under the current user on
# a Linux host (tested on Ubuntu/GNOME). Re-run after moving the repo.
set -euo pipefail

REPO_DIR="${REPO_DIR:-$HOME/git/claude-manager}"
PY_BIN="$REPO_DIR/.venv/bin/python"
ICON_SRC="$REPO_DIR/assets/icon.png"
ICON_DST="$HOME/.local/share/icons/claude-manager.png"
DESKTOP_FILE="$HOME/.local/share/applications/claude-manager.desktop"

if [ ! -f "$ICON_SRC" ]; then
    echo "ERROR: icon not found at $ICON_SRC" >&2
    exit 1
fi
if [ ! -x "$PY_BIN" ]; then
    echo "ERROR: venv python not found at $PY_BIN (run ./setup.sh first)" >&2
    exit 1
fi

mkdir -p "$(dirname "$ICON_DST")" "$(dirname "$DESKTOP_FILE")"
cp -f "$ICON_SRC" "$ICON_DST"

cat > "$DESKTOP_FILE" <<DESKTOP
[Desktop Entry]
Type=Application
Version=1.0
Name=claude-manager
GenericName=Claude Session Manager
Comment=Fleet session manager for Claude Code and tmux
Exec=${PY_BIN} -m src.main --bind 0.0.0.0 --port 44740
Path=${REPO_DIR}
Icon=${ICON_DST}
Terminal=false
Categories=Development;Network;
StartupNotify=true
StartupWMClass=claude-manager
DESKTOP

update-desktop-database "$HOME/.local/share/applications" 2>/dev/null || true
gtk-update-icon-cache "$HOME/.local/share/icons" 2>/dev/null || true

echo "Installed: $DESKTOP_FILE"
echo "Launch from Activities (search 'claude-manager') or pin to the dock."
