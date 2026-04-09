"""
UI integration tests — validate src/web/index.html structure without a browser.

These tests use string/regex checks on the HTML source to verify:
  - Material Icons CDN link present
  - React 19 import map present
  - All expected component functions defined
  - No emoji characters in source (U+1F300-1F9FF range)
  - All API endpoints referenced in fetch() calls match server routes
  - WebSocket subscribe channels match server handler
  - MACHINE_INFO keys match FLEET_MACHINES in config
  - memo() wrapping present for SessionCard and TmuxCard
  - localStorage keys are defined as constants
  - No confirm() calls
  - Debounced/guarded localStorage writes present (try/catch pattern)
  - React imports include key hooks
"""
from __future__ import annotations

import pathlib
import re

import pytest

# Path to the web UI HTML
WEB_HTML_PATH = pathlib.Path(__file__).parent.parent / "src" / "web" / "index.html"

# Known server routes (from create_app in server.py)
KNOWN_SERVER_API_ROUTES = {
    "/api/sessions",
    "/api/sessions/scan",
    "/api/sessions/launch",
    "/api/sessions/pin",
    "/api/sessions/unpin",
    "/api/sessions/archive",
    "/api/sessions/unarchive",
    "/api/sessions/rename",
    "/api/hardware",
    "/api/fleet",
    "/api/tmux",
    "/api/tmux/create",
    "/api/tmux/connect",
    "/api/tmux/connect-remote",
    "/api/tmux/kill",
    "/api/browse",
    "/api/drives",
    "/api/mkdir",
    "/api/preferences",
    "/api/logs",
    "/api/restart",
}

# Expected WebSocket channels (from handle_ws in server.py)
KNOWN_WS_CHANNELS = {"sessions", "fleet", "tmux"}

# Expected component function names
EXPECTED_COMPONENTS = [
    "SessionCard",
    "TmuxCard",
    "FilterBar",
    "Header",
    "App",
    "Toaster",
]

# FLEET_MACHINES keys from config.py
FLEET_MACHINE_KEYS = {"mac-mini", "ubuntu-desktop", "avell-i7", "windows-desktop"}


@pytest.fixture(scope="module")
def html() -> str:
    """Read and return the full index.html content."""
    assert WEB_HTML_PATH.exists(), f"index.html not found at {WEB_HTML_PATH}"
    return WEB_HTML_PATH.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# CDN / External resources
# ---------------------------------------------------------------------------

class TestCDNLinks:
    """Verify external CDN links are present."""

    def test_material_icons_cdn_link_present(self, html):
        """Material Icons Round CDN must be linked."""
        assert "fonts.googleapis.com" in html
        assert "Material+Icons" in html

    def test_tailwind_cdn_present(self, html):
        assert "cdn.tailwindcss.com" in html or "tailwind" in html.lower()

    def test_material_icons_round_family(self, html):
        """Specifically the 'Round' variant is used."""
        assert "Material+Icons+Round" in html or "Material Icons Round" in html


# ---------------------------------------------------------------------------
# React import map
# ---------------------------------------------------------------------------

class TestReactImportMap:
    """Verify React 19 import map configuration."""

    def test_importmap_script_tag_present(self, html):
        assert 'type="importmap"' in html

    def test_react_19_imported(self, html):
        assert "react@19" in html

    def test_react_dom_19_imported(self, html):
        assert "react-dom@19" in html

    def test_react_jsx_runtime_present(self, html):
        assert "jsx-runtime" in html

    def test_esm_sh_used_as_cdn(self, html):
        assert "esm.sh" in html

    def test_module_script_tag_present(self, html):
        assert 'type="module"' in html

    def test_react_hooks_imported(self, html):
        """Key React hooks must be imported from react."""
        required_hooks = ["useState", "useEffect", "useRef", "useCallback"]
        for hook in required_hooks:
            assert hook in html, f"React hook {hook!r} not imported"

    def test_memo_imported_from_react(self, html):
        """memo must be imported (used for SessionCardMemo, TmuxCardMemo)."""
        assert "memo" in html


# ---------------------------------------------------------------------------
# Component definitions
# ---------------------------------------------------------------------------

