"""claude-manager TUI application."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.widgets import (
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    LoadingIndicator,
    Static,
    TabbedContent,
    TabPane,
)
from textual.worker import Worker, WorkerState

from ..config import FLEET_MACHINES, detect_local_machine
from ..fleet import discover_fleet
from ..launcher import launch_claude_session, launch_new_tmux_and_attach, launch_tmux_attach
from ..scanner import ClaudeSession, scan_all, scan_local
from ..tmux_manager import TmuxSession, list_all_tmux, list_local_tmux
from .screens.new_tmux import NewTmuxScreen
from .widgets.header_bar import StatusBar
from .widgets.session_card import format_session_row
from .widgets.tmux_card import format_tmux_row

# ── CSS path ──────────────────────────────────────────────────────────────────
_CSS_PATH = Path(__file__).parent / "styles" / "app.tcss"


class ClaudeManagerApp(App):
    """claude-manager — manage Claude Code sessions and tmux across your fleet."""

    TITLE = "claude-manager"
    CSS_PATH = str(_CSS_PATH)
    DARK = True

    BINDINGS = [
        Binding("r", "rescan", "Rescan", show=True),
        Binding("n", "new_tmux", "New tmux", show=True),
        Binding("/", "filter", "Filter", show=True),
        Binding("escape", "clear_filter", "Clear filter", show=False),
        Binding("q", "quit", "Quit", show=True),
    ]

    # ── Internal state ────────────────────────────────────────────────────────
    _sessions: list[ClaudeSession] = []
    _tmux_sessions: list[TmuxSession] = []
    _fleet_status: dict[str, Any] = {}
    _filter_text: str = ""
    _loading: bool = True

    # ── Compose ───────────────────────────────────────────────────────────────
    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)

        with TabbedContent(id="tabs"):
            with TabPane("Sessions", id="tab-sessions"):
                yield LoadingIndicator(id="sessions-loading")
                yield DataTable(id="sessions-table", show_cursor=True, zebra_stripes=True)
                with Vertical(id="sessions-filter-bar"):
                    yield Label("Filter: ", id="sessions-filter-label")
                    yield Input(placeholder="type to filter…", id="sessions-filter-input")

            with TabPane("Tmux", id="tab-tmux"):
                yield LoadingIndicator(id="tmux-loading")
                yield DataTable(id="tmux-table", show_cursor=True, zebra_stripes=True)
                with Vertical(id="tmux-filter-bar"):
                    yield Label("Filter: ", id="tmux-filter-label")
                    yield Input(placeholder="type to filter…", id="tmux-filter-input")

            with TabPane("Fleet", id="tab-fleet"):
                yield LoadingIndicator(id="fleet-loading")
                yield DataTable(id="fleet-table", show_cursor=True, zebra_stripes=True)

        yield StatusBar(id="status-bar")
        yield Footer()

    # ── Lifecycle ─────────────────────────────────────────────────────────────
    def on_mount(self) -> None:
        self._setup_tables()
        self._hide_tables()
        self.run_worker(self._initial_scan(), exclusive=True, name="initial_scan")
        self.set_interval(30, self._background_rescan)

    def _setup_tables(self) -> None:
        sessions_table = self.query_one("#sessions-table", DataTable)
        sessions_table.add_columns(
            "Status", "Machine", "Project", "Summary", "Msgs", "Modified"
        )
        sessions_table.cursor_type = "row"

        tmux_table = self.query_one("#tmux-table", DataTable)
        tmux_table.add_columns(
            "Machine", "Name", "Windows", "Attached", "Created"
        )
        tmux_table.cursor_type = "row"

        fleet_table = self.query_one("#fleet-table", DataTable)
        fleet_table.add_columns(
            "Machine", "Status", "OS", "IP", "Method", "Dispatch"
        )
        fleet_table.cursor_type = "row"

        # Hide filter bars initially
        for bar_id in ("sessions-filter-bar", "tmux-filter-bar"):
            self.query_one(f"#{bar_id}").styles.display = "none"

    def _hide_tables(self) -> None:
        for tid in ("sessions-table", "tmux-table", "fleet-table"):
            self.query_one(f"#{tid}").styles.display = "none"

    def _show_tables(self) -> None:
        for tid in ("sessions-table", "tmux-table", "fleet-table"):
            self.query_one(f"#{tid}").styles.display = "block"
        for lid in ("sessions-loading", "tmux-loading", "fleet-loading"):
            self.query_one(f"#{lid}").styles.display = "none"

    # ── Workers ───────────────────────────────────────────────────────────────
    async def _initial_scan(self) -> None:
        """Run fleet discovery and all scans, then populate tables."""
        local_machine = detect_local_machine()

        # Discover fleet first
        try:
            fleet = await discover_fleet()
        except Exception:
            fleet = {}

        self._fleet_status = fleet

        # Sessions and tmux in parallel
        try:
            sessions_coro = scan_all(local_machine=local_machine, fleet=fleet)
            tmux_coro = list_all_tmux(
                local_machine=local_machine or "local",
                fleet_status=fleet,
            )
            sessions, tmux_sessions = await asyncio.gather(
                sessions_coro, tmux_coro, return_exceptions=True
            )
        except Exception:
            sessions = []
            tmux_sessions = []

        if isinstance(sessions, Exception):
            sessions = []
        if isinstance(tmux_sessions, Exception):
            tmux_sessions = []

        self._sessions = sessions  # type: ignore[assignment]
        self._tmux_sessions = tmux_sessions  # type: ignore[assignment]

        self.call_from_thread(self._populate_all)

    async def _background_rescan(self) -> None:
        """Periodic background rescan (no visual loading indicator)."""
        self.run_worker(self._do_rescan(), exclusive=False, name="bg_rescan")

    async def _do_rescan(self) -> None:
        """Perform a full rescan without showing loading indicators."""
        local_machine = detect_local_machine()
        try:
            fleet = await discover_fleet()
        except Exception:
            fleet = self._fleet_status

        self._fleet_status = fleet

        try:
            sessions_coro = scan_all(local_machine=local_machine, fleet=fleet)
            tmux_coro = list_all_tmux(
                local_machine=local_machine or "local",
                fleet_status=fleet,
            )
            sessions, tmux_sessions = await asyncio.gather(
                sessions_coro, tmux_coro, return_exceptions=True
            )
        except Exception:
            return

        if isinstance(sessions, Exception) or isinstance(tmux_sessions, Exception):
            return

        self._sessions = sessions  # type: ignore[assignment]
        self._tmux_sessions = tmux_sessions  # type: ignore[assignment]
        self.call_from_thread(self._populate_all)

    # ── Table population ──────────────────────────────────────────────────────
    def _populate_all(self) -> None:
        self._populate_sessions()
        self._populate_tmux()
        self._populate_fleet()
        self._show_tables()
        self._update_tab_labels()
        self._update_status_bar()

    def _populate_sessions(self, filter_text: str = "") -> None:
        table = self.query_one("#sessions-table", DataTable)
        table.clear()

        sessions = self._sessions
        if filter_text:
            q = filter_text.lower()
            sessions = [
                s for s in sessions
                if q in s.machine.lower()
                or q in s.project_path.lower()
                or q in (s.summary or "").lower()
                or q in s.status.lower()
            ]

        for sess in sessions:
            row = format_session_row(sess)
            table.add_row(*row, key=sess.session_id)

    def _populate_tmux(self, filter_text: str = "") -> None:
        table = self.query_one("#tmux-table", DataTable)
        table.clear()

        sessions = self._tmux_sessions
        if filter_text:
            q = filter_text.lower()
            sessions = [
                t for t in sessions
                if q in t.machine.lower() or q in t.name.lower()
            ]

        for ts in sessions:
            row = format_tmux_row(ts)
            table.add_row(*row, key=f"{ts.machine}:{ts.name}")

    def _populate_fleet(self) -> None:
        table = self.query_one("#fleet-table", DataTable)
        table.clear()

        for name, info in FLEET_MACHINES.items():
            health = self._fleet_status.get(name, {})
            online = health.get("online", False)

            status = "[bold green]● Online[/]" if online else "[bold red]✗ Offline[/]"
            os_str = info.get("os", "?")
            os_icon = {"darwin": "macOS", "linux": "Linux", "win32": "Windows"}.get(os_str, os_str)
            ip = info.get("ip", "—")
            method = health.get("method", "—")
            dispatch = "[green]✓[/]" if info.get("dispatch_port") else "[dim]—[/]"

            table.add_row(
                f"[cyan]{name}[/]",
                status,
                os_icon,
                f"[dim]{ip}[/]",
                f"[dim]{method}[/]",
                dispatch,
                key=name,
            )

    def _update_tab_labels(self) -> None:
        tabs = self.query_one("#tabs", TabbedContent)
        n_sess = len(self._sessions)
        n_tmux = len(self._tmux_sessions)
        n_fleet = len(FLEET_MACHINES)
        n_online = sum(1 for h in self._fleet_status.values() if h.get("online"))

        # Textual 0.x sets tab labels via the TabPane label attribute
        # The tab IDs in TabbedContent are the pane IDs
        try:
            tabs.get_tab("tab-sessions").label = f"Sessions ({n_sess})"  # type: ignore[attr-defined]
            tabs.get_tab("tab-tmux").label = f"Tmux ({n_tmux})"  # type: ignore[attr-defined]
            tabs.get_tab("tab-fleet").label = f"Fleet ({n_online}/{n_fleet})"  # type: ignore[attr-defined]
        except Exception:
            pass

    def _update_status_bar(self) -> None:
        bar = self.query_one("#status-bar", StatusBar)
        n_online = sum(1 for h in self._fleet_status.values() if h.get("online"))
        bar.update_stats(
            fleet_online=n_online,
            fleet_total=len(FLEET_MACHINES),
            session_count=len(self._sessions),
            tmux_count=len(self._tmux_sessions),
        )

    # ── Key bindings / actions ────────────────────────────────────────────────
    def action_rescan(self) -> None:
        """Trigger a manual rescan with notification."""
        self.notify("Rescanning…", title="claude-manager", timeout=2)
        self.run_worker(self._do_rescan(), exclusive=False, name="manual_rescan")

    def action_new_tmux(self) -> None:
        """Open the new tmux session modal."""
        tabs = self.query_one("#tabs", TabbedContent)
        if tabs.active != "tab-tmux":
            return
        self.push_screen(NewTmuxScreen(), self._on_new_tmux_result)

    def _on_new_tmux_result(self, result: dict | None) -> None:
        if result is None:
            return
        self.run_worker(
            self._create_tmux(result["machine"], result["name"], result.get("cwd")),
            exclusive=False,
            name="create_tmux",
        )

    async def _create_tmux(self, machine: str, name: str, cwd: str | None) -> None:
        outcome = await launch_new_tmux_and_attach(name, machine, cwd=cwd)
        if outcome.get("ok"):
            self.notify(
                f"Launched '{name}' on {machine}",
                title="Tmux created",
                timeout=3,
            )
            await self._do_rescan()
        else:
            err = outcome.get("error", "unknown error")
            self.notify(f"{err}", title="Error creating session", severity="error", timeout=5)

    def action_filter(self) -> None:
        """Show the filter bar for the active tab."""
        tabs = self.query_one("#tabs", TabbedContent)
        active = tabs.active

        if active == "tab-sessions":
            bar = self.query_one("#sessions-filter-bar")
            inp = self.query_one("#sessions-filter-input", Input)
        elif active == "tab-tmux":
            bar = self.query_one("#tmux-filter-bar")
            inp = self.query_one("#tmux-filter-input", Input)
        else:
            return

        bar.styles.display = "block"
        inp.focus()

    def action_clear_filter(self) -> None:
        """Hide filter bars and clear filters."""
        for bar_id, inp_id, populate in (
            ("sessions-filter-bar", "sessions-filter-input", self._populate_sessions),
            ("tmux-filter-bar", "tmux-filter-input", self._populate_tmux),
        ):
            bar = self.query_one(f"#{bar_id}")
            inp = self.query_one(f"#{inp_id}", Input)
            if bar.styles.display != "none":
                bar.styles.display = "none"
                inp.clear()
                populate()

    # ── Filter input events ───────────────────────────────────────────────────
    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "sessions-filter-input":
            self._populate_sessions(event.value)
        elif event.input.id == "tmux-filter-input":
            self._populate_tmux(event.value)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Close filter bar on Enter."""
        if event.input.id in ("sessions-filter-input", "tmux-filter-input"):
            self.action_clear_filter()

    # ── DataTable row select (Enter key) ─────────────────────────────────────
    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        table = event.data_table
        key = event.row_key.value  # type: ignore[union-attr]

        if table.id == "sessions-table":
            session = next((s for s in self._sessions if s.session_id == key), None)
            if session:
                self.run_worker(
                    self._launch_session(session),
                    exclusive=False,
                    name="launch_session",
                )
        elif table.id == "tmux-table":
            # key is "machine:name"
            if key and ":" in key:
                machine, name = key.split(":", 1)
                self.run_worker(
                    self._attach_tmux(machine, name),
                    exclusive=False,
                    name="attach_tmux",
                )

    async def _launch_session(self, session: ClaudeSession) -> None:
        self.notify(
            f"Launching {session.project_path} on {session.machine}…",
            title="Launching Claude",
            timeout=3,
        )
        result = await launch_claude_session(
            cwd=session.cwd or session.project_path,
            session_id=session.session_id,
            machine=session.machine,
        )
        if not result.get("ok"):
            err = result.get("error", "unknown error")
            self.notify(err, title="Launch failed", severity="error", timeout=5)

    async def _attach_tmux(self, machine: str, name: str) -> None:
        self.notify(
            f"Attaching to '{name}' on {machine}…",
            title="Tmux attach",
            timeout=3,
        )
        result = await launch_tmux_attach(name, machine)
        if not result.get("ok"):
            err = result.get("error", "unknown error")
            self.notify(err, title="Attach failed", severity="error", timeout=5)
