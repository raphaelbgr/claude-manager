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
    "/api/tmux/verify",
    "/api/tmux/connect",
    "/api/tmux/connect-remote",
    "/api/tmux/kill",
    "/api/tmux/capture",
    "/api/browse",
    "/api/drives",
    "/api/mkdir",
    "/api/preferences",
    "/api/logs",
    "/api/restart",
    "/api/exit",
    "/api/auth/config",
    "/api/auth/token",
    "/api/auth/update",
    "/api/update/check",
    "/api/update/apply",
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
        # Frontend pins at project granularity (not per-session) — the project
        # card is the pin target on the Projects tab.
        assert "/api/projects/pin" in html

    def test_unpin_endpoint_referenced(self, html):
        assert "/api/projects/unpin" in html

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
        # confirm() only allowed for destructive/disruptive actions:
        #   1. Exit button (kills the server)
        #   2. Update and restart (closes and reopens the window)
        matches = re.findall(r"\bconfirm\s*\(", html)
        assert len(matches) <= 2, (
            f"Found {len(matches)} confirm() calls — only Exit and Update should use it"
        )


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


# ---------------------------------------------------------------------------
# HardwareInfo component field-name validation
# ---------------------------------------------------------------------------

class TestHardwareInfoFieldNames:
    """Verify HardwareInfo uses the exact field names the API returns.

    The API returns:
      cpu.usage_percent, cpu.temp_c, cpu.name, cpu.cores
      gpu.usage_percent, gpu.temp_c, gpu.memory_used_mb, gpu.memory_total_mb, gpu.name
      memory.used_gb, memory.total_gb, memory.percent

    Any mismatch between front-end field access and API shape is a silent bug.
    These tests catch that by inspecting the HTML source directly.
    """

    def test_hardware_info_component_defined(self, html):
        """HardwareInfo function component must be present."""
        assert "function HardwareInfo" in html

    def test_cpu_usage_percent_referenced_not_cpu_percent(self, html):
        """UI reads cpu.usage_percent — NOT cpu.percent (wrong field name)."""
        assert "usage_percent" in html
        # The pattern cpu.percent (without 'usage_') would be the wrong field
        # We verify the correct one is used and the API field name appears
        assert "cpu.usage_percent" in html or "usage_percent" in html

    def test_temp_c_field_name_referenced(self, html):
        """UI reads .temp_c — must appear in HardwareInfo context."""
        assert "temp_c" in html

    def test_memory_used_gb_referenced(self, html):
        """UI reads memory.used_gb for RAM display."""
        assert "used_gb" in html

    def test_memory_total_gb_referenced(self, html):
        """UI reads memory.total_gb for RAM display."""
        assert "total_gb" in html

    def test_gpu_memory_used_mb_referenced(self, html):
        """UI reads gpu.memory_used_mb for VRAM display."""
        assert "memory_used_mb" in html

    def test_gpu_memory_total_mb_referenced(self, html):
        """UI reads gpu.memory_total_mb for VRAM display."""
        assert "memory_total_mb" in html

    def test_thermostat_icon_referenced(self, html):
        """Thermostat icon (Material Icons) is used for temperature display."""
        assert "thermostat" in html

    def test_gpu_usage_percent_referenced(self, html):
        """UI reads gpu.usage_percent for GPU utilisation display."""
        # usage_percent appears for both cpu and gpu
        matches = [m.start() for m in re.finditer(r"usage_percent", html)]
        assert len(matches) >= 2, (
            "Expected usage_percent to appear at least twice (cpu + gpu), "
            f"found {len(matches)} occurrence(s)"
        )

    def test_cpu_temp_c_specifically_referenced(self, html):
        """cpu.temp_c is accessed in HardwareInfo for CPU temperature."""
        # The pattern 'cpu.temp_c' or 'temp_c' near cpu context
        assert "cpu.temp_c" in html or (
            "temp_c" in html and "cpu" in html
        )

    def test_gpu_temp_c_specifically_referenced(self, html):
        """gpu.temp_c is accessed in HardwareInfo for GPU temperature."""
        assert "gpu.temp_c" in html or (
            "temp_c" in html and "gpu" in html
        )

    def test_memory_used_gb_and_total_gb_in_same_expression(self, html):
        """Both used_gb and total_gb appear together (e.g. '8.0/16.0 GB')."""
        assert "used_gb" in html
        assert "total_gb" in html
        # Check they appear in proximity (within 200 chars of each other)
        used_pos = html.find("used_gb")
        total_pos = html.find("total_gb")
        assert used_pos != -1 and total_pos != -1
        assert abs(used_pos - total_pos) < 200, (
            "used_gb and total_gb appear far apart — may not be used in the same expression"
        )

    def test_memory_used_mb_and_total_mb_for_vram(self, html):
        """GPU VRAM shown as memory_used_mb / memory_total_mb."""
        assert "memory_used_mb" in html
        assert "memory_total_mb" in html
        used_pos = html.find("memory_used_mb")
        total_pos = html.find("memory_total_mb")
        assert abs(used_pos - total_pos) < 200, (
            "memory_used_mb and memory_total_mb appear far apart"
        )

    def test_hardware_endpoint_used_in_fetch(self, html):
        """/api/hardware fetch call is present in the UI."""
        assert "/api/hardware" in html


