"""
Tests for the perf machinery added 2026-05-20: incremental parse_session,
the (path, mtime_ns, size) in-memory cache in scan_local, the disk-
persisted cache (scan-cache.json + git-cache.json), and the batched
cpu_percent sampling in _mark_active_sessions.

These exercise the public surface only — no internal mocks. Each test
writes a real JSONL to a tmp dir and observes parse_session's output
or scan_local's persisted-cache file. Cross-platform safe (no Win-
specific paths).
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from src.scanner import (
    _load_persisted_cache,
    _load_persisted_git_cache,
    _mark_active_sessions,
    _save_persisted_cache,
    _save_persisted_git_cache,
    ClaudeSession,
    parse_session,
    scan_local,
)


# ---------------------------------------------------------------------------
# Test fixtures — build small synthetic JSONL files
# ---------------------------------------------------------------------------

def _write_session_jsonl(path: Path, n_assistant: int = 3, tokens_per: int = 100,
                          cwd_override: str | None = None) -> None:
    """Write a minimal-but-realistic Claude JSONL with N assistant messages,
    each contributing ``tokens_per`` to the input_tokens count.

    ``cwd_override`` lets tests inject a non-temp cwd into the JSONL so
    ``_is_tmp_path`` doesn't filter the resulting session — pytest's
    tmp_path lives under %TEMP% on Windows, which is exactly the set
    of paths the scanner intentionally drops."""
    lines = []
    # First user line (gives slug + cwd + first_message)
    lines.append(json.dumps({
        "type": "user",
        "sessionId": path.stem,
        "slug": "test-slug",
        "cwd": cwd_override or str(path.parent),
        "gitBranch": "master",
        "message": {"content": [{"type": "text", "text": "hello world"}]},
    }))
    # N assistant messages with usage so token sum is deterministic
    for _ in range(n_assistant):
        lines.append(json.dumps({
            "type": "assistant",
            "message": {
                "usage": {"input_tokens": tokens_per, "output_tokens": 0,
                          "cache_creation_input_tokens": 0,
                          "cache_read_input_tokens": 0},
            },
        }))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# parse_session: incremental path
# ---------------------------------------------------------------------------

class TestParseSessionIncremental:
    def test_cold_parse_no_prev_reads_full_file(self, tmp_path):
        f = tmp_path / "sess1.jsonl"
        _write_session_jsonl(f, n_assistant=5, tokens_per=10)
        sess = parse_session(f, str(tmp_path), tmp_path.name)
        assert sess.tokens == 50
        bc = sess._parse_breadcrumbs
        assert bc["last_size"] == f.stat().st_size
        assert bc["tokens"] == 50

    def test_incremental_seeks_past_prev_last_size(self, tmp_path):
        """Grow a file and reparse with prev=breadcrumbs — should read ONLY
        the new bytes and ADD to the previously-seen token count."""
        f = tmp_path / "sess2.jsonl"
        _write_session_jsonl(f, n_assistant=3, tokens_per=100)
        first = parse_session(f, str(tmp_path), tmp_path.name)
        assert first.tokens == 300
        bc = first._parse_breadcrumbs

        # Append two more assistant messages.
        with f.open("a", encoding="utf-8") as fh:
            for _ in range(2):
                fh.write(json.dumps({
                    "type": "assistant",
                    "message": {"usage": {"input_tokens": 50, "output_tokens": 0,
                                          "cache_creation_input_tokens": 0,
                                          "cache_read_input_tokens": 0}},
                }) + "\n")

        second = parse_session(f, str(tmp_path), tmp_path.name, prev=bc)
        # 300 (cached) + 2×50 (incremental) = 400
        assert second.tokens == 400
        assert second._parse_breadcrumbs["last_size"] == f.stat().st_size
        # messages count grew by 2
        assert second.messages == first.messages + 2

    def test_prev_metadata_reused_when_metadata_already_found(self, tmp_path):
        """slug/cwd/git_branch from prev should survive into the new session
        even though we don't re-scan the first 50 lines."""
        f = tmp_path / "sess3.jsonl"
        _write_session_jsonl(f, n_assistant=1, tokens_per=10)
        first = parse_session(f, str(tmp_path), tmp_path.name)
        with f.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"type": "assistant",
                                 "message": {"usage": {"input_tokens": 5,
                                                       "output_tokens": 0,
                                                       "cache_creation_input_tokens": 0,
                                                       "cache_read_input_tokens": 0}}}) + "\n")
        second = parse_session(f, str(tmp_path), tmp_path.name,
                               prev=first._parse_breadcrumbs)
        assert second.slug == "test-slug"
        assert second.git_branch == "master"


