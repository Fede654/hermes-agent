"""Unit tests for CycleDetector ported into gateway/platforms/daemoncraft.py."""
from __future__ import annotations

import os
import sys
import types
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# gateway/platforms/__init__.py eagerly imports yuanbao (httpx) and daemoncraft
# itself needs aiohttp. Stub missing optional deps before import.
def _stub_module(name: str, **attrs):
    if name not in sys.modules:
        mod = types.ModuleType(name)
        for k, v in attrs.items():
            setattr(mod, k, v)
        sys.modules[name] = mod

_stub_module("httpx")
_stub_module("aiohttp", WSMsgType=MagicMock(), ClientSession=MagicMock)

# Import the standalone class directly — no server needed
from gateway.platforms.daemoncraft import CycleDetector


# ---------------------------------------------------------------------------
# CycleDetector unit tests
# ---------------------------------------------------------------------------

class TestCycleDetectorUnit:
    def test_no_cycle_below_threshold(self):
        cd = CycleDetector(n=3, window=10, action="warn")
        for _ in range(2):
            r = cd.record("tool_a", {})
        assert r.triggered is False

    def test_cycle_on_nth_identical_call(self):
        cd = CycleDetector(n=3, window=10, action="warn")
        r = None
        for _ in range(3):
            r = cd.record("tool_a", {})
        assert r.triggered is True
        assert r.count >= 3

    def test_no_double_trigger_on_n_plus_one(self):
        cd = CycleDetector(n=3, window=10, action="warn")
        for _ in range(3):
            cd.record("tool_a", {})
        # 4th call: same sig, already triggered — should suppress
        r = cd.record("tool_a", {})
        assert r.triggered is False

    def test_different_tool_names_no_cycle(self):
        cd = CycleDetector(n=3, window=10, action="warn")
        results = []
        for i in range(6):
            results.append(cd.record(f"tool_{i}", {}))
        assert not any(r.triggered for r in results)

    def test_different_args_no_cycle(self):
        cd = CycleDetector(n=3, window=10, action="warn")
        results = []
        for i in range(6):
            results.append(cd.record("tool_a", {"x": i}))
        assert not any(r.triggered for r in results)

    def test_cycle_clears_after_different_sig_dominates(self):
        """After suppression, a NEW dominant sig should trigger fresh."""
        # Use small window=3 so tool_b can fully dominate and evict tool_a
        cd = CycleDetector(n=3, window=3, action="warn")
        # Trigger first cycle for tool_a
        for _ in range(3):
            cd.record("tool_a", {})
        # Flood with tool_b — fills the window, clears _last_triggered_sig
        for _ in range(3):
            cd.record("tool_b", {})
        # Now tool_a again — should trigger fresh (suppression was cleared)
        r = None
        for _ in range(3):
            r = cd.record("tool_a", {})
        assert r.triggered is True


# ---------------------------------------------------------------------------
# DaemonCraftAdapter._check_cycle integration tests
# ---------------------------------------------------------------------------

def _make_adapter():
    """Build a minimal DaemonCraftAdapter with all external deps mocked."""
    from gateway.platforms.daemoncraft import DaemonCraftAdapter
    from gateway.config import PlatformConfig

    cfg = PlatformConfig(
        enabled=True,
        extra={
            "bot_api_url": "http://localhost:9999",
            "bot_username": "TestBot",
            "profile": "test",
        },
    )
    adapter = DaemonCraftAdapter(cfg)
    # Patch _interrupt_agent so tests don't need a real HTTP session
    adapter._interrupt_agent = AsyncMock()
    return adapter


class TestCheckCycleMethod:
    @pytest.mark.anyio
    async def test_returns_false_when_no_detector(self):
        adapter = _make_adapter()
        assert adapter._cycle_detector is None
        result = await adapter._check_cycle("mc_perceive", {})
        assert result is False

    @pytest.mark.anyio
    async def test_returns_false_for_non_cycling_calls(self):
        adapter = _make_adapter()
        adapter._cycle_detector = CycleDetector(n=3, window=10, action="warn")
        result = await adapter._check_cycle("mc_perceive", {})
        assert result is False

    @pytest.mark.anyio
    async def test_warn_action_returns_false_on_cycle(self):
        """Cycle detected with action='warn' should log but NOT interrupt."""
        adapter = _make_adapter()
        adapter._cycle_detector = CycleDetector(n=3, window=10, action="warn")
        for _ in range(2):
            await adapter._check_cycle("mc_perceive", {})
        result = await adapter._check_cycle("mc_perceive", {})
        # warn = no interrupt
        assert result is False
        adapter._interrupt_agent.assert_not_called()

    @pytest.mark.anyio
    async def test_interrupt_action_returns_true_and_calls_interrupt(self):
        """Cycle with action='interrupt' should call _interrupt_agent and return True."""
        adapter = _make_adapter()
        adapter._cycle_detector = CycleDetector(n=3, window=10, action="interrupt")
        for _ in range(2):
            await adapter._check_cycle("mc_perceive", {})
        result = await adapter._check_cycle("mc_perceive", {})
        assert result is True
        adapter._interrupt_agent.assert_called_once_with("cycle_detected")


class TestAdapterCycleDetectorInit:
    @pytest.mark.anyio
    async def test_no_detector_when_mc_cycle_n_zero(self, monkeypatch):
        monkeypatch.delenv("MC_CYCLE_N", raising=False)
        adapter = _make_adapter()
        # Patch connect internals so no actual socket is opened
        with patch("gateway.platforms.daemoncraft.aiohttp.ClientSession", return_value=MagicMock()), \
             patch("asyncio.create_task", return_value=MagicMock()):
            await adapter.connect()
        assert adapter._cycle_detector is None

    @pytest.mark.anyio
    async def test_detector_created_when_mc_cycle_n_set(self, monkeypatch):
        monkeypatch.setenv("MC_CYCLE_N", "3")
        monkeypatch.setenv("MC_CYCLE_WINDOW", "10")
        monkeypatch.setenv("MC_CYCLE_ACTION", "warn")
        adapter = _make_adapter()
        with patch("gateway.platforms.daemoncraft.aiohttp.ClientSession", return_value=MagicMock()), \
             patch("asyncio.create_task", return_value=MagicMock()):
            await adapter.connect()
        assert adapter._cycle_detector is not None
        assert adapter._cycle_detector.n == 3
        assert adapter._cycle_detector.window == 10
        assert adapter._cycle_detector.action == "warn"