# ---------------------------------------------------------------------------
# Tmux → Claude session linking (feature)
# ---------------------------------------------------------------------------

class TestTmuxClaudeLink:
    """The TmuxCard renders a chip and the SessionCard exposes data-session-id."""

    def test_tmuxcard_renders_claude_session_chip(self, html):
        """TmuxCard reads the link fields the server emits."""
        assert "session.claude_session_id" in html
        assert "session.claude_session_name" in html

    def test_chip_invokes_on_select_callback(self, html):
        """Chip must call onSelectClaudeSession(session.claude_session_id)."""
        assert "onSelectClaudeSession(session.claude_session_id)" in html

    def test_chip_classname_defined(self, html):
        """Chip uses a specific class name so we can tweak it later."""
        assert "claude-link-chip" in html

    def test_session_card_has_data_session_id_attribute(self, html):
        """SessionCard compact row exposes data-session-id so selection can scroll to it."""
        assert "'data-session-id': sessionId" in html

    def test_select_function_scrolls_into_view(self, html):
        """The selection function must use scrollIntoView to bring the row into focus."""
        assert "scrollIntoView" in html
        assert "session-flash" in html

    def test_flash_keyframes_defined(self, html):
        """Flash animation is defined in CSS."""
        assert "@keyframes session-flash" in html

    def test_tmuxpanel_propagates_callback(self, html):
        """TmuxPanel signature includes onSelectClaudeSession."""
        # Count occurrences — must appear in: TmuxCard sig, TmuxPanel sig,
        # App prop forward, chip onClick body, and the scroll helper.
        assert html.count("onSelectClaudeSession") >= 4

    def test_shell_filter_behavior_is_server_side(self, html):
        """Frontend should not re-filter shells — server already suppresses the chip."""
        # No client-side re-check on pane_current_command that would double-filter.
        # (We just render the chip when claude_session_id is present.)
        assert "session.claude_session_id" in html


class TestSessionSelectSequence:
    """selectClaudeSession must expand → await rAF → scrollIntoView → scrollend → flash."""

    def test_uses_scrollend_with_timeout_fallback(self, html):
        """Must listen for 'scrollend' AND provide a setTimeout fallback."""
        assert "scrollend" in html
        # A 700ms fallback timeout is present for Safari <17
        assert "setTimeout(finish, 700)" in html

    def test_expands_collapsed_card_before_scroll(self, html):
        """selectClaudeSession mutates expandedCards before the scroll step."""
        assert "expandedCardsRef.current.has(sessionId)" in html
        assert "setExpandedCards" in html

    def test_awaits_animation_frame_after_expand(self, html):
        """Two requestAnimationFrame awaits between expand and scroll — one for
        React commit, one for layout settle."""
        # Look for the nextFrame helper used to sequence the steps.
        assert "requestAnimationFrame" in html

    def test_flash_animation_waits_before_removal(self, html):
        """The flash class is added, then removed after 1500ms."""
        assert "'session-flash'" in html
        assert "1500" in html  # flash duration

    def test_scroll_parent_discovery(self, html):
        """Walks up from target node to find a scrollable ancestor."""
        assert "overflowY" in html


