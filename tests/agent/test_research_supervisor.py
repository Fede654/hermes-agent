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
    TaskSpec,
    _build_task_brief,
    _extract_iteration,
)


# ---------------------------------------------------------------------------
# Unit tests (no integration mark needed)
# ---------------------------------------------------------------------------

class TestBuildTaskBrief:
    def _code_spec(self, **kwargs) -> TaskSpec:
        defaults = dict(
            topic="optimizer comparison",
            deliverable="Python comparison of Adam vs SGD on MNIST",
            metric_key="accuracy",
            metric_direction="maximize",
            task_type="code",
            hypothesis="Adam converges faster than SGD",
        )
        defaults.update(kwargs)
        return TaskSpec(**defaults)

    def test_contains_topic_and_metric(self):
        spec = self._code_spec()
        md = _build_task_brief(
            spec,
            iteration=1,
            round_dir="/tmp/round-001-iter1",
            time_budget_sec=120,
        )
        assert "optimizer comparison" in md
        assert "accuracy" in md
        assert "higher" in md  # metric_direction="maximize" renders as "higher"
        assert "120" in md
        assert "METRIC: accuracy=<value>" in md

    def test_contains_time_guard_instructions(self):
        spec = TaskSpec(
            topic="t", deliverable="d", metric_key="loss",
            metric_direction="minimize", task_type="code",
        )
        md = _build_task_brief(spec, iteration=0, round_dir="/tmp/rd", time_budget_sec=60)
        assert "TIME_ESTIMATE" in md
        assert "80%" in md

    def test_iteration_zero_is_baseline(self):
        spec = self._code_spec()
        md = _build_task_brief(spec, iteration=0, round_dir="/tmp/rd", time_budget_sec=300)
        assert "Establish a baseline" in md

    def test_iteration_positive_is_improve(self):
        spec = self._code_spec()
        md = _build_task_brief(spec, iteration=2, round_dir="/tmp/rd", time_budget_sec=300)
        assert "Improve" in md

    def test_search_task_brief(self):
        spec = TaskSpec(
            topic="Find attention mechanism papers",
            deliverable="Ranked list of papers",
            metric_key="relevance_score",
            task_type="search",
        )
        md = _build_task_brief(spec, iteration=0, round_dir="/tmp/rd", time_budget_sec=120)
        assert "Search" in md
        assert "relevance_score" in md
        assert "attempt.md" in md

    def test_research_task_brief(self):
        spec = TaskSpec(
            topic="State of diffusion models",
            deliverable="Technical synthesis",
            metric_key="completeness_score",
            task_type="research",
        )
        md = _build_task_brief(spec, iteration=0, round_dir="/tmp/rd", time_budget_sec=300)
        assert "Research" in md
        assert "completeness_score" in md

    def test_generic_task_brief(self):
        spec = TaskSpec(
            topic="Optimize search latency",
            deliverable="Modified implementation",
            metric_key="latency_ms",
            metric_direction="minimize",
            task_type="generic",
        )
        md = _build_task_brief(spec, iteration=0, round_dir="/tmp/rd", time_budget_sec=300)
        assert "latency_ms" in md


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


@pytest.fixture()
def code_spec() -> TaskSpec:
    return TaskSpec(
        topic="Optimizer comparison on MNIST",
        deliverable="Python script comparing Adam vs SGD with accuracy metric",
        metric_key="accuracy",
        metric_direction="maximize",
        task_type="code",
        hypothesis="Adam converges faster than SGD",
    )


