"""
Defensive test for the ``tl.event(name=...) / tl.track(point=...)`` kwarg
collision that broke production twice in the 2026-05-20 audit arc.

``tl.event`` is defined as ``event(name: str, **data)`` — passing ``name=``
as a data field collides with the positional ``name`` parameter and raises
``TypeError: event() got multiple values for argument 'name'`` at runtime.
The collision is INVISIBLE to ``ast.parse`` and to any static check; only
actually calling the function trips it.

This test parses every src/*.py module and statically inspects every
``tl.event(...)`` / ``tl.track(...)`` / ``span(...)`` call site to ensure
the reserved positional kwargs (``name`` for event/span, ``point`` for
track) are NOT used as data kwargs. Lint-level enforcement; runs in <100ms.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest


SRC = Path(__file__).resolve().parent.parent / "src"

# Each (callable, reserved_positional_kwarg_name). When the callable is
# invoked with that name as a keyword arg, Python raises TypeError.
RESERVED: dict[str, str] = {
    "event": "name",
    "track": "point",
    "span": "name",
}


def _collect_calls(tree: ast.AST) -> list[ast.Call]:
    return [n for n in ast.walk(tree) if isinstance(n, ast.Call)]


def _callee_name(call: ast.Call) -> str | None:
    """Return the simple callee name for forms we care about: ``tl.event``,
    ``tl.track``, ``span``, ``_tl.event``. Returns None for anything else."""
    if isinstance(call.func, ast.Attribute):
        # e.g. tl.event, _tl.event, _tracelink.event
        return call.func.attr
    if isinstance(call.func, ast.Name):
        return call.func.id
    return None


@pytest.mark.parametrize("py_path", sorted(SRC.rglob("*.py")))
def test_no_tracelink_kwarg_collision(py_path: Path):
    """For each .py file under src/, walk every Call. If the callee is one
    of {event, track, span}, ensure no keyword argument uses the reserved
    positional name for that callee.

    Failure: lists the file:line of every offending call so a future
    contributor knows exactly which emit to rename.
    """
    text = py_path.read_text(encoding="utf-8")
    tree = ast.parse(text, filename=str(py_path))
    offenders: list[str] = []
    for call in _collect_calls(tree):
        cname = _callee_name(call)
        if cname not in RESERVED:
            continue
        reserved = RESERVED[cname]
        for kw in call.keywords:
            # ast.keyword.arg is None for **kwargs splat; skip those.
            if kw.arg == reserved:
                rel = py_path.relative_to(SRC.parent)
                offenders.append(
                    f"{rel}:{call.lineno}: {cname}(..., {reserved}=...) collides "
                    f"with the positional parameter — rename the data field "
                    f"(e.g. session=, point_id=)."
                )
    assert not offenders, "tracelink kwarg collisions:\n  " + "\n  ".join(offenders)
