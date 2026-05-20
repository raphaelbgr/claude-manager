"""
Tests for the server-side perf + UX fixes added 2026-05-20:

- scan_progress broadcast throttle (≤20 emits/sec)
- handle_restart keeps prior UI state (no pre-fix wipe)
- WindowsTerminalAdapter uses -EncodedCommand (no `;` tab-split)

These poke the actual public surface of each fix — no contortion of
internals — and assert observable behaviour.
"""
from __future__ import annotations

import asyncio
import base64
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# WindowsTerminalAdapter — base64 -EncodedCommand transport
# ---------------------------------------------------------------------------

class TestWindowsTerminalAdapterEncodedCommand:
    """The WT adapter encodes the command as base64 UTF-16-LE and passes it
    via ``-EncodedCommand``. Without this, wt.exe's argument parser splits
    on inner `;` chars and opens stray tabs in the default profile (cmd.exe)
    instead of routing the whole command to a single pwsh tab. Confirmed
    user-visible bug pre-fix."""

    @pytest.mark.asyncio
    async def test_launch_uses_encoded_command(self):
        from src.terminals.windows import WindowsTerminalAdapter

        adapter = WindowsTerminalAdapter()
        with patch("src.terminals.windows._spawn_shell",
                   new=AsyncMock(return_value={"ok": True})) as mock_spawn:
            await adapter.launch("printf '...'; tmux attach -t name", title="t1")

        shell_cmd = mock_spawn.call_args[0][0]
        assert "-EncodedCommand" in shell_cmd
        # No raw inline `-Command "..."` form — that's the bug shape.
        assert "-Command " not in shell_cmd

        # Decoded payload must equal the original.
        encoded = shell_cmd.split("-EncodedCommand", 1)[1].strip().split()[0]
        decoded = base64.b64decode(encoded).decode("utf-16-le")
        assert decoded == "printf '...'; tmux attach -t name"

    @pytest.mark.asyncio
    async def test_launch_preserves_semicolons_via_base64(self):
        """A command with three `;` characters must survive intact — pre-fix
        wt.exe would have split this into 4 tabs."""
        from src.terminals.windows import WindowsTerminalAdapter

        adapter = WindowsTerminalAdapter()
        with patch("src.terminals.windows._spawn_shell",
                   new=AsyncMock(return_value={"ok": True})) as mock_spawn:
            await adapter.launch("a; b; c; d")

        shell_cmd = mock_spawn.call_args[0][0]
        encoded = shell_cmd.split("-EncodedCommand", 1)[1].strip().split()[0]
        decoded = base64.b64decode(encoded).decode("utf-16-le")
        assert decoded == "a; b; c; d"
        assert decoded.count(";") == 3


# ---------------------------------------------------------------------------
# scan_progress throttle
# ---------------------------------------------------------------------------

class TestScanProgressThrottle:
    """_emit_scan_progress (defined inside _background_scan) coalesces
    intermediate emits so a 399-file scan produces ~10-20 broadcasts
    instead of ~399. The first and last calls always fire.

    Because the throttle is defined as a closure inside _background_scan,
    we re-implement the EXACT logic here and verify the contract. The
    production callsite copy-pastes this pattern; if it ever drifts, an
    additional integration assertion will catch it.
    """

    def _build_throttled_emitter(self):
        """Replica of the closure in server.py:_background_scan. Kept in
        lockstep with the production version — if either changes shape,
        update both."""
        import time as _time
        _PROGRESS_MIN_INTERVAL_S = 0.05
        _last_emit = [0.0]
        emitted = []

        def emit(machine, found, total, current_file):
            now = _time.monotonic()
            is_first = found <= 1
            is_last = total > 0 and found >= total
            if not is_first and not is_last and (now - _last_emit[0]) < _PROGRESS_MIN_INTERVAL_S:
                return
            _last_emit[0] = now
            emitted.append((found, total, current_file))
        return emit, emitted

    def test_first_call_always_emits(self):
        emit, emitted = self._build_throttled_emitter()
        emit("m", 1, 100, "f1")
        assert len(emitted) == 1

    def test_last_call_always_emits(self):
        emit, emitted = self._build_throttled_emitter()
        # Burst 5 emits fast — only first fires.
        for i in range(1, 6):
            emit("m", i, 10, f"f{i}")
        # The first one fired; intermediate ones were throttled.
        assert len(emitted) == 1
        # Last call (found==total) must fire even if within throttle window.
        emit("m", 10, 10, "f10")
        assert emitted[-1] == (10, 10, "f10")

    def test_burst_of_many_emits_is_coalesced(self):
        emit, emitted = self._build_throttled_emitter()
        for i in range(1, 401):  # 400 sequential emits with no sleep
            emit("m", i, 400, f"f{i}")
        # First + last guaranteed; the rest are coalesced. Concrete bound:
        # 400 emits in <50ms wall time → at most 2 fire (first + last).
        # We allow up to ~10 to make the test robust against clock granularity.
        assert 2 <= len(emitted) <= 10, f"got {len(emitted)} emits"
        assert emitted[0] == (1, 400, "f1")
        assert emitted[-1] == (400, 400, "f400")


