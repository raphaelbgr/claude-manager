#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_PYTHON="$SCRIPT_DIR/.venv/bin/python3"
SERVICE_NAME="claude-manager"

# Check venv exists
if [ ! -f "$VENV_PYTHON" ]; then
    echo "ERROR: Run setup.sh first"
    exit 1
fi

case "$(uname -s)" in
    Darwin)
        # macOS: launchd plist
        PLIST="$HOME/Library/LaunchAgents/com.claude-manager.plist"
        cat > "$PLIST" << HEREDOC
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.claude-manager</string>
    <key>ProgramArguments</key>
    <array>
        <string>$VENV_PYTHON</string>
        <string>-m</string>
        <string>src.main</string>
        <string>--enable-web</string>
        <string>--bind</string>
        <string>0.0.0.0</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$SCRIPT_DIR</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>$HOME/Library/Logs/claude-manager.log</string>
    <key>StandardErrorPath</key>
    <string>$HOME/Library/Logs/claude-manager.err</string>
</dict>
</plist>
HEREDOC
        launchctl load "$PLIST"
        echo "Installed: launchd service (com.claude-manager)"
        echo "Logs: ~/Library/Logs/claude-manager.log"
        echo "Stop: launchctl unload $PLIST"
        ;;

    Linux)
        # Linux: systemd user service
        UNIT_DIR="$HOME/.config/systemd/user"
        mkdir -p "$UNIT_DIR"
        cat > "$UNIT_DIR/$SERVICE_NAME.service" << HEREDOC
[Unit]
Description=Claude Manager
After=network.target

[Service]
Type=simple
WorkingDirectory=$SCRIPT_DIR
ExecStart=$VENV_PYTHON -m src.main --enable-web --bind 0.0.0.0
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
HEREDOC
        systemctl --user daemon-reload
        systemctl --user enable "$SERVICE_NAME"
        systemctl --user start "$SERVICE_NAME"
        echo "Installed: systemd user service ($SERVICE_NAME)"
        echo "Logs: journalctl --user -u $SERVICE_NAME"
        echo "Stop: systemctl --user stop $SERVICE_NAME"
        ;;

    *)
        echo "Windows: Use Task Scheduler to run:"
        echo "  $VENV_PYTHON -m src.main --enable-web --bind 0.0.0.0"
        echo "  Working directory: $SCRIPT_DIR"
        ;;
esac
