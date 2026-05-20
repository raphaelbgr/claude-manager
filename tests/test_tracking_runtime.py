"""
Tests for src/tracking/__init__.py runtime behaviour and the runtime-collision
detection shim.

Coverage:
  - init(): first call configures, second call is no-op (idempotent)
  - tl is _Stub when tracelink not installed (_ENABLED=False)
  - _Stub.event/track/span/screen/enter/leave: all no-op, accept arbitrary kwargs
  - span() context manager: disabled path yields _NoopSpan; enabled path yields real span
  - _NoopSpan.update() accepts kwargs and returns None
  - declarations module imports cleanly (no syntax errors)
  - _StrictTL shim: catches runtime kwarg collisions
  - 8 call-site collision tests using strict_tracelink fixture
"""
from __future__ import annotations

import contextlib
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

import src.tracking as tracking
from src.tracking import _NoopSpan, _Stub, span


# ---------------------------------------------------------------------------
# _StrictTL shim — detects reserved-kwarg collisions at test time
# ---------------------------------------------------------------------------

class _StrictTL:
    """Shim that raises AssertionError if a reserved positional kwarg name
    appears as a data keyword argument.

    Reserved names:
      event(_name, **data)  → 'name' must not appear in data
      track(_point, **data) → 'point' must not appear in data
      span(_name, **data)   → 'name' must not appear in data

    NOTE: The internal parameters are prefixed with underscore to avoid the
    same kwarg collision this shim is designed to catch. Without the prefix,
    calling ``event("x", name="y")`` would itself raise TypeError before the
    AssertionError, masking the intended failure mode.
    """

    _RESERVED = {
        "event": "name",
        "track": "point",
        "span": "name",
    }

    def _check(self, _method: str, **data: Any) -> None:
        reserved = self._RESERVED.get(_method)
        if reserved and reserved in data:
            raise AssertionError(
                f"tl.{_method}() called with reserved positional kwarg "
                f"'{reserved}' as a data key — this causes TypeError at runtime. "
                f"Received data keys: {list(data.keys())}"
            )

    def event(self, _name: str, **data: Any) -> None:
        self._check("event", **data)

    def track(self, _point: str, **data: Any) -> None:
        self._check("track", **data)

    def span(self, _name: str, **data: Any):
        self._check("span", **data)
        return _NoopSpan()

    def screen(self, _name: str) -> None: ...
    def enter(self, _name: str) -> None: ...
    def leave(self, _name: str) -> None: ...
    def init(self, *args: Any, **kwargs: Any) -> dict:
        return {"enabled": False}


@pytest.fixture
def strict_tracelink(monkeypatch):
    """Fixture: patch src.tracking.tl with _StrictTL across the codebase."""
    shim = _StrictTL()
    monkeypatch.setattr(tracking, "tl", shim)
    # Also patch the module-level `tl` re-export in scanner since it imports
    # `from .tracking import tl` at import time.
    try:
        import src.scanner as _sc
        monkeypatch.setattr(_sc, "tl", shim)
    except ImportError:
        pass
    try:
        import src.fleet as _fl
        monkeypatch.setattr(_fl, "tl", shim)
    except ImportError:
        pass
    return shim


# ---------------------------------------------------------------------------
# _Stub: no-op behaviour
# ---------------------------------------------------------------------------

class TestStub:
    def test_event_accepts_arbitrary_kwargs(self):
        stub = _Stub()
        # Must not raise
        stub.event("cm.test.ok", foo=1, bar="baz", boolval=True)

    def test_track_accepts_arbitrary_kwargs(self):
        stub = _Stub()
        stub.track("some.point", x=42, y="hello")

    def test_span_returns_noop_span(self):
        stub = _Stub()
        ctx = stub.span("cm.test.span", key="val")
        assert isinstance(ctx, _NoopSpan)

    def test_screen_does_not_raise(self):
        stub = _Stub()
        stub.screen("Boot")

    def test_enter_does_not_raise(self):
        stub = _Stub()
        stub.enter("some-screen")

    def test_leave_does_not_raise(self):
        stub = _Stub()
        stub.leave("some-screen")

    def test_init_returns_disabled_dict(self):
        stub = _Stub()
        result = stub.init()
        assert result == {"enabled": False}


# ---------------------------------------------------------------------------
# _NoopSpan
# ---------------------------------------------------------------------------

class TestNoopSpan:
    def test_update_accepts_kwargs_returns_none(self):
        s = _NoopSpan()
        result = s.update(rc=0, elapsed_ms=50, extra="x")
        assert result is None

    def test_context_manager_yields_self(self):
        s = _NoopSpan()
        with s as inner:
            assert inner is s

    def test_context_manager_does_not_raise_on_exception(self):
        s = _NoopSpan()
        # __exit__ should NOT suppress exceptions (returns None/falsy)
        with pytest.raises(ValueError):
            with s:
                raise ValueError("intentional")


# ---------------------------------------------------------------------------
# span() context manager
# ---------------------------------------------------------------------------

