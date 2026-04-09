"""Tmux card widget for the claude-manager TUI."""
from __future__ import annotations

from datetime import datetime, timezone

from textual.widgets import Static

from ...tmux_manager import TmuxSession


def _relative_time(iso_str: str) -> str:
    """Return a human-friendly relative time string like '2h ago'."""
    if not iso_str:
        return ""
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
        return ""


def format_tmux_row(ts: TmuxSession) -> tuple[str, str, str, str, str]:
    """
    Return a 5-tuple of Rich-markup strings for a DataTable row.

    Columns: machine, name, windows, attached, created
    """
    machine = f"[cyan]{ts.machine}[/]"
    name = f"[white bold]{ts.name}[/]"
    windows = f"[yellow]{ts.windows}[/]"

    if ts.attached:
        attached = "[bold green]● attached[/]"
    else:
        attached = "[dim]○ detached[/]"

    created = f"[dim]{_relative_time(ts.created)}[/]" if ts.created else "[dim]—[/]"

    return machine, name, windows, attached, created


class TmuxCard(Static):
    """
    A compact Rich-formatted block displaying one tmux session.
    """

    DEFAULT_CSS = """
    TmuxCard {
        height: auto;
        padding: 0 1;
        border-bottom: dashed $primary-background;
    }
    TmuxCard:hover {
        background: $primary-background-lighten-1;
    }
    """

    def __init__(self, ts: TmuxSession, **kwargs):
        self._ts = ts
        super().__init__(self._render_markup(), **kwargs)

    def _render_markup(self) -> str:
        ts = self._ts
        diamond = "[bold magenta]◆[/]"
        machine = f"[cyan bold]{ts.machine}[/]"
        name = f"[white bold]{ts.name}[/]"
        windows_str = f"{ts.windows} window{'s' if ts.windows != 1 else ''}"

        attached_str = (
            "[bold green]attached[/]" if ts.attached else "[dim]detached[/]"
        )

        parts = [windows_str, attached_str]
        if ts.created:
            parts.append(_relative_time(ts.created))

        detail = ", ".join(str(p) for p in parts if p)
        return f"{diamond} {machine}  {name}  [dim]({detail})[/]"

    def update_session(self, ts: TmuxSession) -> None:
        """Refresh the card with new data."""
        self._ts = ts
        self.update(self._render_markup())