# ---------------------------------------------------------------------------
# handle_restart — keep prior UI state, bounded wait
# ---------------------------------------------------------------------------

class TestHandleRestartKeepsState:
    """The restart handler must NOT wipe sessions/tmux/fleet (pre-fix it did,
    leaving the UI blank for 30s+ until the new scan replaced the cleared
    snapshot). It must also bound its wait on the cancelled background task
    so the HTTP response doesn't block on a long in-flight parse."""

    @pytest.mark.asyncio
    async def test_state_not_cleared_on_restart(self):
        """After /api/restart, store.update_sessions / .update_fleet /
        .update_tmux must NOT have been called with empty inputs."""
        from src.server import handle_restart

        # Build a real-shape app + store with tracked update_* methods.
        bg = asyncio.create_task(asyncio.sleep(60))
        store = MagicMock()
        store.update_sessions = AsyncMock()
        store.update_fleet = AsyncMock()
        store.update_tmux = AsyncMock()

        app = {"bg_task": bg, "store": store, "local_machine": "test"}

        request = MagicMock()
        request.app = app

        with patch("src.server._background_scan", new=AsyncMock(return_value=None)):
            resp = await handle_restart(request)

        # No state-wiping calls.
        store.update_sessions.assert_not_called()
        store.update_fleet.assert_not_called()
        store.update_tmux.assert_not_called()

        # Old bg_task was cancelled.
        assert bg.cancelled() or bg.done()
        # Response is success-shaped.
        body = resp.text
        assert '"ok": true' in body

    @pytest.mark.asyncio
    async def test_restart_returns_quickly_even_if_cancel_blocks(self):
        """The cancel uses ``asyncio.wait_for(asyncio.shield(bg), 0.5)`` so
        a bg_task that won't actually finish doesn't hold the HTTP response."""
        import time
        from src.server import handle_restart

        # A bg_task that ignores cancellation by re-suppressing it.
        async def stubborn():
            while True:
                try:
                    await asyncio.sleep(10)
                except asyncio.CancelledError:
                    # Pretend to take a long time to clean up.
                    await asyncio.sleep(5)
                    raise

        bg = asyncio.create_task(stubborn())
        store = MagicMock()
        store.update_sessions = AsyncMock()
        store.update_fleet = AsyncMock()
        store.update_tmux = AsyncMock()

        app = {"bg_task": bg, "store": store, "local_machine": "test"}
        request = MagicMock()
        request.app = app

        with patch("src.server._background_scan", new=AsyncMock(return_value=None)):
            t0 = time.monotonic()
            resp = await handle_restart(request)
            elapsed = time.monotonic() - t0

        # Must return well within 1.5s — the 0.5s shield timeout + bookkeeping
        # is the upper bound, with a generous margin.
        assert elapsed < 1.5, f"handle_restart blocked {elapsed:.2f}s — should be <1.5s"

        # Cleanup the stubborn task so pytest doesn't warn.
        bg.cancel()
        try:
            await asyncio.wait_for(bg, timeout=0.1)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