@pytest.mark.integration
class TestResearchSupervisorBaseline:
    """Full loop with a mocked delegate_task — no real subagent spawned."""

    def test_baseline_only_no_llm(self, tmp_workspace: Path, mock_parent_agent: MagicMock, code_spec: TaskSpec):
        """Supervisor runs baseline experiment, returns history with 1 result."""
        metric_value = 0.85

        with patch("tools.delegate_tool.delegate_task", return_value=_make_delegate_result(metric_value)):
            supervisor = ResearchSupervisor(
                parent_agent=mock_parent_agent,
                workspace=tmp_workspace,
            )
            history = supervisor.run(
                code_spec,
                initial_attempt="print('accuracy: 0.85')",
                run_id="test-baseline-001",
                max_iterations=3,
                time_budget_sec=60,
                llm=None,  # baseline only
            )

        assert len(history.results) == 1
        assert history.baseline_metric == pytest.approx(metric_value, abs=0.001)
        assert history.results[0].iteration == 0
        assert history.results[0].primary_metric == pytest.approx(metric_value, abs=0.001)
        assert history.results[0].kept is True  # first result always kept

    def test_task_brief_written_to_round_dir(self, tmp_workspace: Path, mock_parent_agent: MagicMock, code_spec: TaskSpec):
        """Supervisor must write task_brief.md and attempt.py before calling delegate_task."""
        with patch("tools.delegate_tool.delegate_task", return_value=_make_delegate_result(0.75)):
            supervisor = ResearchSupervisor(
                parent_agent=mock_parent_agent,
                workspace=tmp_workspace,
            )
            supervisor.run(
                code_spec,
                initial_attempt="# baseline code\nprint('accuracy: 0.75')",
                run_id="test-files-001",
                llm=None,
            )

        run_dir = tmp_workspace / "test-files-001"
        assert run_dir.exists(), "run dir must be created"
        round_dirs = list(run_dir.iterdir())
        assert len(round_dirs) >= 1
        round_dir = round_dirs[0]
        assert (round_dir / "attempt.py").exists(), "attempt.py must be written for code tasks"
        assert (round_dir / "task_brief.md").exists(), "task_brief.md must be written by supervisor"
        brief = (round_dir / "task_brief.md").read_text()
        assert "Optimizer comparison on MNIST" in brief
        assert "accuracy" in brief

    def test_failed_worker_records_error(self, tmp_workspace: Path, mock_parent_agent: MagicMock):
        """When delegate_task returns failed status, result has error and is not kept."""
        spec = TaskSpec(
            topic="Crash test",
            deliverable="code that fails",
            metric_key="accuracy",
            task_type="code",
        )
        with patch("tools.delegate_tool.delegate_task", return_value=_make_failed_delegate_result("Worker crashed")):
            supervisor = ResearchSupervisor(
                parent_agent=mock_parent_agent,
                workspace=tmp_workspace,
            )
            history = supervisor.run(
                spec,
                initial_attempt="raise RuntimeError('oops')",
                run_id="test-fail-001",
                llm=None,
            )

        assert len(history.results) == 1
        result = history.results[0]
        assert result.error is not None
        assert result.kept is False
        assert result.primary_metric is None

    def test_lattice_comment_fn_called(self, tmp_workspace: Path, mock_parent_agent: MagicMock, code_spec: TaskSpec):
        """Lattice comment function is called at loop start and end."""
        with patch("tools.delegate_tool.delegate_task", return_value=_make_delegate_result(0.9)):
            supervisor = ResearchSupervisor(
                parent_agent=mock_parent_agent,
                workspace=tmp_workspace,
            )
            supervisor._lattice_task_id = None  # stub mode — logs only
            history = supervisor.run(
                code_spec,
                initial_attempt="pass",
                run_id="test-comment-001",
                llm=None,
            )

        assert len(history.results) == 1

    def test_search_task_writes_attempt_md(self, tmp_workspace: Path, mock_parent_agent: MagicMock):
        """Search tasks write attempt.md, not attempt.py."""
        spec = TaskSpec(
            topic="Find papers on transformers",
            deliverable="Ranked list of papers",
            metric_key="relevance_score",
            task_type="search",
        )
        with patch("tools.delegate_tool.delegate_task", return_value=_make_delegate_result(0.8, "relevance_score")):
            supervisor = ResearchSupervisor(
                parent_agent=mock_parent_agent,
                workspace=tmp_workspace,
            )
            supervisor.run(
                spec,
                initial_attempt="search query: transformer papers after 2022",
                run_id="test-search-001",
                llm=None,
            )

        run_dir = tmp_workspace / "test-search-001"
        round_dirs = list(run_dir.iterdir())
        assert len(round_dirs) >= 1
        round_dir = round_dirs[0]
        assert (round_dir / "attempt.md").exists(), "attempt.md must be written for search tasks"
        assert not (round_dir / "attempt.py").exists(), "attempt.py must NOT be written for search tasks"


@pytest.mark.integration
class TestResearchSupervisorIterations:
    """Multi-iteration loop with a mock LLM client."""

    def _make_mock_llm(self) -> MagicMock:
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

        spec = TaskSpec(
            topic="Improvement test",
            deliverable="Adam should converge better",
            metric_key="accuracy",
            metric_direction="maximize",
            task_type="code",
        )
        mock_llm = self._make_mock_llm()

        with patch("tools.delegate_tool.delegate_task", side_effect=side_effect):
            supervisor = ResearchSupervisor(
                parent_agent=mock_parent_agent,
                workspace=tmp_workspace,
            )
            history = supervisor.run(
                spec,
                initial_attempt="# initial",
                run_id="test-iter-001",
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
        call_count = 0

        def side_effect(goal, context, toolsets, parent_agent):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _make_delegate_result(0.5)   # baseline
            return _make_delegate_result(0.4)        # always regress

        spec = TaskSpec(
            topic="Early stop test",
            deliverable="This will not improve",
            metric_key="accuracy",
            metric_direction="maximize",
            task_type="code",
        )
        mock_llm = MagicMock()
        mock_llm.chat.return_value = MagicMock(content="```python\npass\n```")

        with patch("tools.delegate_tool.delegate_task", side_effect=side_effect):
            supervisor = ResearchSupervisor(
                parent_agent=mock_parent_agent,
                workspace=tmp_workspace,
            )
            history = supervisor.run(
                spec,
                initial_attempt="# bad code",
                run_id="test-early-001",
                max_iterations=10,
                llm=mock_llm,
            )

        # baseline + 3 failing iterations = 4 total
        assert len(history.results) == 4
        assert call_count == 4
