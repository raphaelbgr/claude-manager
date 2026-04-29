"""
Shared fixtures for claude-manager test suite.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Disable the asyncssh pool during tests.
#
# SSHExecutor.exec_shell routes through a persistent asyncssh connection pool
# in production. Tests mock `asyncio.create_subprocess_exec` and expect ALL
# remote commands to flow through subprocess-ssh — the pool would otherwise
# connect to real fleet hosts and bypass those mocks.
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _disable_ssh_pool(monkeypatch):
    """Force SSHExecutor.exec_shell to skip the pool and take the subprocess
    fallback. Flipping src.ssh_pool.asyncssh to None makes the `if _asyncssh
    is not None` guard fail → the except path logs and falls through to the
    legacy subprocess-ssh code, which is what tests mock."""
    import src.ssh_pool as _pool
    monkeypatch.setattr(_pool, "asyncssh", None, raising=False)
    yield


# ---------------------------------------------------------------------------
# HARD-BLOCK real terminal spawns during every test, at the subprocess layer.
#
# Symptom: tests bloated the macOS Desktop with Terminal.app / iTerm windows
# because some code path executed osascript / open / gnome-terminal / wt
# without its surrounding mock. Stubbing at launcher-function granularity
# breaks the suite that exercises those launcher helpers directly.
#
# Surgical fix: wrap the three subprocess entry points used on every platform
# to spawn a GUI terminal — subprocess.Popen, subprocess.run,
# asyncio.create_subprocess_exec — and short-circuit them ONLY when argv[0]
# is a known terminal-spawning binary. Every other subprocess call passes
# through unchanged, so tests that rely on real subprocess semantics (for
# non-terminal commands) keep working. Tests that need to assert on how these
# spawners were invoked still apply their own `patch(...)` which replaces
# our filter inside the nested `with` block.
# ---------------------------------------------------------------------------

_TERMINAL_SPAWN_BINARIES = {
    "osascript",                           # macOS AppleScript
    "open",                                 # macOS `open -a Terminal.app`
    "gnome-terminal", "konsole", "xterm",  # Linux
    "alacritty", "kitty", "terminator",
    "xfce4-terminal", "lxterminal", "tilix",
    "wt.exe", "wt",                         # Windows Terminal
    "cmd.exe",                              # cmd start /wait
    "powershell.exe", "pwsh.exe", "pwsh",   # PowerShell spawns
    "iterm", "iterm2",
}


def _is_terminal_spawn(cmd):
    import os as _os
    if not cmd:
        return False
    if isinstance(cmd, (list, tuple)):
        head = cmd[0]
    elif isinstance(cmd, str):
        head = cmd.split()[0] if cmd.strip() else ""
    else:
        return False
    name = _os.path.basename(str(head)).lower()
    return name in _TERMINAL_SPAWN_BINARIES


# ---------------------------------------------------------------------------
# Disable the launch_terminal auto-pick during tests.
#
# In production, launch_terminal() probes installed terminals on the daemon
# host and routes through the highest-priority adapter when no terminal_id is
# supplied. Unit tests assert the legacy _launch_macos / _launch_linux /
# _launch_windows dispatch — auto-pick would short-circuit that path and the
# assertions would never fire. Force auto-pick to return None (= "no adapter
# picked, fall through to legacy"). Tests that specifically exercise the
# auto-pick path can override this fixture.
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _disable_terminal_auto_pick(monkeypatch):
    try:
        import src.launcher as _launcher
    except ImportError:
        yield
        return
    from unittest.mock import AsyncMock
    monkeypatch.setattr(
        _launcher, "_auto_pick_local_adapter_id",
        AsyncMock(return_value=None), raising=False,
    )
    # Also clear the per-process cache so state doesn't leak across tests.
    if hasattr(_launcher, "_AUTO_ADAPTER_CACHE"):
        _launcher._AUTO_ADAPTER_CACHE.clear()
    yield


@pytest.fixture(autouse=True)
def _block_real_terminal_spawns(monkeypatch):
    import os
    import subprocess
    import asyncio as _asyncio
    from unittest.mock import MagicMock, AsyncMock

    _orig_popen = subprocess.Popen
    _orig_run = subprocess.run
    _orig_exec = _asyncio.create_subprocess_exec

    def _filtered_popen(cmd, *args, **kwargs):
        if _is_terminal_spawn(cmd):
            m = MagicMock()
            m.pid = 0
            m.returncode = 0
            m.stdout = None
            m.stderr = None
            m.wait = MagicMock(return_value=0)
            m.communicate = MagicMock(return_value=(b"", b""))
            m.__enter__ = lambda self=m: self
            m.__exit__ = lambda *_a, **_kw: False
            return m
        return _orig_popen(cmd, *args, **kwargs)

    def _filtered_run(cmd, *args, **kwargs):
        if _is_terminal_spawn(cmd):
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout="", stderr="",
            )
        return _orig_run(cmd, *args, **kwargs)

    async def _filtered_exec(program, *args, **kwargs):
        # asyncio.create_subprocess_exec takes the program as the first arg,
        # then positional args — reconstruct a pseudo-argv for classification.
        argv = [program, *args]
        if _is_terminal_spawn(argv):
            m = MagicMock()
            m.pid = 0
            m.returncode = 0
            m.wait = AsyncMock(return_value=0)
            m.communicate = AsyncMock(return_value=(b"", b""))
            return m
        return await _orig_exec(program, *args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", _filtered_popen)
    monkeypatch.setattr(subprocess, "run", _filtered_run)
    monkeypatch.setattr(_asyncio, "create_subprocess_exec", _filtered_exec)
    yield


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
