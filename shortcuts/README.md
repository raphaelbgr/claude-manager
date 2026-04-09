# Claude Manager Shortcuts

Platform-specific launchers for Claude Manager.

## macOS (`.command` files)

Double-click any `.command` file in Finder to launch:

| File | Mode |
|------|------|
| `Claude Manager.command` | Default GUI/TUI mode |
| `Claude Manager API.command` | Headless API server only (port 44740) |
| `Claude Manager TUI.command` | Terminal UI mode |

If macOS blocks execution: right-click → Open → Open anyway (first run only).

## Linux (`.desktop` + `launch.sh`)

1. Edit `claude-manager.desktop` and replace `/path/to/claude-manager` with your actual repo path.
2. Copy to `~/.local/share/applications/` for user-level app entry.
3. `launch.sh` is the underlying launcher script.

```bash
# Install desktop entry
sed "s|/path/to/claude-manager|$(pwd)/..|g" claude-manager.desktop \
  > ~/.local/share/applications/claude-manager.desktop
```

## Windows (`.bat` files)

Double-click any `.bat` file or run from cmd/PowerShell:

| File | Mode |
|------|------|
| `Claude Manager.bat` | Default mode |
| `Claude Manager API.bat` | Headless API server only (port 44740) |
| `Claude Manager TUI.bat` | Terminal UI mode |
