#!/usr/bin/env python3
"""Generate VERSION.json from git state.

Writes the file to the repo root. Run before commit or as part of CI/install.

Fields:
    version:     monotonic int (git rev-list --count HEAD, includes THIS commit)
    commit:      short hash
    commit_full: full hash
    branch:      current branch name
    date:        commit date (ISO 8601)
    message:     first line of commit message
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def _git(*args: str, repo: Path) -> str:
    return subprocess.check_output(["git", *args], cwd=repo, text=True, timeout=5).strip()


def generate(repo: Path) -> dict:
    # +1 so the next commit (which will include this VERSION.json bump) gets a unique int
    count = int(_git("rev-list", "--count", "HEAD", repo=repo))
    return {
        "version": count + 1,
        "commit": _git("rev-parse", "--short", "HEAD", repo=repo),
        "commit_full": _git("rev-parse", "HEAD", repo=repo),
        "branch": _git("rev-parse", "--abbrev-ref", "HEAD", repo=repo),
        "date": _git("log", "-1", "--format=%cI", repo=repo),
        "message": _git("log", "-1", "--format=%s", repo=repo),
    }


def main() -> int:
    repo = Path(__file__).resolve().parent.parent
    try:
        data = generate(repo)
    except subprocess.CalledProcessError as exc:
        print(f"gen_version: git failed: {exc}", file=sys.stderr)
        return 1

    out = repo / "VERSION.json"
    out.write_text(json.dumps(data, indent=2) + "\n")
    print(f"VERSION.json v{data['version']} ({data['commit']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
