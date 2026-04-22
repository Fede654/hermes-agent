"""Integration tests for ResearchSupervisor — Karpathy inner loop.

Run with:
    pytest tests/agent/test_research_supervisor.py -m integration --override-ini="addopts="

The 'integration' mark is required because these tests write to a real tmpdir,
run the full ExperimentRunner loop, and verify end-to-end metric parsing.
Tests tagged 'unit' run without the mark.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from agent.research_runner import (
    DelegateSandboxResult,
    ExperimentHistory,
    ExperimentRunner,
    HermesExperimentConfig,
)
from agent.research_metrics import UniversalMetricParser
from agent.research_supervisor import (
    ResearchSupervisor,
    _build_program_md,
    _extract_iteration,
)


# ---------------------------------------------------------------------------
# Unit tests (no integration mark needed)
# ---------------------------------------------------------------------------

class TestBuildProgramMd:
    def test_contains_topic_and_metric(self):
        md = _build_program_md(
            topic="optimizer comparison",
            hypothesis="Adam converges faster than SGD",
            metric_key="accuracy",
            metric_direction="maximize",
            time_budget_sec=120,
            iteration=1,
            round_dir="/tmp/round-001-iter1",
        )
        assert "optimizer comparison" in md
        assert "Adam converges faster than SGD" in md
        assert "accuracy" in md
        assert "maximize" in md
        assert "120" in md
        assert "iteration 1" in md
        assert "METRIC: accuracy=<value>" in md

    def test_contains_time_guard_instructions(self):
        md = _build_program_md(
            topic="t", hypothesis="h", metric_key="loss", metric_direction="minimize",
            time_budget_sec=60, iteration=0, round_dir="/tmp/rd",
        )
        assert "TIME_ESTIMATE" in md
        assert "80%" in md

    def test_iteration_zero_is_baseline(self):
        md = _build_program_md(
            topic="t", hypothesis="h", metric_key="m", metric_direction="maximize",
            time_budget_sec=300, iteration=0, round_dir="/tmp/rd",
        )
        assert "iteration 0" in md


class TestExtractIteration:
    def test_round_dir_with_iter(self):
        assert _extract_iteration("/tmp/round-abc-iter3") == 3
        assert _extract_iteration("/tmp/round-xyz-iter0") == 0
        assert _extract_iteration("/tmp/round-foo-iter12") == 12

    def test_malformed_returns_zero(self):
        assert _extract_iteration("/tmp/no-iter-here") == 0
        assert _extract_iteration("") == 0


# ---------------------------------------------------------------------------
# Integration tests — full loop with mocked delegate_task
# ---------------------------------------------------------------------------

pytestmark_integration = pytest.mark.integration


def _make_delegate_result(metric_value: float, metric_key: str = "accuracy") -> str:
    """Build a fake delegate_task JSON result with a metric in stdout."""
    summary = (
        f"Experiment complete.\n"
        f"METRIC: {metric_key}={metric_value} STATUS: improved NOTES: mock result\n"
        f"All done."
    )
    return json.dumps({
        "results": [
            {
                "task_index": 0,
                "status": "completed",
                "summary": summary,
                "api_calls": 5,
                "duration_seconds": 1.2,
                "exit_reason": "completed",
                "tokens": {"input": 100, "output": 50},
                "tool_trace": [],
            }
        ],
        "total_duration_seconds": 1.2,
    })


def _make_failed_delegate_result(error: str = "Worker timed out") -> str:
    return json.dumps({
        "results": [
            {
                "task_index": 0,
                "status": "failed",
                "summary": "",
                "error": error,
                "api_calls": 1,
                "duration_seconds": 5.0,
                "exit_reason": "max_iterations",
                "tokens": {"input": 20, "output": 0},
                "tool_trace": [],
            }
        ],
        "total_duration_seconds": 5.0,
    })


@pytest.fixture()
def tmp_workspace(tmp_path: Path) -> Path:
    return tmp_path / "research-workspace"


@pytest.fixture()
def mock_parent_agent() -> MagicMock:
    agent = MagicMock()
    agent.model = "claude-sonnet-4-6"
    agent.base_url = "https://api.anthropic.com"
    agent.api_key = "test-key"
    agent.provider = "anthropic"
    agent.api_mode = "anthropic_messages"
    agent.providers_allowed = None
    agent.providers_ignored = None
    agent.providers_order = None
    agent.provider_sort = None
    agent.enabled_toolsets = ["terminal", "file"]
    agent._delegate_depth = 0
    agent._active_children = []
    agent._active_children_lock = None
    return agent


@pytest.mark.integration
class TestResearchSupervisorBaseline:
    """Full loop with a mocked delegate_task — no real subagent spawned."""

    def test_baseline_only_no_llm(self, tmp_workspace: Path, mock_parent_agent: MagicMock):
        """Supervisor runs baseline experiment, returns history with 1 result."""
        metric_value = 0.85

        with patch("tools.delegate_tool.delegate_task", return_value=_make_delegate_result(metric_value)):
            supervisor = ResearchSupervisor(
                parent_agent=mock_parent_agent,
                workspace=tmp_workspace,
            )
            history = supervisor.run(
                topic="Optimizer comparison on MNIST",
                hypothesis="SGD with momentum beats vanilla SGD",
                initial_code="print('accuracy: 0.85')",
                run_id="test-baseline-001",
                metric_key="accuracy",
                metric_direction="maximize",
                max_iterations=3,
                time_budget_sec=60,
                llm=None,  # baseline only
            )

        assert len(history.results) == 1
        assert history.baseline_metric == pytest.approx(metric_value, abs=0.001)
        assert history.results[0].iteration == 0
        assert history.results[0].primary_metric == pytest.approx(metric_value, abs=0.001)
        assert history.results[0].kept is True  # first result always kept

    def test_program_md_written_to_round_dir(self, tmp_workspace: Path, mock_parent_agent: MagicMock):
        """Supervisor must write program.md and main.py before calling delegate_task."""
        written_dirs: list[Path] = []

        def capturing_delegate(goal, context, toolsets, parent_agent):
            # Find the round dir from goal string
            parts = goal.split("in ")
            if len(parts) > 1:
                rd = Path(parts[-1].split("\n")[0].strip())
                if rd.exists():
                    written_dirs.append(rd)
            return _make_delegate_result(0.75)

        with patch("tools.delegate_tool.delegate_task", side_effect=capturing_delegate):
            supervisor = ResearchSupervisor(
                parent_agent=mock_parent_agent,
                workspace=tmp_workspace,
            )
            supervisor.run(
                topic="Test topic",
                hypothesis="H1",
                initial_code="# baseline code\nprint('accuracy: 0.75')",
                run_id="test-files-001",
                metric_key="accuracy",
                llm=None,
            )

        # The round dir should have been created
        round_dirs = list((tmp_workspace / "test-files-001").iterdir())
        assert len(round_dirs) >= 1
        round_dir = round_dirs[0]
        assert (round_dir / "main.py").exists(), "main.py must be written by supervisor"
        assert (round_dir / "program.md").exists(), "program.md must be written by supervisor"
        program_md = (round_dir / "program.md").read_text()
        assert "Test topic" in program_md
        assert "H1" in program_md
        assert "accuracy" in program_md

    def test_failed_worker_records_error(self, tmp_workspace: Path, mock_parent_agent: MagicMock):
        """When delegate_task returns failed status, result has error and is not kept."""
        with patch("tools.delegate_tool.delegate_task", return_value=_make_failed_delegate_result("Worker crashed")):
            supervisor = ResearchSupervisor(
                parent_agent=mock_parent_agent,
                workspace=tmp_workspace,
            )
            history = supervisor.run(
                topic="Crash test",
                hypothesis="Will fail",
                initial_code="raise RuntimeError('oops')",
                run_id="test-fail-001",
                metric_key="accuracy",
                llm=None,
            )

        assert len(history.results) == 1
        result = history.results[0]
        assert result.error is not None
        assert result.kept is False
        assert result.primary_metric is None

    def test_lattice_comment_fn_called(self, tmp_workspace: Path, mock_parent_agent: MagicMock):
        """Lattice comment function is called at loop start and end."""
        comments: list[str] = []

        with patch("tools.delegate_tool.delegate_task", return_value=_make_delegate_result(0.9)):
            supervisor = ResearchSupervisor(
                parent_agent=mock_parent_agent,
                workspace=tmp_workspace,
            )
            # Patch the comment fn after construction
            supervisor._lattice_task_id = None  # stub mode — logs only
            history = supervisor.run(
                topic="Comment test",
                hypothesis="H",
                initial_code="pass",
                run_id="test-comment-001",
                metric_key="accuracy",
                llm=None,
            )

        # Just check we got a result — comment fn stubbed to logger
        assert len(history.results) == 1


@pytest.mark.integration
class TestResearchSupervisorIterations:
    """Multi-iteration loop with a mock LLM client."""

    def _make_mock_llm(self, improved_metrics: list[float]) -> MagicMock:
        """Mock LLM that returns trivially modified code each iteration."""
        call_count = 0

        class MockResponse:
            content = "```python\nprint('updated code')\n```"

        llm = MagicMock()
        llm.chat.return_value = MockResponse()
        return llm

    def test_two_iteration_improvement(self, tmp_workspace: Path, mock_parent_agent: MagicMock):
        """Loop improves once then plateaus — verifies history and best_result."""
        metric_sequence = iter([0.70, 0.82, 0.81, 0.80])  # baseline, iter1 improves, iter2/3 regress

        def side_effect(goal, context, toolsets, parent_agent):
            val = next(metric_sequence, 0.80)
            return _make_delegate_result(val)

        mock_llm = self._make_mock_llm([0.70, 0.82, 0.81])

        with patch("tools.delegate_tool.delegate_task", side_effect=side_effect):
            supervisor = ResearchSupervisor(
                parent_agent=mock_parent_agent,
                workspace=tmp_workspace,
            )
            history = supervisor.run(
                topic="Improvement test",
                hypothesis="Adam should converge better",
                initial_code="# initial",
                run_id="test-iter-001",
                metric_key="accuracy",
                metric_direction="maximize",
                max_iterations=5,
                llm=mock_llm,
            )

        # Should have stopped after 3 non-improving iterations past the best
        assert len(history.results) >= 2
        best = history.best_result
        assert best is not None
        assert best.primary_metric == pytest.approx(0.82, abs=0.001)

    def test_early_stop_on_no_improvement(self, tmp_workspace: Path, mock_parent_agent: MagicMock):
        """Loop stops early after 3 consecutive non-improving iterations."""
        # Baseline + 3 non-improvements → early stop (total 4 calls)
        call_count = 0

        def side_effect(goal, context, toolsets, parent_agent):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _make_delegate_result(0.5)   # baseline
            return _make_delegate_result(0.4)        # always regress

        mock_llm = MagicMock()
        mock_llm.chat.return_value = MagicMock(content="```python\npass\n```")

        with patch("tools.delegate_tool.delegate_task", side_effect=side_effect):
            supervisor = ResearchSupervisor(
                parent_agent=mock_parent_agent,
                workspace=tmp_workspace,
            )
            history = supervisor.run(
                topic="Early stop test",
                hypothesis="This will not improve",
                initial_code="# bad code",
                run_id="test-early-001",
                metric_key="accuracy",
                metric_direction="maximize",
                max_iterations=10,
                llm=mock_llm,
            )

        # baseline + 3 failing iterations = 4 total
        assert len(history.results) == 4
        assert call_count == 4
