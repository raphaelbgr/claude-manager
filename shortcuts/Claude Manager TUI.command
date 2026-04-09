#!/bin/bash
# Claude Manager TUI — Terminal UI mode
cd "$(dirname "$0")/.."
source .venv/bin/activate 2>/dev/null
python3 -m src.main --tui
