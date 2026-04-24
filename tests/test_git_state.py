"""Tests for per-cwd git state collection (Phase B).

Covers the `_collect_git_state(cwd)` helper and verifies that the new
ClaudeSession fields git_upstream / git_ahead / git_behind / git_dirty are
populated end-to-end. Also sanity-checks REMOTE_SCAN_SCRIPT still compiles
and contains the added git commands.
"""
from __future__ import annotations

import ast
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from src.scanner import ClaudeSession, REMOTE_SCAN_SCRIPT, _collect_git_state


# ---------------------------------------------------------------------------
# ClaudeSession dataclass
# ---------------------------------------------------------------------------

class TestClaudeSessionGitFields:
    def _make(self, **kw) -> ClaudeSession:
        defaults = dict(
            session_id="s", machine="m", project_folder="-p", project_path="/p",
            cwd="/p", slug="", summary="", messages=0, modified="",
            status="idle", pid=None,
        )
        defaults.update(kw)
        return ClaudeSession(**defaults)

    def test_defaults_are_none_or_false(self):
        s = self._make()
        assert s.git_upstream is None
        assert s.git_ahead is None
        assert s.git_behind is None
        assert s.git_dirty is None

    def test_to_dict_exposes_new_fields(self):
        s = self._make(git_upstream="origin/main", git_ahead=1, git_behind=3, git_dirty=True)
        d = s.to_dict()
        assert d["git_upstream"] == "origin/main"
        assert d["git_ahead"] == 1
        assert d["git_behind"] == 3
        assert d["git_dirty"] is True

    def test_can_round_trip_via_asdict(self):
        """to_dict + reconstruct ClaudeSession(**d) keeps new fields intact."""
        s = self._make(git_ahead=0, git_behind=5, git_dirty=False, git_upstream="origin/master")
        d = s.to_dict()
        s2 = ClaudeSession(**d)
        assert s2.git_ahead == 0
        assert s2.git_behind == 5
        assert s2.git_dirty is False
        assert s2.git_upstream == "origin/master"


# ---------------------------------------------------------------------------
# _collect_git_state() — real git repos in tempdirs
# ---------------------------------------------------------------------------

def _has_git() -> bool:
    return shutil.which("git") is not None


@pytest.mark.skipif(not _has_git(), reason="git not available")
class TestCollectGitState:
    def _init_repo(self, path: Path, *, user="t@t", name="t") -> None:
        subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
        subprocess.run(["git", "-C", str(path), "config", "user.email", user], check=True)
        subprocess.run(["git", "-C", str(path), "config", "user.name", name], check=True)

    def _commit(self, path: Path, fname: str = "f.txt", content: str = "x") -> None:
        (path / fname).write_text(content)
        subprocess.run(["git", "-C", str(path), "add", fname], check=True)
        subprocess.run(["git", "-C", str(path), "commit", "-q", "-m", f"add {fname}"], check=True)

    def test_non_git_dir_returns_no_state(self, tmp_path):
        state = _collect_git_state(str(tmp_path))
        assert state["git_upstream"] is None
        assert state["git_ahead"] is None
        assert state["git_behind"] is None
        # dirty is explicitly None when status fails (not False — we can't distinguish
        # "not a repo" from "clean repo" without the upstream check succeeding)
        assert state["git_dirty"] is None

    def test_clean_repo_no_upstream(self, tmp_path):
        self._init_repo(tmp_path)
        self._commit(tmp_path)
        state = _collect_git_state(str(tmp_path))
        # No upstream configured — ahead/behind are unknown.
        assert state["git_upstream"] is None
        assert state["git_ahead"] is None
        assert state["git_behind"] is None
        # Dirty is resolvable even without upstream — clean tree.
        assert state["git_dirty"] is False

    def test_dirty_tree_tracked_file_modified(self, tmp_path):
        self._init_repo(tmp_path)
        self._commit(tmp_path, "f.txt", "a")
        (tmp_path / "f.txt").write_text("b")  # modify tracked file
        state = _collect_git_state(str(tmp_path))
        assert state["git_dirty"] is True

    def test_untracked_files_do_not_mark_dirty(self, tmp_path):
        """We run with --untracked-files=no so scratch notes / build output
        don't falsely disable the Pull button."""
        self._init_repo(tmp_path)
        self._commit(tmp_path)
        (tmp_path / "scratch.log").write_text("noise")
        state = _collect_git_state(str(tmp_path))
        assert state["git_dirty"] is False

    def test_ahead_behind_against_upstream(self, tmp_path):
        """Set up a local upstream so ahead/behind is computable."""
        origin = tmp_path / "origin"
        origin.mkdir()
        # Bare origin
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)

        clone = tmp_path / "clone"
        subprocess.run(["git", "clone", "-q", str(origin), str(clone)], check=True)
        subprocess.run(["git", "-C", str(clone), "config", "user.email", "t@t"], check=True)
        subprocess.run(["git", "-C", str(clone), "config", "user.name", "t"], check=True)
        # Initial commit + push so upstream tracking engages
        self._commit(clone, "init.txt", "a")
        subprocess.run(["git", "-C", str(clone), "push", "-q", "-u", "origin", "main"], check=True)

        # Case: clean + up-to-date
        state = _collect_git_state(str(clone))
        assert state["git_upstream"] == "origin/main"
        assert state["git_ahead"] == 0
        assert state["git_behind"] == 0
        assert state["git_dirty"] is False

        # Case: local ahead (commit locally, don't push)
        self._commit(clone, "ahead.txt", "a")
        state = _collect_git_state(str(clone))
        assert state["git_ahead"] == 1
        assert state["git_behind"] == 0

        # Case: remote ahead — make a second clone, commit+push, back to first clone, fetch
        clone2 = tmp_path / "clone2"
        subprocess.run(["git", "clone", "-q", str(origin), str(clone2)], check=True)
        subprocess.run(["git", "-C", str(clone2), "config", "user.email", "t@t"], check=True)
        subprocess.run(["git", "-C", str(clone2), "config", "user.name", "t"], check=True)
        self._commit(clone2, "fromother.txt", "a")
        subprocess.run(["git", "-C", str(clone2), "push", "-q"], check=True)

        subprocess.run(["git", "-C", str(clone), "fetch", "-q"], check=True)
        state = _collect_git_state(str(clone))
        # clone is 1 ahead (its local "ahead.txt") AND 1 behind (clone2's push)
        assert state["git_ahead"] == 1
        assert state["git_behind"] == 1


# ---------------------------------------------------------------------------
# REMOTE_SCAN_SCRIPT — must compile and include new commands
# ---------------------------------------------------------------------------

class TestRemoteScanScriptContents:
    def test_remote_script_is_valid_python(self):
        ast.parse(REMOTE_SCAN_SCRIPT)

    def test_remote_script_collects_upstream(self):
        assert "@{upstream}" in REMOTE_SCAN_SCRIPT or "@{u}" in REMOTE_SCAN_SCRIPT

    def test_remote_script_runs_status_porcelain_untracked_no(self):
        assert "--porcelain" in REMOTE_SCAN_SCRIPT
        assert "--untracked-files=no" in REMOTE_SCAN_SCRIPT

    def test_remote_script_computes_left_right_ahead_behind(self):
        assert "--left-right" in REMOTE_SCAN_SCRIPT
        assert "--count" in REMOTE_SCAN_SCRIPT

    def test_remote_script_emits_new_keys_in_output(self):
        """The JSON result list each item must include the new fields."""
        for key in ("git_upstream", "git_ahead", "git_behind", "git_dirty"):
            assert f"'{key}'" in REMOTE_SCAN_SCRIPT, f"missing {key}"