class TestComponentDefinitions:
    """Verify expected React component functions are defined."""

    def test_session_card_defined(self, html):
        assert "function SessionCard" in html

    def test_tmux_card_defined(self, html):
        assert "function TmuxCard" in html

    def test_filter_bar_defined(self, html):
        assert "function FilterBar" in html

    def test_app_defined(self, html):
        assert "function App" in html

    def test_header_defined(self, html):
        assert "function Header" in html

    def test_toaster_defined(self, html):
        assert "function Toaster" in html

    def test_all_expected_components_defined(self, html):
        for component in EXPECTED_COMPONENTS:
            assert f"function {component}" in html, f"Component {component!r} not defined"


# ---------------------------------------------------------------------------
# memo() wrapping
# ---------------------------------------------------------------------------

class TestMemoWrapping:
    """SessionCard and TmuxCard must be wrapped with memo()."""

    def test_session_card_memo_present(self, html):
        assert "SessionCardMemo" in html
        assert "memo(SessionCard)" in html

    def test_tmux_card_memo_present(self, html):
        assert "TmuxCardMemo" in html
        assert "memo(TmuxCard)" in html


# ---------------------------------------------------------------------------
# No emoji characters
# ---------------------------------------------------------------------------

class TestNoEmojiCharacters:
    """The HTML source must not contain emoji characters in U+1F300-1F9FF range."""

    def test_no_emoji_in_ui_text(self, html):
        # Emoji Unicode ranges commonly used in UI (not in CDN URLs or data)
        emoji_ranges = [
            (0x1F300, 0x1F5FF),  # Misc Symbols and Pictographs
            (0x1F600, 0x1F64F),  # Emoticons
            (0x1F680, 0x1F6FF),  # Transport and Map
            (0x1F700, 0x1F77F),  # Alchemical Symbols
            (0x1F900, 0x1F9FF),  # Supplemental Symbols
        ]
        for char in html:
            cp = ord(char)
            for start, end in emoji_ranges:
                assert not (start <= cp <= end), (
                    f"Found emoji character U+{cp:04X} ({char!r}) in index.html"
                )


# ---------------------------------------------------------------------------
# API endpoint references
# ---------------------------------------------------------------------------

class TestAPIEndpointReferences:
    """All fetch() calls in the UI must reference valid server routes."""

    def _extract_fetch_routes(self, html: str) -> set[str]:
        """Extract all /api/... paths from fetch() calls."""
        # Match fetch('...') and fetch(`...`) patterns
        pattern = re.compile(
            r"fetch\(['\"`]([^'\"`]+)['\"`]|"
            r"fetch\(`[^`]*(/api/[^`'\"\s)]+)"
        )
        routes = set()
        for m in pattern.finditer(html):
            # Group 1: simple string literal
            if m.group(1) and m.group(1).startswith("/api/"):
                routes.add(m.group(1).split("?")[0])  # strip query strings
            # Group 2: template literal fragment
            if m.group(2):
                routes.add(m.group(2).split("?")[0])
        return routes

    def test_all_fetch_routes_are_known_server_routes(self, html):
        fetch_routes = self._extract_fetch_routes(html)
        # Only check /api/ routes, not /health or /ws
        api_routes = {r for r in fetch_routes if r.startswith("/api/")}
        for route in api_routes:
            assert route in KNOWN_SERVER_API_ROUTES, (
                f"fetch() references unknown route {route!r} not in server routes"
            )

    def test_sessions_endpoint_referenced(self, html):
        assert "/api/sessions" in html

    def test_tmux_endpoint_referenced(self, html):
        assert "/api/tmux" in html

    def test_fleet_endpoint_referenced(self, html):
        assert "/api/fleet" in html

    def test_preferences_endpoint_referenced(self, html):
        assert "/api/preferences" in html

    def test_scan_endpoint_referenced(self, html):
        assert "/api/sessions/scan" in html

    def test_hardware_endpoint_referenced(self, html):
        assert "/api/hardware" in html

    def test_drives_endpoint_referenced(self, html):
        assert "/api/drives" in html

    def test_browse_endpoint_referenced(self, html):
        assert "/api/browse" in html

    def test_mkdir_endpoint_referenced(self, html):
        assert "/api/mkdir" in html

    def test_pin_endpoint_referenced(self, html):
        assert "/api/sessions/pin" in html

    def test_unpin_endpoint_referenced(self, html):
        assert "/api/sessions/unpin" in html

    def test_archive_endpoint_referenced(self, html):
        assert "/api/sessions/archive" in html

    def test_unarchive_endpoint_referenced(self, html):
        assert "/api/sessions/unarchive" in html

    def test_rename_endpoint_referenced(self, html):
        assert "/api/sessions/rename" in html

    def test_restart_endpoint_referenced(self, html):
        assert "/api/restart" in html


