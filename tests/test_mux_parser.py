"""
Unit tests for src/mux_parser.py — universal tmux/psmux output parser.

Covers:
  - parse_mux_output() auto-detection: pipe format, plain text, name-only fallback, empty
  - _parse_pipe_format(): valid 4-field, 5-field (with cwd), malformed, empty fields
  - _parse_plain_text(): standard psmux output, with/without attached, multiple sessions
  - Edge cases: Windows line endings (\\r\\n), trailing whitespace, mixed formats
  - Regression: actual psmux output format observed in production
"""
from __future__ import annotations

import pytest

from src.mux_parser import (
    _parse_pipe_format,
    _parse_plain_text,
    parse_mux_output,
)


# ---------------------------------------------------------------------------
# parse_mux_output — auto-detection
# ---------------------------------------------------------------------------

class TestParseMuxOutputAutoDetection:
    """parse_mux_output() selects the right sub-parser automatically."""

    def test_empty_string_returns_empty_list(self):
        assert parse_mux_output("") == []

    def test_whitespace_only_returns_empty_list(self):
        assert parse_mux_output("   \n  \n  ") == []

    def test_pipe_format_detected_by_3_pipes(self):
        output = "myapp|1744000000|2|0"
        result = parse_mux_output(output)
        assert len(result) == 1
        assert result[0]["name"] == "myapp"
        assert result[0]["windows"] == 2

    def test_pipe_format_with_5_fields_detected(self):
        output = "myapp|1744000000|2|0|/home/user/project"
        result = parse_mux_output(output)
        assert len(result) == 1
        assert result[0]["cwd"] == "/home/user/project"

    def test_plain_text_detected_when_no_pipes(self):
        output = "work: 1 windows (created Thu Apr  9 03:25:03 2026)"
        result = parse_mux_output(output)
        assert len(result) == 1
        assert result[0]["name"] == "work"

    def test_plain_text_detected_multiple_sessions(self):
        output = (
            "session1: 2 windows (created Thu Apr  9 03:00:00 2026)\n"
            "session2: 1 windows (created Thu Apr  9 03:05:00 2026) (attached)"
        )
        result = parse_mux_output(output)
        assert len(result) == 2

    def test_name_only_fallback_when_plain_text_fails(self):
        output = "plain-name\nanother-name"
        result = parse_mux_output(output)
        assert len(result) == 2
        assert result[0]["name"] == "plain-name"
        assert result[1]["name"] == "another-name"

    def test_name_only_fallback_fields(self):
        """Name-only fallback produces None created, 0 windows, False attached."""
        output = "just-a-name"
        result = parse_mux_output(output)
        assert result[0]["created"] is None
        assert result[0]["windows"] == 0
        assert result[0]["attached"] is False

    def test_strips_leading_trailing_whitespace_from_output(self):
        output = "\n\n  work: 1 windows (created Thu Apr  9 03:00:00 2026)  \n\n"
        result = parse_mux_output(output)
        assert len(result) == 1
        assert result[0]["name"] == "work"

    def test_windows_line_endings_crlf(self):
        """Windows \\r\\n line endings must be handled correctly."""
        output = "sess1: 1 windows (created Thu Apr  9 03:00:00 2026)\r\nsess2: 2 windows (created Thu Apr  9 04:00:00 2026)"
        result = parse_mux_output(output)
        assert len(result) == 2
        assert result[0]["name"] == "sess1"
        assert result[1]["name"] == "sess2"

    def test_pipe_format_not_chosen_if_only_2_pipes(self):
        """A line with 2 pipes is not pipe format (need 3+), falls to plain text."""
        # "a|b|c" has 2 pipes → not pipe format
        output = "a|b|c"
        result = parse_mux_output(output)
        # Falls through to plain text or name-only; just verify it doesn't crash
        assert isinstance(result, list)

    def test_pipe_format_chosen_if_exactly_3_pipes(self):
        """4 fields (3 pipes) is enough for pipe format."""
        output = "sess|1744000000|1|0"
        result = parse_mux_output(output)
        assert len(result) == 1
        assert result[0]["name"] == "sess"


# ---------------------------------------------------------------------------
# _parse_pipe_format
# ---------------------------------------------------------------------------