# ---------------------------------------------------------------------------
# Disk-persisted caches
# ---------------------------------------------------------------------------

class TestPersistedCache:
    def test_save_then_load_roundtrip(self, tmp_path, monkeypatch):
        # Redirect the persisted-cache path into tmp_path so we don't pollute
        # the real ~/.claude-manager dir during the test.
        monkeypatch.setattr("src.scanner._persisted_cache_path",
                            lambda: tmp_path / "scan-cache.json")

        # Build a fake cache entry.
        sess = ClaudeSession(
            session_id="abc", machine="local",
            project_folder="-test", project_path=str(tmp_path),
            cwd=str(tmp_path), slug="s", summary="x", messages=10,
            modified="2026-05-20T00:00:00", status="idle", pid=None,
            file_size=123, tokens=42, git_branch="main",
        )
        sess._parse_breadcrumbs = {"last_size": 123, "tokens": 42}
        cache = {str(tmp_path / "abc.jsonl"): (sess, 123, 999_000_000_000)}

        _save_persisted_cache(cache)
        loaded = _load_persisted_cache()
        assert len(loaded) == 1
        loaded_sess, last_size, last_mtime = next(iter(loaded.values()))
        assert loaded_sess.tokens == 42
        assert loaded_sess.slug == "s"
        assert last_size == 123
        assert last_mtime == 999_000_000_000
        assert getattr(loaded_sess, "_parse_breadcrumbs", {}).get("tokens") == 42

    def test_load_missing_file_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr("src.scanner._persisted_cache_path",
                            lambda: tmp_path / "nonexistent.json")
        assert _load_persisted_cache() == {}

    def test_load_malformed_json_returns_empty(self, tmp_path, monkeypatch):
        p = tmp_path / "scan-cache.json"
        p.write_text("not json at all { ]", encoding="utf-8")
        monkeypatch.setattr("src.scanner._persisted_cache_path", lambda: p)
        assert _load_persisted_cache() == {}

    def test_git_cache_roundtrip(self, tmp_path, monkeypatch):
        monkeypatch.setattr("src.scanner._git_cache_path",
                            lambda: tmp_path / "git-cache.json")
        cache = {
            (str(tmp_path), 12345): {
                "git_remote": "git@example.com:x.git",
                "git_commits": 7,
                "git_upstream": "origin/master",
                "git_ahead": 0, "git_behind": 0, "git_dirty": False,
                "readme_path": "",
            }
        }
        _save_persisted_git_cache(cache)
        loaded = _load_persisted_git_cache()
        assert len(loaded) == 1
        info = next(iter(loaded.values()))
        assert info["git_remote"] == "git@example.com:x.git"
        assert info["git_commits"] == 7


# ---------------------------------------------------------------------------
# scan_local: cache hits + incremental + disk persistence
# ---------------------------------------------------------------------------

