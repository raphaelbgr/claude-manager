"""SSH-key-derived bearer token authentication for the API.

Strategy:
    token = sha256(public_key_file_content).hexdigest()[:32]

The server reads its own SSH public key at startup and computes the token.
Clients (desktop app, other LAN devices) that have read access to the same
public key file can compute the same token and authenticate.

This is a shared-secret scheme, not signature auth. It protects against:
    - Network attackers (can't read the file)
    - Drive-by HTTP scanners on the LAN
    - Accidental exposure when binding to 0.0.0.0

It does NOT protect against:
    - Someone with filesystem access to the key file (they own the machine)
    - Someone who can read the token from localStorage on an authorized client

Auth config is persisted to ~/.claude-manager/auth.json:
    {
        "enabled": true,
        "key_path": "/Users/rbgnr/.ssh/id_rsa.pub",
        "created": "2026-04-10T03:00:00Z"
    }
"""
from __future__ import annotations

import hashlib
import json
import logging
import pathlib
from datetime import datetime
from typing import Optional

from .config import AUTH_CONFIG_DIR, AUTH_CONFIG_FILE, DEFAULT_PUBKEY_PATHS

log = logging.getLogger("claude_manager.auth")


class AuthConfig:
    """In-memory auth configuration loaded from disk at startup."""

    def __init__(
        self,
        enabled: bool = False,
        key_path: Optional[pathlib.Path] = None,
        token: Optional[str] = None,
    ):
        self.enabled = enabled
        self.key_path = key_path
        self.token = token

    def to_public_dict(self) -> dict:
        """Return a JSON-safe dict WITHOUT the token (for /api/auth/config)."""
        return {
            "enabled": self.enabled,
            "key_path": str(self.key_path) if self.key_path else None,
        }


def compute_token(key_path: pathlib.Path) -> str:
    """Compute the bearer token from an SSH public key file.

    Raises FileNotFoundError if the file doesn't exist.
    Raises PermissionError if it can't be read.
    """
    content = key_path.read_bytes().strip()
    if not content:
        raise ValueError(f"Key file is empty: {key_path}")
    return hashlib.sha256(content).hexdigest()[:32]


def find_default_pubkey() -> Optional[pathlib.Path]:
    """Find the first existing default SSH public key, preferring ed25519."""
    for p in DEFAULT_PUBKEY_PATHS:
        if p.is_file():
            return p
    return None


def list_available_pubkeys() -> list[pathlib.Path]:
    """Return all *.pub files in ~/.ssh/ so the UI can offer a picker."""
    ssh_dir = pathlib.Path.home() / ".ssh"
    if not ssh_dir.is_dir():
        return []
    return sorted(p for p in ssh_dir.glob("*.pub") if p.is_file())


def load_auth_config() -> AuthConfig:
    """Load auth config from ~/.claude-manager/auth.json. Missing file → disabled."""
    if not AUTH_CONFIG_FILE.is_file():
        return AuthConfig(enabled=False)
    try:
        data = json.loads(AUTH_CONFIG_FILE.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("auth.json corrupt, disabling auth: %s", exc)
        return AuthConfig(enabled=False)

    if not data.get("enabled"):
        return AuthConfig(enabled=False)

    key_path_str = data.get("key_path")
    if not key_path_str:
        log.warning("auth.json enabled but missing key_path, disabling")
        return AuthConfig(enabled=False)

    key_path = pathlib.Path(key_path_str)
    try:
        token = compute_token(key_path)
    except (FileNotFoundError, PermissionError, ValueError) as exc:
        log.error("auth.json key unreadable: %s → disabling auth", exc)
        return AuthConfig(enabled=False)

    return AuthConfig(enabled=True, key_path=key_path, token=token)


def save_auth_config(enabled: bool, key_path: Optional[pathlib.Path]) -> AuthConfig:
    """Write auth config to disk and return the loaded config object."""
    AUTH_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "enabled": enabled,
        "key_path": str(key_path) if key_path else None,
        "updated": datetime.now().astimezone().isoformat(),
    }
    AUTH_CONFIG_FILE.write_text(json.dumps(payload, indent=2))
    # Restrict permissions on Unix (0600)
    try:
        AUTH_CONFIG_FILE.chmod(0o600)
    except OSError:
        pass
    return load_auth_config()


def extract_bearer_token(header_value: Optional[str]) -> Optional[str]:
    """Parse 'Bearer <token>' from an Authorization header value."""
    if not header_value:
        return None
    parts = header_value.strip().split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1].strip()


def is_loopback(remote_addr: Optional[str]) -> bool:
    """Return True if the client is on loopback (same machine)."""
    if not remote_addr:
        return False
    return remote_addr in ("127.0.0.1", "::1", "localhost")
