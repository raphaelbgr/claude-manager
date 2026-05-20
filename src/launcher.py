"""Cross-platform terminal launcher."""
import asyncio
import logging
import shlex
import shutil
import sys
from .command_adapter import get_adapter, sanitize_mux_name
from .config import FLEET_MACHINES, detect_local_machine, SSH_TIMEOUT
from .subprocess_utils import _win32_asyncio_kwargs
from .tracking import tl, span

log = logging.getLogger("claude_manager.launcher")


# Strict allow-list: only safe identifier-ish characters survive into a title.
# Anything else (shell metacharacters, control bytes, spaces that could hide
# embedded commands) causes the whole segment to be dropped rather than
# partially scrubbed — so an attacker can't smuggle "rm -rf" into the title
# by using only letters and dashes.
_TITLE_SAFE = __import__("re").compile(r"^[A-Za-z0-9._-]+$")


def _sanitize_title_segment(s: str) -> str:
    """Return `s` if it's a safe identifier-ish string; otherwise empty.

    This is intentionally strict. The title is embedded in a `printf '\\033]0;…'`
    (or PowerShell `$Host.UI.RawUI.WindowTitle = '…'`) command that runs in a
    shell. Today the single-quote wrapping makes even unsanitized content safe
    at the shell level, but a later refactor that loses those quotes would
    leak every character of the segment into a shell context. Dropping any
    segment that doesn't look like a plain identifier closes that risk.

    Note: this also rejects ASCII space — sessionsand fleet names are expected
    to be hyphen/underscore-separated, never space-separated.
    """
    if not s:
        return s
    return s if _TITLE_SAFE.match(s) else ""


def build_window_title(
    origin: str | None,
    destination: str | None,
    mux_session: str | None,
    project: str | None,
) -> str:
    """Build the arrow-separated window title: Origin -> Dest -> Session -> Project.

    Any segment that's None or empty is dropped. Local launches (origin == dest
    or dest is None) drop the destination. Terminal-only (no mux) drops the
    session. Missing project is tolerated — you still get at least the origin.

    Every segment is scrubbed of shell-dangerous characters — the title is
    concatenated into an OSC-0 escape sequence that runs inside a shell, and
    the upstream inputs (project = cwd basename, session name = user-supplied)
    aren't guaranteed safe.
    """
    parts: list[str] = []
    for seg in (origin, destination if destination and destination != origin else None,
                mux_session, project):
        if not seg:
            continue
        clean = _sanitize_title_segment(seg)
        if clean:
            parts.append(clean)
    return " → ".join(parts) if parts else "claude-manager"


def _title_prefix_unix(title: str) -> str:
    """printf the ANSI OSC 0 sequence that sets the terminal window/tab title.
    Honored by iTerm2, Terminal.app, gnome-terminal, konsole, xterm, alacritty,
    Windows Terminal, tmux (when pass-through is on). Silently ignored elsewhere.

    Uses double quotes — not single — because the whole SSH command is later
    wrapped by ``shlex.quote`` (which uses single quotes). If this prefix
    contained inner single quotes, ``shlex.quote`` would escape them as the
    POSIX ``'\\''`` sequence, which a downstream Windows host (``pwsh -Command``
    parsing the SSH invocation) cannot tokenize — the chain produces garbled
    args like ``'"' tmux attach -t name'"*'`` and the launch fails with
    ``error 0x80070002 (file not found)``. Double-quoted printf is bash-
    equivalent here because the OSC 0 payload has no ``$`` to interpolate.
    """
    safe = title.replace('"', '\\"')
    return f'printf "\\033]0;{safe}\\007"; '


def _title_prefix_powershell(title: str) -> str:
    """PowerShell: set $Host.UI.RawUI.WindowTitle, wrapped in try/catch so
    environments without a real host (e.g. ISE, unknown terminal) don't error."""
    safe = title.replace("'", "''")
    return f"try {{ $Host.UI.RawUI.WindowTitle = '{safe}' }} catch {{}} ; "