class TestScanLocalCache:
    def test_warm_scan_hits_in_memory_cache(self, tmp_path, monkeypatch):
        """First scan_local populates the cache; second call returns cached
        ClaudeSession instances when file (size, mtime_ns) match."""
        # Build a fake ~/.claude tree.
        claude_home = tmp_path / ".claude"
        proj_dir = claude_home / "projects" / "-home-rbgnr-proj"
        proj_dir.mkdir(parents=True)
        f = proj_dir / "abc-123.jsonl"
        # Use a non-temp cwd so _is_tmp_path doesn't drop the session.
        _write_session_jsonl(f, n_assistant=2, tokens_per=10,
                             cwd_override="/home/rbgnr/proj")
        # Sessions dir empty — no live PIDs.
        (claude_home / "sessions").mkdir()

        # Reset the cache + redirect persisted paths to tmp_path so the test
        # is hermetic.
        if hasattr(scan_local, "_session_cache"):
            delattr(scan_local, "_session_cache")
        if hasattr(scan_local, "_git_cache"):
            delattr(scan_local, "_git_cache")
        monkeypatch.setattr("src.scanner._persisted_cache_path",
                            lambda: tmp_path / "scan-cache.json")
        monkeypatch.setattr("src.scanner._git_cache_path",
                            lambda: tmp_path / "git-cache.json")

        sessions_1 = scan_local(claude_home=claude_home, machine="t1")
        sessions_2 = scan_local(claude_home=claude_home, machine="t1")
        assert len(sessions_1) == 1
        assert len(sessions_2) == 1
        # Same identity: cached object returned, not re-built.
        assert sessions_2[0] is sessions_1[0]

    def test_incremental_parse_on_file_growth(self, tmp_path, monkeypatch):
        """Append to a session JSONL between scans; the second scan should
        seek past the prior offset, not re-parse from byte 0."""
        claude_home = tmp_path / ".claude"
        proj_dir = claude_home / "projects" / "-home-rbgnr-proj"
        proj_dir.mkdir(parents=True)
        f = proj_dir / "abc-456.jsonl"
        _write_session_jsonl(f, n_assistant=2, tokens_per=10,
                             cwd_override="/home/rbgnr/proj")
        (claude_home / "sessions").mkdir()

        if hasattr(scan_local, "_session_cache"):
            delattr(scan_local, "_session_cache")
        if hasattr(scan_local, "_git_cache"):
            delattr(scan_local, "_git_cache")
        monkeypatch.setattr("src.scanner._persisted_cache_path",
                            lambda: tmp_path / "scan-cache.json")
        monkeypatch.setattr("src.scanner._git_cache_path",
                            lambda: tmp_path / "git-cache.json")

        s1 = scan_local(claude_home=claude_home, machine="t1")
        assert s1[0].tokens == 20  # 2 × 10

        # Wait briefly so mtime advances on filesystems with coarse mtimes,
        # then append.
        time.sleep(0.05)
        with f.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "type": "assistant",
                "message": {"usage": {"input_tokens": 7, "output_tokens": 0,
                                      "cache_creation_input_tokens": 0,
                                      "cache_read_input_tokens": 0}},
            }) + "\n")

        s2 = scan_local(claude_home=claude_home, machine="t1")
        assert s2[0].tokens == 27  # incremental: 20 cached + 7 new
        # NOT the same object — incremental returned a fresh ClaudeSession.
        assert s2[0] is not s1[0]


# ---------------------------------------------------------------------------
# _mark_active_sessions: batched cpu_percent sampling
# ---------------------------------------------------------------------------

class TestMarkActiveSessionsBatched:
    def test_batched_cpu_does_not_sleep_per_process(self, monkeypatch):
        """The batched-prime pattern must call cpu_percent(interval=None) per
        process and sleep ONCE for the whole batch — not per-process. Verify
        by counting prime calls and asserting time.sleep is called exactly
        once when there are N active sessions."""
        from src import scanner as _scanner

        prime_calls = {"n": 0}
        read_calls = {"n": 0}

        class _FakeProc:
            def __init__(self, pid):
                self.pid = pid
                self._primed = False
            def cpu_percent(self, interval=None):
                # interval=None is the documented non-blocking form.
                assert interval is None, "batched code must not pass interval=0.1"
                if not self._primed:
                    self._primed = True
                    prime_calls["n"] += 1
                    return 0.0
                read_calls["n"] += 1
                return 12.3
            def children(self, recursive=False):
                return []

        sleeps = []
        monkeypatch.setattr(_scanner.psutil, "Process",
                            lambda pid: _FakeProc(pid))
        monkeypatch.setattr(_scanner.time, "sleep",
                            lambda d: sleeps.append(d))

        # Three active sessions.
        sessions = []
        for sid in ("a", "b", "c"):
            sessions.append(ClaudeSession(
                session_id=sid, machine="local", project_folder="", project_path="",
                cwd="", slug="", summary="", messages=0,
                modified="2026-05-20T00:00:00", status="idle", pid=None,
            ))
        active_pids = {"a": 1001, "b": 1002, "c": 1003}

        _mark_active_sessions(sessions, active_pids, names=None)

        # 3 primes (one per active process), 3 reads, ONE sleep.
        assert prime_calls["n"] == 3
        assert read_calls["n"] == 3
        assert len(sleeps) == 1, f"expected exactly 1 sleep, got {len(sleeps)}: {sleeps}"
        # Each session got the read value.
        for sess in sessions:
            assert sess.cpu_percent == 12.3
            assert sess.status == "working"  # cpu>5 → working

    def test_no_sleep_when_no_active_sessions(self, monkeypatch):
        from src import scanner as _scanner
        sleeps = []
        monkeypatch.setattr(_scanner.time, "sleep", lambda d: sleeps.append(d))

        sessions = [ClaudeSession(
            session_id="x", machine="local", project_folder="", project_path="",
            cwd="", slug="", summary="", messages=0,
            modified="2026-05-20T00:00:00", status="idle", pid=None,
        )]
        _mark_active_sessions(sessions, active_pids={}, names=None)
        assert sleeps == [], "must not sleep when there are no active processes"
