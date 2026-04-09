#!/bin/bash
# Claude Manager — Double-click to launch
cd "$(dirname "$0")/.."
source .venv/bin/activate 2>/dev/null
python3 -m src.main
