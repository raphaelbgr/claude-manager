"""
OS-aware command builder for cross-platform session management.

Handles command generation for any source→target OS combination:
- Local commands (same OS)
- SSH commands (macOS→Linux, macOS→Windows, etc.)
- tmux/psmux send-keys commands (bash vs cmd.exe shells)
"""
import logging
import shlex
from .config import FLEET_MACHINES

log = logging.getLogger("claude_manager.command_adapter")


class CommandAdapter:
    """Build shell commands adapted to the target OS."""

    def __init__(self, target_os: str, mux_type: str = "tmux"):
        """
        Args:
            target_os: "darwin", "linux", or "win32"
            mux_type:  "tmux" or "psmux"
        """
        self.target_os = target_os
        self.mux_type = mux_type
        self.is_windows = target_os == "win32"
        # psmux sessions run cmd.exe; tmux sessions run bash
        self.target_shell = "cmd" if mux_type == "psmux" else "bash"

    def quote_path(self, path: str) -> str:
        """Quote a filesystem path for the target shell."""
        if self.target_shell == "cmd":
            # cmd.exe: use double quotes when the path contains spaces or ampersands
            if " " in path or "&" in path:
                return f'"{path}"'
            return path
        else:
            return shlex.quote(path)

    def cd_command(self, path: str) -> str:
        """Generate a cd command for the target shell."""
        if self.target_shell == "cmd":
            # /d switch changes the drive letter as well as the directory
            quoted = self.quote_path(path)
            return f"cd /d {quoted}"
        else:
            return f"cd {shlex.quote(path)}"

    def chain_commands(self, *commands: str) -> str:
        """Chain multiple commands with && (works in both bash and cmd.exe)."""
        return " && ".join(commands)

    def cd_command_ssh(self, path: str) -> str:
        """Generate a cd command for the SSH login shell.

        On Windows, SSH defaults to Git Bash but we invoke PowerShell
        explicitly for reliable Windows path handling.
        """
        if self.is_windows:
            # Will be wrapped in powershell -NoExit -Command "..."
            return f"Set-Location '{path}'"
        return f"cd {shlex.quote(path)}"

    def build_session_command_ssh(self, cwd: str, session_id: str, skip_permissions: bool = False) -> str:
        """Build cd + claude resume for SSH -t direct execution.

        The returned string is wrapped in SINGLE QUOTES by the caller for SSH:
            ssh host -t '<this string>'

        Windows SSH default shell is PowerShell (enforced fleet-wide — see
        global CLAUDE.md Windows SSH Shell Policy). PowerShell syntax:
            Set-Location 'C:\\path'; claude --resume <id>
        PowerShell 5.1 does NOT support &&; use ; as separator.
        """
        if self.is_windows:
            # PowerShell syntax — escape single quotes by doubling them
            cwd_ps = cwd.replace("'", "''")
            resume = self.claude_resume_command(session_id, skip_permissions)
            return f"Set-Location '{cwd_ps}'; {resume}"

        cd = f"cd {shlex.quote(cwd)}"
        resume = self.claude_resume_command(session_id, skip_permissions)
        return self.chain_commands(cd, resume)

    def build_new_session_command_ssh(self, cwd: str, skip_permissions: bool = False) -> str:
        """Build cd + claude (fresh session, no --resume) for SSH -t direct execution.

        Used by the 'New session' button. Same shell rules as build_session_command_ssh.
        """
        claude = "claude"
        if skip_permissions:
            claude += " --dangerously-skip-permissions"

        if self.is_windows:
            cwd_ps = cwd.replace("'", "''")
            return f"Set-Location '{cwd_ps}'; {claude}"

        cd = f"cd {shlex.quote(cwd)}"
        return self.chain_commands(cd, claude)

    @staticmethod
    def _win_path_to_bash(path: str) -> str:
        """Convert C:\\Users\\path to /c/Users/path for Git Bash."""
        import re
        # Match drive letter: C:\ or C:/
        m = re.match(r'^([A-Za-z]):[/\\](.*)$', path)
        if m:
            drive = m.group(1).lower()
            rest = m.group(2).replace('\\', '/')
            return f"/{drive}/{rest}"
        # Already a Unix path or no drive letter
        return path.replace('\\', '/')

    def quote_arg(self, arg: str) -> str:
        """Quote a shell argument for the target shell."""
        if self.target_shell == "cmd":
            # cmd.exe: double-quote if it has special chars
            if any(c in arg for c in ' &|<>^;"'):
                return f'"{arg}"'
            return arg
        else:
            return shlex.quote(arg)

    def claude_resume_command(self, session_id: str, skip_permissions: bool = False) -> str:
        """Build the claude --resume command with safe quoting."""
        quoted_id = self.quote_arg(session_id)
        cmd = f"claude --resume {quoted_id}"
        if skip_permissions:
            cmd += " --dangerously-skip-permissions"
        return cmd

    def build_session_command(
        self,
        cwd: str,
        session_id: str,
        skip_permissions: bool = False,
    ) -> str:
        """Build the full cd + claude resume command for the target shell.

        Returns:
            bash:   cd '/path/to/project' && claude --resume <uuid>
            cmd.exe: cd /d C:\\path\\to\\project && claude --resume <uuid>
        """
        cd = self.cd_command(cwd)
        resume = self.claude_resume_command(session_id, skip_permissions)
        return self.chain_commands(cd, resume)

    def mux_create_session(self, name: str) -> str:
        """Build mux new-session command (creates a detached session)."""
        return f"{self.mux_type} new-session -d -s {shlex.quote(name)}"

    def mux_send_keys(self, session_name: str, command: str) -> str:
        """Build mux send-keys command.

        The send-keys wrapper is always executed by the SSH shell (bash/Git Bash),
        so the session name and command are quoted with POSIX shlex.  The text
        that gets typed into the mux pane must already be in the target shell's
        syntax (cmd.exe for psmux, bash for tmux) — callers must pass a command
        already built by build_session_command().
        """
        quoted_name = shlex.quote(session_name)
        quoted_cmd = shlex.quote(command)
        return f"{self.mux_type} send-keys -t {quoted_name} {quoted_cmd} Enter"

    def mux_attach(self, session_name: str) -> str:
        """Build mux attach command."""
        return f"{self.mux_type} attach -t {shlex.quote(session_name)}"

    def mux_kill_session(self, session_name: str) -> str:
        """Build mux kill-session command."""
        return f"{self.mux_type} kill-session -t {shlex.quote(session_name)}"

    def ssh_wrap(
        self,
        ssh_alias: str,
        remote_command: str,
        allocate_tty: bool = False,
    ) -> str:
        """Wrap a command for SSH execution (returns a shell string)."""
        tty = "-t " if allocate_tty else ""
        return f"ssh {tty}{shlex.quote(ssh_alias)} {shlex.quote(remote_command)}"

    def for_terminal(self, command: str, keep_open: bool = True) -> str:
        """Wrap command to keep the terminal open after execution.

        cmd.exe interactive sessions do not need this; bash does.
        """
        if self.target_shell == "cmd":
            return command
        else:
            if keep_open:
                return f"{command}; exec bash"
            return command


    def generate_mux_session_name(self, machine: str, project_folder: str, existing_names: list[str]) -> str:
        """Generate a unique mux session name with auto-increment.

        Format: {machine}_{project}-session-{NN}
        Uses underscore (not slash) — tmux/psmux reject / in names.
        """
        import re
        base = f"{machine}_{project_folder}-session"
        max_n = 0
        for name in existing_names:
            m = re.match(re.escape(base) + r"-(\d+)$", name)
            if m:
                max_n = max(max_n, int(m.group(1)))
        n = max_n + 1
        return f"{base}-{n:02d}"

    def generate_claude_session_name(self, machine: str, project_folder: str, session_number: int) -> str:
        """Generate a Claude session display name.

        Format: [{mux_type}] {machine}/{project} session {NN}
        """
        return f"[{self.mux_type}] {machine}/{project_folder} session {session_number:02d}"


def get_adapter(machine_name: str) -> CommandAdapter:
    """Get the appropriate CommandAdapter for a fleet machine.

    Falls back to darwin/tmux defaults for unknown machines.
    """
    info = FLEET_MACHINES.get(machine_name, {})
    target_os = info.get("os", "darwin")
    mux_type = info.get("mux", "tmux")
    adapter = CommandAdapter(target_os=target_os, mux_type=mux_type)
    log.debug("adapter(%s): target_shell=%s, mux=%s", machine_name, adapter.target_shell, mux_type)
    return adapter
