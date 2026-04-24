"""Unit tests for src/project_identity.py.

Covers:
  - normalize_remote(): SSH / HTTPS / self-hosted / .git suffix / empty
  - project_id(): git_remote priority, basename fallback, project_folder last resort
  - canonical_basename(): bare directory-name lowercased with separator normalisation
"""
from __future__ import annotations

from dataclasses import dataclass

from src.project_identity import (
    canonical_basename,
    normalize_remote,
    project_display_name,
    project_id,
)


@dataclass
class _Sess:
    """Lightweight stand-in for ClaudeSession — only fields project_identity reads."""
    git_remote: str = ""
    cwd: str = ""
    project_path: str = ""
    project_folder: str = ""


# ---------------------------------------------------------------------------
# normalize_remote
# ---------------------------------------------------------------------------

class TestNormalizeRemote:
    def test_ssh_url_strips_git_suffix_and_lowercases(self):
        assert normalize_remote("git@github.com:NextstarDigital/Streams-Android.git") == \
            "github.com/nextstardigital/streams-android"

    def test_ssh_url_without_git_suffix(self):
        assert normalize_remote("git@github.com:owner/repo") == "github.com/owner/repo"

    def test_https_url_with_git_suffix(self):
        assert normalize_remote("https://github.com/Owner/Repo.git") == "github.com/owner/repo"

    def test_https_url_without_git_suffix(self):
        assert normalize_remote("https://github.com/owner/repo") == "github.com/owner/repo"

    def test_https_url_with_query_string(self):
        assert normalize_remote("https://gitlab.com/group/proj?ref=main") == "gitlab.com/group/proj"

    def test_self_hosted_https_falls_through_to_generic(self):
        assert normalize_remote("https://git.mycorp.internal/team/svc.git") == \
            "git.mycorp.internal/team/svc"

    def test_self_hosted_ssh_falls_through_to_generic(self):
        # ssh://user@host/path/to/repo.git
        assert normalize_remote("ssh://git@gitea.local:2222/team/repo.git") == "team/repo"

    def test_empty_url_returns_empty(self):
        assert normalize_remote("") == ""

    def test_whitespace_only_returns_empty_via_strip(self):
        # Only whitespace collapses to empty via strip → '' check at top
        assert normalize_remote("   ") == ""


# ---------------------------------------------------------------------------
# project_id
# ---------------------------------------------------------------------------

class TestProjectId:
    def test_git_remote_takes_precedence_over_path(self):
        s = _Sess(git_remote="git@github.com:x/foo.git", cwd="/Users/me/diff-name")
        assert project_id(s) == "github.com/x/foo"

    def test_empty_remote_falls_back_to_cwd_basename(self):
        s = _Sess(cwd="/Users/rbgnr/git/streams-android")
        assert project_id(s) == "streams-android"

    def test_windows_path_basename(self):
        s = _Sess(cwd=r"C:\Users\rbgnr\git\streams-android")
        assert project_id(s) == "streams-android"

    def test_no_cwd_falls_back_to_project_path(self):
        s = _Sess(project_path="/srv/apps/my-api")
        assert project_id(s) == "my-api"

    def test_no_path_or_remote_uses_project_folder(self):
        s = _Sess(project_folder="-Users-rbgnr-git-MYRepo")
        assert project_id(s) == "-users-rbgnr-git-myrepo"

    def test_empty_session_returns_unknown_sentinel(self):
        assert project_id(_Sess()) == "unknown"

    def test_trailing_slash_stripped_before_basename(self):
        s = _Sess(cwd="/home/me/work/")
        assert project_id(s) == "work"


# ---------------------------------------------------------------------------
# canonical_basename (new — Phase A helper)
# ---------------------------------------------------------------------------

class TestCanonicalBasename:
    def test_unix_path(self):
        s = _Sess(cwd="/Users/rbgnr/git/streams-android")
        assert canonical_basename(s) == "streams-android"

    def test_windows_path(self):
        s = _Sess(cwd=r"C:\Users\rbgnr\git\Streams-Android")
        assert canonical_basename(s) == "streams-android"

    def test_mixed_separators(self):
        s = _Sess(cwd="/Users/rbgnr\\git/foo")
        assert canonical_basename(s) == "foo"

    def test_trailing_separator(self):
        s = _Sess(cwd="/Users/rbgnr/git/bar/")
        assert canonical_basename(s) == "bar"

    def test_falls_back_to_project_path_if_no_cwd(self):
        s = _Sess(project_path="/srv/apps/svc")
        assert canonical_basename(s) == "svc"

    def test_returns_empty_string_when_no_path_fields(self):
        # Unlike project_id, canonical_basename MUST return '' when there is no
        # usable path — the consolidation logic uses '' as a sentinel meaning
        # "cannot merge by basename, leave alone".
        assert canonical_basename(_Sess()) == ""

    def test_ignores_git_remote(self):
        # Even if git_remote is present, canonical_basename only cares about the
        # filesystem path. This is the point — it's the fallback id.
        s = _Sess(git_remote="git@github.com:x/foo.git", cwd="/srv/bar")
        assert canonical_basename(s) == "bar"


# ---------------------------------------------------------------------------
# project_display_name
# ---------------------------------------------------------------------------

class TestDisplayName:
    def test_host_owner_repo_returns_repo(self):
        assert project_display_name("github.com/owner/my-repo") == "my-repo"

    def test_bare_basename_returned_as_is(self):
        assert project_display_name("my-repo") == "my-repo"

    def test_empty_returns_unknown(self):
        assert project_display_name("") == "unknown"