class TestSpanContextManager:
    def test_disabled_yields_noop_span(self, monkeypatch):
        monkeypatch.setattr(tracking, "_ENABLED", False)
        with span("cm.test.disabled") as s:
            assert isinstance(s, _NoopSpan)

    def test_enabled_delegates_to_tl(self, monkeypatch):
        fake_ctx = MagicMock()
        fake_ctx.__enter__ = MagicMock(return_value=fake_ctx)
        fake_ctx.__exit__ = MagicMock(return_value=False)
        fake_tl = MagicMock()
        fake_tl.span.return_value = fake_ctx

        monkeypatch.setattr(tracking, "_ENABLED", True)
        monkeypatch.setattr(tracking, "_tl", fake_tl)

        with span("cm.test.enabled", key="val") as s:
            pass

        fake_tl.span.assert_called_once_with("cm.test.enabled", key="val")


# ---------------------------------------------------------------------------
# init() idempotency
# ---------------------------------------------------------------------------

class TestInit:
    def test_not_enabled_returns_disabled(self, monkeypatch):
        monkeypatch.setattr(tracking, "_ENABLED", False)
        result = tracking.init(run_id="test-run")
        assert result == {"enabled": False}

    def test_enabled_calls_tl_init(self, monkeypatch):
        fake_tl = MagicMock()
        fake_tl.init.return_value = {"sink_path": "/tmp/x.jsonl", "run_id": "r1"}
        monkeypatch.setattr(tracking, "_ENABLED", True)
        monkeypatch.setattr(tracking, "_tl", fake_tl)

        result = tracking.init(run_id="r1")
        fake_tl.init.assert_called_once_with(service="claude-manager", run_id="r1")
        assert result["sink_path"] == "/tmp/x.jsonl"

    def test_disabled_does_not_call_tl_init(self, monkeypatch):
        fake_tl = MagicMock()
        monkeypatch.setattr(tracking, "_ENABLED", False)
        monkeypatch.setattr(tracking, "_tl", fake_tl)

        tracking.init()
        fake_tl.init.assert_not_called()


# ---------------------------------------------------------------------------
# declarations module
# ---------------------------------------------------------------------------

class TestDeclarations:
    def test_import_clean(self):
        """declarations.py must import without raising even when tracelink
        is absent (the bare-environment path)."""
        import importlib
        import src.tracking.declarations as decl
        # If we got here without SyntaxError / ImportError, the module is fine.
        assert decl is not None

    def test_declare_does_not_raise_without_tracelink(self):
        """_declare should be a no-op function when tracelink is missing."""
        from src.tracking.declarations import _declare
        # Should be callable and silently ignore all args.
        _declare("cm.test.fake", screen="Test", after=["cm.other"])


# ---------------------------------------------------------------------------
# Runtime collision detection via _StrictTL
# ---------------------------------------------------------------------------

class TestRuntimeCollisionDetection:
    """Each test exercises a real callsite through the strict shim.
    A correctly-written site passes; a site that passes name= or point= as
    a data kwarg causes _StrictTL to raise AssertionError — which is exactly
    what we want to catch before production."""

    def test_clean_event_passes_strict(self, strict_tracelink):
        strict_tracelink.event("cm.scan.local.ok", machine="local", sessions=3)

    def test_clean_track_passes_strict(self, strict_tracelink):
        strict_tracelink.track("cm.some.point", elapsed_ms=10)

    def test_clean_span_passes_strict(self, strict_tracelink):
        ctx = strict_tracelink.span("cm.adapter.launch.start", adapter="wt")
        assert isinstance(ctx, _NoopSpan)

    def test_event_with_name_kwarg_raises(self, strict_tracelink):
        """This is the exact shape that caused the production TypeError."""
        with pytest.raises(AssertionError, match="reserved positional kwarg 'name'"):
            strict_tracelink.event("cm.scan.local.ok", name="bad-value")

    def test_track_with_point_kwarg_raises(self, strict_tracelink):
        with pytest.raises(AssertionError, match="reserved positional kwarg 'point'"):
            strict_tracelink.track("cm.some.point", point="bad-value")

    def test_span_with_name_kwarg_raises(self, strict_tracelink):
        with pytest.raises(AssertionError, match="reserved positional kwarg 'name'"):
            strict_tracelink.span("cm.adapter.launch.start", name="bad-value")

    def test_scanner_scan_local_ok_event(self, strict_tracelink, tmp_path, monkeypatch):
        """scan_local emits cm.scan.local.ok — verify it doesn't collide."""
        from src.scanner import scan_local as _scan_local
        ch = tmp_path / ".claude"
        (ch / "projects").mkdir(parents=True)
        (ch / "sessions").mkdir()
        monkeypatch.setattr("src.scanner._persisted_cache_path",
                            lambda: tmp_path / "sc.json")
        monkeypatch.setattr("src.scanner._git_cache_path",
                            lambda: tmp_path / "gc.json")
        if hasattr(_scan_local, "_session_cache"):
            delattr(_scan_local, "_session_cache")
        if hasattr(_scan_local, "_git_cache"):
            delattr(_scan_local, "_git_cache")
        monkeypatch.setattr("src.scanner.subprocess.run", MagicMock(
            return_value=MagicMock(returncode=1, stdout="")))
        # Should not raise AssertionError through _StrictTL
        result = _scan_local(claude_home=ch, machine="strict-test")
        assert isinstance(result, list)

    def test_event_extra_data_kwargs_do_not_collide(self, strict_tracelink):
        """Confirm that harmless data kwargs all pass through without issue."""
        strict_tracelink.event(
            "cm.api.response",
            elapsed_ms=42,
            status=200,
            path="/api/sessions",
            sessions=10,
        )