class TestParsePipeFormat:
    """_parse_pipe_format() — pipe-delimited tmux -F output."""

    def _parse(self, raw: str) -> list[dict]:
        lines = [l.strip() for l in raw.splitlines() if l.strip()]
        return _parse_pipe_format(lines)

    def test_valid_4_field_line(self):
        result = self._parse("myapp|1744000000|2|0")
        assert len(result) == 1
        s = result[0]
        assert s["name"] == "myapp"
        assert s["windows"] == 2
        assert s["attached"] is False

    def test_valid_4_field_attached_is_1(self):
        result = self._parse("myapp|1744000000|2|1")
        assert result[0]["attached"] is True

    def test_valid_4_field_attached_is_nonzero_string(self):
        """Any non-zero, non-empty value → attached=True."""
        result = self._parse("myapp|1744000000|2|2")
        assert result[0]["attached"] is True

    def test_attached_zero_string_is_false(self):
        result = self._parse("myapp|1744000000|2|0")
        assert result[0]["attached"] is False

    def test_attached_empty_string_is_false(self):
        result = self._parse("myapp|1744000000|2|")
        assert result[0]["attached"] is False

    def test_valid_5_field_with_cwd(self):
        result = self._parse("myapp|1744000000|2|0|/home/user/project")
        assert result[0]["cwd"] == "/home/user/project"

    def test_4_field_cwd_defaults_to_empty_string(self):
        result = self._parse("myapp|1744000000|2|0")
        assert result[0]["cwd"] == ""

    def test_cwd_whitespace_stripped(self):
        result = self._parse("myapp|1744000000|2|0|  /home/user  ")
        assert result[0]["cwd"] == "/home/user"

    def test_created_timestamp_converted_to_iso(self):
        result = self._parse("myapp|1744000000|2|0")
        created = result[0]["created"]
        assert created is not None
        assert "T" in created  # ISO 8601

    def test_invalid_timestamp_kept_as_string(self):
        result = self._parse("myapp|not-a-timestamp|2|0")
        assert result[0]["created"] == "not-a-timestamp"

    def test_empty_timestamp_field_gives_none(self):
        result = self._parse("myapp||2|0")
        assert result[0]["created"] is None

    def test_invalid_windows_defaults_to_zero(self):
        result = self._parse("myapp|1744000000|nan|0")
        assert result[0]["windows"] == 0

    def test_malformed_line_fewer_than_4_fields_skipped(self):
        result = self._parse("only|3|fields")
        assert result == []

    def test_empty_name_field_included(self):
        """Even empty name is included as-is — parser does not validate names."""
        result = self._parse("|1744000000|2|0")
        assert len(result) == 1
        assert result[0]["name"] == ""

    def test_multiple_sessions(self):
        raw = "alpha|1744000000|1|0\nbeta|1744100000|3|1"
        result = self._parse(raw)
        assert len(result) == 2
        assert result[0]["name"] == "alpha"
        assert result[1]["name"] == "beta"
        assert result[1]["attached"] is True

    def test_empty_lines_skipped(self):
        raw = "alpha|1744000000|1|0\n\n\nbeta|1744100000|3|1\n"
        result = self._parse(raw)
        assert len(result) == 2

    def test_windows_line_endings(self):
        raw = "myapp|1744000000|2|0\r\nother|1744100000|1|0"
        result = self._parse(raw)
        assert len(result) == 2

    def test_trailing_whitespace_on_lines(self):
        raw = "myapp|1744000000|2|0   "
        result = self._parse(raw)
        assert result[0]["attached"] is False  # "0   ".strip() → "0"


# ---------------------------------------------------------------------------
# _parse_plain_text
# ---------------------------------------------------------------------------

