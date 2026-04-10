"""
Universal tmux/psmux output parser.

Tries multiple formats from specific to general:
1. Pipe-delimited format (tmux -F output): name|created_ts|windows|attached
2. Structured plain text: "name: N windows (created DATE) (attached)"
3. Simple name-per-line fallback

All functions return a consistent list of dicts:
[{"name": str, "created": str|None, "windows": int, "attached": bool}]
"""
import re
from datetime import datetime, timezone


def parse_mux_output(output: str) -> list[dict]:
    """Parse tmux or psmux output, auto-detecting the format.

    Tries formats from most specific to most general:
    1. Pipe-delimited (tmux -F '#{session_name}|#{session_created}|#{session_windows}|#{session_attached}')
    2. Plain text "name: N windows (created DATE)" (psmux default)
    3. One session name per line (last resort)
    """
    output = output.strip()
    if not output:
        return []

    lines = [l.strip() for l in output.splitlines() if l.strip()]
    if not lines:
        return []

    # Try pipe-delimited first (check if first line has 3+ pipes)
    if "|" in lines[0] and lines[0].count("|") >= 3:
        return _parse_pipe_format(lines)

    # Try structured plain text
    results = _parse_plain_text(lines)
    if results:
        return results

    # Fallback: treat each line as a session name
    return [{"name": line, "created": None, "windows": 0, "attached": False} for line in lines]


def _parse_pipe_format(lines: list[str]) -> list[dict]:
    """Parse pipe-delimited tmux -F output.

    Expected fields: name|created_ts|windows|attached[|pane_current_path]
    The 5th field (cwd) is optional — older format strings omit it.
    """
    sessions = []
    for line in lines:
        parts = line.split("|")
        if len(parts) < 4:
            continue
        name = parts[0]
        try:
            created = datetime.fromtimestamp(int(parts[1])).astimezone().isoformat()
        except (ValueError, OSError):
            created = parts[1] if parts[1] else None
        try:
            windows = int(parts[2])
        except ValueError:
            windows = 0
        attached = parts[3].strip() not in ("0", "")
        cwd = parts[4].strip() if len(parts) >= 5 else ""
        sessions.append({"name": name, "created": created, "windows": windows, "attached": attached, "cwd": cwd})
    return sessions


_PLAIN_RE = re.compile(
    r'^(.+?):\s+(\d+)\s+windows?\s+'
    r'(?:\(created\s+(.+?)\))?'
    r'(?:\s+\(attached\))?$'
)

_PLAIN_ATTACHED_RE = re.compile(r'\(attached\)')


def _parse_plain_text(lines: list[str]) -> list[dict]:
    """Parse plain text psmux/tmux output like 'name: N windows (created DATE) (attached)'."""
    sessions = []
    for line in lines:
        m = _PLAIN_RE.match(line)
        if m:
            sessions.append({
                "name": m.group(1).strip(),
                "created": m.group(3).strip() if m.group(3) else None,
                "windows": int(m.group(2)),
                "attached": bool(_PLAIN_ATTACHED_RE.search(line)),
            })
        elif ":" in line:
            # Partial match: "name: ..." but doesn't fully match
            name = line.split(":")[0].strip()
            if name:
                sessions.append({"name": name, "created": None, "windows": 0, "attached": False})
    return sessions
