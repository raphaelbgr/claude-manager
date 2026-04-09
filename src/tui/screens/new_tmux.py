"""Modal screen for creating a new tmux session."""
from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, Horizontal
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Static

from ...config import FLEET_MACHINES, detect_local_machine


class NewTmuxScreen(ModalScreen[dict | None]):
    """
    Modal dialog to create a new tmux session.

    Returns a dict {"machine": ..., "name": ..., "cwd": ...} on confirm,
    or None if cancelled.
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
    ]

    CSS = """
    NewTmuxScreen {
        align: center middle;
    }

    #dialog {
        background: $panel;
        border: thick $accent;
        padding: 1 2;
        width: 62;
        height: auto;
    }

    #dialog-title {
        color: $accent;
        text-style: bold;
        text-align: center;
        padding-bottom: 1;
        width: 100%;
    }

    #dialog .field-label {
        color: $text-muted;
        padding-bottom: 0;
        height: 1;
    }

    #dialog Input {
        background: $surface;
        border: tall $primary;
        color: $text;
        margin-bottom: 1;
        width: 100%;
    }

    #dialog Input:focus {
        border: tall $accent;
    }

    #dialog-buttons {
        layout: horizontal;
        align: center middle;
        height: auto;
        margin-top: 1;
        width: 100%;
    }

    #btn-create {
        background: $accent;
        color: $background;
        text-style: bold;
        margin-right: 1;
        min-width: 14;
    }

    #btn-create:hover {
        background: $accent-lighten-1;
    }

    #btn-cancel {
        background: $panel-darken-1;
        color: $text-muted;
        min-width: 14;
    }

    #btn-cancel:hover {
        background: $panel-lighten-1;
        color: $text;
    }

    #error-label {
        color: $error;
        height: 1;
        padding-bottom: 0;
    }
    """

    def __init__(self, default_machine: str | None = None, **kwargs):
        super().__init__(**kwargs)
        self._default_machine = default_machine or detect_local_machine() or "mac-mini"

    def compose(self) -> ComposeResult:
        machines = list(FLEET_MACHINES.keys())
        machine_hint = " | ".join(machines)

        with Vertical(id="dialog"):
            yield Static("◆  New Tmux Session", id="dialog-title")

            yield Label("Machine", classes="field-label")
            yield Input(
                value=self._default_machine,
                placeholder=machine_hint,
                id="input-machine",
            )

            yield Label("Session name", classes="field-label")
            yield Input(
                placeholder="e.g. dev-backend",
                id="input-name",
            )

            yield Label("Working directory  [dim](optional)[/]", classes="field-label", markup=True)
            yield Input(
                placeholder="e.g. /Users/rbgnr/git/my-project",
                id="input-cwd",
            )

            yield Static("", id="error-label")

            with Horizontal(id="dialog-buttons"):
                yield Button("Create", id="btn-create", variant="primary")
                yield Button("Cancel", id="btn-cancel", variant="default")

    def on_mount(self) -> None:
        self.query_one("#input-name", Input).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-cancel":
            self.dismiss(None)
        elif event.button.id == "btn-create":
            self._submit()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Allow pressing Enter in any field to move to next / submit."""
        input_id = event.input.id
        if input_id == "input-machine":
            self.query_one("#input-name", Input).focus()
        elif input_id == "input-name":
            self.query_one("#input-cwd", Input).focus()
        elif input_id == "input-cwd":
            self._submit()

    def _submit(self) -> None:
        machine = self.query_one("#input-machine", Input).value.strip()
        name = self.query_one("#input-name", Input).value.strip()
        cwd = self.query_one("#input-cwd", Input).value.strip() or None
        error_label = self.query_one("#error-label", Static)

        if not machine:
            error_label.update("[red]Machine is required.[/]")
            self.query_one("#input-machine", Input).focus()
            return

        if machine not in FLEET_MACHINES:
            valid = ", ".join(FLEET_MACHINES.keys())
            error_label.update(f"[red]Unknown machine. Valid: {valid}[/]")
            self.query_one("#input-machine", Input).focus()
            return

        if not name:
            error_label.update("[red]Session name is required.[/]")
            self.query_one("#input-name", Input).focus()
            return

        error_label.update("")
        self.dismiss({"machine": machine, "name": name, "cwd": cwd})

    def action_cancel(self) -> None:
        self.dismiss(None)