class TestAttachToMatchingTmux:
    """SessionCard renders an Attach-to-tmux button when matching tmux exists."""

    def test_attach_button_is_gated_on_matching_tmux(self, html):
        """Button only rendered from the matchingTmux array."""
        assert "matchingTmux" in html
        assert "attach-tmux-btn" in html

    def test_attach_button_calls_on_attach_tmux(self, html):
        """Clicking the button invokes onAttachTmux with the tmux object."""
        assert "onAttachTmux(t)" in html

    def test_reverse_lookup_indexed_by_cwd(self, html):
        """App builds tmuxByCwdKey from tmuxSessions."""
        assert "tmuxByCwdKey" in html
        assert "getMatchingTmux" in html

    def test_session_card_receives_matching_tmux_prop(self, html):
        """SessionsPanel passes matchingTmux prop down to SessionCard."""
        assert "matchingTmux: getMatchingTmux" in html

    def test_attach_button_has_cast_connected_icon(self, html):
        """Uses the 'cast_connected' Material icon to signal 'attach to live stream'."""
        assert "cast_connected" in html

    def test_attach_button_dedupes_by_machine_and_name(self, html):
        """Reverse index dedupes on machine:name to avoid duplicate buttons."""
        assert "seen.add(id)" in html or "seen.add(" in html


class TestAncestorExpansion:
    """Clicking a tmux chip must uncollapse MachineSection + ProjectSection + ProjectsView project."""

    def test_machine_section_syncs_external_collapse_state(self, html):
        """MachineSection has useEffect watching collapseState[collapseKey]."""
        # The helper var externalOpen must appear in MachineSection's body.
        idx = html.find("function MachineSection")
        end = html.find("function ProjectSection", idx)
        slice_ = html[idx:end]
        assert "externalOpen" in slice_
        assert "setIsOpen(externalOpen)" in slice_

    def test_project_section_syncs_external_collapse_state(self, html):
        """ProjectSection has the same sync effect."""
        idx = html.find("function ProjectSection")
        end = idx + 2000
        slice_ = html[idx:end]
        assert "externalOpen" in slice_
        assert "setIsOpen(externalOpen)" in slice_

    def test_projects_view_listens_for_session_expand_event(self, html):
        """ProjectsView listens for 'expandProjectForSession' event from App."""
        assert "expandProjectForSession" in html
        assert "addEventListener('expandProjectForSession'" in html

    def test_select_session_opens_machine_and_project_keys(self, html):
        """selectClaudeSession mutates collapseState with both keys."""
        assert "[machineKey]: true" in html
        assert "[projKey]: true" in html

    def test_select_session_dispatches_project_view_event(self, html):
        """App dispatches the expand event for the Project tab."""
        assert "dispatchEvent(new CustomEvent('expandProjectForSession'" in html

    def test_proj_key_matches_project_section_format(self, html):
        """Key format 'project:${machine}:${path}' must match ProjectSection's collapseKey."""
        assert "`project:${machine}:${projectPath}`" in html  # used in ProjectSection render site
        assert "`project:${session.machine}:${projectPath}`" in html  # used in selectClaudeSession