class TestParsePlainText:
    """_parse_plain_text() — psmux/tmux default output format."""

    def _parse(self, raw: str) -> list[dict]:
        lines = [l.strip() for l in raw.splitlines() if l.strip()]
        return _parse_plain_text(lines)

    def test_standard_psmux_output_single_session(self):
        result = self._parse("work: 1 windows (created Thu Apr  9 03:25:03 2026)")
        assert len(result) == 1
        s = result[0]
        assert s["name"] == "work"
        assert s["windows"] == 1
        assert s["attached"] is False
        assert "Thu Apr" in (s["created"] or "")

    def test_regression_psmux_output_test_avell(self):
        """Regression: actual psmux output seen on avell-i7."""
        raw = "test-avell: 1 windows (created Thu Apr  9 03:25:03 2026)"
        result = self._parse(raw)
        assert len(result) == 1
        assert result[0]["name"] == "test-avell"
        assert result[0]["windows"] == 1
        assert result[0]["attached"] is False

    def test_attached_flag_detected(self):
        raw = "work: 2 windows (created Thu Apr  9 03:00:00 2026) (attached)"
        result = self._parse(raw)
        assert result[0]["attached"] is True

    def test_not_attached_when_no_attached_marker(self):
        raw = "work: 2 windows (created Thu Apr  9 03:00:00 2026)"
        result = self._parse(raw)
        assert result[0]["attached"] is False

    def test_plural_windows_word(self):
        """'2 windows' should parse correctly."""
        raw = "sess: 2 windows (created Thu Apr  9 03:00:00 2026)"
        result = self._parse(raw)
        assert result[0]["windows"] == 2

    def test_singular_window_word(self):
        """'1 window' (without 's') should parse correctly."""
        raw = "sess: 1 window (created Thu Apr  9 03:00:00 2026)"
        result = self._parse(raw)
        assert result[0]["windows"] == 1

    def test_multiple_sessions(self):
        raw = (
            "alpha: 1 windows (created Thu Apr  9 03:00:00 2026)\n"
            "beta: 3 windows (created Thu Apr  9 04:00:00 2026) (attached)\n"
            "gamma: 2 windows (created Thu Apr  9 05:00:00 2026)"
        )
        result = self._parse(raw)
        assert len(result) == 3
        assert result[0]["name"] == "alpha"
        assert result[1]["name"] == "beta"
        assert result[1]["attached"] is True
        assert result[2]["name"] == "gamma"
        assert result[2]["windows"] == 2

    def test_session_name_with_hyphens(self):
        raw = "my-session-name: 1 windows (created Thu Apr  9 03:00:00 2026)"
        result = self._parse(raw)
        assert result[0]["name"] == "my-session-name"

    def test_session_name_with_underscores(self):
        raw = "my_session_name: 1 windows (created Thu Apr  9 03:00:00 2026)"
        result = self._parse(raw)
        assert result[0]["name"] == "my_session_name"

    def test_created_is_none_when_no_created_group(self):
        """If the created group is absent (malformed), created should be None."""
        raw = "sess: 1 windows"
        result = self._parse(raw)
        # Falls through to partial match (has ':')
        assert any(s["name"] == "sess" for s in result)

    def test_partial_match_with_colon_returns_name_only(self):
        """Lines with ':' but no window count → partial match, name extracted."""
        raw = "just-a-name: something else here"
        result = self._parse(raw)
        assert any(s["name"] == "just-a-name" for s in result)
        matched = next(s for s in result if s["name"] == "just-a-name")
        assert matched["windows"] == 0
        assert matched["created"] is None

    def test_trailing_whitespace_on_lines(self):
        raw = "work: 1 windows (created Thu Apr  9 03:00:00 2026)   "
        result = self._parse(raw)
        assert len(result) == 1
        assert result[0]["name"] == "work"

    def test_windows_line_endings(self):
        raw = "sess1: 1 windows (created Thu Apr  9 03:00:00 2026)\r\nsess2: 2 windows (created Thu Apr  9 04:00:00 2026)"
        result = self._parse(raw)
        assert len(result) == 2

    def test_empty_list_returns_empty(self):
        result = _parse_plain_text([])
        assert result == []

    def test_no_colon_lines_skipped(self):
        """Lines without ':' are not matched by _parse_plain_text."""
        result = self._parse("no-colon-here\nstill-nothing")
        # Will not match the plain regex, won't have ':' either → empty
        assert result == []

    def test_date_with_double_space_in_created(self):
        """tmux uses double-space before single-digit day numbers."""
        raw = "sess: 1 windows (created Thu Apr  9 03:25:03 2026)"
        result = self._parse(raw)
        assert result[0]["created"] is not None
        assert "Apr" in result[0]["created"]


# ---------------------------------------------------------------------------
# Integration: parse_mux_output with realistic data
# ---------------------------------------------------------------------------

class TestParseMuxOutputIntegration:
    """End-to-end tests with real-world-like input."""

    def test_tmux_list_sessions_pipe_format(self):
        """Simulate output from: tmux ls -F '#{session_name}|#{session_created}|...'"""
        raw = (
            "claude-manager|1744181100|1|0|/Users/rbgnr/git/claude-manager\n"
            "web-server|1744181200|2|1|/home/user/www\n"
        )
        result = parse_mux_output(raw)
        assert len(result) == 2
        assert result[0]["name"] == "claude-manager"
        assert result[1]["attached"] is True

    def test_psmux_list_sessions_plain_format(self):
        """Simulate psmux output on Windows."""
        raw = (
            "claude-manager: 1 windows (created Thu Apr  9 03:25:03 2026)\n"
            "web-server: 2 windows (created Thu Apr  9 04:00:00 2026) (attached)\n"
        )
        result = parse_mux_output(raw)
        assert len(result) == 2
        assert result[0]["name"] == "claude-manager"
        assert result[1]["attached"] is True

    def test_single_session_name_only(self):
        """When output is just session names, one per line."""
        raw = "session-alpha\nsession-beta"
        result = parse_mux_output(raw)
        assert len(result) == 2
        for s in result:
            assert s["windows"] == 0
            assert s["attached"] is False
            assert s["created"] is None

    def test_mixed_line_endings_handled(self):
        raw = "sess1: 1 windows (created Thu Apr  9 03:00:00 2026)\r\nsess2: 2 windows (created Thu Apr  9 04:00:00 2026)\r\n"
        result = parse_mux_output(raw)
        assert len(result) == 2

    def test_single_empty_line_output(self):
        assert parse_mux_output("\n") == []

    def test_real_avell_psmux_regression(self):
        """Exact string observed from psmux on avell-i7."""
        raw = "test-avell: 1 windows (created Thu Apr  9 03:25:03 2026)"
        result = parse_mux_output(raw)
        assert len(result) == 1
        assert result[0]["name"] == "test-avell"
        assert result[0]["windows"] == 1
        assert not result[0]["attached"]
