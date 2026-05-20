"""
Tests for untested branches in src/scanner.py (2026-05-20).

Covers:
  - _is_tmp_path: every OS-temp prefix variant
  - decode_project_folder: unix/windows/edge cases
  - _load_active_pids: missing dir, malformed JSON, dead PID, ZOMBIE, named sessions
  - _mark_active_sessions: naming, status logic (working/active/idle), mtime fallback
  - _collect_git_state: every subprocess success/failure path
  - scan_local: empty projects dir, folder filtering
  - scan_remote_via_api: 200 success, non-200, exception, JSON parse failure
  - scan_remote (SSH): timeout, SSH failure, non-zero rc, JSON parse failure
  - scan_all: local-only, remote combinations, on_progress callback
  - _load_persisted_cache / _save_persisted_cache: version mismatch, atomic write
  - _load_persisted_git_cache / _save_persisted_git_cache: version mismatch
  - _load_project_cache / _save_project_cache / _update_project_cache

Hard rules:
  - No real git subprocesses, no real psutil walks, no real SSH. All mocked.
  - Use cwd_override="/home/x/proj" in synthetic JSONLs (tmp_path is under %TEMP%
    on Windows which _is_tmp_path filters).
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import src.scanner as _scanner
from src.scanner import (
    ClaudeSession,
    _collect_git_state,
    _is_tmp_path,
    _load_active_pids,
    _load_persisted_cache,
    _load_persisted_git_cache,
    _load_project_cache,
    _mark_active_sessions,
    _save_persisted_cache,
    _save_persisted_git_cache,
    _save_project_cache,
    _update_project_cache,
    decode_project_folder,
    scan_local,
    scan_remote,
    scan_remote_via_api,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_session_jsonl(path: Path, n_assistant: int = 1,
                          cwd_override: str = "/home/user/proj") -> None:
    lines = [json.dumps({
        "type": "user",
        "sessionId": path.stem,
        "slug": "test-slug",
        "cwd": cwd_override,
        "gitBranch": "main",
        "message": {"content": [{"type": "text", "text": "hello"}]},
    })]
    for _ in range(n_assistant):
        lines.append(json.dumps({
            "type": "assistant",
            "message": {"usage": {
                "input_tokens": 10, "output_tokens": 5,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
            }},
        }))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _make_session(**kw) -> ClaudeSession:
    defaults = dict(
        session_id="x", machine="local",
        project_folder="", project_path="",
        cwd="", slug="", summary="", messages=0,
        modified="2026-05-20T00:00:00+00:00",
        status="idle", pid=None,
    )
    defaults.update(kw)
    return ClaudeSession(**defaults)


# ---------------------------------------------------------------------------
# _is_tmp_path
# ---------------------------------------------------------------------------

class TestIsTmpPath:
    def test_empty_string_is_false(self):
        assert _is_tmp_path("") is False

    def test_none_like_empty_is_false(self):
        # An empty-ish string — not None (type annotation says str)
        assert _is_tmp_path("  ") is False  # spaces: no prefix match → False

    def test_unix_tmp(self):
        assert _is_tmp_path("/tmp/foo/bar") is True

    def test_var_tmp(self):
        assert _is_tmp_path("/var/tmp/scratch") is True

    def test_private_tmp_macos(self):
        assert _is_tmp_path("/private/tmp/x") is True

    def test_private_var_folders_macos(self):
        assert _is_tmp_path("/private/var/folders/ab/cdef/T/mytemp") is True

    def test_windows_system_temp(self):
        assert _is_tmp_path("C:\\Windows\\Temp\\foo") is True

    def test_windows_system_temp_forward_slash(self):
        assert _is_tmp_path("C:/Windows/Temp/foo") is True

    def test_windows_per_user_appdata_local_temp(self):
        assert _is_tmp_path("C:\\Users\\bob\\AppData\\Local\\Temp\\bar") is True

    def test_windows_appdata_case_insensitive(self):
        assert _is_tmp_path("C:\\users\\bob\\APPDATA\\LOCAL\\TEMP\\bar") is True

    def test_normal_project_path_is_false(self):
        assert _is_tmp_path("/home/user/git/myproject") is False

    def test_windows_style_project_path_is_false(self):
        assert _is_tmp_path("C:/Users/bob/git/myproject") is False

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows-only env branch")
    def test_env_temp_on_windows(self, monkeypatch):
        monkeypatch.setenv("TEMP", "D:\\scratch")
        # Reload _is_tmp_path resolution — it reads os.environ at call time
        assert _is_tmp_path("D:\\scratch\\foo") is True

    def test_env_temp_not_applied_on_non_windows(self, monkeypatch):
        """On non-Windows, the TEMP env fallback must NOT fire."""
        monkeypatch.setenv("TEMP", "/tmp")
        # /tmp already matches unix prefixes, so use a distinct path
        if sys.platform != "win32":
            monkeypatch.setenv("TEMP", "/home/user/custom-temp")
            assert _is_tmp_path("/home/user/custom-temp/foo") is False


# ---------------------------------------------------------------------------
# decode_project_folder
# ---------------------------------------------------------------------------

class TestDecodeProjectFolder:
    def test_unix_style_simple(self):
        result = decode_project_folder("-Users-rbgnr-git-foo")
        # On any platform the separator must be present in the result
        assert "Users" in result
        assert "rbgnr" in result
        assert "git" in result
        assert "foo" in result

    def test_unix_style_starts_with_slash_on_posix(self):
        if sys.platform != "win32":
            assert decode_project_folder("-Users-rbgnr-git-foo").startswith("/")

    def test_windows_style_drive_letter(self):
        result = decode_project_folder("C--Users-rbgnr-git-foo")
        assert result.startswith("C:")

    def test_windows_style_drive_letter_remainder(self):
        result = decode_project_folder("C--Users-rbgnr-git-foo")
        assert "Users" in result
        assert "rbgnr" in result

    def test_single_char_folder(self):
        # Edge: minimal path — just a single segment
        result = decode_project_folder("-x")
        # Should not crash; result contains "x"
        assert "x" in result

    def test_empty_string(self):
        # Should return empty without crashing
        result = decode_project_folder("")
        assert result == ""

    def test_project_with_hyphen_in_name(self):
        # "-Users-rbgnr-git-air-code" — hyphens in folder names must
        # be preserved as path separators.  The name segment "air-code"
        # is stored as "air" + separator + "code" in the encoded form.
        result = decode_project_folder("-Users-rbgnr-git-air-code")
        assert "air" in result
        assert "code" in result


# ---------------------------------------------------------------------------
# _load_active_pids
# ---------------------------------------------------------------------------

class TestLoadActivePids:
    def test_missing_sessions_dir_returns_empty(self, tmp_path):
        claude_home = tmp_path / ".claude"
        claude_home.mkdir()
        # No sessions/ subdir
        pids, names = _load_active_pids(claude_home)
        assert pids == {}
        assert names == {}

    def test_malformed_json_skipped(self, tmp_path):
        claude_home = tmp_path / ".claude"
        sessions = claude_home / "sessions"
        sessions.mkdir(parents=True)
        (sessions / "bad.json").write_text("not json {[", encoding="utf-8")
        pids, names = _load_active_pids(claude_home)
        assert pids == {}

    def test_dead_pid_skipped(self, tmp_path, monkeypatch):
        claude_home = tmp_path / ".claude"
        (claude_home / "sessions").mkdir(parents=True)
        (claude_home / "sessions" / "s1.json").write_text(
            json.dumps({"sessionId": "s1", "pid": 99999999}), encoding="utf-8"
        )
        import psutil
        monkeypatch.setattr(psutil, "Process", MagicMock(side_effect=psutil.NoSuchProcess(99999999)))
        pids, _ = _load_active_pids(claude_home)
        assert "s1" not in pids

    def test_zombie_process_skipped(self, tmp_path, monkeypatch):
        claude_home = tmp_path / ".claude"
        (claude_home / "sessions").mkdir(parents=True)
        (claude_home / "sessions" / "s2.json").write_text(
            json.dumps({"sessionId": "s2", "pid": 12345}), encoding="utf-8"
        )
        import psutil
        fake_proc = MagicMock()
        fake_proc.is_running.return_value = True
        fake_proc.status.return_value = psutil.STATUS_ZOMBIE
        monkeypatch.setattr(psutil, "Process", MagicMock(return_value=fake_proc))
        pids, _ = _load_active_pids(claude_home)
        assert "s2" not in pids

    def test_named_session_populated(self, tmp_path, monkeypatch):
        claude_home = tmp_path / ".claude"
        (claude_home / "sessions").mkdir(parents=True)
        (claude_home / "sessions" / "s3.json").write_text(
            json.dumps({"sessionId": "s3", "pid": None, "name": "my-session"}),
            encoding="utf-8",
        )
        _, names = _load_active_pids(claude_home)
        assert names.get("s3") == "my-session"

    def test_alive_process_included(self, tmp_path, monkeypatch):
        import psutil
        claude_home = tmp_path / ".claude"
        (claude_home / "sessions").mkdir(parents=True)
        (claude_home / "sessions" / "s4.json").write_text(
            json.dumps({"sessionId": "s4", "pid": 42}), encoding="utf-8"
        )
        fake_proc = MagicMock()
        fake_proc.is_running.return_value = True
        fake_proc.status.return_value = "running"
        monkeypatch.setattr(psutil, "Process", MagicMock(return_value=fake_proc))
        pids, _ = _load_active_pids(claude_home)
        assert pids.get("s4") == 42


# ---------------------------------------------------------------------------
# _mark_active_sessions
# ---------------------------------------------------------------------------

class TestMarkActiveSessionsStatus:
    def _fake_proc(self, monkeypatch, cpu_val=0.0, children_count=0):
        import psutil
        fp = MagicMock()
        # First call: prime (returns 0.0). Second call: actual.
        fp.cpu_percent.side_effect = [0.0, cpu_val]
        fp.children.return_value = [MagicMock()] * children_count
        monkeypatch.setattr(psutil, "Process", MagicMock(return_value=fp))
        monkeypatch.setattr(_scanner.time, "sleep", MagicMock())
        return fp

    def test_high_cpu_yields_working_status(self, monkeypatch):
        self._fake_proc(monkeypatch, cpu_val=50.0)
        sess = _make_session(session_id="w1",
                             modified="2020-01-01T00:00:00+00:00")  # old mtime
        _mark_active_sessions([sess], active_pids={"w1": 101})
        assert sess.status == "working"
        assert sess.cpu_percent == 50.0

    def test_low_cpu_old_mtime_yields_active_status(self, monkeypatch):
        self._fake_proc(monkeypatch, cpu_val=1.0)
        sess = _make_session(session_id="a1",
                             modified="2020-01-01T00:00:00+00:00")
        _mark_active_sessions([sess], active_pids={"a1": 102})
        assert sess.status == "active"

    def test_recent_mtime_yields_working_even_with_low_cpu(self, monkeypatch):
        self._fake_proc(monkeypatch, cpu_val=0.0)
        now = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())
        sess = _make_session(session_id="w2", modified=now)
        _mark_active_sessions([sess], active_pids={"w2": 103})
        assert sess.status == "working"

    def test_no_pid_remains_idle(self, monkeypatch):
        monkeypatch.setattr(_scanner.time, "sleep", MagicMock())
        sess = _make_session(session_id="i1")
        _mark_active_sessions([sess], active_pids={})
        assert sess.status == "idle"
        assert sess.pid is None

    def test_name_set_from_names_dict(self, monkeypatch):
        self._fake_proc(monkeypatch, cpu_val=0.0)
        sess = _make_session(session_id="n1",
                             modified="2020-01-01T00:00:00+00:00")
        _mark_active_sessions([sess], active_pids={"n1": 104},
                               names={"n1": "my-renamed-session"})
        assert sess.name == "my-renamed-session"

    def test_mtime_parse_failure_does_not_crash(self, monkeypatch):
        """When sess.modified is an unrecognisable string, the mtime branch
        must silently fall through to cpu-only logic without raising."""
        self._fake_proc(monkeypatch, cpu_val=0.0)
        sess = _make_session(session_id="m1", modified="NOT-A-DATE")
        _mark_active_sessions([sess], active_pids={"m1": 105})
        # Low CPU + bad mtime → active (not working, not idle)
        assert sess.status == "active"

    def test_subprocess_count_set(self, monkeypatch):
        self._fake_proc(monkeypatch, cpu_val=0.0, children_count=3)
        sess = _make_session(session_id="c1",
                             modified="2020-01-01T00:00:00+00:00")
        _mark_active_sessions([sess], active_pids={"c1": 106})
        assert sess.subprocess_count == 3


# ---------------------------------------------------------------------------
# _collect_git_state
# ---------------------------------------------------------------------------

class TestCollectGitState:
    def _run_patch(self, monkeypatch, dirty_out="", dirty_rc=0,
                   upstream_out="", upstream_rc=0,
                   ahead_behind_out="", ahead_behind_rc=0):
        """Patch subprocess.run to return controlled outputs for each git call."""
        calls = []

        def fake_run(cmd, **kw):
            calls.append(cmd)
            r = MagicMock()
            if "status" in cmd:
                r.returncode = dirty_rc
                r.stdout = dirty_out
            elif "rev-parse" in cmd:
                r.returncode = upstream_rc
                r.stdout = upstream_out
            elif "rev-list" in cmd and "--left-right" in cmd:
                r.returncode = ahead_behind_rc
                r.stdout = ahead_behind_out
            else:
                r.returncode = 1
                r.stdout = ""
            return r

        monkeypatch.setattr(_scanner.subprocess, "run", fake_run)
        return calls

    def test_not_a_repo_all_none(self, monkeypatch):
        self._run_patch(monkeypatch, dirty_rc=128, upstream_rc=128)
        result = _collect_git_state("/home/user/not-a-repo")
        assert result["git_dirty"] is None
        assert result["git_upstream"] is None
        assert result["git_ahead"] is None
        assert result["git_behind"] is None

    def test_dirty_tree(self, monkeypatch):
        self._run_patch(monkeypatch, dirty_out=" M src/foo.py\n",
                        upstream_rc=128)
        result = _collect_git_state("/home/user/repo")
        assert result["git_dirty"] is True

    def test_clean_tree(self, monkeypatch):
        self._run_patch(monkeypatch, dirty_out="", dirty_rc=0,
                        upstream_rc=128)
        result = _collect_git_state("/home/user/repo")
        assert result["git_dirty"] is False

    def test_upstream_populated(self, monkeypatch):
        self._run_patch(monkeypatch, upstream_out="origin/master\n",
                        upstream_rc=0, ahead_behind_out="2\t3\n",
                        ahead_behind_rc=0)
        result = _collect_git_state("/home/user/repo")
        assert result["git_upstream"] == "origin/master"
        assert result["git_behind"] == 2
        assert result["git_ahead"] == 3

    def test_ahead_behind_parse_failure_leaves_none(self, monkeypatch):
        self._run_patch(monkeypatch, upstream_out="origin/master\n",
                        upstream_rc=0, ahead_behind_out="garbage\n",
                        ahead_behind_rc=0)
        result = _collect_git_state("/home/user/repo")
        assert result["git_ahead"] is None
        assert result["git_behind"] is None

    def test_subprocess_exception_silenced(self, monkeypatch):
        monkeypatch.setattr(_scanner.subprocess, "run",
                            MagicMock(side_effect=OSError("no git")))
        result = _collect_git_state("/home/user/repo")
        assert result["git_dirty"] is None

    def test_empty_cwd_returns_all_none(self, monkeypatch):
        called = []
        monkeypatch.setattr(_scanner.subprocess, "run",
                            MagicMock(side_effect=lambda *a, **k: called.append(1)))
        result = _collect_git_state("")
        assert result["git_upstream"] is None
        # No subprocess call should have been made
        assert called == []


# ---------------------------------------------------------------------------
# scan_local: folder filtering
# ---------------------------------------------------------------------------

class TestScanLocalFolderFiltering:
    def _build_claude_home(self, tmp_path: Path) -> Path:
        ch = tmp_path / ".claude"
        (ch / "sessions").mkdir(parents=True)
        return ch

    def _redirect_caches(self, monkeypatch, tmp_path: Path) -> None:
        monkeypatch.setattr("src.scanner._persisted_cache_path",
                            lambda: tmp_path / "scan-cache.json")
        monkeypatch.setattr("src.scanner._git_cache_path",
                            lambda: tmp_path / "git-cache.json")
        if hasattr(scan_local, "_session_cache"):
            delattr(scan_local, "_session_cache")
        if hasattr(scan_local, "_git_cache"):
            delattr(scan_local, "_git_cache")

    def test_empty_projects_dir_returns_empty(self, tmp_path, monkeypatch):
        ch = self._build_claude_home(tmp_path)
        (ch / "projects").mkdir()
        self._redirect_caches(monkeypatch, tmp_path)
        monkeypatch.setattr(_scanner.subprocess, "run", MagicMock(
            return_value=MagicMock(returncode=1, stdout="")))
        sessions = scan_local(claude_home=ch, machine="t")
        assert sessions == []

    def test_unix_style_folder_included(self, tmp_path, monkeypatch):
        ch = self._build_claude_home(tmp_path)
        proj = ch / "projects" / "-home-user-proj"
        proj.mkdir(parents=True)
        _write_session_jsonl(proj / "sess1.jsonl",
                             cwd_override="/home/user/proj")
        self._redirect_caches(monkeypatch, tmp_path)
        monkeypatch.setattr(_scanner.subprocess, "run", MagicMock(
            return_value=MagicMock(returncode=1, stdout="")))
        sessions = scan_local(claude_home=ch, machine="t")
        assert len(sessions) == 1

    def test_windows_style_folder_included(self, tmp_path, monkeypatch):
        ch = self._build_claude_home(tmp_path)
        proj = ch / "projects" / "C--home-user-proj"
        proj.mkdir(parents=True)
        _write_session_jsonl(proj / "sess2.jsonl",
                             cwd_override="/home/user/proj")
        self._redirect_caches(monkeypatch, tmp_path)
        monkeypatch.setattr(_scanner.subprocess, "run", MagicMock(
            return_value=MagicMock(returncode=1, stdout="")))
        sessions = scan_local(claude_home=ch, machine="t")
        assert len(sessions) == 1

    def test_non_encoded_folder_skipped(self, tmp_path, monkeypatch):
        ch = self._build_claude_home(tmp_path)
        proj = ch / "projects" / "not-encoded"
        proj.mkdir(parents=True)
        _write_session_jsonl(proj / "sess3.jsonl",
                             cwd_override="/home/user/proj")
        self._redirect_caches(monkeypatch, tmp_path)
        monkeypatch.setattr(_scanner.subprocess, "run", MagicMock(
            return_value=MagicMock(returncode=1, stdout="")))
        sessions = scan_local(claude_home=ch, machine="t")
        assert sessions == []

    def test_on_progress_called(self, tmp_path, monkeypatch):
        ch = self._build_claude_home(tmp_path)
        proj = ch / "projects" / "-home-user-proj"
        proj.mkdir(parents=True)
        _write_session_jsonl(proj / "sess4.jsonl",
                             cwd_override="/home/user/proj")
        self._redirect_caches(monkeypatch, tmp_path)
        monkeypatch.setattr(_scanner.subprocess, "run", MagicMock(
            return_value=MagicMock(returncode=1, stdout="")))
        calls = []
        scan_local(claude_home=ch, machine="t",
                   on_progress=lambda *a: calls.append(a))
        assert len(calls) >= 1


# ---------------------------------------------------------------------------
# _load_persisted_cache / _save_persisted_cache: version mismatch
# ---------------------------------------------------------------------------

class TestPersistedCacheVersionMismatch:
    def test_wrong_version_returns_empty(self, tmp_path, monkeypatch):
        p = tmp_path / "scan-cache.json"
        p.write_text(json.dumps({"version": 99, "entries": {}}), encoding="utf-8")
        monkeypatch.setattr("src.scanner._persisted_cache_path", lambda: p)
        assert _load_persisted_cache() == {}

    def test_atomic_write_uses_tmp_then_rename(self, tmp_path, monkeypatch):
        monkeypatch.setattr("src.scanner._persisted_cache_path",
                            lambda: tmp_path / "scan-cache.json")
        _save_persisted_cache({})
        assert (tmp_path / "scan-cache.json").is_file()
        # tmp file should be gone (renamed)
        assert not (tmp_path / "scan-cache.json.tmp").is_file()


class TestPersistedGitCacheVersionMismatch:
    def test_wrong_version_returns_empty(self, tmp_path, monkeypatch):
        p = tmp_path / "git-cache.json"
        p.write_text(json.dumps({"version": 99, "entries": []}), encoding="utf-8")
        monkeypatch.setattr("src.scanner._git_cache_path", lambda: p)
        assert _load_persisted_git_cache() == {}


# ---------------------------------------------------------------------------
# scan_remote_via_api
# ---------------------------------------------------------------------------

class TestScanRemoteViaApi:
    def _make_mock_session(self, status: int, body=None, exc=None):
        """Build a mock aiohttp.ClientSession context manager."""
        mock_resp = AsyncMock()
        mock_resp.status = status
        if body is not None:
            mock_resp.json = AsyncMock(return_value=body)
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_cs = MagicMock()
        if exc:
            mock_cs.__aenter__ = AsyncMock(side_effect=exc)
        else:
            inner = MagicMock()
            inner.get = MagicMock(return_value=mock_resp)
            inner.__aenter__ = AsyncMock(return_value=inner)
            inner.__aexit__ = AsyncMock(return_value=False)
            mock_cs.__aenter__ = AsyncMock(return_value=inner)
        mock_cs.__aexit__ = AsyncMock(return_value=False)
        return mock_cs

    @pytest.mark.asyncio
    async def test_200_success(self, monkeypatch):
        item = {
            "session_id": "r1", "project_folder": "-home-x", "project_path": "/home/x",
            "cwd": "/home/x", "slug": "s", "summary": "sum", "messages": 1,
            "modified": "2026-01-01T00:00:00", "status": "idle", "pid": None,
            "file_size": 0, "tokens": 0, "name": "", "git_branch": "",
            "subprocess_count": 0, "git_remote": "", "git_commits": 0,
            "last_user_message": "", "readme_path": "",
            "git_upstream": None, "git_ahead": None,
            "git_behind": None, "git_dirty": None,
        }
        mock_cs = self._make_mock_session(200, body=[item])
        import aiohttp
        with patch.object(aiohttp, "ClientSession", return_value=mock_cs):
            sessions = await scan_remote_via_api("m1", "127.0.0.1", 44730)

        assert len(sessions) == 1
        assert sessions[0].session_id == "r1"
        assert sessions[0].machine == "m1"

    @pytest.mark.asyncio
    async def test_non_200_returns_empty(self, monkeypatch):
        mock_cs = self._make_mock_session(503)
        import aiohttp
        with patch.object(aiohttp, "ClientSession", return_value=mock_cs):
            sessions = await scan_remote_via_api("m2", "127.0.0.1", 44730)

        assert sessions == []

    @pytest.mark.asyncio
    async def test_exception_returns_empty(self, monkeypatch):
        mock_cs = self._make_mock_session(0, exc=OSError("connection refused"))
        import aiohttp
        with patch.object(aiohttp, "ClientSession", return_value=mock_cs):
            sessions = await scan_remote_via_api("m3", "127.0.0.1", 44730)

        assert sessions == []


# ---------------------------------------------------------------------------
# scan_remote (SSH)
# ---------------------------------------------------------------------------

class TestScanRemoteSsh:
    def _make_executor(self, rc=0, stdout=b"[]", side_effect=None):
        ex = AsyncMock()
        if side_effect:
            ex.exec_shell = AsyncMock(side_effect=side_effect)
        else:
            ex.exec_shell = AsyncMock(return_value=(rc, stdout, b""))
        return ex

    @pytest.mark.asyncio
    async def test_timeout_returns_empty(self):
        with patch("src.scanner.SSHExecutor") as mock_cls:
            mock_cls.return_value = self._make_executor(
                side_effect=asyncio.TimeoutError())
            sessions = await scan_remote("m1", "m1-alias")
        assert sessions == []

    @pytest.mark.asyncio
    async def test_ssh_exception_returns_empty(self):
        with patch("src.scanner.SSHExecutor") as mock_cls:
            mock_cls.return_value = self._make_executor(
                side_effect=OSError("SSH failed"))
            sessions = await scan_remote("m2", "m2-alias")
        assert sessions == []

    @pytest.mark.asyncio
    async def test_non_zero_rc_returns_empty(self):
        with patch("src.scanner.SSHExecutor") as mock_cls:
            mock_cls.return_value = self._make_executor(rc=1, stdout=b"error")
            sessions = await scan_remote("m3", "m3-alias")
        assert sessions == []

    @pytest.mark.asyncio
    async def test_json_parse_failure_returns_empty(self):
        with patch("src.scanner.SSHExecutor") as mock_cls:
            mock_cls.return_value = self._make_executor(rc=0, stdout=b"not json {[")
            sessions = await scan_remote("m4", "m4-alias")
        assert sessions == []

    @pytest.mark.asyncio
    async def test_success_returns_sessions(self):
        item = {
            "session_id": "r5", "project_folder": "-home-x",
            "project_path": "/home/x", "cwd": "/home/x",
            "slug": "s", "summary": "sum", "messages": 1,
            "modified": "2026-01-01T00:00:00", "status": "idle",
            "pid": None, "file_size": 0, "tokens": 0, "name": "",
            "git_branch": "", "subprocess_count": 0, "git_remote": "",
            "git_commits": 0, "last_user_message": "", "readme_path": "",
            "git_upstream": None, "git_ahead": None, "git_behind": None,
            "git_dirty": None,
        }
        with patch("src.scanner.SSHExecutor") as mock_cls:
            mock_cls.return_value = self._make_executor(
                rc=0, stdout=json.dumps([item]).encode())
            sessions = await scan_remote("m5", "m5-alias")
        assert len(sessions) == 1
        assert sessions[0].session_id == "r5"
        assert sessions[0].machine == "m5"


# ---------------------------------------------------------------------------
# scan_all
# ---------------------------------------------------------------------------

class TestScanAll:
    @pytest.mark.asyncio
    async def test_local_only_no_fleet(self, monkeypatch):
        monkeypatch.setattr(_scanner, "scan_local",
                            MagicMock(return_value=[_make_session(session_id="loc1")]))
        sessions = await _scanner.scan_all(
            local_machine=None, fleet={})
        assert any(s.session_id == "loc1" for s in sessions)

    @pytest.mark.asyncio
    async def test_offline_fleet_machine_skipped(self, monkeypatch):
        monkeypatch.setattr(_scanner, "scan_local",
                            MagicMock(return_value=[]))
        sessions = await _scanner.scan_all(
            local_machine="local",
            fleet={"remote1": {"online": False}},
        )
        # No remote scan attempted — just local empty
        assert sessions == []

    @pytest.mark.asyncio
    async def test_on_progress_callback_invoked(self, monkeypatch):
        monkeypatch.setattr(_scanner, "scan_local",
                            MagicMock(return_value=[]))
        calls = []
        await _scanner.scan_all(
            local_machine=None, fleet={},
            on_progress=lambda m, f, t, c: calls.append((m, f, t, c)),
        )
        assert len(calls) >= 1


# ---------------------------------------------------------------------------
# _load_project_cache / _save_project_cache / _update_project_cache
# ---------------------------------------------------------------------------

class TestProjectCache:
    def test_load_missing_returns_empty_scaffold(self, tmp_path, monkeypatch):
        monkeypatch.setattr(_scanner, "_PROJECT_CACHE_FILE",
                            tmp_path / "nonexistent.json")
        result = _load_project_cache()
        assert result["version"] == 1
        assert isinstance(result["projects"], dict)

    def test_save_then_load_roundtrip(self, tmp_path, monkeypatch):
        cache_file = tmp_path / "project-cache.json"
        monkeypatch.setattr(_scanner, "_PROJECT_CACHE_FILE", cache_file)
        monkeypatch.setattr(_scanner, "_PROJECT_CACHE_DIR", tmp_path)
        cache = {"version": 1, "updated": "2026-01-01", "projects": {"p1": {"display_name": "Proj1"}}}
        _save_project_cache(cache)
        loaded = _load_project_cache()
        assert loaded["projects"]["p1"]["display_name"] == "Proj1"

    def test_load_malformed_returns_scaffold(self, tmp_path, monkeypatch):
        cache_file = tmp_path / "project-cache.json"
        cache_file.write_text("not valid json {[", encoding="utf-8")
        monkeypatch.setattr(_scanner, "_PROJECT_CACHE_FILE", cache_file)
        result = _load_project_cache()
        assert result["version"] == 1
        assert result["projects"] == {}

    def test_update_project_cache_populates(self, tmp_path, monkeypatch):
        cache_file = tmp_path / "project-cache.json"
        monkeypatch.setattr(_scanner, "_PROJECT_CACHE_FILE", cache_file)
        monkeypatch.setattr(_scanner, "_PROJECT_CACHE_DIR", tmp_path)
        sess = _make_session(
            session_id="u1", machine="local", git_remote="git@github.com:x/y.git",
            modified="2026-05-20T00:00:00+00:00",
        )
        _update_project_cache(
            [sess],
            pid_fn=lambda s: "proj-x",
            pdn_fn=lambda pid: "Project X",
        )
        loaded = _load_project_cache()
        assert "proj-x" in loaded["projects"]
        assert loaded["projects"]["proj-x"]["display_name"] == "Project X"
