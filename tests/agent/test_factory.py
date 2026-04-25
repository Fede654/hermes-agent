"""Tests for agent.factory — centralized AIAgent construction for
detached entrypoints (HRM-57).

Run with:
    pytest tests/agent/test_factory.py -q --override-ini="addopts="
"""
from __future__ import annotations

import os
from unittest.mock import patch, MagicMock

from agent.factory import build_agent_for_research_job


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

    def test_runtime_invariants_passed_as_kwargs(self):
        """After HRM-57 full, the runtime invariants are constructor kwargs.
        The factory must hand them to AIAgent rather than patch post-init."""
        captured: dict = {}
        with patch("run_agent.AIAgent") as MockAgent:
            MockAgent.side_effect = lambda *a, **kw: captured.update(kw) or MagicMock(tool_progress_callback=None)
            build_agent_for_research_job(_SPEC)

        assert captured["delegate_depth"] == 0
        assert "terminal_cwd" in captured
        assert "cwd" in captured
        assert captured["subdirectory_hints"] is None

    def test_progress_callback_is_no_op_when_none(self):
        """The factory still sets a no-op tool_progress_callback when AIAgent
        leaves it as None — callers can dispatch without nil-checking."""
        with patch("run_agent.AIAgent") as MockAgent:
            mock_agent = MagicMock()
            mock_agent.tool_progress_callback = None
            MockAgent.return_value = mock_agent
            agent = build_agent_for_research_job(_SPEC)

        assert agent.tool_progress_callback is not None
        # Calling it should not raise
        agent.tool_progress_callback("event", "name", "preview")
