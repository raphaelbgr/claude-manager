#!/bin/bash
cd "$(dirname "$(readlink -f "$0")")/.."
source .venv/bin/activate 2>/dev/null
python3 -m src.main
