"""
Test VERSION.json freshness and schema — the file that fleet-watchdog polls
to decide whether to auto-pull + redeploy the app.

If VERSION.json goes stale (committed file lags behind actual git HEAD),
fleet-watchdog's _auto_update_tick sees `remote_version == installed_version`
and silently skips — no updates propagate fleet-wide until a human notices.
This happened 2026-04-22: v151 committed, HEAD was at v155+, ubuntu-desktop
didn't auto-pull today's fixes until manual pull.

These tests make the freshness invariant enforceable: CI fails if VERSION.json
falls out of sync with HEAD. Human regenerates via `python3 scripts/gen_version.py`
and re-commits. Prevents "I pushed my fix but nothing propagates" confusion.
"""
import json
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
VERSION_FILE = REPO_ROOT / "VERSION.json"


def _git(*args: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(REPO_ROOT), *args], text=True
    ).strip()


@pytest.fixture(scope="module")
def version_manifest() -> dict:
    assert VERSION_FILE.exists(), (
        f"VERSION.json missing at {VERSION_FILE}. "
        "fleet-watchdog cannot detect updates without it. "
        "Generate via: python3 scripts/gen_version.py"
    )
    with VERSION_FILE.open() as fh:
        return json.load(fh)


class TestVersionManifestSchema:
    """VERSION.json must satisfy the fleet-watchdog schema contract."""

    REQUIRED_FIELDS = {"version", "commit", "commit_full", "branch", "date", "message"}

    def test_has_all_required_fields(self, version_manifest):
        missing = self.REQUIRED_FIELDS - set(version_manifest)
        assert not missing, f"VERSION.json missing required fields: {missing}"

    def test_version_is_positive_int(self, version_manifest):
        v = version_manifest["version"]
        assert isinstance(v, int), f"version must be int, got {type(v).__name__}"
        assert v > 0, f"version must be > 0, got {v}"

    def test_commit_short_is_7_to_12_hex(self, version_manifest):
        c = version_manifest["commit"]
        assert isinstance(c, str)
        assert 7 <= len(c) <= 12, f"short commit length {len(c)} out of expected range"
        assert all(ch in "0123456789abcdef" for ch in c), f"non-hex in commit: {c!r}"

    def test_commit_full_is_40_hex(self, version_manifest):
        c = version_manifest["commit_full"]
        assert isinstance(c, str)
        assert len(c) == 40, f"commit_full must be 40 hex chars, got {len(c)}"
        assert all(ch in "0123456789abcdef" for ch in c), f"non-hex in commit_full: {c!r}"

    def test_short_commit_is_prefix_of_full(self, version_manifest):
        short = version_manifest["commit"]
        full = version_manifest["commit_full"]
        assert full.startswith(short), (
            f"commit {short!r} is not a prefix of commit_full {full!r}"
        )


