"""Terminal adapter registry + discovery.

Entry points:
- `get_adapter(os, id)` — fetch a concrete adapter by OS + stable id
- `list_available(os, runner)` — probe every adapter for that OS through a
  runner callable; returns only the ones installed, priority-sorted desc.
  The same function serves both local and remote machines — the caller
  supplies a runner that executes a shell string locally or via SSH.
- `auto_pick(os, runner)` — returns the highest-priority available adapter.

Registration is implicit: importing this module imports each platform
submodule, which registers its adapters via `@register`. Adding a new
terminal = drop a class into the matching submodule with `@register`.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

from .base import TerminalAdapter
from ..tracking import tl

log = logging.getLogger("claude_manager.terminals")


# Registry keyed by (os, id) so different OSes can reuse an id like "alacritty".
_REGISTRY: dict[tuple[str, str], TerminalAdapter] = {}


def register(cls: type[TerminalAdapter]) -> type[TerminalAdapter]:
    """Class decorator: instantiate + add to registry. Idempotent."""
    inst = cls()
    key = (inst.os, inst.id)
    if key in _REGISTRY:
        log.debug("terminals.register: %s already registered, skipping", key)
        return cls
    _REGISTRY[key] = inst
    return cls


def get_adapter(os: str, id: str) -> TerminalAdapter | None:
    return _REGISTRY.get((os, id))


def all_for_os(os: str) -> list[TerminalAdapter]:
    return [a for (o, _), a in _REGISTRY.items() if o == os]


# Runner contract — returns (rc, stdout, stderr). Any callable matching this
# works: a local subprocess runner, an SSH-pool runner, a subprocess-ssh
# fallback. Keeps list_available host-agnostic.
Runner = Callable[[str], Awaitable[tuple[int, bytes, bytes]]]


async def list_available(os: str, runner: Runner) -> list[dict]:
    """Probe every adapter for `os` in parallel through `runner`.

    Returns a list of dicts: [{id, name, priority}, ...], installed-only,
    priority-desc. Failures silently exclude the adapter.
    """
    adapters = all_for_os(os)
    if not adapters:
        return []

    async def _probe(a: TerminalAdapter) -> TerminalAdapter | None:
        try:
            rc, _, _ = await asyncio.wait_for(runner(a.probe_shell()), timeout=8)
            try:
                tl.event(
                    "cm.adapter.probe.attempt",
                    adapter_id=a.id,
                    os=os,
                    rc=int(rc),
                    probe=(a.probe_shell() or "")[:200],
                )
            except Exception:
                pass
            return a if rc == 0 else None
        except Exception as exc:
            try:
                tl.event(
                    "cm.adapter.probe.attempt",
                    adapter_id=a.id,
                    os=os,
                    rc=-1,
                    error=(str(exc) or "")[:200],
                )
            except Exception:
                pass
            log.debug("probe(%s/%s) failed: %s", os, a.id, exc)
            return None

    results = await asyncio.gather(*(_probe(a) for a in adapters), return_exceptions=False)
    installed = [a for a in results if a is not None]
    installed.sort(key=lambda a: a.priority, reverse=True)
    return [{"id": a.id, "name": a.name, "priority": a.priority} for a in installed]


async def auto_pick(os: str, runner: Runner) -> TerminalAdapter | None:
    """Return the highest-priority available adapter, or None if nothing's installed."""
    avail = await list_available(os, runner)
    if not avail:
        return None
    return get_adapter(os, avail[0]["id"])


# Side-effect: import submodules to populate the registry.
from . import darwin as _darwin  # noqa: E402, F401
from . import linux as _linux    # noqa: E402, F401
from . import windows as _windows  # noqa: E402, F401


# Apply @register to every concrete adapter class in each submodule. Doing it
# here instead of per-class-decorator keeps the submodules pure data and makes
# registration explicit + visible in one place.
for _mod in (_darwin, _linux, _windows):
    for _name in dir(_mod):
        _cls = getattr(_mod, _name)
        if (
            isinstance(_cls, type)
            and issubclass(_cls, TerminalAdapter)
            and _cls is not TerminalAdapter
            and getattr(_cls, "id", None)
        ):
            register(_cls)
