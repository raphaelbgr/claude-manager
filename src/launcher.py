"""Cross-platform terminal launcher."""
import asyncio
import shlex
import shutil
import sys
from .config import FLEET_MACHINES, detect_local_machine


def applescript_string(s: str) -> str:
    """Escape a string for safe embedding inside an AppleScript double-quoted string."""
    # Escape backslashes first, then double quotes
    escaped = s.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


async def launch_terminal(command: str) -> dict:
    """
    Open a new terminal window on the local machine and run command in it.

    Returns:
        {"ok": True} on success, {"ok": False, "error": str} on failure.
    """
    if sys.platform == "darwin":
        return await _launch_macos(command)
    elif sys.platform.startswith("linux"):
        return await _launch_linux(command)
    elif sys.platform == "win32":
        return await _launch_windows(command)
    else:
        return {"ok": False, "error": f"Unsupported platform: {sys.platform}"}


async def _run_osascript(script: str) -> dict:
    """Run an AppleScript snippet via osascript. Returns ok/error dict."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "osascript", "-e", script,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
        if proc.returncode != 0:
            return {"ok": False, "error": stderr.decode().strip()}
        return {"ok": True}
    except asyncio.TimeoutError:
        return {"ok": False, "error": "osascript timed out"}
    except OSError as exc:
        return {"ok": False, "error": str(exc)}


async def _launch_macos(command: str) -> dict:
    """Launch a terminal on macOS — tries iTerm2 first, falls back to Terminal.app."""
    cmd_esc = applescript_string(command)

    iterm_script = (
        'tell application "iTerm2"\n'
        '    activate\n'
        '    set newWindow to (create window with default profile)\n'
        '    tell current session of newWindow\n'
        f'        write text {cmd_esc}\n'
        '    end tell\n'
        'end tell'
    )
    result = await _run_osascript(iterm_script)
    if result["ok"]:
        return result

    # Fall back to Terminal.app
    terminal_script = (
        'tell application "Terminal"\n'
        '    activate\n'
        f'    do script {cmd_esc}\n'
        'end tell'
    )
    return await _run_osascript(terminal_script)


async def _launch_linux(command: str) -> dict:
    """Launch a terminal on Linux — tries common emulators in order."""
    emulators = [
        "x-terminal-emulator",
        "gnome-terminal",
        "konsole",
        "xfce4-terminal",
        "xterm",
    ]

    chosen = None
    for emulator in emulators:
        if shutil.which(emulator):
            chosen = emulator
            break

    if not chosen:
        return {"ok": False, "error": "No supported terminal emulator found on PATH"}

    if chosen == "gnome-terminal":
        # gnome-terminal uses -- to separate its args from the command
        cmd_args = [chosen, "--", "bash", "-c", f"{command}; exec bash"]
    else:
        cmd_args = [chosen, "-e", f"bash -c {shlex.quote(command + '; exec bash')}"]

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd_args,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        # Give the emulator a moment to launch; don't wait for it to exit
        try:
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=5)
            if proc.returncode is not None and proc.returncode != 0:
                return {"ok": False, "error": stderr.decode().strip()}
        except asyncio.TimeoutError:
            # The terminal is still running (expected) — that's fine
            pass
        return {"ok": True}
    except OSError as exc:
        return {"ok": False, "error": str(exc)}


async def _launch_windows(command: str) -> dict:
    """Launch a PowerShell window on Windows."""
    # Escape double quotes inside the command for PowerShell -Command string
    ps_command = command.replace('"', '`"')
    full_cmd = f'cmd /c start powershell -NoExit -Command "{ps_command}"'
    try:
        proc = await asyncio.create_subprocess_shell(
            full_cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)
            if proc.returncode is not None and proc.returncode != 0:
                return {"ok": False, "error": stderr.decode().strip()}
        except asyncio.TimeoutError:
            pass
        return {"ok": True}
    except OSError as exc:
        return {"ok": False, "error": str(exc)}


async def launch_claude_session(cwd: str, session_id: str, machine: str) -> dict:
    """
    Open a terminal and resume a Claude session (local or remote).

    Args:
        cwd:        Working directory for the session.
        session_id: Claude session ID to resume.
        machine:    Machine name (key in FLEET_MACHINES).

    Returns:
        {"ok": True} or {"ok": False, "error": str}.
    """
    local_machine = detect_local_machine()
    quoted_cwd = shlex.quote(cwd)
    quoted_sid = shlex.quote(session_id)

    if machine == local_machine:
        cmd = f"cd {quoted_cwd} && claude --resume {quoted_sid}"
    else:
        info = FLEET_MACHINES.get(machine, {})
        alias = info.get("ssh_alias", machine)
        cmd = f"ssh {shlex.quote(alias)} -t 'cd {quoted_cwd} && claude --resume {quoted_sid}'"

    return await launch_terminal(cmd)


async def launch_tmux_attach(session_name: str, machine: str) -> dict:
    """
    Open a terminal and attach to an existing tmux/psmux session.

    Args:
        session_name: Name of the tmux session to attach to.
        machine:      Machine name (key in FLEET_MACHINES).

    Returns:
        {"ok": True} or {"ok": False, "error": str}.
    """
    local_machine = detect_local_machine()
    info = FLEET_MACHINES.get(machine, {})
    mux = info.get("mux", "tmux")
    quoted_name = shlex.quote(session_name)

    if machine == local_machine:
        cmd = f"tmux attach -t {quoted_name}"
    else:
        alias = info.get("ssh_alias", machine)
        # -t flag is required for PTY allocation when attaching to a mux session
        cmd = f"ssh {shlex.quote(alias)} -t '{mux} attach -t {quoted_name}'"

    return await launch_terminal(cmd)


async def launch_remote_terminal(command: str, machine: str) -> dict:
    """
    Open a terminal ON THE REMOTE MACHINE's own display (not locally via SSH).

    Uses SSH to trigger a terminal launch on the remote machine's desktop:
    - macOS: osascript to open Terminal.app/iTerm2
    - Linux: DISPLAY=:0 x-terminal-emulator
    - Windows: powershell Start-Process
    """
    info = FLEET_MACHINES.get(machine, {})
    alias = info.get("ssh_alias", machine)
    remote_os = info.get("os", "")
    escaped = command.replace("'", "'\\''")

    if remote_os == "darwin":
        # Open Terminal.app on the remote Mac's display
        applescript = (
            f'tell application "Terminal"\n'
            f'  activate\n'
            f'  do script "{escaped}"\n'
            f'end tell'
        )
        ssh_cmd = f"ssh {shlex.quote(alias)} osascript -e {shlex.quote(applescript)}"
    elif remote_os in ("linux",):
        # Open terminal on remote Linux's X display
        inner = shlex.quote(escaped + "; exec bash")
        ssh_cmd = f"ssh {shlex.quote(alias)} 'DISPLAY=:0 nohup x-terminal-emulator -e bash -c {inner} &>/dev/null &'"
    elif remote_os == "win32":
        # Open PowerShell window on remote Windows
        ps_escaped = command.replace('"', '`"')
        ssh_cmd = f"ssh {shlex.quote(alias)} \"powershell -Command Start-Process powershell -ArgumentList '-NoExit','-Command','{ps_escaped}'\""
    else:
        return {"ok": False, "error": f"Unknown remote OS for {machine}: {remote_os}"}

    try:
        proc = await asyncio.create_subprocess_exec(
            *ssh_cmd.split()[:3],  # don't split — use shell
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        # Actually, we need shell=True for the complex quoting
    except Exception as e:
        return {"ok": False, "error": str(e)}

    # Use shell execution for the complex SSH command
    try:
        proc = await asyncio.create_subprocess_shell(
            ssh_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await asyncio.wait_for(proc.communicate(), timeout=10)
        return {"ok": True}
    except asyncio.TimeoutError:
        return {"ok": True}  # fire-and-forget, timeout is OK
    except Exception as e:
        return {"ok": False, "error": str(e)}


async def launch_tmux_attach_remote(session_name: str, machine: str) -> dict:
    """Open a terminal ON THE REMOTE MACHINE attached to the tmux session."""
    info = FLEET_MACHINES.get(machine, {})
    mux = info.get("mux", "tmux")
    return await launch_remote_terminal(f"{mux} attach -t {shlex.quote(session_name)}", machine)


async def launch_new_tmux_and_attach(
    name: str,
    machine: str,
    cwd: str | None = None,
    command: str | None = None,
) -> dict:
    """
    Create a new detached tmux/psmux session, then open a terminal and attach to it.

    Args:
        name:    Session name.
        machine: Machine name (key in FLEET_MACHINES).
        cwd:     Optional working directory for the new session.
        command: Optional command to run in the new session.

    Returns:
        {"ok": True} or {"ok": False, "error": str}.
    """
    from .tmux_manager import create_tmux_session

    create_result = await create_tmux_session(machine, name, cwd=cwd, command=command)
    if not create_result.get("ok"):
        return create_result

    return await launch_tmux_attach(name, machine)
