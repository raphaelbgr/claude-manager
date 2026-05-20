"""claude-manager tracelink integration.

Strategic-point structured tracing. Imports `tracelink` if present; if not
(e.g. on fleet machines where tracelink hasn't been installed yet), every
helper degrades to a no-op so production code paths stay identical.

Usage from any module:

    from .tracking import tl, span

    tl.event("cm.scan.local.ok", machines=1, sessions=42)

    with span("cm.adapter.launch", adapter="wt") as s:
        s.update(rc=0)
        ...

The `init()` function is idempotent — call it once from src/server.py and
src/tui/app.py entry points. Sink defaults to
``$TEMP/tracelink/claude-manager-<run_id>.jsonl``.

Declarations live in ``declarations.py``. Importing this package runs
them as a side effect (any `declare()` left without a matching emit
shows up in the drift check at CI/pre-commit time).
"""
from __future__ import annotations

import contextlib
import logging
from typing import Any

log = logging.getLogger("claude_manager.tracking")

try:
    import tracelink as _tl
    _ENABLED = True
except ImportError:
    _tl = None
    _ENABLED = False


class _NoopSpan:
    def update(self, **kwargs: Any) -> None: ...
    def __enter__(self): return self
    def __exit__(self, *args: Any) -> None: ...


class _Stub:
    """tracelink API surface that degrades to no-ops when the package is absent."""
    def init(self, *args: Any, **kwargs: Any) -> dict: return {"enabled": False}
    def event(self, name: str, **data: Any) -> None: ...
    def track(self, point: str, **data: Any) -> None: ...
    def span(self, name: str, **data: Any): return _NoopSpan()
    def screen(self, name: str) -> None: ...
    def enter(self, name: str) -> None: ...
    def leave(self, name: str) -> None: ...


tl = _tl if _ENABLED else _Stub()


@contextlib.contextmanager
def span(name: str, **data: Any):
    """Context-managed span (compat for `with span(...) as s:`)."""
    if _ENABLED:
        with _tl.span(name, **data) as s:
            yield s
    else:
        yield _NoopSpan()


def init(run_id: str | None = None) -> dict:
    """Initialize tracelink for this process. Safe to call multiple times."""
    if not _ENABLED:
        log.info("tracelink not installed — events will no-op")
        return {"enabled": False}
    cfg = _tl.init(service="claude-manager", run_id=run_id)
    log.info("tracelink: sink=%s run_id=%s", cfg.get("sink_path"), cfg.get("run_id"))
    # Import declarations after init so the registry is populated.
    try:
        from . import declarations  # noqa: F401 (side effect: declare() calls)
    except Exception as exc:
        log.warning("tracelink declarations failed to load: %s", exc)
    return cfg


__all__ = ["tl", "span", "init"]
