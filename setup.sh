#!/usr/bin/env bash
set -euo pipefail

echo "=== claude-manager setup ==="

# Detect Python
PYTHON=""
for p in python3.12 python3.11 python3; do
    if command -v "$p" &>/dev/null; then
        PYTHON="$p"
        break
    fi
done

if [ -z "$PYTHON" ]; then
    echo "ERROR: Python 3.11+ required but not found"
    exit 1
fi

echo "Using: $PYTHON ($($PYTHON --version))"

# Create venv
if [ ! -d ".venv" ]; then
    echo "Creating virtual environment..."
    $PYTHON -m venv .venv
fi

# Activate
source .venv/bin/activate

# Install
echo "Installing dependencies..."
pip install -e ".[all]" 2>&1 | tail -5

echo ""
echo "=== Setup complete ==="
echo ""
echo "Activate:  source .venv/bin/activate"
echo "Run API:   claude-manager"
echo "Run TUI:   claude-manager --tui"
echo "Run Web:   claude-manager --enable-web"
echo "Run Desktop: claude-manager --enable-desktop"
echo "Full:      claude-manager --enable-web --bind 0.0.0.0"