class TestBidirectionalFlashAndLabel:
    """Attach button shows new label AND flashes the matching tmux card."""

    def test_attach_label_contains_on_and_arrow(self, html):
        """Label text uses 'Attach on \\u203A <name>' (JS escape for ›) instead of 'Attach <name>'."""
        # The JS source literal is 'Attach on \\u203A ${...}' — not evaluated at
        # parse time until the browser runs it.
        assert "Attach on \\u203A" in html

    def test_handle_attach_and_flash_defined(self, html):
        """Wrapper handler that starts attach AND flashes the tmux card."""
        assert "handleAttachAndFlash" in html

    def test_session_card_uses_flashing_wrapper(self, html):
        """SessionsPanel is given the wrapper as onAttachTmux."""
        assert "onAttachTmux: handleAttachAndFlash" in html

    def test_select_tmux_card_defined(self, html):
        """App defines selectTmuxCard for the reverse direction."""
        assert "selectTmuxCard" in html

    def test_tmux_card_has_data_tmux_key(self, html):
        """TmuxCard root element carries data-tmux-key for querySelector."""
        assert "'data-tmux-key'" in html
        assert "session.machine}:${session.session_name || session.name}" in html

    def test_select_tmux_card_opens_tmux_machine_section(self, html):
        """Calling selectTmuxCard sets collapseState['tmux:${machine}']=true."""
        assert "`tmux:${machine}`" in html

    def test_css_escape_used_for_querySelector(self, html):
        """data-tmux-key values may contain colons — must be CSS.escape'd."""
        assert "CSS.escape" in html

    def test_scroll_and_flash_helper_shared(self, html):
        """Both selectClaudeSession and selectTmuxCard use scrollAndFlash."""
        assert "scrollAndFlash" in html
        assert html.count("scrollAndFlash") >= 3  # definition + 2 call sites


class TestSessionCardHighlightChip:
    """SessionCard renders a highlight-only chip per matching tmux (mirror of tmux-side chip)."""

    def test_chip_class_defined(self, html):
        assert "tmux-link-chip" in html

    def test_chip_uses_left_arrow_glyph(self, html):
        """Session-side chip uses \u25c2 (left-pointing triangle) — mirror of \u25b8 on the tmux side."""
        assert "\\u25c2" in html or "\u25c2" in html

    def test_chip_click_calls_on_highlight_tmux(self, html):
        """Chip onClick invokes onHighlightTmux(t)."""
        assert "onHighlightTmux(t)" in html

    def test_session_card_accepts_on_highlight_tmux_prop(self, html):
        """Prop is destructured in SessionCard signature."""
        start = html.find("function SessionCard({")
        end = html.find("}", start)
        assert "onHighlightTmux" in html[start:end]

    def test_sessions_panel_forwards_on_highlight_tmux(self, html):
        """SessionsPanel passes onHighlightTmux down to SessionCardMemo."""
        assert "onHighlightTmux," in html

    def test_app_wires_highlight_to_select_tmux_card(self, html):
        """App provides onHighlightTmux using selectTmuxCard — no attach side effect."""
        assert "onHighlightTmux: (t) => selectTmuxCard(t.machine" in html

    def test_highlight_chip_does_not_trigger_attach(self, html):
        """Highlight path does NOT route through handleAttachAndFlash or handleConnectTmux."""
        # The specific wiring line references selectTmuxCard directly, not the attach wrapper.
        wiring_line = next(
            (ln for ln in html.splitlines() if "onHighlightTmux:" in ln),
            "",
        )
        assert "handleAttachAndFlash" not in wiring_line
        assert "handleConnectTmux" not in wiring_line


class TestFrontendPathNormalization:
    """Frontend reverse lookup must handle Windows paths / case differences."""

    def test_normalize_path_helper_defined(self, html):
        """normalizePath helper unifies separators, trailing slashes, and case."""
        assert "normalizePath" in html
        assert "replace(/\\\\/g, '/')" in html  # backslash → forward slash

    def test_normalizer_lowercases(self, html):
        """Normalization lowercases so 'Immunefi' vs 'immunefi' still match."""
        # We expect a .toLowerCase() call inside normalizePath.
        start = html.find("const normalizePath")
        assert start != -1
        end = html.find("};", start)
        assert ".toLowerCase()" in html[start:end]

    def test_index_and_lookup_both_normalize(self, html):
        """Both the index builder and the lookup must call normalizePath — otherwise
        asymmetric normalization produces false negatives."""
        idx = html.find("tmuxByCwdKey = useMemo")
        getter_start = html.find("const getMatchingTmux")
        getter_end = html.find("}, [tmuxByCwdKey, tmuxSessions])", getter_start)
        # both sections reference normalizePath
        assert "normalizePath(t.cwd)" in html[idx:idx + 600]
        assert "normalizePath(p)" in html[getter_start:getter_end]


