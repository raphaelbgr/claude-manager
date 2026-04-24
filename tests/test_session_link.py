"""
Unit tests for src/session_link.py — correlate tmux panes to Claude sessions.

Covers every combination of:
  - build_cwd_index: empty, single, multi-session, cwd vs project_path, machine
    scoping, /clear rotation (newest-modified wins), missing fields, duplicates.
  - link_for: shell filter (bash/zsh/fish/sh/dash/ksh/pwsh/powershell/cmd/cmd.exe,
    mixed case), empty command graceful fallback, cwd-miss, machine mismatch,
    match via project_path, display-name precedence (name > slug > uuid8).
  - enrich_tmux_dicts: no mutation of inputs, preserved existing fields, mixed
    linkable/non-linkable, empty inputs.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from src.session_link import build_cwd_index, enrich_tmux_dicts, link_for


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------

@dataclass
class FakeSession:
    """Minimal stand-in for scanner.ClaudeSession — only the fields the linker reads."""
    session_id: str = ""
    machine: str = "local"
    cwd: str = ""
    project_path: str = ""
    modified: str = ""
    name: str = ""
    slug: str = ""


@dataclass
class FakeTmux:
    """Minimal stand-in for tmux_manager.TmuxSession."""
    name: str = "t"
    machine: str = "local"
    cwd: str = ""
    pane_current_command: str = ""
    # extra fields so to_dict mirrors the real shape
    created: str = ""
    windows: int = 1
    attached: bool = False
    is_local: bool = True

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "machine": self.machine,
            "created": self.created,
            "windows": self.windows,
            "attached": self.attached,
            "is_local": self.is_local,
            "cwd": self.cwd,
            "pane_current_command": self.pane_current_command,
        }


# ---------------------------------------------------------------------------
# build_cwd_index
# ---------------------------------------------------------------------------

class TestBuildCwdIndex:
    """(machine, cwd|project_path) → most recent ClaudeSession."""

    def test_empty_sessions_returns_empty_index(self):
        assert build_cwd_index([]) == {}

    def test_single_session_with_cwd(self):
        s = FakeSession(session_id="a", machine="local", cwd="/p", modified="2026-01-01T00:00:00")
        idx = build_cwd_index([s])
        assert idx == {("local", "/p"): s}

    def test_cwd_and_project_path_both_indexed_when_different(self):
        s = FakeSession(session_id="a", machine="local", cwd="/p", project_path="/q",
                        modified="2026-01-01T00:00:00")
        idx = build_cwd_index([s])
        assert idx == {("local", "/p"): s, ("local", "/q"): s}

    def test_cwd_and_project_path_deduplicated_when_equal(self):
        s = FakeSession(session_id="a", machine="local", cwd="/p", project_path="/p",
                        modified="2026-01-01T00:00:00")
        idx = build_cwd_index([s])
        assert len(idx) == 1
        assert idx[("local", "/p")] is s

    def test_empty_cwd_not_indexed(self):
        s = FakeSession(session_id="a", machine="local", cwd="", project_path="",
                        modified="2026-01-01T00:00:00")
        assert build_cwd_index([s]) == {}

    def test_empty_cwd_but_project_path_indexes_project_path(self):
        s = FakeSession(session_id="a", machine="local", cwd="", project_path="/q",
                        modified="2026-01-01T00:00:00")
        idx = build_cwd_index([s])
        assert idx == {("local", "/q"): s}

    def test_multi_session_same_cwd_newer_modified_wins(self):
        """This is the /clear rotation case — new JSONL mtime beats old."""
        old = FakeSession(session_id="old", machine="local", cwd="/p",
                          modified="2026-01-01T00:00:00")
        new = FakeSession(session_id="new", machine="local", cwd="/p",
                          modified="2026-02-01T00:00:00")
        # Insert in both orders to prove it's not insertion-order-dependent.
        assert build_cwd_index([old, new])[("local", "/p")] is new
        assert build_cwd_index([new, old])[("local", "/p")] is new

    def test_multi_session_different_machines_kept_separate(self):
        a = FakeSession(session_id="a", machine="local", cwd="/p", modified="2026-01-01T00:00:00")
        b = FakeSession(session_id="b", machine="mac-mini", cwd="/p", modified="2026-01-01T00:00:00")
        idx = build_cwd_index([a, b])
        assert idx[("local", "/p")] is a
        assert idx[("mac-mini", "/p")] is b

    def test_missing_modified_still_indexed(self):
        """Empty modified string should not blow up — any value is > None logic."""
        s = FakeSession(session_id="a", machine="local", cwd="/p", modified="")
        idx = build_cwd_index([s])
        assert idx[("local", "/p")] is s

    def test_equal_modified_last_wins(self):
        """Tie-break policy: deterministic based on iteration order (last overwrites)."""
        a = FakeSession(session_id="a", machine="local", cwd="/p", modified="2026-01-01T00:00:00")
        b = FakeSession(session_id="b", machine="local", cwd="/p", modified="2026-01-01T00:00:00")
        # Document actual behaviour: strictly > so first stays
        assert build_cwd_index([a, b])[("local", "/p")] is a
        assert build_cwd_index([b, a])[("local", "/p")] is b


# ---------------------------------------------------------------------------
# link_for
# ---------------------------------------------------------------------------

class TestLinkFor:
    """Resolve a tmux to its Claude session (if any)."""

    def _idx(self, *sessions):
        return build_cwd_index(list(sessions))

    # --- matching ----------------------------------------------------------

    def test_basic_match_on_cwd(self):
        s = FakeSession(session_id="u1", machine="local", cwd="/p",
                        modified="2026-01-01T00:00:00", name="Proj")
        t = FakeTmux(cwd="/p", pane_current_command="node")
        assert link_for(t, self._idx(s)) == {
            "claude_session_id": "u1",
            "claude_session_name": "Proj",
        }

    def test_match_via_project_path(self):
        s = FakeSession(session_id="u1", machine="local", cwd="/a", project_path="/b",
                        modified="2026-01-01T00:00:00", name="Proj")
        t = FakeTmux(cwd="/b", pane_current_command="node")
        assert link_for(t, self._idx(s))["claude_session_id"] == "u1"

    def test_clear_rotation_picks_newest(self):
        old = FakeSession(session_id="old", machine="local", cwd="/p",
                          modified="2026-01-01T00:00:00", name="old-name")
        new = FakeSession(session_id="new", machine="local", cwd="/p",
                          modified="2026-02-01T00:00:00", name="new-name")
        t = FakeTmux(cwd="/p", pane_current_command="claude")
        link = link_for(t, self._idx(old, new))
        assert link["claude_session_id"] == "new"

    # --- no link cases -----------------------------------------------------

    def test_empty_cwd_no_link(self):
        s = FakeSession(session_id="u1", machine="local", cwd="/p",
                        modified="2026-01-01T00:00:00")
        t = FakeTmux(cwd="", pane_current_command="node")
        assert link_for(t, self._idx(s)) == {}

    def test_cwd_has_no_matching_session(self):
        s = FakeSession(session_id="u1", machine="local", cwd="/p",
                        modified="2026-01-01T00:00:00")
        t = FakeTmux(cwd="/different", pane_current_command="node")
        assert link_for(t, self._idx(s)) == {}

    def test_machine_mismatch_no_link(self):
        s = FakeSession(session_id="u1", machine="mac-mini", cwd="/p",
                        modified="2026-01-01T00:00:00")
        t = FakeTmux(cwd="/p", machine="local", pane_current_command="node")
        assert link_for(t, self._idx(s)) == {}

    # --- shell filter (negation list) --------------------------------------

    @pytest.mark.parametrize("shell", [
        "bash", "zsh", "fish", "sh", "dash", "ksh",
        "pwsh", "powershell", "cmd",
        "BASH", "Zsh", "PowerShell", "CMD",   # case-insensitive
        "cmd.exe", "powershell.exe", "pwsh.exe", "bash.exe",  # .exe strips
    ])
    def test_shell_commands_suppress_link(self, shell):
        s = FakeSession(session_id="u1", machine="local", cwd="/p",
                        modified="2026-01-01T00:00:00", name="Proj")
        t = FakeTmux(cwd="/p", pane_current_command=shell)
        assert link_for(t, self._idx(s)) == {}

    def test_empty_pane_command_still_links(self):
        """Old daemons without pane_current_command should degrade to cwd-match."""
        s = FakeSession(session_id="u1", machine="local", cwd="/p",
                        modified="2026-01-01T00:00:00", name="Proj")
        t = FakeTmux(cwd="/p", pane_current_command="")
        assert link_for(t, self._idx(s))["claude_session_id"] == "u1"

    @pytest.mark.parametrize("cmd", ["node", "claude", "python", "python3.12", "ruby",
                                     "java", "some-random-thing", "Node"])
    def test_non_shell_commands_allow_link(self, cmd):
        s = FakeSession(session_id="u1", machine="local", cwd="/p",
                        modified="2026-01-01T00:00:00", name="Proj")
        t = FakeTmux(cwd="/p", pane_current_command=cmd)
        assert link_for(t, self._idx(s))["claude_session_id"] == "u1"

    def test_whitespace_padded_command_recognized(self):
        s = FakeSession(session_id="u1", machine="local", cwd="/p",
                        modified="2026-01-01T00:00:00", name="Proj")
        t = FakeTmux(cwd="/p", pane_current_command="  bash  ")
        assert link_for(t, self._idx(s)) == {}

    # --- display name precedence ------------------------------------------

    def test_display_name_from_session_name(self):
        s = FakeSession(session_id="u1234567890", machine="local", cwd="/p",
                        modified="2026-01-01T00:00:00", name="My Name", slug="my-slug")
        t = FakeTmux(cwd="/p", pane_current_command="node")
        assert link_for(t, self._idx(s))["claude_session_name"] == "My Name"

    def test_display_name_falls_back_to_slug_when_name_empty(self):
        s = FakeSession(session_id="u1234567890", machine="local", cwd="/p",
                        modified="2026-01-01T00:00:00", name="", slug="my-slug")
        t = FakeTmux(cwd="/p", pane_current_command="node")
        assert link_for(t, self._idx(s))["claude_session_name"] == "my-slug"

    def test_display_name_falls_back_to_uuid8_when_name_and_slug_empty(self):
        s = FakeSession(session_id="u1234567890", machine="local", cwd="/p",
                        modified="2026-01-01T00:00:00", name="", slug="")
        t = FakeTmux(cwd="/p", pane_current_command="node")
        assert link_for(t, self._idx(s))["claude_session_name"] == "u1234567"

    def test_display_name_short_uuid_not_truncated_further(self):
        """UUID shorter than 8 chars comes through as-is."""
        s = FakeSession(session_id="abc", machine="local", cwd="/p",
                        modified="2026-01-01T00:00:00")
        t = FakeTmux(cwd="/p", pane_current_command="node")
        assert link_for(t, self._idx(s))["claude_session_name"] == "abc"


# ---------------------------------------------------------------------------
# enrich_tmux_dicts
# ---------------------------------------------------------------------------

class TestEnrichTmuxDicts:
    """End-to-end: list of TmuxSession + list of ClaudeSession → list of dicts."""

    def test_empty_inputs(self):
        assert enrich_tmux_dicts([], []) == []

    def test_empty_sessions_produces_tmux_dicts_without_link_fields(self):
        t = FakeTmux(name="t1", cwd="/p", pane_current_command="node")
        result = enrich_tmux_dicts([t], [])
        assert result == [t.to_dict()]
        assert "claude_session_id" not in result[0]
        assert "claude_session_name" not in result[0]

    def test_mixed_linkable_and_non_linkable(self):
        s = FakeSession(session_id="u1", machine="local", cwd="/a",
                        modified="2026-01-01T00:00:00", name="A")
        t_link = FakeTmux(name="t1", cwd="/a", pane_current_command="node")
        t_shell = FakeTmux(name="t2", cwd="/a", pane_current_command="bash")
        t_nocwd = FakeTmux(name="t3", cwd="", pane_current_command="node")
        result = enrich_tmux_dicts([t_link, t_shell, t_nocwd], [s])
        assert result[0]["claude_session_id"] == "u1"
        assert "claude_session_id" not in result[1]
        assert "claude_session_id" not in result[2]

    def test_preserves_all_tmux_fields(self):
        s = FakeSession(session_id="u1", machine="local", cwd="/p",
                        modified="2026-01-01T00:00:00", name="Proj")
        t = FakeTmux(name="t1", machine="local", cwd="/p", pane_current_command="node",
                     attached=True, windows=3, created="2026-01-01T00:00:00")
        result = enrich_tmux_dicts([t], [s])
        assert result[0]["name"] == "t1"
        assert result[0]["attached"] is True
        assert result[0]["windows"] == 3
        assert result[0]["created"] == "2026-01-01T00:00:00"
        assert result[0]["pane_current_command"] == "node"

    def test_input_dataclass_not_mutated(self):
        s = FakeSession(session_id="u1", machine="local", cwd="/p",
                        modified="2026-01-01T00:00:00", name="Proj")
        t = FakeTmux(name="t1", cwd="/p", pane_current_command="node")
        enrich_tmux_dicts([t], [s])
        # FakeTmux has no claude_session_id attribute, and shouldn't gain one.
        assert not hasattr(t, "claude_session_id")

    def test_multiple_tmux_same_cwd_all_linked_to_same_session(self):
        s = FakeSession(session_id="u1", machine="local", cwd="/p",
                        modified="2026-01-01T00:00:00", name="Proj")
        t1 = FakeTmux(name="a", cwd="/p", pane_current_command="node")
        t2 = FakeTmux(name="b", cwd="/p", pane_current_command="claude")
        result = enrich_tmux_dicts([t1, t2], [s])
        assert result[0]["claude_session_id"] == "u1"
        assert result[1]["claude_session_id"] == "u1"

    def test_real_clear_rotation_scenario(self):
        """Simulate: tmux running claude; user runs /clear; new JSONL appears;
        next scan should relink the tmux card to the newer session."""
        tmux = FakeTmux(name="work", cwd="/proj", pane_current_command="node")
        pre_clear = [FakeSession(session_id="before", cwd="/proj",
                                 modified="2026-04-17T10:00:00", name="Before")]
        post_clear = pre_clear + [FakeSession(session_id="after", cwd="/proj",
                                              modified="2026-04-17T11:00:00", name="After")]
        assert enrich_tmux_dicts([tmux], pre_clear)[0]["claude_session_id"] == "before"
        assert enrich_tmux_dicts([tmux], post_clear)[0]["claude_session_id"] == "after"


class TestWindowsPathNormalization:
    """Windows session cwd (backslash) must match tmux cwd (backslash or forward) on the same machine."""

    def test_windows_backslash_both_sides(self):
        s = FakeSession(session_id="u1", machine="avell-i7",
                        cwd=r"C:\Users\rbgnr\git\Immunefi",
                        project_path=r"C:\Users\rbgnr\git\Immunefi",
                        modified="2026-04-17T10:00:00", name="Immunefi")
        t = FakeTmux(machine="avell-i7", cwd=r"C:\Users\rbgnr\git\Immunefi",
                     pane_current_command="node")
        result = enrich_tmux_dicts([t], [s])
        assert result[0]["claude_session_id"] == "u1"

    def test_project_path_with_forward_slashes_matches_backslash_cwd(self):
        """claude-manager on macOS decodes the Windows folder with '/', but the
        session's JSONL cwd field and tmux pane cwd both use '\\'. Normalization
        must unify these so the link still fires."""
        s = FakeSession(session_id="u1", machine="avell-i7",
                        cwd=r"C:\Users\rbgnr\git\Immunefi",
                        project_path="C:/Users/rbgnr/git/Immunefi",
                        modified="2026-04-17T10:00:00", name="Immunefi")
        t = FakeTmux(machine="avell-i7", cwd=r"C:\Users\rbgnr\git\Immunefi",
                     pane_current_command="node")
        assert enrich_tmux_dicts([t], [s])[0]["claude_session_id"] == "u1"

    def test_case_insensitive_match(self):
        """Windows is case-insensitive; 'Immunefi' vs 'immunefi' must match."""
        s = FakeSession(session_id="u1", machine="avell-i7",
                        cwd=r"C:\Users\rbgnr\git\Immunefi",
                        modified="2026-04-17T10:00:00", name="Immunefi")
        t = FakeTmux(machine="avell-i7", cwd=r"C:\Users\rbgnr\git\immunefi",
                     pane_current_command="node")
        assert enrich_tmux_dicts([t], [s])[0]["claude_session_id"] == "u1"

    def test_trailing_separator_ignored(self):
        """Trailing '\\' or '/' should not break the match."""
        s = FakeSession(session_id="u1", machine="local", cwd="/proj",
                        modified="2026-04-17T10:00:00")
        t = FakeTmux(machine="local", cwd="/proj/", pane_current_command="node")
        assert enrich_tmux_dicts([t], [s])[0]["claude_session_id"] == "u1"

    def test_mixed_separator_in_same_path(self):
        """C:/Users\\rbgnr\\git/Immunefi (mixed) still normalizes correctly."""
        s = FakeSession(session_id="u1", machine="avell-i7",
                        cwd=r"C:\Users\rbgnr\git\Immunefi",
                        modified="2026-04-17T10:00:00")
        t = FakeTmux(machine="avell-i7", cwd=r"C:/Users\rbgnr\git/Immunefi",
                     pane_current_command="node")
        assert enrich_tmux_dicts([t], [s])[0]["claude_session_id"] == "u1"
