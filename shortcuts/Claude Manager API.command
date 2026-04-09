#!/bin/bash
# Claude Manager API — Headless server mode
cd "$(dirname "$0")/.."
source .venv/bin/activate 2>/dev/null
python3 -m src.main --api-only
