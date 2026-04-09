#!/bin/bash
# Claude Manager — launches native GUI (no visible terminal)
cd "$(dirname "$0")/.."
source .venv/bin/activate 2>/dev/null
# Use pythonw or redirect to hide terminal output
exec python3 -m src.main </dev/null &>/dev/null &
disown
# Close this terminal window
osascript -e 'tell application "Terminal" to close (every window whose name contains "Claude Manager.command")' &>/dev/null &
exit 0