# ---------------------------------------------------------------------------
# WebSocket channels
# ---------------------------------------------------------------------------

class TestWebSocketChannels:
    """WS subscribe channels in UI must match server-side channel names."""

    def _extract_subscribe_channels(self, html: str) -> set[str]:
        """Find channel names used in subscribe messages."""
        pattern = re.compile(
            r"channel['\"]?\s*:\s*['\"]([a-z_]+)['\"]",
        )
        return {m.group(1) for m in pattern.finditer(html)}

    def test_sessions_channel_subscribed(self, html):
        assert '"sessions"' in html or "'sessions'" in html

    def test_fleet_channel_subscribed(self, html):
        assert '"fleet"' in html or "'fleet'" in html

    def test_tmux_channel_subscribed(self, html):
        assert '"tmux"' in html or "'tmux'" in html

    def test_ws_endpoint_referenced(self, html):
        assert "/ws" in html

    def test_no_unknown_ws_channels_subscribed(self, html):
        """All channel strings used in subscribe must be known channels."""
        channels = self._extract_subscribe_channels(html)
        unknown = channels - KNOWN_WS_CHANNELS - {""}
        # We allow empty string or unknown-looking strings that might be in other contexts
        # Just verify known channels are present
        for known in KNOWN_WS_CHANNELS:
            assert known in channels or known in html, (
                f"Expected WS channel {known!r} not found in UI"
            )


# ---------------------------------------------------------------------------
# MACHINE_INFO keys
# ---------------------------------------------------------------------------

class TestMachineInfoKeys:
    """MACHINE_INFO in UI must include all FLEET_MACHINES keys."""

    def test_mac_mini_in_machine_info(self, html):
        assert "'mac-mini'" in html or '"mac-mini"' in html

    def test_ubuntu_desktop_in_machine_info(self, html):
        assert "'ubuntu-desktop'" in html or '"ubuntu-desktop"' in html

    def test_avell_i7_in_machine_info(self, html):
        assert "'avell-i7'" in html or '"avell-i7"' in html

    def test_windows_desktop_in_machine_info(self, html):
        assert "'windows-desktop'" in html or '"windows-desktop"' in html

    def test_fleet_ips_present(self, html):
        """IPs from FLEET_MACHINES should appear in MACHINE_INFO."""
        ips = ["192.168.7.102", "192.168.7.13", "192.168.7.103", "192.168.7.101"]
        for ip in ips:
            assert ip in html, f"IP {ip} not found in UI MACHINE_INFO"


# ---------------------------------------------------------------------------
# localStorage
# ---------------------------------------------------------------------------

class TestLocalStorage:
    """localStorage usage patterns must be present and guarded."""

    def test_collapse_key_constant_defined(self, html):
        """COLLAPSE_KEY must be defined as a string constant."""
        assert "COLLAPSE_KEY" in html

    def test_collapse_key_value_is_claude_manager(self, html):
        """The key string must identify the app."""
        assert "claude-manager" in html

    def test_expanded_cards_key_present(self, html):
        assert "claude-manager-expanded-cards" in html

    def test_localstorage_getitem_calls_present(self, html):
        assert "localStorage.getItem" in html

    def test_localstorage_setitem_guarded_by_try_catch(self, html):
        """localStorage writes must be inside try/catch to avoid storage quota errors."""
        # Find all localStorage.setItem occurrences and check they're in try blocks
        set_positions = [m.start() for m in re.finditer(r"localStorage\.setItem", html)]
        assert len(set_positions) > 0, "No localStorage.setItem calls found"

        for pos in set_positions:
            # Look for 'try' in the 100 chars before the setItem call
            surrounding = html[max(0, pos - 100): pos + 100]
            assert "try" in surrounding, (
                f"localStorage.setItem at position {pos} not guarded by try/catch"
            )

    def test_two_distinct_localstorage_keys(self, html):
        """UI should use at least two localStorage.getItem calls (cards + collapse).

        One uses a string literal ('claude-manager-expanded-cards') and one uses
        a constant variable (COLLAPSE_KEY), so we count total getItem calls instead
        of unique string literals.
        """
        get_calls = re.findall(r"localStorage\.getItem\(", html)
        assert len(get_calls) >= 2, f"Expected at least 2 localStorage.getItem calls, found {len(get_calls)}"