def title_prefix_for(target_os: str, title: str) -> str:
    """Return a shell-prepended title-set for the given target OS. Empty string
    if target is unknown or title is empty (fail-open — launches must never
    break just because we couldn't label the window)."""
    if not title:
        return ""
    try:
        if target_os == "win32":
            return _title_prefix_powershell(title)
        return _title_prefix_unix(title)
    except Exception:
        return ""


def _ssh_path_prefix(machine: str) -> str:
    """Return PATH export prefix for SSH to Unix machines, empty for Windows."""
    info = FLEET_MACHINES.get(machine, {})
    if info.get("os") == "win32":
        return ""
    return "export PATH=/opt/homebrew/bin:/usr/local/bin:/snap/bin:$PATH; "


def applescript_string(s: str) -> str:
    """Escape a string for safe embedding inside an AppleScript double-quoted string."""
    # Escape backslashes first, then double quotes
    escaped = s.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


_AUTO_ADAPTER_CACHE: dict[str, str | None] = {}


def _reg_os_for_local() -> str:
    if sys.platform == "win32":
        return "win32"
    if sys.platform.startswith("linux"):
        return "linux"
    return "darwin"


async def _auto_pick_local_adapter_id() -> str | None:
    """Probe installed terminals on the daemon host, cache the top-priority
    one, return its id. Invoked only when the caller didn't supply a
    terminal_id. Prevents the primary "click Attach with no dropdown" path
    from falling through to the legacy _launch_windows fallback, which tries
    to run cmd.exe-syntax commands (cd /d, &&) inside PowerShell -Command
    where they are invalid (PS 5.1).
    """
    reg_os = _reg_os_for_local()
    if reg_os in _AUTO_ADAPTER_CACHE:
        cached = _AUTO_ADAPTER_CACHE[reg_os]
        tl.event("cm.adapter.pick", id=cached, source="cache", os=reg_os)
        return cached

    from . import terminals as _terms
    from .subprocess_utils import _win32_asyncio_kwargs

    tl.event("cm.adapter.probe.start", os=reg_os)

    async def runner(shell_script: str) -> tuple[int, bytes, bytes]:
        if sys.platform == "win32":
            argv = ["powershell", "-NoProfile", "-Command", shell_script]
        else:
            argv = ["/bin/bash", "-c", shell_script]
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                **_win32_asyncio_kwargs(),
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=6)
            return (proc.returncode if proc.returncode is not None else 1), stdout, stderr
        except Exception as probe_exc:
            return 1, b"", str(probe_exc).encode()

    try:
        adapter = await _terms.auto_pick(reg_os, runner)
    except Exception as exc:
        log.warning("auto_pick failed: %s", exc)
        adapter = None

    picked_id = adapter.id if adapter is not None else None
    _AUTO_ADAPTER_CACHE[reg_os] = picked_id
    if picked_id:
        log.info("auto-picked terminal adapter: %s (%s)", picked_id, reg_os)
        tl.event("cm.adapter.probe.ok", picked=picked_id, os=reg_os)
        tl.event("cm.adapter.pick", id=picked_id, source="probe", os=reg_os)
    else:
        log.warning("auto-pick: no adapter installed on %s — using legacy spawn", reg_os)
        tl.event("cm.adapter.probe.ok", picked=None, os=reg_os, fallback="legacy")
    return picked_id


async def launch_terminal(
    command: str,
    *,
    terminal_id: str | None = None,
    title: str | None = None,
) -> dict:
    """Open a new terminal window on the local machine and run `command` in it.

    When `terminal_id` is provided, dispatch through the matching adapter in
    src.terminals (e.g. "iterm2", "wt", "alacritty"). When it's None, probe
    installed terminals once and route through the highest-priority one — the
    same path the dropdown takes. Only if no adapter is installed does the
    legacy auto-detect run; that fallback is known-fragile on Windows
    (spawns cmd.exe-syntax commands inside PowerShell -Command).

    `title` is forwarded to the adapter and, for adapters that support it,
    overrides the ANSI OSC 0 title that title_prefix_for() may have already
    baked into `command`.
    """
    log.info("launch_terminal: terminal_id=%s command=%s...", terminal_id, command[:80])

    if not terminal_id:
        terminal_id = await _auto_pick_local_adapter_id()

    if terminal_id:
        from . import terminals as _terms
        adapter = _terms.get_adapter(_reg_os_for_local(), terminal_id)
        if adapter is not None:
            tl.event("cm.adapter.launch.start",
                     adapter=terminal_id, cmd_head=(command or "")[:120], has_title=bool(title))
            result = await adapter.launch(command, title=title)
            if result.get("ok"):
                tl.event("cm.adapter.launch.ok", adapter=terminal_id)
            else:
                err = str(result.get("error", ""))[:200]
                log.error("launch_terminal(%s): %s", terminal_id, err)
                tl.event("cm.adapter.launch.err", adapter=terminal_id, err=err)
            return result
        log.warning("launch_terminal: unknown terminal_id=%r, falling back to auto", terminal_id)
        tl.event("cm.adapter.launch.err",
                 adapter=terminal_id, err="unknown terminal_id, falling back")

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