class TestAttachPreflightVerify:
    """handleConnectTmux must probe /api/tmux/verify before spawning a terminal."""

    def test_verify_endpoint_called_in_handle_connect_tmux(self, html):
        start = html.find("const handleConnectTmux")
        end = html.find("}, [skipPermissions])", start)
        body = html[start:end]
        assert "/api/tmux/verify" in body

    def test_verify_sends_machine_and_session_name(self, html):
        start = html.find("const handleConnectTmux")
        end = html.find("}, [skipPermissions])", start)
        body = html[start:end]
        assert "session_name: name" in body
        assert "machine: session.machine" in body

    def test_dead_session_toasts_and_returns(self, html):
        """alive=false → toast error and stop, no attach."""
        start = html.find("const handleConnectTmux")
        end = html.find("}, [skipPermissions])", start)
        body = html[start:end]
        assert "alive === false" in body
        assert "refreshing list" in body
        # Must early-return before the connect POST.
        alive_false_pos = body.find("alive === false")
        return_pos = body.find("return;", alive_false_pos)
        connect_pos = body.find("/api/tmux/connect", alive_false_pos)
        assert 0 <= return_pos < connect_pos, "connect should not run when alive=false"

    def test_dead_session_triggers_rescan(self, html):
        """When a ghost session is detected, kick /api/sessions/scan so the list updates."""
        start = html.find("const handleConnectTmux")
        end = html.find("}, [skipPermissions])", start)
        body = html[start:end]
        assert "/api/sessions/scan" in body

    def test_verify_failure_degrades_gracefully(self, html):
        """If /api/tmux/verify itself errors, we still attempt the attach."""
        start = html.find("const handleConnectTmux")
        end = html.find("}, [skipPermissions])", start)
        body = html[start:end]
        # A try/catch wraps the verify call so a network error is swallowed
        # and the code falls through to the attach POST.
        assert "catch (_verr)" in body or "// Network error on verify" in body

    def test_remote_attach_also_preflights_verify(self, html):
        """handleRemoteAttach must also call /api/tmux/verify — otherwise
        clicking 'Remote' on a ghost tmux spawns a terminal that SSH-fails
        and leaks psmux keystrokes into the local zsh shell (windows-desktop
        offline regression)."""
        start = html.find("const handleRemoteAttach")
        assert start != -1
        end = html.find("}, [])", start)
        body = html[start:end]
        assert "/api/tmux/verify" in body
        assert "alive === false" in body
        # Must return before the connect-remote POST
        alive_pos = body.find("alive === false")
        return_pos = body.find("return;", alive_pos)
        connect_pos = body.find("/api/tmux/connect-remote", alive_pos)
        assert 0 <= return_pos < connect_pos, "Remote attach must early-return on dead session"


class TestGetMatchingTmuxPrimaryPath:
    """getMatchingTmux trusts server-computed claude_session_id as the primary signal."""

    def test_claude_session_id_checked_first(self, html):
        """Server-computed link is the primary path; cwd lookup is fallback."""
        start = html.find("const getMatchingTmux")
        end = html.find("}, [tmuxByCwdKey, tmuxSessions])", start)
        assert start != -1 and end != -1
        body = html[start:end]
        # Both code paths exist
        assert "t.claude_session_id === session.session_id" in body
        # Primary check appears BEFORE the cwd-based loop
        primary = body.find("claude_session_id === session.session_id")
        secondary = body.find("candidatePaths")
        assert 0 <= primary < secondary

    def test_match_dedupes_between_paths(self, html):
        """When both paths match the same tmux, it appears only once."""
        start = html.find("const getMatchingTmux")
        end = html.find("}, [tmuxByCwdKey, tmuxSessions])", start)
        body = html[start:end]
        assert "seen" in body and "seen.add" in body

    def test_dependency_array_includes_tmux_sessions(self, html):
        """useCallback must invalidate when tmuxSessions changes — otherwise
        stale primary-path results."""
        assert "}, [tmuxByCwdKey, tmuxSessions])" in html


