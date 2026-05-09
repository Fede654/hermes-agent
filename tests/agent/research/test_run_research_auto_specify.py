"""run_research(auto_specify=True) fills missing fields from a vague topic
without ever overriding explicit caller values."""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest


SCAFFOLD = {
    "deliverable": "Python classify(payload) function",
    "metric_key": "pass_rate",
    "metric_direction": "minimize",   # deliberately not the default
    "task_type": "code",
    "evaluation_mode": "llm_judge",   # deliberately not the default
    "evaluation_prompt": "Score 0-1: does the function classify correctly?",
}


def _captured_supervisor_factory(captured: dict):
    def factory(**kwargs):
        sup = MagicMock()
        sup.run.return_value = MagicMock(results=[], best_result=None)
        captured["sup"] = sup
        return sup
    return factory


class TestAutoSpecifyFillsMissingFields:
    def test_fills_when_deliverable_and_metric_empty(self):
        captured: dict = {}
        with patch(
            "agent.research.auto_specify.auto_specify_topic",
            return_value=SCAFFOLD,
        ), patch(
            "tools.research_tool.ResearchSupervisor",
            side_effect=_captured_supervisor_factory(captured),
        ):
            from tools.research_tool import run_research
            out = run_research(
                topic="classify daemoncraft heartbeat events",
                parent_agent=MagicMock(),
                auto_specify=True,
                disable_evolution_overlay=True,
            )
        json.loads(out)  # smoke: must be valid JSON
        assert captured["sup"].run.called
        spec = captured["sup"].run.call_args.args[0]
        # Phase C must adopt scaffold values that differ from the run_research defaults.
        assert spec.deliverable == SCAFFOLD["deliverable"]
        assert spec.metric_key == SCAFFOLD["metric_key"]
        assert spec.metric_direction == "minimize"
        assert spec.task_type == "code"
        assert spec.evaluation_mode == "llm_judge"
        assert spec.evaluation_prompt == SCAFFOLD["evaluation_prompt"]

    def test_does_not_override_explicit_caller_values(self):
        """Caller passed deliverable + metric_key + task_type explicitly →
        scaffold's competing values must be ignored."""
        captured: dict = {}
        with patch(
            "agent.research.auto_specify.auto_specify_topic",
            return_value=SCAFFOLD,
        ), patch(
            "tools.research_tool.ResearchSupervisor",
            side_effect=_captured_supervisor_factory(captured),
        ):
            from tools.research_tool import run_research
            run_research(
                topic="some topic",
                deliverable="EXPLICIT deliverable",
                metric_key="EXPLICIT_metric",
                task_type="research",   # explicit, must stick
                parent_agent=MagicMock(),
                auto_specify=True,
                disable_evolution_overlay=True,
            )
        spec = captured["sup"].run.call_args.args[0]
        assert spec.deliverable == "EXPLICIT deliverable"
        assert spec.metric_key == "EXPLICIT_metric"
        assert spec.task_type == "research"

    def test_falls_back_when_aux_returns_none(self):
        """auto_specify failure must not crash run_research."""
        captured: dict = {}
        with patch(
            "agent.research.auto_specify.auto_specify_topic",
            return_value=None,
        ), patch(
            "tools.research_tool.ResearchSupervisor",
            side_effect=_captured_supervisor_factory(captured),
        ):
            from tools.research_tool import run_research
            run_research(
                topic="vague",
                parent_agent=MagicMock(),
                auto_specify=True,
                disable_evolution_overlay=True,
            )
        spec = captured["sup"].run.call_args.args[0]
        # Defaults for missing fields stick.
        assert spec.deliverable == ""
        assert spec.metric_key == ""
        assert spec.metric_direction == "maximize"
        assert spec.task_type == "generic"
        assert spec.evaluation_mode == "self_report"

    def test_disabled_does_not_call_aux(self):
        """auto_specify=False (default) must not invoke the aux LLM."""
        captured: dict = {}
        called = {"aux": False}

        def fail_aux(*a, **kw):
            called["aux"] = True
            return SCAFFOLD

        with patch(
            "agent.research.auto_specify.auto_specify_topic",
            side_effect=fail_aux,
        ), patch(
            "tools.research_tool.ResearchSupervisor",
            side_effect=_captured_supervisor_factory(captured),
        ):
            from tools.research_tool import run_research
            run_research(
                topic="t",
                deliverable="d",
                metric_key="m",
                parent_agent=MagicMock(),
                disable_evolution_overlay=True,
            )
        assert called["aux"] is False

    def test_skipped_when_caller_supplied_both_required_fields(self):
        """If caller provided deliverable AND metric_key, auto_specify is a no-op."""
        called = {"aux": False}

        def fail_aux(*a, **kw):
            called["aux"] = True
            return SCAFFOLD

        captured: dict = {}
        with patch(
            "agent.research.auto_specify.auto_specify_topic",
            side_effect=fail_aux,
        ), patch(
            "tools.research_tool.ResearchSupervisor",
            side_effect=_captured_supervisor_factory(captured),
        ):
            from tools.research_tool import run_research
            run_research(
                topic="t",
                deliverable="explicit",
                metric_key="explicit_metric",
                parent_agent=MagicMock(),
                auto_specify=True,
                disable_evolution_overlay=True,
            )
        assert called["aux"] is False