async def _launch_macos_multi(commands: list[str], delays: list[float] = None) -> dict:
    """Launch iTerm2 and type multiple commands with delays between them.

    Used for Windows SSH sessions where we need to:
    1. ssh host  (wait for connection)
    2. powershell  (wait for PS prompt)
    3. cd path
    4. claude --resume ...
    """
    if not commands:
        return {"ok": False, "error": "No commands"}
    if delays is None:
        delays = [0] + [2] + [0.5] * (len(commands) - 2)  # 2s after SSH, 0.5s between rest

    lines = []
    for i, cmd in enumerate(commands):
        if i > 0 and i < len(delays) and delays[i] > 0:
            lines.append(f'        delay {delays[i]}')
        lines.append(f'        write text {applescript_string(cmd)}')

    body = '\n'.join(lines)
    iterm_script = (
        'tell application "iTerm2"\n'
        '    activate\n'
        '    set newWindow to (create window with default profile)\n'
        '    tell current session of newWindow\n'
        f'{body}\n'
        '    end tell\n'
        'end tell'
    )
    result = await _run_osascript(iterm_script)
    if result["ok"]:
        return result

    # Terminal.app fallback — use 'do script' for each command
    term_lines = [f'    do script {applescript_string(commands[0])}']
    for i, cmd in enumerate(commands[1:], 1):
        if i < len(delays) and delays[i] > 0:
            term_lines.append(f'    delay {delays[i]}')
        term_lines.append(f'    do script {applescript_string(cmd)} in front window')

    term_body = '\n'.join(term_lines)
    terminal_script = (
        'tell application "Terminal"\n'
        '    activate\n'
        f'{term_body}\n'
        'end tell'
    )
    return await _run_osascript(terminal_script)


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
    """Launch a PowerShell window on Windows. Prefers pwsh 7 over powershell 5.1."""
    # Escape double quotes inside the command for PowerShell -Command string
    ps_command = command.replace('"', '`"')
    host = "pwsh" if (shutil.which("pwsh") or shutil.which("pwsh.exe")) else "powershell"
    full_cmd = f'cmd /c start {host} -NoExit -Command "{ps_command}"'
    try:
        proc = await asyncio.create_subprocess_shell(
            full_cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            **_win32_asyncio_kwargs(),
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


async def launch_claude_session(cwd: str, session_id: str, machine: str, skip_permissions: bool = False, terminal_id: str | None = None) -> dict:
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

    tl.event("cm.launch.session.start",
             machine=machine, session_id_head=(session_id or "")[:12],
             skip_permissions=skip_permissions, terminal_id=terminal_id or "auto")

    project_name = (cwd or "").replace("\\", "/").rstrip("/").split("/")[-1] or None

    if machine == local_machine:
        # Local: title = Origin -> Project (no destination, no mux).
        title = build_window_title(local_machine, None, None, project_name)
        local_os = "win32" if sys.platform == "win32" else ("linux" if sys.platform.startswith("linux") else "darwin")
        prefix = title_prefix_for("win32" if local_os == "win32" else local_os, title)
        cmd = adapter.build_session_command(cwd, session_id, skip_permissions)
        return await launch_terminal(prefix + cmd, terminal_id=terminal_id, title=title)

    info = FLEET_MACHINES.get(machine, {})
    alias = info.get("ssh_alias", machine)
    remote_os = info.get("os", "")

    # Remote: title = Origin -> Dest -> Project. The title is set INSIDE the
    # SSH session so it reflects the remote context. Skipped for Windows
    # targets — see launch_tmux_attach for the POSIX-quote-in-PS context
    # explanation (PSMUX_SESSION nested-session error).
    title = build_window_title(local_machine, machine, None, project_name)
    inner_prefix = "" if remote_os == "win32" else title_prefix_for(remote_os, title)

    # Same approach for ALL platforms: SSH -t with single command.
    # Windows: build_session_command_ssh converts C:\path to /c/path for Git Bash.
    # Linux/macOS: uses native paths. Both use bash syntax with SSH -t.
    session_cmd = adapter.build_session_command_ssh(cwd, session_id, skip_permissions)
    terminal_cmd = _ssh_path_prefix(machine) + inner_prefix + adapter.for_terminal(session_cmd, keep_open=True)
    cmd = f"ssh {shlex.quote(alias)} -t {shlex.quote(terminal_cmd)}"
    return await launch_terminal(cmd, terminal_id=terminal_id, title=title)


_SHELL_PROMPT_RE = __import__("re").compile(
    r"(?:"
    r"[A-Z]:\\[^\n]*>\s*$"      # cmd.exe:    C:\Users\x>
    r"|PS\s+[A-Z]:\\[^\n]*>\s*$"  # PowerShell: PS C:\Users\x>
    r"|[^\n]*[\$#]\s*$"          # bash/zsh:   user@host$  or #
    r")"
)


def _looks_like_shell_prompt(pane_text: str) -> bool:
    """True if the captured pane ends at what appears to be an idle shell
    prompt (not inside claude). Conservative: on any uncertainty, returns
    False so we don't inject stray text into a running claude."""
    if not pane_text:
        return False
    # If we see claude's TUI frame characters or greeting, it's running.
    for marker in ("Welcome to Claude", "Claude Code", "╭", "╰", "│ >"):
        if marker in pane_text:
            return False
    lines = [ln.rstrip() for ln in pane_text.splitlines() if ln.strip()]
    if not lines:
        return False
    last = lines[-1]
    return bool(_SHELL_PROMPT_RE.search(last))


async def _ensure_claude_running(machine: str, session_name: str, skip_permissions: bool = False) -> None:
    """If the existing session is sitting at an idle shell prompt, type
    `claude` into it via mux send-keys before we attach. This recovers
    legacy/empty sessions so 'Attach' actually lands you inside claude
    instead of a bare cmd.exe / bash prompt.
    """
    from .tmux_manager import capture_pane  # local import to avoid cycle
    try:
        pane = await capture_pane(machine, session_name, lines=15)
    except Exception as exc:
        log.warning("ensure_claude: capture_pane(%s, %s) failed: %s", machine, session_name, exc)
        return
    if not _looks_like_shell_prompt(pane):
        return

    tl.event("cm.launch.ensure_claude.run", machine=machine, session=session_name,
             skip_permissions=skip_permissions)
    adapter = get_adapter(machine)
    claude_cmd = "claude --dangerously-skip-permissions" if skip_permissions else "claude"
    send_keys = adapter.mux_send_keys(session_name, claude_cmd)

    local_machine = detect_local_machine()
    if machine == local_machine:
        try:
            proc = await asyncio.create_subprocess_shell(
                send_keys,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                **_win32_asyncio_kwargs(),
            )
            await asyncio.wait_for(proc.communicate(), timeout=5)
        except Exception as exc:
            log.warning("ensure_claude: local send-keys failed: %s", exc)
        return

    # Route the pre-attach keystroke through the asyncssh pool so Windows
    # targets don't spawn a fresh sshd-session (and its conhost child) per
    # attach. exec_shell falls back to subprocess-ssh only if the pool is
    # unavailable or in backoff.
    from .executor import SSHExecutor
    executor = SSHExecutor(machine)
    try:
        rc, _, stderr = await executor.exec_shell(send_keys, timeout=10)
        if rc != 0:
            log.warning(
                "ensure_claude: remote send-keys failed on %s: %s",
                machine, stderr.decode(errors="replace").strip(),
            )
        else:
            log.info("ensure_claude: sent `claude` to %s/%s", machine, session_name)
            # Give claude a moment to spawn before we attach, so the TUI is
            # already drawing when the user's terminal connects.
            await asyncio.sleep(0.8)
    except Exception as exc:
        log.warning("ensure_claude: remote send-keys exception: %s", exc)


async def launch_tmux_attach(session_name: str, machine: str, skip_permissions: bool = False, terminal_id: str | None = None) -> dict:
    """
    Open a terminal and attach to an existing tmux/psmux session.

    If the target session is sitting at a bare shell prompt (legacy session
    created without claude, or claude crashed/quit), type `claude` into it
    via send-keys first so the attach lands inside a running claude.

    Args:
        session_name:     Name of the tmux session to attach to.
        machine:          Machine name (key in FLEET_MACHINES).
        skip_permissions: Pass --dangerously-skip-permissions when we need
                          to start claude in the session.

    Returns:
        {"ok": True} or {"ok": False, "error": str}.
    """
    local_machine = detect_local_machine()
    info = FLEET_MACHINES.get(machine, {})
    alias = info.get("ssh_alias", machine)
    adapter = get_adapter(machine)
    remote_os = info.get("os", "")

    tl.event("cm.launch.tmux.start",
             machine=machine, remote_os=remote_os, session=session_name,
             skip_permissions=skip_permissions, terminal_id=terminal_id or "auto")

    # Pre-attach probe: if no claude is running inside the session, start one.
    await _ensure_claude_running(machine, session_name, skip_permissions=skip_permissions)

    # Title: Origin -> (Dest if remote) -> mux session name. Project is not
    # trivially recoverable from the mux session name alone so we omit it here.
    if machine == local_machine:
        title = build_window_title(local_machine, None, session_name, None)
        local_os = "win32" if sys.platform == "win32" else ("linux" if sys.platform.startswith("linux") else "darwin")
        prefix = title_prefix_for("win32" if local_os == "win32" else local_os, title)
        return await launch_terminal(prefix + adapter.mux_attach(session_name), terminal_id=terminal_id, title=title)

    title = build_window_title(local_machine, machine, session_name, None)
    # When remote is Windows, the in-shell title-setter uses single-quoted
    # PowerShell ('try { $Host.UI.RawUI.WindowTitle = '...' } catch {} ;') which
    # ``shlex.quote`` then POSIX-escapes (`'\''`). On a Windows LOCAL host
    # (wt/pwsh/powershell), PowerShell does not understand POSIX `'\''` — it
    # tokenises the inner `;` outside any string and runs the trailing
    # `psmux attach -t name` as a separate LOCAL statement, hitting psmux's
    # nested-session guard with `PSMUX_SESSION` already set in the user's
    # outer shell. Skip the inner title-set for Windows targets; the outer
    # `wt --title` already labels the tab.
    inner_prefix = "" if remote_os == "win32" else title_prefix_for(remote_os, title)

    if adapter.mux_type == "psmux":
        # psmux attach over SSH -t fails with "Incorrect function (os error 1)"
        # because psmux can't forward its PTY through a single SSH remote
        # command. Mirror the strategy handle_sessions_launch uses for fresh
        # Windows sessions: open a terminal, SSH in (lands in PowerShell),
        # wait for the login shell to be ready, then type `psmux attach -t`.
        attach_cmd = inner_prefix + adapter.mux_attach(session_name)
        if sys.platform == "darwin":
            return await _launch_macos_multi(
                [f"ssh {alias}", attach_cmd],
                delays=[0, 2],
            )
        # Non-mac orchestrator fallback: open a terminal running SSH, then
        # let the user press Enter on the attach command we've pre-typed.
        return await launch_terminal(
            f"ssh {shlex.quote(alias)} -t {shlex.quote(attach_cmd)}",
            terminal_id=terminal_id, title=title,
        )

    # tmux: SSH -t with direct attach works.
    # -CC enables iTerm2 native tmux integration (windows/tabs instead of raw TUI).
    cc = terminal_id == "iterm2" and adapter.mux_type == "tmux"
    attach_cmd = _ssh_path_prefix(machine) + inner_prefix + adapter.mux_attach(session_name, cc_mode=cc)
    return await launch_terminal(
        f"ssh {shlex.quote(alias)} -t {shlex.quote(attach_cmd)}",
        terminal_id=terminal_id, title=title,
    )




async def launch_remote_terminal(command: str, machine: str) -> dict:
    """
    Open a terminal ON THE REMOTE MACHINE's own display (not locally via SSH).

    Strategy: pipe a shell/AppleScript/PowerShell script via stdin to SSH
    (avoids all quoting hell). Each OS gets a tailored script that:
    - Includes the PATH so tmux/psmux are found
    - Spawns a terminal on the machine's own display
    - Runs the command inside that terminal
    """
    info = FLEET_MACHINES.get(machine, {})
    alias = info.get("ssh_alias", machine)
    remote_os = info.get("os", "")

    ssh_args = [
        "ssh",
        "-o", f"ConnectTimeout={SSH_TIMEOUT}",
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=no",
        alias,
    ]

    if remote_os == "darwin":
        # macOS: osascript read from stdin via "osascript -"
        # Add Homebrew PATH so tmux is found when Terminal.app runs the command
        applescript = f'''
tell application "Terminal"
    activate
    do script "export PATH=/opt/homebrew/bin:/usr/local/bin:$PATH; {command}"
end tell
'''
        ssh_args.append("osascript -")
        stdin_input = applescript.encode()
    elif remote_os == "linux":
        # Linux: bash script piped in, opens x-terminal-emulator on DISPLAY=:0
        bash_script = f'''
export DISPLAY=:0
export PATH=/opt/homebrew/bin:/usr/local/bin:/snap/bin:$PATH
for t in x-terminal-emulator gnome-terminal konsole xfce4-terminal xterm; do
    if command -v "$t" >/dev/null 2>&1; then
        if [ "$t" = "gnome-terminal" ]; then
            nohup "$t" -- bash -c "{command}; exec bash" >/dev/null 2>&1 &
        else
            nohup "$t" -e bash -c "{command}; exec bash" >/dev/null 2>&1 &
        fi
        exit 0
    fi
done
echo "No terminal emulator found on remote" >&2
exit 1
'''
        ssh_args.append("bash -s")
        stdin_input = bash_script.encode()
    elif remote_os == "win32":
        # Windows: PowerShell script via stdin (SSH default shell is PowerShell)
        # Start-Process opens a new PowerShell window running the command
        # Use single quotes around command to avoid escape issues, escape any internal '
        cmd_escaped = command.replace("'", "''")
        ps_script = f"Start-Process powershell -ArgumentList '-NoExit','-Command',\"{cmd_escaped}\"\n"
        ssh_args.append("powershell -Command -")
        stdin_input = ps_script.encode()
    else:
        return {"ok": False, "error": f"Unknown remote OS for {machine}: {remote_os}"}

    try:
        proc = await asyncio.create_subprocess_exec(
            *ssh_args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **_win32_asyncio_kwargs(),
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(input=stdin_input), timeout=10
            )
            if proc.returncode != 0:
                err = stderr.decode(errors="replace").strip()
                log.error("launch_remote_terminal(%s): %s", machine, err)
                return {"ok": False, "error": err or f"exit code {proc.returncode}"}
            return {"ok": True}
        except asyncio.TimeoutError:
            # Fire-and-forget is OK for terminal launches
            return {"ok": True}
    except Exception as e:
        log.error("launch_remote_terminal(%s): %s", machine, e)
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
    terminal_id: str | None = None,
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

    name = sanitize_mux_name(name)
    create_result = await create_tmux_session(machine, name, cwd=cwd, command=command)
    if not create_result.get("ok"):
        return create_result

    # Use the sanitized name returned by create_tmux_session (may differ from input)
    actual_name = create_result.get("name", name)
    return await launch_tmux_attach(actual_name, machine, terminal_id=terminal_id)
