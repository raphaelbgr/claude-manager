#!/bin/bash
# Create a minimal claude-manager.app wrapper in ~/Applications so the app
# gets a proper icon in Spotlight/Launchpad/Dock. Re-run this script after
# moving the repo.
set -euo pipefail

REPO_DIR="${REPO_DIR:-$HOME/git/claude-manager}"
APP_DIR="${APP_DIR:-$HOME/Applications/claude-manager.app}"
ICON_SRC="$REPO_DIR/assets/icon.icns"
PY_BIN="$REPO_DIR/.venv/bin/python"

if [ ! -f "$ICON_SRC" ]; then
    echo "ERROR: icon not found at $ICON_SRC" >&2
    exit 1
fi
if [ ! -x "$PY_BIN" ]; then
    echo "ERROR: venv python not found at $PY_BIN (run ./setup.sh first)" >&2
    exit 1
fi

rm -rf "$APP_DIR"
mkdir -p "$APP_DIR/Contents/MacOS" "$APP_DIR/Contents/Resources"
cp "$ICON_SRC" "$APP_DIR/Contents/Resources/icon.icns"

cat > "$APP_DIR/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key>
    <string>claude-manager</string>
    <key>CFBundleDisplayName</key>
    <string>claude-manager</string>
    <key>CFBundleIdentifier</key>
    <string>dev.rbgnr.claude-manager</string>
    <key>CFBundleVersion</key>
    <string>1.0</string>
    <key>CFBundleShortVersionString</key>
    <string>1.0</string>
    <key>CFBundleExecutable</key>
    <string>run</string>
    <key>CFBundleIconFile</key>
    <string>icon</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>LSMinimumSystemVersion</key>
    <string>11.0</string>
    <key>NSHighResolutionCapable</key>
    <true/>
    <key>LSUIElement</key>
    <false/>
</dict>
</plist>
PLIST

cat > "$APP_DIR/Contents/MacOS/run" <<RUN
#!/bin/bash
cd "$REPO_DIR"
exec "$PY_BIN" -m src.main --bind 0.0.0.0 --port 44740
RUN
chmod +x "$APP_DIR/Contents/MacOS/run"

# Kick LaunchServices so Finder picks up the bundle/icon immediately
/System/Library/Frameworks/CoreServices.framework/Versions/A/Frameworks/LaunchServices.framework/Versions/A/Support/lsregister \
    -f "$APP_DIR" 2>/dev/null || true
touch "$APP_DIR"

echo "Installed: $APP_DIR"
echo "Open it from Spotlight (Cmd+Space → 'claude-manager') or drag to the Dock."
