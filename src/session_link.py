"""
Link tmux/psmux sessions to their running Claude Code session.

A tmux pane is linked to a Claude session when:
  - pane cwd equals the session's cwd (or project_path) on the SAME machine
  - pane is not running a known shell (bash/zsh/fish/sh/dash/ksh/pwsh/
    powershell/cmd) — prevents shell-only panes from getting mislabeled.

When multiple Claude sessions share the same (machine, cwd) — which happens
normally as a user runs `/clear` or restarts the REPL — the one with the
newest `modified` timestamp wins. No persistence needed: the derivation is
re-run on every scan, so rotation events resolve themselves on the next tick.

Empty `pane_current_command` (older remote daemons that don't report it)
degrades gracefully to cwd-only matching rather than suppressing all links.
"""
from __future__ import annotations

from typing import Any, Iterable


# Process names that indicate the pane is at a shell prompt, not running
# Claude. Compared case-insensitively, with a trailing `.exe` stripped on
# Windows. Keep this list tight: false negatives (missed suppression) just
# produce an off-by-one link chip, which is recoverable; false positives
# (a real workload in a shell called "bash") would hide the chip entirely.
_SHELL_COMMANDS = frozenset({
    "bash", "zsh", "fish", "sh", "dash", "ksh",
    "pwsh", "powershell", "cmd",
})


def _is_shell(command: str) -> bool:
    cmd = (command or "").strip().lower()
    if not cmd:
        return False
    if cmd.endswith(".exe"):
        cmd = cmd[:-4]
    return cmd in _SHELL_COMMANDS


def _norm_path(p: str) -> str:
    """Normalize separators, case, and trailing slashes so Windows paths match.

    claude-manager typically runs on macOS and decodes Windows project folders
    with '/', while session JSONLs and tmux pane cwds carry '\\'. Case can also
    differ ('Immunefi' vs 'immunefi'). Normalize all three so the index lookup
    doesn't miss.
    """
    if not p:
        return ""
    return p.replace("\\", "/").rstrip("/").lower()


def _display_name(session: Any) -> str:
    """Pick the best human label for a session: name > slug > uuid[:8]."""
    name = getattr(session, "name", "") or ""
    if name:
        return name
    slug = getattr(session, "slug", "") or ""
    if slug:
        return slug
    sid = getattr(session, "session_id", "") or ""
    return sid[:8]


def build_cwd_index(sessions: Iterable[Any]) -> dict[tuple[str, str], Any]:
    """Map (machine, normalized-path) → most recent ClaudeSession.

    Indexes each session under its normalized `cwd` AND `project_path` when
    they differ, so a tmux can match whichever path form it carries.
    Normalization collapses Windows/Unix separator + case differences.
    Ties on `modified` are broken by iteration order (first-wins).
    """
    index: dict[tuple[str, str], Any] = {}
    for s in sessions:
        machine = getattr(s, "machine", "") or ""
        paths = {_norm_path(p) for p in (getattr(s, "cwd", ""), getattr(s, "project_path", ""))}
        paths.discard("")
        for path in paths:
            key = (machine, path)
            existing = index.get(key)
            if existing is None:
                index[key] = s
                continue
            if (getattr(s, "modified", "") or "") > (getattr(existing, "modified", "") or ""):
                index[key] = s
    return index


def link_for(tmux: Any, index: dict[tuple[str, str], Any]) -> dict[str, str]:
    """Return {claude_session_id, claude_session_name} or {} if no link."""
    cwd = _norm_path(getattr(tmux, "cwd", ""))
    if not cwd:
        return {}
    if _is_shell(getattr(tmux, "pane_current_command", "")):
        return {}
    machine = getattr(tmux, "machine", "") or ""
    match = index.get((machine, cwd))
    if match is None:
        return {}
    return {
        "claude_session_id": getattr(match, "session_id", "") or "",
        "claude_session_name": _display_name(match),
    }


def enrich_tmux_dicts(tmux_list: Iterable[Any], sessions: Iterable[Any]) -> list[dict]:
    """Serialize each tmux.to_dict() with the optional link fields merged in."""
    index = build_cwd_index(sessions)
    enriched: list[dict] = []
    for t in tmux_list:
        d = t.to_dict()
        d.update(link_for(t, index))
        enriched.append(d)
    return enriched
