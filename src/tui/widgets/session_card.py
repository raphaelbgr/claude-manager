"""Session card widget for the claude-manager TUI."""
from __future__ import annotations

from datetime import datetime, timezone, timedelta

from textual.widgets import Static

from ...scanner import ClaudeSession


def _relative_time(iso_str: str) -> str:
    """Return a human-friendly relative time string like '2h ago'."""
    if not iso_str:
        return "unknown"
    try:
        dt = datetime.fromisoformat(iso_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        delta = datetime.now(tz=timezone.utc) - dt
        seconds = int(delta.total_seconds())
        if seconds < 60:
            return f"{seconds}s ago"
        elif seconds < 3600:
            return f"{seconds // 60}m ago"
        elif seconds < 86400:
            return f"{seconds // 3600}h ago"
        else:
            return f"{seconds // 86400}d ago"
    except (ValueError, TypeError):
        return "unknown"


def format_session_row(session: ClaudeSession) -> tuple[str, str, str, str, str, str]:
    """
    Return a 6-tuple of Rich-markup strings for use in a DataTable row.

    Columns: status_icon, machine, project, summary, messages, modified
    """
    if session.status == "active":
        status = "[bold green]● active[/]"
    else:
        status = "[dim]○ idle[/]"

    machine = f"[cyan]{session.machine}[/]"

    # Show last segment of path for brevity, full path in tooltip-style
    path = session.project_path or session.cwd or session.project_folder
    parts = path.replace("\\", "/").split("/")
    short_path = "/".join(parts[-2:]) if len(parts) >= 2 else path
    project = f"[white]{short_path}[/]"

    summary = session.summary or "[dim italic]<no summary>[/]"
    if len(summary) > 80:
        summary = summary[:77] + "..."

    messages = f"[yellow]{session.messages}[/]"
    modified = f"[dim]{_relative_time(session.modified)}[/]"

    return status, machine, project, summary, messages, modified


class SessionCard(Static):
    """
    A compact Rich-formatted block displaying one Claude session.

    Used in the detail panel or wherever a card view is needed.
    """

    DEFAULT_CSS = """
    SessionCard {
        height: auto;
        padding: 0 1;
        border-bottom: dashed $primary-background;
    }
    SessionCard:hover {
        background: $primary-background-lighten-1;
    }
    """

    def __init__(self, session: ClaudeSession, **kwargs):
        self._session = session
        super().__init__(self._render_markup(), **kwargs)

    def _render_markup(self) -> str:
        s = self._session
        dot = "[bold green]●[/]" if s.status == "active" else "[dim]○[/]"
        machine = f"[cyan bold]{s.machine}[/]"
        path = s.project_path or s.cwd or s.project_folder
        project = f"[white]{path}[/]"

        summary = s.summary or "[dim italic]<no summary>[/]"
        if len(summary) > 100:
            summary = summary[:97] + "..."

        time_str = _relative_time(s.modified)
        msg_str = f"{s.messages} msg"

        line1 = f"{dot} {machine}  {project}"
        line2 = f'  [dim italic]"{summary}"[/]  [dim]({msg_str}, {time_str})[/]'
        return f"{line1}\n{line2}"

    def update_session(self, session: ClaudeSession) -> None:
        """Refresh the card with new session data."""
        self._session = session
        self.update(self._render_markup())
