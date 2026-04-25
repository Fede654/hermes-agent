"""Tests for agent.factory — centralized AIAgent construction for
detached entrypoints (HRM-57).

Run with:
    pytest tests/agent/test_factory.py -q --override-ini="addopts="
"""
from __future__ import annotations

import os
from unittest.mock import patch, MagicMock

from agent.factory import build_agent_for_research_job, _apply_runtime_invariants


# A minimal spec the factory should accept.
_SPEC = {
    "job_id": "test-001",
    "model": "kimi-k2.6",
    "provider": "kimi-coding",
    "base_url": "https://api.kimi.com/coding/v1",
    "api_key": "",
    "toolsets": ["research", "terminal", "file"],
}


class TestBuildAgentForResearchJob:
    def test_default_inherits_profile_context(self):
        """Without skip_* in spec, defaults flip to load profile context —
        consistent with HRM-58: research workers want the curated profile."""
        captured: dict = {}
        with patch("run_agent.AIAgent") as MockAgent:
            MockAgent.side_effect = lambda *a, **kw: captured.update(kw) or MagicMock()
            build_agent_for_research_job(_SPEC)
        assert captured["skip_context_files"] is False
        assert captured["skip_memory"] is False
        assert captured["model"] == "kimi-k2.6"
        assert captured["session_id"] == "research-job:test-001"

    def test_spec_can_opt_out_of_profile_context(self):
        """spec.skip_context_files=True still wins for callers that want
        a blank-slate detached run (e.g. provider benchmarking)."""
        captured: dict = {}
        spec = {**_SPEC, "skip_context_files": True, "skip_memory": True}
        with patch("run_agent.AIAgent") as MockAgent:
            MockAgent.side_effect = lambda *a, **kw: captured.update(kw) or MagicMock()
            build_agent_for_research_job(spec)
        assert captured["skip_context_files"] is True
        assert captured["skip_memory"] is True

    def test_runtime_invariants_applied(self):
        """The factory must set the post-init attrs delegate_task expects.

        Use a plain object so attribute assignments are observable directly,
        sidestepping MagicMock's restrictions on __setattr__ override.
        """
        class FakeAgent:
            def __init__(self, *_a, **_kw):
                pass

        with patch("run_agent.AIAgent", FakeAgent):
            agent = build_agent_for_research_job(_SPEC)

        for attr in (
            "_delegate_depth", "terminal_cwd", "cwd", "_subdirectory_hints",
            "_delegate_spinner", "tool_progress_callback",
            "providers_allowed", "providers_ignored",
            "providers_order", "provider_sort",
        ):
            assert hasattr(agent, attr), f"factory must set {attr}"

        assert agent._delegate_depth == 0
        assert agent._subdirectory_hints is None
        assert agent._delegate_spinner is None
        assert agent.terminal_cwd == os.getcwd()
        assert agent.cwd == os.getcwd()


class TestApplyRuntimeInvariants:
    def test_idempotent_on_simple_object(self):
        """Calling _apply_runtime_invariants twice must not raise and
        must end with the same final state."""
        class Bag:
            pass

        bag = Bag()
        _apply_runtime_invariants(bag)
        first = (bag._delegate_depth, bag.cwd, bag._subdirectory_hints)
        _apply_runtime_invariants(bag)
        second = (bag._delegate_depth, bag.cwd, bag._subdirectory_hints)
        assert first == second
