#!/usr/bin/env python3
"""Idempotent venv creator for fleet-watchdog install_cmd.

`python -m venv .venv` overwrites `python.exe` in the target dir, which fails
with EACCES on Windows whenever the existing venv's interpreter is in use
(e.g. the previous claude-manager is still running when fleet-watchdog runs
the auto-update install_cmd). We skip creation if a usable interpreter
already exists; the subsequent pip install step works against either a
freshly-created or an existing venv.
"""
from __future__ import annotations

import os
import sys
import venv


def main() -> int:
    venv_dir = ".venv"
    py = os.path.join(
        venv_dir,
        "Scripts" if sys.platform == "win32" else "bin",
        "python.exe" if sys.platform == "win32" else "python",
    )
    if os.path.isfile(py):
        print(f"setup-venv: {py} already exists — skipping venv creation")
        return 0
    print(f"setup-venv: creating {venv_dir}")
    venv.create(venv_dir, with_pip=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