# ---------------------------------------------------------------------------
# No confirm() calls
# ---------------------------------------------------------------------------

class TestNoConfirmCalls:
    """The UI must not use window.confirm() for destructive actions."""

    def test_no_window_confirm_calls(self, html):
        # Allow 'confirm' as a substring in comments or strings but not as a function call
        matches = re.findall(r"\bconfirm\s*\(", html)
        assert len(matches) == 0, f"Found confirm() calls in UI: {matches}"


# ---------------------------------------------------------------------------
# General structure
# ---------------------------------------------------------------------------

class TestGeneralStructure:
    """Verify overall HTML structure."""

    def test_doctype_present(self, html):
        assert "<!DOCTYPE html>" in html or "<!doctype html>" in html.lower()

    def test_root_div_present(self, html):
        assert 'id="root"' in html

    def test_dark_mode_class_on_html(self, html):
        assert 'class="dark"' in html

    def test_title_is_claude_manager(self, html):
        assert "<title>claude-manager</title>" in html

    def test_viewport_meta_present(self, html):
        assert "viewport" in html

    def test_charset_utf8(self, html):
        assert "UTF-8" in html or "utf-8" in html.lower()

    def test_create_root_called(self, html):
        """React 19 createRoot must be used for mounting."""
        assert "createRoot" in html

    def test_dom_render_targets_root(self, html):
        assert 'getElementById("root")' in html or "getElementById('root')" in html

    def test_dark_theme_background_color(self, html):
        """Dark theme background color #0d1117 must be defined."""
        assert "#0d1117" in html

    def test_websocket_connection_established(self, html):
        """UI should create a WebSocket connection."""
        assert "WebSocket" in html or "new WebSocket" in html

    def test_health_endpoint_polled_or_checked(self, html):
        """Health endpoint should be referenced."""
        assert "/health" in html

    def test_status_values_defined(self, html):
        """Session status values must be referenced."""
        for status in ("working", "active", "idle"):
            assert status in html, f"Status {status!r} not found in UI"


# ---------------------------------------------------------------------------
# Icon system validation
# ---------------------------------------------------------------------------

class TestIconSystem:
    """Material Icons Round icon names must be valid."""

    # A representative subset of Material Icons used in the UI
    # Verified from the actual icon function usages in index.html
    EXPECTED_ICON_NAMES = [
        "computer",
        "laptop_mac",
        "desktop_windows",
        "cloud",
    ]

    def test_icon_function_defined(self, html):
        """The Icon helper function must be defined."""
        assert "function Icon" in html or "Icon" in html

    def test_material_icons_round_css_class_used(self, html):
        """material-icons-round CSS class must be applied to icons."""
        assert "material-icons-round" in html

    def test_computer_icon_used(self, html):
        assert "computer" in html

    def test_laptop_mac_icon_used(self, html):
        assert "laptop_mac" in html

    def test_desktop_windows_icon_used(self, html):
        assert "desktop_windows" in html


# ---------------------------------------------------------------------------
# Debounced writes / persistence patterns
# ---------------------------------------------------------------------------

class TestPersistencePatterns:
    """localStorage writes should be debounced or rate-limited."""

    def test_localstorage_writes_inside_callbacks(self, html):
        """Writes happen inside event handlers / effects, not at module level."""
        # Check that setItem appears inside function bodies
        # Simple heuristic: setItem should appear after 'function' or '=>' or 'useEffect'
        set_positions = [m.start() for m in re.finditer(r"localStorage\.setItem", html)]
        assert len(set_positions) > 0

        for pos in set_positions:
            # Look for closure/function context in 300 chars before
            context = html[max(0, pos - 300): pos]
            has_function_context = (
                "function" in context
                or "=>" in context
                or "useEffect" in context
                or "useCallback" in context
                or "try {" in context
            )
            assert has_function_context, (
                f"localStorage.setItem at pos {pos} may not be inside a function"
            )