class TestVersionManifestFreshness:
    """VERSION.json must be in sync with the actual git HEAD.

    Chicken-and-egg: gen_version.py captures HEAD *before* the regen commit
    is made, so after committing the regen itself HEAD advances by one.
    Two valid states:

      A) Fresh (most recent):     VERSION.json.commit_full == HEAD
      B) Bump-pending:            HEAD is a 'chore: regenerate VERSION.json'
                                  commit, and VERSION.json.commit_full == HEAD~1

    Anything else = stale. If stale, run `python3 scripts/gen_version.py`
    and commit the result. A stale VERSION.json blocks fleet-watchdog
    auto-updates fleet-wide — happened 2026-04-22 on ubuntu-desktop.
    """

    @staticmethod
    def _is_regen_commit(subject: str) -> bool:
        s = subject.lower()
        return (
            "regenerate version.json" in s
            or "bump version.json" in s
            or "chore: version" in s
            or ("version.json" in s and ("bump" in s or "regen" in s or "sync" in s))
        )

    def test_commit_full_matches_head_or_bump_parent(self, version_manifest):
        head = _git("rev-parse", "HEAD")
        head_subject = _git("log", "-1", "--format=%s", "HEAD")

        if version_manifest["commit_full"] == head:
            return  # State A — freshest

        # State B — only acceptable if HEAD is itself a VERSION.json bump
        # and VERSION.json references HEAD's parent.
        if self._is_regen_commit(head_subject):
            parent = _git("rev-parse", "HEAD~1")
            assert version_manifest["commit_full"] == parent, (
                f"VERSION.json.commit_full ({version_manifest['commit_full']!r}) "
                f"should reference HEAD ({head[:12]}) or HEAD~1 ({parent[:12]}) — "
                "but points to neither. Regenerate."
            )
            return

        pytest.fail(
            f"VERSION.json.commit_full ({version_manifest['commit_full']!r}) "
            f"does not match HEAD ({head[:12]}) and HEAD is not a regen commit "
            f"(subject: {head_subject!r}). Run: python3 scripts/gen_version.py && "
            "git add VERSION.json && git commit -m 'chore: regenerate VERSION.json'"
        )

    def test_short_commit_is_prefix_of_commit_full(self, version_manifest):
        short = version_manifest["commit"]
        full = version_manifest["commit_full"]
        assert full.startswith(short), (
            f"commit {short!r} is not a prefix of commit_full {full!r}"
        )

    def test_branch_matches_git(self, version_manifest):
        try:
            branch = _git("rev-parse", "--abbrev-ref", "HEAD")
        except subprocess.CalledProcessError:
            pytest.skip("git branch unreadable")
        if branch == "HEAD":
            pytest.skip("detached HEAD — branch check not meaningful")
        assert version_manifest["branch"] == branch, (
            f"VERSION.json.branch ({version_manifest['branch']!r}) "
            f"does not match git branch ({branch!r})"
        )

    def test_version_matches_rev_list_count(self, version_manifest):
        # gen_version.py convention: version = rev-list count + 1 *at the time
        # of regen* (so the bump commit itself ends up with rev-list == version).
        # After the bump commit lands, the invariant is:
        #   version == rev-list (if HEAD is the bump commit)
        #   version == rev-list + 1 (if VERSION.json is still 'fresh' — HEAD
        #                            hasn't been bumped-committed yet).
        count = int(_git("rev-list", "--count", "HEAD"))
        v = version_manifest["version"]
        assert v in (count, count + 1), (
            f"VERSION.json.version ({v}) must equal git rev-list --count HEAD "
            f"({count}) or {count}+1. It's neither — regenerate."
        )

    def test_message_matches_head_or_parent_subject(self, version_manifest):
        head_subject = _git("log", "-1", "--format=%s", "HEAD")
        if version_manifest["message"] == head_subject:
            return
        # Acceptable variant: HEAD is a regen bump, VERSION.json still
        # references the prior commit's subject.
        if self._is_regen_commit(head_subject):
            parent_subject = _git("log", "-1", "--format=%s", "HEAD~1")
            assert version_manifest["message"] == parent_subject, (
                f"VERSION.json.message ({version_manifest['message']!r}) "
                f"does not match HEAD ({head_subject!r}) or HEAD~1 "
                f"({parent_subject!r}) — regenerate."
            )
            return
        pytest.fail(
            f"VERSION.json.message ({version_manifest['message']!r}) "
            f"does not match HEAD commit subject ({head_subject!r})"
        )


class TestVersionManifestMonotonic:
    """Across history, VERSION.json.version must never go backwards."""

    def test_never_regresses_across_commits(self):
        # Walk recent commits on the current branch; any VERSION.json change
        # that decreases version is a serious bug (auto-update loop would
        # see a downgrade and refuse to pull, blocking future updates).
        try:
            log = _git("log", "--pretty=format:%H", "-n", "30", "--", "VERSION.json")
        except subprocess.CalledProcessError:
            pytest.skip("git log for VERSION.json unreadable")
        commits = [c for c in log.splitlines() if c]
        if len(commits) < 2:
            pytest.skip("not enough VERSION.json history to check monotonicity")
        versions: list[tuple[str, int]] = []
        for sha in commits:
            try:
                blob = subprocess.check_output(
                    ["git", "-C", str(REPO_ROOT), "show", f"{sha}:VERSION.json"],
                    text=True,
                )
                versions.append((sha, int(json.loads(blob)["version"])))
            except (subprocess.CalledProcessError, KeyError, ValueError, json.JSONDecodeError):
                continue
        # History is newest-first from `git log`; reverse so we walk forward in time.
        versions.reverse()
        for (prev_sha, prev_v), (curr_sha, curr_v) in zip(versions, versions[1:]):
            assert curr_v >= prev_v, (
                f"VERSION.json regressed: {prev_sha[:7]} had v{prev_v}, "
                f"{curr_sha[:7]} dropped to v{curr_v}"
            )
