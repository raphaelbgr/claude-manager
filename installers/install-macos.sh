#!/usr/bin/env bash
set -euo pipefail
# claude-manager installer for macOS
# Usage: curl -fsSL .../install-macos.sh | bash

echo "Installing claude-manager..."

# Check/install Python 3.11+ (but not 3.14+)
NEED_PYTHON=false
if ! command -v python3 &>/dev/null; then
    NEED_PYTHON=true
else
    PY_VER=$(python3 -c 'import sys; print(f"{sys.version_info.minor}")')
    if [ "$PY_VER" -lt 11 ] || [ "$PY_VER" -ge 14 ]; then
        NEED_PYTHON=true
    fi
fi

if $NEED_PYTHON; then
    echo "Python 3.11-3.13 required. Installing via Homebrew..."
    if ! command -v brew &>/dev/null; then
        /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
    fi
    brew install python@3.12
fi

# Clone or update
INSTALL_DIR="$HOME/.claude-manager"
if [ -d "$INSTALL_DIR" ]; then
    cd "$INSTALL_DIR" && git pull
else
    git clone https://github.com/raphaelbgr/claude-manager.git "$INSTALL_DIR"
fi
cd "$INSTALL_DIR"

# Create venv and install deps
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

# Create .app bundle
APP_DIR="$INSTALL_DIR/shortcuts/Claude Manager.app/Contents/MacOS"
RES_DIR="$INSTALL_DIR/shortcuts/Claude Manager.app/Contents/Resources"
mkdir -p "$APP_DIR" "$RES_DIR"

# Copy icon if available
if [ -f "$INSTALL_DIR/assets/icon.icns" ]; then
    cp "$INSTALL_DIR/assets/icon.icns" "$RES_DIR/AppIcon.icns"
fi

cat > "$APP_DIR/launch" << 'SCRIPT'
#!/bin/bash
cd "$(dirname "$0")/../../../../"
source .venv/bin/activate 2>/dev/null
exec python3 -m src.main 2>/dev/null
SCRIPT
chmod +x "$APP_DIR/launch"

cat > "$INSTALL_DIR/shortcuts/Claude Manager.app/Contents/Info.plist" << 'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleExecutable</key><string>launch</string>
    <key>CFBundleName</key><string>Claude Manager</string>
    <key>CFBundleIdentifier</key><string>com.raphaelbgr.claude-manager</string>
    <key>CFBundleVersion</key><string>1.0.1</string>
    <key>CFBundleIconFile</key><string>AppIcon</string>
    <key>LSUIElement</key><true/>
    <key>CFBundlePackageType</key><string>APPL</string>
</dict>
</plist>
PLIST

# Desktop shortcut
ln -sf "$INSTALL_DIR/shortcuts/Claude Manager.app" "$HOME/Desktop/Claude Manager.app"

# Add to PATH
SHELL_RC="$HOME/.zshrc"
[ -f "$HOME/.bashrc" ] && SHELL_RC="$HOME/.bashrc"
if ! grep -q "claude-manager" "$SHELL_RC" 2>/dev/null; then
    echo "export PATH=\"$INSTALL_DIR/.venv/bin:\$PATH\"" >> "$SHELL_RC"
    echo "alias claude-manager='cd $INSTALL_DIR && source .venv/bin/activate && python3 -m src.main'" >> "$SHELL_RC"
fi

echo ""
echo "claude-manager installed!"
echo "  Desktop shortcut: ~/Desktop/Claude Manager.app"
echo "  CLI: claude-manager (restart terminal first)"
echo "  Web: http://$(hostname -I 2>/dev/null || echo localhost):44740"
echo ""
echo "  Double-click 'Claude Manager' on your Desktop to start."
echo "  Note: System tray is available on Linux/Windows only."
