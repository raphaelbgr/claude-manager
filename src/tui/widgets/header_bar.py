"""Status bar widget for the claude-manager TUI."""
from __future__ import annotations

from datetime import datetime, timezone

from textual.widgets import Static
from textual.reactive import reactive


class StatusBar(Static):
    """
    Bottom status bar displaying fleet / session / tmux counts and last scan time.

    Example: Fleet: 3/4 online | Sessions: 42 | Tmux: 8 | Last scan: 5s ago
    """

    DEFAULT_CSS = """
    StatusBar {
        dock: bottom;
        height: 1;
        background: $primary-background;
        color: $text-muted;
        padding: 0 1;
        content-align: left middle;
    }
    """

    fleet_online: reactive[int] = reactive(0)
    fleet_total: reactive[int] = reactive(0)
    session_count: reactive[int] = reactive(0)
    tmux_count: reactive[int] = reactive(0)
    last_scan: reactive[datetime | None] = reactive(None)

    def on_mount(self) -> None:
        self.set_interval(1, self.refresh_display)

    def refresh_display(self) -> None:
        self.update(self._render())

    def _render(self) -> str:
        fleet_str = f"[cyan]Fleet:[/] {self.fleet_online}/{self.fleet_total} online"
        sessions_str = f"[cyan]Sessions:[/] [yellow]{self.session_count}[/]"
        tmux_str = f"[cyan]Tmux:[/] [yellow]{self.tmux_count}[/]"

        if self.last_scan is None:
            scan_str = "[cyan]Last scan:[/] [dim]scanning…[/]"
        else:
            delta = datetime.now().astimezone() - self.last_scan
            secs = int(delta.total_seconds())
            if secs < 60:
                age = f"{secs}s ago"
            elif secs < 3600:
                age = f"{secs // 60}m ago"
            else:
                age = f"{secs // 3600}h ago"
            scan_str = f"[cyan]Last scan:[/] [dim]{age}[/]"

        return f"  {fleet_str}  [dim]|[/]  {sessions_str}  [dim]|[/]  {tmux_str}  [dim]|[/]  {scan_str}"

    def update_stats(
        self,
        fleet_online: int,
        fleet_total: int,
        session_count: int,
        tmux_count: int,
    ) -> None:
        """Update all counters and record a new scan timestamp."""
        self.fleet_online = fleet_online
        self.fleet_total = fleet_total
        self.session_count = session_count
        self.tmux_count = tmux_count
        self.last_scan = datetime.now().astimezone()
        self.update(self._render())
