"""
Shared fixtures for claude-manager test suite.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Sample JSONL session content
# ---------------------------------------------------------------------------

SAMPLE_JSONL_LINES = [
    # First line: user message with sessionId, slug, cwd, and text content
    json.dumps({
        "type": "user",
        "sessionId": "abc123",
        "slug": "fix-login-bug",
        "cwd": "/Users/rbgnr/git/myproject",
        "message": {
            "content": "Please fix the login bug in auth.py"
        },
    }),
    # Assistant reply
    json.dumps({
        "type": "assistant",
        "sessionId": "abc123",
        "message": {
            "content": "Sure, I'll fix that now."
        },
    }),
    # Second user message
    json.dumps({
        "type": "user",
        "sessionId": "abc123",
        "slug": "fix-login-bug",
        "cwd": "/Users/rbgnr/git/myproject",
        "message": {
            "content": "Also fix the registration endpoint."
        },
    }),
]

SAMPLE_JSONL_CONTENT = "\n".join(SAMPLE_JSONL_LINES) + "\n"


@pytest.fixture
def sample_jsonl_content() -> str:
    """Valid JSONL content representing a 3-line session."""
    return SAMPLE_JSONL_CONTENT


# ---------------------------------------------------------------------------
# Sample JSONL with block-style content array
# ---------------------------------------------------------------------------

SAMPLE_JSONL_BLOCK_LINES = [
    json.dumps({
        "type": "user",
        "sessionId": "def456",
        "slug": "refactor-utils",
        "cwd": "/Users/rbgnr/git/utils",
        "message": {
            "content": [
                {"type": "text", "text": "Refactor the utility functions in utils.py"},
                {"type": "image", "source": "..."},
            ]
        },
    }),
    json.dumps({
        "type": "assistant",
        "sessionId": "def456",
        "message": {"content": "On it."},
    }),
]

SAMPLE_JSONL_BLOCK_CONTENT = "\n".join(SAMPLE_JSONL_BLOCK_LINES) + "\n"


@pytest.fixture
def sample_jsonl_block_content() -> str:
    """Valid JSONL content where user message content is a block array."""
    return SAMPLE_JSONL_BLOCK_CONTENT


# ---------------------------------------------------------------------------
# Sample active-session JSON (written to ~/.claude/sessions/<id>.json)
# ---------------------------------------------------------------------------

@pytest.fixture
def sample_session_json() -> dict:
    """Dict representing a ~/.claude/sessions/<id>.json active session entry."""
    return {
        "sessionId": "abc123",
        "pid": os.getpid(),   # current process — guaranteed alive
    }


# ---------------------------------------------------------------------------
# Mock ~/.claude directory structure
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_claude_home(tmp_path: Path, sample_jsonl_content: str) -> Path:
    """
    Temp directory mimicking a ~/.claude layout:

      <tmp>/
        projects/
          -Users-rbgnr-git-myproject/
            abc123.jsonl        ← valid session
          -Users-rbgnr-git-other/
            def456.jsonl        ← another valid session
          not-an-encoded-path/  ← should be skipped
            ignored.jsonl
        sessions/
          abc123.json           ← active session (current PID)
    """
    claude_home = tmp_path / ".claude"

    # Project 1 — single session
    proj1 = claude_home / "projects" / "-Users-rbgnr-git-myproject"
    proj1.mkdir(parents=True)
    (proj1 / "abc123.jsonl").write_text(sample_jsonl_content, encoding="utf-8")

    # Project 2 — another session
    proj2 = claude_home / "projects" / "-Users-rbgnr-git-other"
    proj2.mkdir(parents=True)
    (proj2 / "def456.jsonl").write_text(
        "\n".join([
            json.dumps({
                "type": "user",
                "sessionId": "def456",
                "slug": "other-work",
                "cwd": "/Users/rbgnr/git/other",
                "message": {"content": "Do the other thing"},
            }),
        ]) + "\n",
        encoding="utf-8",
    )

    # Non-encoded folder — must be skipped by scan_local
    skip_dir = claude_home / "projects" / "not-an-encoded-path"
    skip_dir.mkdir(parents=True)
    (skip_dir / "ignored.jsonl").write_text('{"type":"user"}\n', encoding="utf-8")

    # Active sessions dir — abc123 is "alive" (uses current PID)
    sessions_dir = claude_home / "sessions"
    sessions_dir.mkdir(parents=True)
    (sessions_dir / "abc123.json").write_text(
        json.dumps({"sessionId": "abc123", "pid": os.getpid()}),
        encoding="utf-8",
    )

    return claude_home
