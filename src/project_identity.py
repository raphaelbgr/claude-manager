"""
Project identity helpers for claude-manager.

Maps ClaudeSession objects to canonical project identifiers so that sessions
from different machines that belong to the same git repository are grouped
together in the Projects view.

Stdlib-only — no third-party deps.
"""
from __future__ import annotations

import re
from pathlib import Path


# ---------------------------------------------------------------------------
# URL normalisation
# ---------------------------------------------------------------------------

# Patterns for the two common git remote URL formats:
#   SSH:   git@github.com:owner/repo.git
#   HTTPS: https://github.com/owner/repo.git
_SSH_RE = re.compile(
    r"^git@(?P<host>[^:]+):(?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?$"
)
_HTTPS_RE = re.compile(
    r"^https?://(?P<host>[^/]+)/(?P<owner>[^/]+)/(?P<repo>[^/?#]+?)(?:\.git)?(?:[/?#].*)?$"
)

_KNOWN_HOSTS = {"github.com", "gitlab.com", "bitbucket.org"}


def normalize_remote(url: str) -> str:
    """Normalize a git remote URL to a canonical project id.

    - Strips trailing .git
    - Lowercases
    - Extracts 'host/owner/repo' from SSH or HTTPS URLs for known hosts
    - Fallback: lowercased last two non-empty path segments joined with '/'

    Returns '' if url is empty.
    """
    if not url:
        return ""

    url = url.strip()

    # SSH format: git@github.com:owner/repo.git
    m = _SSH_RE.match(url)
    if m:
        host = m.group("host").lower()
        owner = m.group("owner").lower()
        repo = m.group("repo").lower()
        return f"{host}/{owner}/{repo}"

    # HTTPS format: https://github.com/owner/repo.git
    m = _HTTPS_RE.match(url)
    if m:
        host = m.group("host").lower()
        owner = m.group("owner").lower()
        repo = m.group("repo").lower()
        return f"{host}/{owner}/{repo}"

    # Generic fallback: take last two non-empty path segments
    # Works for self-hosted instances, non-standard URLs, etc.
    # Normalise separators first
    cleaned = url.replace("\\", "/").rstrip("/")
    # Strip scheme
    cleaned = re.sub(r"^[a-zA-Z][a-zA-Z0-9+\-.]*://", "", cleaned)
    # Strip auth (user:pass@host or user@host)
    cleaned = re.sub(r"^[^@]+@", "", cleaned)
    # Strip .git suffix
    if cleaned.endswith(".git"):
        cleaned = cleaned[:-4]
    parts = [p for p in cleaned.replace(":", "/").split("/") if p]
    if len(parts) >= 2:
        return (parts[-2] + "/" + parts[-1]).lower()
    if parts:
        return parts[-1].lower()
    return ""


# ---------------------------------------------------------------------------
# Session → project id
# ---------------------------------------------------------------------------

def project_id(session) -> str:
    """Compute the project identity for a ClaudeSession.

    - If git_remote is set: return normalize_remote(git_remote).
    - Else: return the basename of cwd or project_path (lowercased).
    - Never returns ''.
    """
    remote = getattr(session, "git_remote", "") or ""
    if remote:
        pid = normalize_remote(remote)
        if pid:
            return pid

    # Fallback: directory basename
    path = getattr(session, "cwd", "") or getattr(session, "project_path", "") or ""
    if path:
        # Normalise separators
        basename = path.replace("\\", "/").rstrip("/").split("/")[-1]
        if basename:
            return basename.lower()

    # Last resort: project_folder
    folder = getattr(session, "project_folder", "") or ""
    if folder:
        return folder.lower()

    return "unknown"


# ---------------------------------------------------------------------------
# Display name
# ---------------------------------------------------------------------------

def project_display_name(pid: str) -> str:
    """Human-friendly display name for a project id.

    For 'host/owner/repo' style ids: returns 'repo' (last segment).
    For bare names: returns the name as-is.
    """
    if not pid:
        return "unknown"
    parts = pid.split("/")
    return parts[-1] if parts else pid
