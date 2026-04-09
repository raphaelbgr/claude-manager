"""
Unit tests for src/scanner.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest
import pytest_asyncio

from src.scanner import (
    ClaudeSession,
    decode_project_folder,
    parse_session,
    scan_local,
    scan_remote,
    scan_all,
    _load_active_pids,
    _mark_active_sessions,
)


# ---------------------------------------------------------------------------
# decode_project_folder
# ---------------------------------------------------------------------------

class TestDecodeProjectFolder:
    """decode_project_folder() — Unix and Windows paths."""

    # ----- Unix style (non-Windows platform) --------------------------------

    @pytest.mark.skipif(sys.platform == "win32", reason="Unix sep only")
    def test_unix_simple(self):
        assert decode_project_folder("-Users-rbgnr-git-foo") == "/Users/rbgnr/git/foo"

    @pytest.mark.skipif(sys.platform == "win32", reason="Unix sep only")
    def test_unix_nested(self):
        # Every '-' becomes '/', so 'air-code' becomes 'air/code' (not 'air-code')
        assert decode_project_folder("-Users-rbgnr-git-air-code") == "/Users/rbgnr/git/air/code"

    @pytest.mark.skipif(sys.platform == "win32", reason="Unix sep only")
    def test_unix_home(self):
        assert decode_project_folder("-home-rbgnr") == "/home/rbgnr"

    @pytest.mark.skipif(sys.platform == "win32", reason="Unix sep only")
    def test_unix_single_segment(self):
        # "-foo" → "/foo"
        assert decode_project_folder("-foo") == "/foo"

    @pytest.mark.skipif(sys.platform == "win32", reason="Unix sep only")
    def test_unix_root_only(self):
        # "-" → "/"
        assert decode_project_folder("-") == "/"

    # ----- Windows style ----------------------------------------------------

    @pytest.mark.skipif(sys.platform == "win32", reason="Unix sep only")
    def test_windows_style_on_unix(self):
        """C--Users-rbgnr-git-foo → C:/Users/rbgnr/git/foo (/ sep on non-Windows)."""
        result = decode_project_folder("C--Users-rbgnr-git-foo")
        assert result == "C:/Users/rbgnr/git/foo"

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows sep only")
    def test_windows_style_on_windows(self):
        result = decode_project_folder("C--Users-rbgnr-git-foo")
        assert result == "C:\\Users\\rbgnr\\git\\foo"

    @pytest.mark.skipif(sys.platform == "win32", reason="Unix sep only")
    def test_windows_drive_d(self):
        # Every '-' after the drive prefix becomes '/', so 'my-app' → 'my/app'
        result = decode_project_folder("D--Projects-my-app")
        assert result == "D:/Projects/my/app"

    # ----- Edge cases -------------------------------------------------------

    def test_empty_string(self):
        # Must not raise; result may be empty or just separator
        result = decode_project_folder("")
        assert isinstance(result, str)

    @pytest.mark.skipif(sys.platform == "win32", reason="Unix sep only")
    def test_no_dashes(self):
        # A string with no dashes is returned as-is (no substitution happens)
        assert decode_project_folder("nodashes") == "nodashes"

    @pytest.mark.skipif(sys.platform == "win32", reason="Unix sep only")
    def test_preserves_non_dash_chars(self):
        result = decode_project_folder("-home-user-my.project")
        assert result == "/home/user/my.project"

    def test_returns_string(self):
        result = decode_project_folder("-some-path")
        assert isinstance(result, str)

    @pytest.mark.skipif(sys.platform == "win32", reason="Unix sep only")
    def test_lowercase_drive_letter_treated_as_windows(self):
        # Any single alpha char followed by '-' at position 1 is treated as
        # a Windows drive letter (folder[0].isalpha() and folder[1]=='-').
        # The drive prefix consumes positions 0-1, then '-' → '/' for the rest.
        # "a-foo-bar": drive='a', i starts at 2 → "foo-bar" → "foo/bar"
        # Result: "a:foo/bar"  (no leading sep after the colon)
        result = decode_project_folder("a-foo-bar")
        assert result == "a:foo/bar"


# ---------------------------------------------------------------------------
# parse_session
# ---------------------------------------------------------------------------

class TestParseSession:

    def _write_jsonl(self, tmp_path: Path, name: str, lines: list[str]) -> Path:
        p = tmp_path / name
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return p

    def test_returns_claude_session_instance(self, tmp_path, sample_jsonl_content):
        f = tmp_path / "abc123.jsonl"
        f.write_text(sample_jsonl_content, encoding="utf-8")
        sess = parse_session(f, "/Users/rbgnr/git/myproject", "-Users-rbgnr-git-myproject")
        assert isinstance(sess, ClaudeSession)

    def test_session_id_is_stem(self, tmp_path, sample_jsonl_content):
        f = tmp_path / "abc123.jsonl"
        f.write_text(sample_jsonl_content, encoding="utf-8")
        sess = parse_session(f, "/Users/rbgnr/git/myproject", "-Users-rbgnr-git-myproject")
        assert sess.session_id == "abc123"

    def test_slug_extracted(self, tmp_path, sample_jsonl_content):
        f = tmp_path / "abc123.jsonl"
        f.write_text(sample_jsonl_content, encoding="utf-8")
        sess = parse_session(f, "/Users/rbgnr/git/myproject", "-Users-rbgnr-git-myproject")
        assert sess.slug == "fix-login-bug"

    def test_cwd_extracted(self, tmp_path, sample_jsonl_content):
        f = tmp_path / "abc123.jsonl"
        f.write_text(sample_jsonl_content, encoding="utf-8")
        sess = parse_session(f, "/Users/rbgnr/git/myproject", "-Users-rbgnr-git-myproject")
        assert sess.cwd == "/Users/rbgnr/git/myproject"

    def test_summary_extracted(self, tmp_path, sample_jsonl_content):
        f = tmp_path / "abc123.jsonl"
        f.write_text(sample_jsonl_content, encoding="utf-8")
        sess = parse_session(f, "/Users/rbgnr/git/myproject", "-Users-rbgnr-git-myproject")
        assert "login bug" in sess.summary

    def test_summary_truncated_to_120_chars(self, tmp_path):
        long_text = "A" * 200
        line = json.dumps({
            "type": "user",
            "sessionId": "s1",
            "slug": "long-msg",
            "cwd": "/tmp",
            "message": {"content": long_text},
        })
        f = tmp_path / "s1.jsonl"
        f.write_text(line + "\n", encoding="utf-8")
        sess = parse_session(f, "/tmp", "-tmp")
        assert len(sess.summary) <= 120

    def test_message_count(self, tmp_path, sample_jsonl_content):
        f = tmp_path / "abc123.jsonl"
        f.write_text(sample_jsonl_content, encoding="utf-8")
        sess = parse_session(f, "/proj", "-proj")
        # sample_jsonl_content has 3 non-empty lines
        assert sess.messages == 3

    def test_machine_default_is_local(self, tmp_path, sample_jsonl_content):
        f = tmp_path / "abc123.jsonl"
        f.write_text(sample_jsonl_content, encoding="utf-8")
        sess = parse_session(f, "/proj", "-proj")
        assert sess.machine == "local"

    def test_machine_custom(self, tmp_path, sample_jsonl_content):
        f = tmp_path / "abc123.jsonl"
        f.write_text(sample_jsonl_content, encoding="utf-8")
        sess = parse_session(f, "/proj", "-proj", machine="mac-mini")
        assert sess.machine == "mac-mini"

    def test_project_folder_stored(self, tmp_path, sample_jsonl_content):
        f = tmp_path / "abc123.jsonl"
        f.write_text(sample_jsonl_content, encoding="utf-8")
        folder = "-Users-rbgnr-git-myproject"
        sess = parse_session(f, "/Users/rbgnr/git/myproject", folder)
        assert sess.project_folder == folder

    def test_project_path_stored(self, tmp_path, sample_jsonl_content):
        f = tmp_path / "abc123.jsonl"
        f.write_text(sample_jsonl_content, encoding="utf-8")
        sess = parse_session(f, "/Users/rbgnr/git/myproject", "-Users-rbgnr-git-myproject")
        assert sess.project_path == "/Users/rbgnr/git/myproject"

    def test_status_is_idle_initially(self, tmp_path, sample_jsonl_content):
        f = tmp_path / "abc123.jsonl"
        f.write_text(sample_jsonl_content, encoding="utf-8")
        sess = parse_session(f, "/proj", "-proj")
        assert sess.status == "idle"

    def test_pid_is_none_initially(self, tmp_path, sample_jsonl_content):
        f = tmp_path / "abc123.jsonl"
        f.write_text(sample_jsonl_content, encoding="utf-8")
        sess = parse_session(f, "/proj", "-proj")
        assert sess.pid is None

    def test_modified_is_iso8601(self, tmp_path, sample_jsonl_content):
        f = tmp_path / "abc123.jsonl"
        f.write_text(sample_jsonl_content, encoding="utf-8")
        sess = parse_session(f, "/proj", "-proj")
        # Basic check: contains 'T' and '+' or 'Z'
        assert "T" in sess.modified

    def test_malformed_jsonl_skipped_gracefully(self, tmp_path):
        """Lines that are not valid JSON should be skipped without raising."""
        lines = [
            "not json at all }{",
            '{"type": "assistant", "message": "ok"}',
            json.dumps({
                "type": "user",
                "sessionId": "x1",
                "slug": "ok-slug",
                "cwd": "/tmp",
                "message": {"content": "Hello"},
            }),
        ]
        f = tmp_path / "x1.jsonl"
        f.write_text("\n".join(lines) + "\n", encoding="utf-8")
        sess = parse_session(f, "/tmp", "-tmp")
        assert sess.slug == "ok-slug"
        assert "Hello" in sess.summary

    def test_all_malformed_jsonl(self, tmp_path):
        """A file with only bad JSON lines should return empty slug/summary."""
        f = tmp_path / "bad.jsonl"
        f.write_text("}{bad\n{also bad\n", encoding="utf-8")
        sess = parse_session(f, "/tmp", "-tmp")
        assert sess.slug == ""
        assert sess.summary == ""

    def test_empty_file(self, tmp_path):
        f = tmp_path / "empty.jsonl"
        f.write_text("", encoding="utf-8")
        sess = parse_session(f, "/tmp", "-tmp")
        assert sess.messages == 0
        assert sess.slug == ""

    def test_block_content_array_extracts_text(self, tmp_path, sample_jsonl_block_content):
        f = tmp_path / "def456.jsonl"
        f.write_text(sample_jsonl_block_content, encoding="utf-8")
        sess = parse_session(f, "/Users/rbgnr/git/utils", "-Users-rbgnr-git-utils")
        assert "Refactor" in sess.summary

    def test_blank_lines_not_counted_in_messages(self, tmp_path):
        lines = [
            json.dumps({"type": "user", "sessionId": "z1", "slug": "s", "cwd": "/t",
                        "message": {"content": "hi"}}),
            "",
            "",
            json.dumps({"type": "assistant", "message": {"content": "ok"}}),
        ]
        f = tmp_path / "z1.jsonl"
        f.write_text("\n".join(lines) + "\n", encoding="utf-8")
        sess = parse_session(f, "/t", "-t")
        assert sess.messages == 2  # 2 non-empty lines


# ---------------------------------------------------------------------------
# ClaudeSession.to_dict
# ---------------------------------------------------------------------------

class TestClaudeSessionToDict:

    def _make_session(self, **overrides) -> ClaudeSession:
        defaults = dict(
            session_id="s1",
            machine="local",
            project_folder="-proj",
            project_path="/proj",
            cwd="/proj",
            slug="my-slug",
            summary="A summary",
            messages=5,
            modified="2024-01-01T00:00:00+00:00",
            status="idle",
            pid=None,
        )
        defaults.update(overrides)
        return ClaudeSession(**defaults)

    def test_to_dict_returns_dict(self):
        sess = self._make_session()
        assert isinstance(sess.to_dict(), dict)

    def test_to_dict_has_all_fields(self):
        sess = self._make_session()
        d = sess.to_dict()
        expected_keys = {
            "session_id", "machine", "project_folder", "project_path",
            "cwd", "slug", "summary", "messages", "modified", "status", "pid",
        }
        assert set(d.keys()) == expected_keys

    def test_to_dict_values_match(self):
        sess = self._make_session(slug="test-slug", messages=10, pid=12345)
        d = sess.to_dict()
        assert d["slug"] == "test-slug"
        assert d["messages"] == 10
        assert d["pid"] == 12345

    def test_to_dict_pid_none(self):
        sess = self._make_session(pid=None)
        assert sess.to_dict()["pid"] is None

    def test_to_dict_status_active(self):
        sess = self._make_session(status="active", pid=9999)
        d = sess.to_dict()
        assert d["status"] == "active"
        assert d["pid"] == 9999


# ---------------------------------------------------------------------------
# scan_local
# ---------------------------------------------------------------------------

class TestScanLocal:

    def test_returns_list(self, mock_claude_home):
        result = scan_local(claude_home=mock_claude_home)
        assert isinstance(result, list)

    def test_skips_non_encoded_folder(self, mock_claude_home):
        result = scan_local(claude_home=mock_claude_home)
        # The "not-an-encoded-path" folder must never appear
        folders = {s.project_folder for s in result}
        assert "not-an-encoded-path" not in folders

    def test_finds_both_projects(self, mock_claude_home):
        result = scan_local(claude_home=mock_claude_home)
        folders = {s.project_folder for s in result}
        assert "-Users-rbgnr-git-myproject" in folders
        assert "-Users-rbgnr-git-other" in folders

    def test_returns_claude_session_objects(self, mock_claude_home):
        result = scan_local(claude_home=mock_claude_home)
        assert all(isinstance(s, ClaudeSession) for s in result)

    def test_sessions_sorted_by_modified_desc(self, mock_claude_home):
        result = scan_local(claude_home=mock_claude_home)
        modified_times = [s.modified for s in result]
        assert modified_times == sorted(modified_times, reverse=True)

    def test_active_session_detected(self, mock_claude_home):
        """abc123 has a sessions JSON with current PID → should be active."""
        result = scan_local(claude_home=mock_claude_home)
        abc_sessions = [s for s in result if s.session_id == "abc123"]
        assert abc_sessions, "abc123 session not found"
        assert abc_sessions[0].status == "active"
        assert abc_sessions[0].pid == os.getpid()

    def test_idle_session_not_active(self, mock_claude_home):
        """def456 has no sessions JSON → should remain idle."""
        result = scan_local(claude_home=mock_claude_home)
        def_sessions = [s for s in result if s.session_id == "def456"]
        assert def_sessions, "def456 session not found"
        assert def_sessions[0].status == "idle"
        assert def_sessions[0].pid is None

    def test_machine_label_applied(self, mock_claude_home):
        result = scan_local(claude_home=mock_claude_home, machine="mac-mini")
        assert all(s.machine == "mac-mini" for s in result)

    def test_returns_empty_when_no_projects_dir(self, tmp_path):
        claude_home = tmp_path / ".claude"
        claude_home.mkdir()
        result = scan_local(claude_home=claude_home)
        assert result == []

    def test_empty_project_folder_skipped(self, tmp_path):
        """A valid encoded folder with no JSONL files should contribute 0 sessions."""
        claude_home = tmp_path / ".claude"
        empty_proj = claude_home / "projects" / "-Users-rbgnr-git-empty"
        empty_proj.mkdir(parents=True)
        result = scan_local(claude_home=claude_home)
        assert result == []

    def test_caps_at_20_sessions_per_project(self, tmp_path):
        """At most 20 JSONL files per project should be returned."""
        claude_home = tmp_path / ".claude"
        proj = claude_home / "projects" / "-Users-rbgnr-git-big"
        proj.mkdir(parents=True)
        line = json.dumps({
            "type": "user",
            "sessionId": "sx",
            "slug": "s",
            "cwd": "/x",
            "message": {"content": "hi"},
        })
        for i in range(25):
            (proj / f"sess{i:03d}.jsonl").write_text(line + "\n", encoding="utf-8")
        result = scan_local(claude_home=claude_home)
        assert len(result) <= 20

    def test_unix_style_folder_decoded(self, mock_claude_home):
        result = scan_local(claude_home=mock_claude_home)
        proj_paths = {s.project_path for s in result}
        assert "/Users/rbgnr/git/myproject" in proj_paths or any(
            "myproject" in p for p in proj_paths
        )


# ---------------------------------------------------------------------------
# scan_remote
# ---------------------------------------------------------------------------

class TestScanRemote:

    def _make_remote_payload(self) -> list[dict]:
        return [
            {
                "session_id": "remote001",
                "project_folder": "-Users-rbgnr-git-remote",
                "project_path": "/Users/rbgnr/git/remote",
                "cwd": "/Users/rbgnr/git/remote",
                "slug": "remote-work",
                "summary": "Doing remote stuff",
                "messages": 7,
                "modified": "2024-06-01T12:00:00+00:00",
                "status": "idle",
                "pid": None,
            }
        ]

    def _build_proc_mock(self, stdout: bytes, returncode: int = 0) -> AsyncMock:
        proc = AsyncMock()
        proc.returncode = returncode
        proc.communicate = AsyncMock(return_value=(stdout, b""))
        return proc

    @pytest.mark.asyncio
    async def test_returns_list_of_claude_sessions(self):
        payload = self._make_remote_payload()
        proc = self._build_proc_mock(json.dumps(payload).encode())

        with (
            patch("asyncio.create_subprocess_exec", return_value=proc),
            patch("asyncio.wait_for", side_effect=_wrap_wait_for_remote(proc, payload)),
        ):
            result = await scan_remote("mac-mini", "mac-mini")

        assert isinstance(result, list)
        assert all(isinstance(s, ClaudeSession) for s in result)

    @pytest.mark.asyncio
    async def test_session_tagged_with_machine_name(self):
        payload = self._make_remote_payload()
        proc = self._build_proc_mock(json.dumps(payload).encode())

        with (
            patch("asyncio.create_subprocess_exec", return_value=proc),
            patch("asyncio.wait_for", side_effect=_wrap_wait_for_remote(proc, payload)),
        ):
            result = await scan_remote("mac-mini", "mac-mini")

        assert all(s.machine == "mac-mini" for s in result)

    @pytest.mark.asyncio
    async def test_session_fields_match_payload(self):
        payload = self._make_remote_payload()
        proc = self._build_proc_mock(json.dumps(payload).encode())

        with (
            patch("asyncio.create_subprocess_exec", return_value=proc),
            patch("asyncio.wait_for", side_effect=_wrap_wait_for_remote(proc, payload)),
        ):
            result = await scan_remote("mac-mini", "mac-mini")

        assert len(result) == 1
        sess = result[0]
        assert sess.session_id == "remote001"
        assert sess.slug == "remote-work"
        assert sess.summary == "Doing remote stuff"
        assert sess.messages == 7

    @pytest.mark.asyncio
    async def test_returns_empty_on_nonzero_returncode(self):
        proc = self._build_proc_mock(b"", returncode=1)

        with (
            patch("asyncio.create_subprocess_exec", return_value=proc),
            patch("asyncio.wait_for", side_effect=_wrap_wait_for_remote(proc, [])),
        ):
            result = await scan_remote("mac-mini", "mac-mini")

        assert result == []

    @pytest.mark.asyncio
    async def test_returns_empty_on_malformed_json(self):
        proc = self._build_proc_mock(b"not json {{")

        with (
            patch("asyncio.create_subprocess_exec", return_value=proc),
            patch("asyncio.wait_for", side_effect=_wrap_wait_for_remote(proc, None)),
        ):
            result = await scan_remote("mac-mini", "mac-mini")

        assert result == []

    @pytest.mark.asyncio
    async def test_returns_empty_on_ssh_timeout(self):
        async def _raise_timeout(*args, **kwargs):
            raise asyncio.TimeoutError()

        with patch("asyncio.wait_for", side_effect=_raise_timeout):
            result = await scan_remote("mac-mini", "mac-mini")

        assert result == []

    @pytest.mark.asyncio
    async def test_returns_empty_on_exception(self):
        async def _raise(*args, **kwargs):
            raise OSError("ssh not found")

        with patch("asyncio.create_subprocess_exec", side_effect=_raise):
            result = await scan_remote("mac-mini", "mac-mini")

        assert result == []

    @pytest.mark.asyncio
    async def test_skips_malformed_items_in_list(self):
        """Items missing required keys should be skipped; valid ones returned."""
        payload = [
            {"bad": "no session_id here"},
            {
                "session_id": "ok001",
                "project_folder": "-tmp",
                "project_path": "/tmp",
                "cwd": "/tmp",
                "slug": "ok",
                "summary": "fine",
                "messages": 1,
                "modified": "2024-01-01T00:00:00+00:00",
                "status": "idle",
                "pid": None,
            },
        ]
        proc = self._build_proc_mock(json.dumps(payload).encode())

        with (
            patch("asyncio.create_subprocess_exec", return_value=proc),
            patch("asyncio.wait_for", side_effect=_wrap_wait_for_remote(proc, payload)),
        ):
            result = await scan_remote("mac-mini", "mac-mini")

        # Only the valid item should come through
        assert len(result) == 1
        assert result[0].session_id == "ok001"


# ---------------------------------------------------------------------------
# scan_all
# ---------------------------------------------------------------------------

class TestScanAll:

    @pytest.mark.asyncio
    async def test_includes_local_sessions(self, mock_claude_home):
        fleet = {}  # no remote machines

        with patch("src.scanner.scan_local", return_value=[
            ClaudeSession("s1", "local", "-p", "/p", "/p", "slug", "summary", 1,
                          "2024-01-02T00:00:00+00:00", "idle", None)
        ]):
            result = await scan_all(local_machine="local", fleet=fleet)

        assert any(s.session_id == "s1" for s in result)

    @pytest.mark.asyncio
    async def test_skips_offline_remote_machines(self):
        fleet = {
            "mac-mini": {"online": False, "os": "darwin", "ip": "192.168.7.102"},
        }
        scan_remote_mock = AsyncMock(return_value=[])

        with (
            patch("src.scanner.scan_local", return_value=[]),
            patch("src.scanner.scan_remote", scan_remote_mock),
        ):
            await scan_all(local_machine=None, fleet=fleet)

        scan_remote_mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_local_machine_in_remote_scan(self):
        fleet = {
            "mac-mini": {"online": True, "os": "darwin", "ip": "192.168.7.102"},
        }
        scan_remote_mock = AsyncMock(return_value=[])

        with (
            patch("src.scanner.scan_local", return_value=[]),
            patch("src.scanner.scan_remote", scan_remote_mock),
        ):
            await scan_all(local_machine="mac-mini", fleet=fleet)

        # mac-mini is the local machine → should not be scanned remotely
        scan_remote_mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_aggregates_local_and_remote(self):
        local_sess = ClaudeSession(
            "local1", "local", "-p1", "/p1", "/p1", "l-slug", "local summary", 2,
            "2024-06-01T12:00:00+00:00", "idle", None,
        )
        remote_sess = ClaudeSession(
            "remote1", "mac-mini", "-p2", "/p2", "/p2", "r-slug", "remote summary", 3,
            "2024-06-02T12:00:00+00:00", "idle", None,
        )
        fleet = {
            "mac-mini": {"online": True, "os": "darwin", "ip": "192.168.7.102"},
        }

        with (
            patch("src.scanner.scan_local", return_value=[local_sess]),
            patch("src.scanner.scan_remote", AsyncMock(return_value=[remote_sess])),
        ):
            result = await scan_all(local_machine=None, fleet=fleet)

        ids = {s.session_id for s in result}
        assert "local1" in ids
        assert "remote1" in ids

    @pytest.mark.asyncio
    async def test_result_sorted_by_modified_desc(self):
        sessions = [
            ClaudeSession("a", "local", "-p", "/p", "/p", "s", "sum", 1,
                          "2024-01-01T00:00:00+00:00", "idle", None),
            ClaudeSession("b", "local", "-p", "/p", "/p", "s", "sum", 1,
                          "2024-06-01T00:00:00+00:00", "idle", None),
            ClaudeSession("c", "local", "-p", "/p", "/p", "s", "sum", 1,
                          "2023-01-01T00:00:00+00:00", "idle", None),
        ]

        with (
            patch("src.scanner.scan_local", return_value=sessions),
        ):
            result = await scan_all(local_machine=None, fleet={})

        modified_times = [s.modified for s in result]
        assert modified_times == sorted(modified_times, reverse=True)

    @pytest.mark.asyncio
    async def test_handles_remote_exception_gracefully(self):
        """If a remote scan raises, scan_all should still return local results."""
        local_sess = ClaudeSession(
            "local1", "local", "-p", "/p", "/p", "l", "local", 1,
            "2024-06-01T12:00:00+00:00", "idle", None,
        )
        fleet = {
            "ubuntu-desktop": {"online": True, "os": "linux", "ip": "192.168.7.13"},
        }

        async def _failing_remote(*args, **kwargs):
            raise RuntimeError("SSH exploded")

        with (
            patch("src.scanner.scan_local", return_value=[local_sess]),
            patch("src.scanner.scan_remote", side_effect=_failing_remote),
        ):
            result = await scan_all(local_machine=None, fleet=fleet)

        # local session should still appear
        assert any(s.session_id == "local1" for s in result)


# ---------------------------------------------------------------------------
# _load_active_pids / _mark_active_sessions
# ---------------------------------------------------------------------------

class TestLoadActivePids:

    def test_returns_empty_when_sessions_dir_missing(self, tmp_path):
        claude_home = tmp_path / ".claude"
        claude_home.mkdir()
        result = _load_active_pids(claude_home)
        assert result == {}

    def test_current_pid_is_alive(self, tmp_path):
        claude_home = tmp_path / ".claude"
        sessions_dir = claude_home / "sessions"
        sessions_dir.mkdir(parents=True)
        (sessions_dir / "s1.json").write_text(
            json.dumps({"sessionId": "s1", "pid": os.getpid()}),
            encoding="utf-8",
        )
        result = _load_active_pids(claude_home)
        assert "s1" in result
        assert result["s1"] == os.getpid()

    def test_dead_pid_excluded(self, tmp_path):
        # PID 1 exists on macOS/Linux but almost certainly doesn't own our session.
        # Use a PID that definitely doesn't exist: 99999999
        claude_home = tmp_path / ".claude"
        sessions_dir = claude_home / "sessions"
        sessions_dir.mkdir(parents=True)
        (sessions_dir / "s2.json").write_text(
            json.dumps({"sessionId": "s2", "pid": 99999999}),
            encoding="utf-8",
        )
        result = _load_active_pids(claude_home)
        assert "s2" not in result

    def test_malformed_json_skipped(self, tmp_path):
        claude_home = tmp_path / ".claude"
        sessions_dir = claude_home / "sessions"
        sessions_dir.mkdir(parents=True)
        (sessions_dir / "bad.json").write_text("}{bad json", encoding="utf-8")
        result = _load_active_pids(claude_home)
        assert result == {}


class TestMarkActiveSessions:

    def _sess(self, session_id: str) -> ClaudeSession:
        return ClaudeSession(
            session_id=session_id, machine="local", project_folder="-p",
            project_path="/p", cwd="/p", slug="s", summary="sum",
            messages=1, modified="2024-01-01T00:00:00+00:00",
            status="idle", pid=None,
        )

    def test_marks_matched_session_active(self):
        sess = self._sess("abc")
        _mark_active_sessions([sess], {"abc": 1234})
        assert sess.status == "active"
        assert sess.pid == 1234

    def test_unmatched_session_stays_idle(self):
        sess = self._sess("xyz")
        _mark_active_sessions([sess], {"abc": 1234})
        assert sess.status == "idle"
        assert sess.pid is None

    def test_multiple_sessions_partial_match(self):
        s1 = self._sess("match")
        s2 = self._sess("nomatch")
        _mark_active_sessions([s1, s2], {"match": 5678})
        assert s1.status == "active"
        assert s2.status == "idle"


# ---------------------------------------------------------------------------
# Helpers for remote scan mocking
# ---------------------------------------------------------------------------

def _wrap_wait_for_remote(proc, payload):
    """
    Returns an async side_effect for asyncio.wait_for.

    First call wraps create_subprocess_exec → returns proc.
    Second call wraps proc.communicate() → returns (stdout_bytes, b"").
    """
    call_count = [0]
    stdout = json.dumps(payload).encode() if payload is not None else b"not json {{"

    async def _inner(coro, timeout=None):
        call_count[0] += 1
        if call_count[0] == 1:
            return proc
        else:
            proc.returncode = 0
            return (stdout, b"")

    return _inner