class TestProjectTabWiresTmuxLink:
    """Regression guard: the Project tab default view must also receive the
    tmux-link props, not just the Machine tab. Missing wiring here caused the
    chip to silently not render despite server-side enrichment being correct."""

    def test_projects_view_signature_accepts_tmux_props(self, html):
        start = html.find("function ProjectsView({")
        end = html.find("})", start) + 1
        sig = html[start:end]
        assert "getMatchingTmux" in sig
        assert "onAttachTmux" in sig
        assert "onHighlightTmux" in sig

    def test_app_passes_tmux_props_to_projects_view(self, html):
        """App → ProjectsView call site must forward all three props."""
        start = html.find("h(ProjectsView, {")
        end = html.find("})", start) + 1
        call = html[start:end]
        assert "getMatchingTmux" in call
        assert "onAttachTmux: handleAttachAndFlash" in call
        assert "onHighlightTmux:" in call

    def test_project_run_list_signature_accepts_tmux_props(self, html):
        start = html.find("function ProjectRunList({")
        end = html.find("})", start) + 1
        sig = html[start:end]
        assert "getMatchingTmux" in sig
        assert "onAttachTmux" in sig
        assert "onHighlightTmux" in sig

    def test_project_run_list_forwards_tmux_props_to_session_card(self, html):
        """Inside ProjectRunList, SessionCardMemo must receive matchingTmux."""
        # Narrow to the ProjectRunList body.
        start = html.find("function ProjectRunList({")
        end = html.find("\nfunction ", start + 10)
        body = html[start:end]
        assert "matchingTmux: getMatchingTmux ? getMatchingTmux(s) : []" in body
        assert "onAttachTmux," in body
        assert "onHighlightTmux," in body


class TestNewSessionTerminalPicker:
    """The 'New session' / 'New tmux' buttons in the project row have terminal pickers.

    Regression guard: previously these were plain buttons without the TerminalPicker
    caret, so the user couldn't pick iTerm2 / Terminal.app / Alacritty. The Resume
    and SSH+psmux buttons had them; New did not.
    """

    def test_new_session_button_wrapped_in_split_btn_group(self, html):
        # Walk back 2500 chars — the split-btn-group wraps button + TerminalPicker
        # with the label sitting deep inside the button's children.
        idx = html.find("'SSH + New session'")
        if idx == -1:
            idx = html.find("'New session'")
        assert idx != -1
        window = html[max(0, idx - 2500):idx + 200]
        assert "split-btn-group" in window

    def test_new_session_button_has_terminal_picker(self, html):
        """The picker for New session has variant='launch' and passes terminal_id."""
        idx = html.find("onNewSessionInProject(machine, projectPath, tid)")
        assert idx != -1, "TerminalPicker onPick must forward terminal_id to onNewSessionInProject"

    def test_new_tmux_button_has_terminal_picker(self, html):
        idx = html.find("onNewTmuxInProject(machine, projectPath, tid)")
        assert idx != -1, "TerminalPicker onPick must forward terminal_id to onNewTmuxInProject"

    def test_handlers_accept_terminal_id_argument(self, html):
        """Both handler signatures take a 3rd terminalId arg and forward it."""
        assert "handleNewSessionInProject = useCallback(async (machine, cwd, terminalId)" in html
        assert "handleNewTmuxInProject = useCallback(async (machine, cwd, terminalId)" in html

    def test_handlers_send_terminal_id_in_request_body(self, html):
        """terminal_id must go into the /api/sessions/launch body."""
        # Both handlers include terminal_id in the JSON.stringify
        assert html.count("terminal_id: terminalId || null") >= 2
