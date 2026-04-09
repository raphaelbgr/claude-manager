"""Cross-platform terminal launcher."""
import asyncio
import logging
import shlex
import shutil
import sys
from .command_adapter import get_adapter
from .config import FLEET_MACHINES, detect_local_machine

log = logging.getLogger("claude_manager.launcher")


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
    log.info("launch_terminal: command=%s...", command[:80])
    if sys.platform == "darwin":
        result = await _launch_macos(command)
    elif sys.platform.startswith("linux"):
        result = await _launch_linux(command)
    elif sys.platform == "win32":
        result = await _launch_windows(command)
    else:
        result = {"ok": False, "error": f"Unsupported platform: {sys.platform}"}
    if not result.get("ok"):
        log.error("launch_terminal: failed: %s", result.get("error"))
    return result


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


async def launch_claude_session(cwd: str, session_id: str, machine: str, skip_permissions: bool = False) -> dict:
    """
    Open a terminal and resume a Claude session (local or remote).

    Args:
        cwd:              Working directory for the session.
        session_id:       Claude session ID to resume.
        machine:          Machine name (key in FLEET_MACHINES).
        skip_permissions: When True, appends --dangerously-skip-permissions to the command.

    Returns:
        {"ok": True} or {"ok": False, "error": str}.
    """
    local_machine = detect_local_machine()
    adapter = get_adapter(machine)
    log.info("launch_claude_session(%s, %s): mode=terminal", machine, session_id[:12])

    if machine == local_machine:
        cmd = adapter.build_session_command(cwd, session_id, skip_permissions)
    else:
        info = FLEET_MACHINES.get(machine, {})
        alias = info.get("ssh_alias", machine)
        # Build the remote command using the SSH adapter (PowerShell on Windows)
        session_cmd = adapter.build_session_command_ssh(cwd, session_id, skip_permissions)
        terminal_cmd = adapter.for_terminal(session_cmd, keep_open=True)
        if adapter.is_windows:
            # Windows OpenSSH uses Git Bash as DefaultShell (DefaultShellCommandOption=-c).
            # With SSH -t (PTY): OpenSSH wraps the command in ConPTY/conhost and the
            # -Command "..." argument is NOT passed to PowerShell — it starts interactive.
            # Without -t: Git Bash runs `bash -c "user_cmd"` normally and PowerShell
            # receives the full -Command "..." argument correctly.
            # Fix: omit -t for Windows. PowerShell -NoExit keeps the session open.
            # Single quotes preserve backslashes in C:\paths across the bash→SSH chain.
            cmd = f"ssh {alias} '{terminal_cmd}'"
        else:
            cmd = f"ssh {shlex.quote(alias)} -t {shlex.quote(terminal_cmd)}"

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
    alias = info.get("ssh_alias", machine)
    adapter = get_adapter(machine)

    if machine == local_machine:
        cmd = adapter.mux_attach(session_name)
    elif adapter.mux_type == "psmux":
        # psmux attach over SSH -t fails with "Incorrect function (os error 1)".
        # This is a known psmux limitation — it can't handle PTY from SSH -t.
        #
        # Workaround: SSH into the machine with a remote command that runs
        # bash interactively and immediately executes the attach.
        # The --rcfile trick sources .bashrc then runs our command.
        attach = adapter.mux_attach(session_name)
        # psmux can't forward PTY from SSH. The session IS running (created
        # via send-keys). SSH in and show the user how to attach manually.
        cmd = f"ssh {shlex.quote(alias)}"
    else:
        # tmux: SSH -t with direct attach works
        cmd = f"ssh {shlex.quote(alias)} -t {shlex.quote(adapter.mux_attach(session_name))}"

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
    adapter = get_adapter(machine)
    return await launch_remote_terminal(adapter.mux_attach(session_name), machine)


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
