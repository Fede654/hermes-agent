"""Tests locking in the three polish fixes for ResearchSupervisor mechanics:

1. acceptance_criterion is parsed and short-circuits the loop
2. _observe distinguishes plateau (neutral) from regression
3. disable_evolution_overlay suppresses the overlay loader
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agent.research.runner import ExperimentResult
from agent.research.supervisor import (
    ResearchSupervisor,
    TaskSpec,
    _parse_acceptance_criterion,
)


# ---------------------------------------------------------------------------
# 1. Acceptance criterion parser
# ---------------------------------------------------------------------------

class TestAcceptanceCriterionParser:
    def test_parses_geq(self):
        test = _parse_acceptance_criterion("pass_rate >= 0.9")
        assert test is not None
        assert test(0.95) is True
        assert test(0.9) is True
        assert test(0.89) is False

    def test_parses_lt(self):
        test = _parse_acceptance_criterion("latency_ms < 200")
        assert test is not None
        assert test(199.9) is True
        assert test(200) is False
        assert test(250) is False

    def test_parses_op_only(self):
        test = _parse_acceptance_criterion(">= 0.5")
        assert test is not None
        assert test(0.5) is True
        assert test(0.4) is False

    def test_parses_negative_threshold(self):
        test = _parse_acceptance_criterion("delta > -0.01")
        assert test is not None
        assert test(0.0) is True
        assert test(-0.02) is False

    def test_qualitative_returns_none(self):
        assert _parse_acceptance_criterion("looks good to a human reviewer") is None

    def test_empty_returns_none(self):
        assert _parse_acceptance_criterion("") is None


# ---------------------------------------------------------------------------
# 2. _observe plateau / regression / neutral
# ---------------------------------------------------------------------------

def _make_result(iteration, primary_metric, improved):
    return ExperimentResult(
        run_id="test",
        iteration=iteration,
        code="",
        metrics={"pass_rate": str(primary_metric)} if primary_metric is not None else {},
        primary_metric=primary_metric,
        improved=improved,
        kept=improved,
        elapsed_sec=0.0,
        stdout="METRIC: pass_rate=%s NOTES: t" % primary_metric,
        stderr="",
        error=None,
    )


class TestObserveClassification:
    def setup_method(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="observe-test-"))
        self.spec = TaskSpec(
            topic="t", deliverable="d",
            metric_key="pass_rate", metric_direction="maximize",
        )
        self.sup = ResearchSupervisor(
            parent_agent=MagicMock(), workspace=self.tmp,
        )

    def _last_type(self):
        line = (self.tmp / "learnings.jsonl").read_text().strip().splitlines()[-1]
        return json.loads(line)["type"]

    def test_improvement_when_strictly_better(self):
        r = _make_result(1, 0.9, improved=True)
        self.sup._observe(r, self.spec, self.tmp, previous_best=0.7)
        assert self._last_type() == "improvement"

    def test_neutral_when_equal_to_best(self):
        r = _make_result(2, 0.8, improved=False)
        self.sup._observe(r, self.spec, self.tmp, previous_best=0.8)
        assert self._last_type() == "neutral"

    def test_regression_when_strictly_worse_maximize(self):
        r = _make_result(3, 0.6, improved=False)
        self.sup._observe(r, self.spec, self.tmp, previous_best=0.8)
        assert self._last_type() == "regression"

    def test_regression_when_strictly_worse_minimize(self):
        spec = TaskSpec(
            topic="t", deliverable="d",
            metric_key="latency_ms", metric_direction="minimize",
        )
        r = _make_result(2, 250.0, improved=False)
        self.sup._observe(r, spec, self.tmp, previous_best=200.0)
        assert self._last_type() == "regression"

    def test_failure_when_metric_none(self):
        r = _make_result(1, None, improved=False)
        self.sup._observe(r, self.spec, self.tmp, previous_best=0.5)
        assert self._last_type() == "failure"

    def test_neutral_when_no_prior_best_and_not_improved(self):
        # Edge case: result.improved=False with previous_best=None
        # (e.g. first round had no metric). Should be neutral, not regression.
        r = _make_result(0, 0.5, improved=False)
        self.sup._observe(r, self.spec, self.tmp, previous_best=None)
        assert self._last_type() == "neutral"


# ---------------------------------------------------------------------------
# 3. disable_evolution_overlay
# ---------------------------------------------------------------------------

class TestDisableEvolutionOverlay:
    def test_disabled_skips_loader(self):
        tmp = Path(tempfile.mkdtemp(prefix="overlay-test-"))
        spec = TaskSpec(
            topic="t", deliverable="d",
            metric_key="m", metric_direction="maximize",
        )
        sup = ResearchSupervisor(
            parent_agent=MagicMock(), workspace=tmp,
        )
        # Sentinel: if this gets called when disabled, the test fails.
        with patch.object(sup, "_load_evolution_overlay") as mock_loader:
            mock_loader.side_effect = AssertionError("should not be called when disabled")
            with patch("agent.research.supervisor._call_delegate_task") as mock_dt:
                mock_dt.return_value = {"results": [{"status": "completed", "summary": "METRIC: m=1.0"}]}
                sup.run(
                    spec, initial_attempt="x", run_id="t",
                    max_iterations=0,  # baseline only
                    disable_evolution_overlay=True,
                )
            mock_loader.assert_not_called()

    def test_enabled_calls_loader(self):
        tmp = Path(tempfile.mkdtemp(prefix="overlay-test-"))
        spec = TaskSpec(
            topic="t", deliverable="d",
            metric_key="m", metric_direction="maximize",
        )
        sup = ResearchSupervisor(
            parent_agent=MagicMock(), workspace=tmp,
        )
        with patch.object(sup, "_load_evolution_overlay", return_value="") as mock_loader:
            with patch("agent.research.supervisor._call_delegate_task") as mock_dt:
                mock_dt.return_value = {"results": [{"status": "completed", "summary": "METRIC: m=1.0"}]}
                sup.run(
                    spec, initial_attempt="x", run_id="t",
                    max_iterations=0,
                    disable_evolution_overlay=False,
                )
            mock_loader.assert_called_once()


# ---------------------------------------------------------------------------
# 4. Acceptance criterion early termination (integration)
# ---------------------------------------------------------------------------

@pytest.mark.integration
class TestAcceptanceTerminationE2E:
    """Full supervisor.run() with scripted scores. Loop must short-circuit
    when the metric crosses the criterion threshold."""

    def test_loop_terminates_when_acceptance_met(self):
        scores = [0.50, 0.70, 0.95, 0.99]  # iter 2 should terminate
        n = {"i": 0}

        def fake_delegate(*args, **kwargs):
            idx = min(n["i"], len(scores) - 1)
            n["i"] += 1
            return {"results": [{
                "status": "completed",
                "summary": f"METRIC: pass_rate={scores[idx]} NOTES: iter-{idx}",
            }]}

        tmp = Path(tempfile.mkdtemp(prefix="accept-e2e-"))
        spec = TaskSpec(
            topic="t", deliverable="d",
            metric_key="pass_rate", metric_direction="maximize",
            acceptance_criterion="pass_rate >= 0.9",
        )
        sup = ResearchSupervisor(
            parent_agent=MagicMock(), workspace=tmp,
        )
        stub_llm = MagicMock()
        stub_llm.chat.return_value = MagicMock(content="```python\nx\n```")

        with patch("agent.research.supervisor._call_delegate_task", side_effect=fake_delegate):
            history = sup.run(
                spec, initial_attempt="x", run_id="t",
                max_iterations=10,
                llm=stub_llm,
                disable_evolution_overlay=True,
            )
        # Must have stopped at iter 2 (first score >= 0.9), not run all 10.
        assert len(history.results) == 3, f"expected 3 iters (0,1,2), got {len(history.results)}"
        assert history.best_result.primary_metric == 0.95

    def test_loop_runs_to_max_when_acceptance_not_met(self):
        scores = [0.50, 0.60, 0.70]  # never crosses 0.9
        n = {"i": 0}

        def fake_delegate(*args, **kwargs):
            idx = min(n["i"], len(scores) - 1)
            n["i"] += 1
            return {"results": [{
                "status": "completed",
                "summary": f"METRIC: pass_rate={scores[idx]} NOTES: iter-{idx}",
            }]}

        tmp = Path(tempfile.mkdtemp(prefix="accept-fail-"))
        spec = TaskSpec(
            topic="t", deliverable="d",
            metric_key="pass_rate", metric_direction="maximize",
            acceptance_criterion="pass_rate >= 0.9",
        )
        sup = ResearchSupervisor(
            parent_agent=MagicMock(), workspace=tmp,
        )
        stub_llm = MagicMock()
        stub_llm.chat.return_value = MagicMock(content="```python\nx\n```")

        with patch("agent.research.supervisor._call_delegate_task", side_effect=fake_delegate):
            history = sup.run(
                spec, initial_attempt="x", run_id="t",
                max_iterations=2,
                llm=stub_llm,
                disable_evolution_overlay=True,
            )
        # All 3 iterations executed (0, 1, 2) since acceptance never met.
        assert len(history.results) == 3
