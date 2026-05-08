"""Tests for HRM-108: Hypothesis Fan-Out parallelism in ResearchSupervisor."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from agent.research.supervisor import ResearchSupervisor, TaskSpec, _call_delegate_task_batch
from agent.research.runner import ExperimentHistory, ExperimentResult, DelegateSandboxResult


@pytest.fixture
def mock_llm():
    """LLM mock that returns content from a configurable response."""
    m = MagicMock()
    m.chat.return_value = MagicMock(content="")
    return m


@pytest.fixture
def mock_parent_agent():
    """Minimal parent agent mock for delegate_task."""
    m = MagicMock()
    m._delegate_depth = 0
    m.session_id = "test-session"
    m._interrupt_requested = False
    return m


@pytest.fixture
def tmp_workspace(tmp_path: Path) -> Path:
    return tmp_path / "workspace"


@pytest.fixture
def code_spec() -> TaskSpec:
    return TaskSpec(
        topic="sort an array",
        deliverable="python function sort_array(arr)",
        metric_key="accuracy",
        metric_direction="maximize",
        task_type="code",
    )


class TestFanOutAttempts:
    """_fan_out_attempts generates N diverse revision hypotheses."""

    def test_fan_out_generates_n_attempts(
        self, mock_llm, mock_parent_agent, tmp_workspace, code_spec
    ):
        mock_llm.chat.return_value = MagicMock(
            content=(
                "=== VARIANT 1 ===\n"
                "[hypothesis: use quicksort]\n"
                "def sort_array(arr): return sorted(arr)\n"
                "=== VARIANT 2 ===\n"
                "[hypothesis: use mergesort]\n"
                "def sort_array(arr): return merge_sort(arr)\n"
                "=== VARIANT 3 ===\n"
                "[hypothesis: use heapsort]\n"
                "def sort_array(arr): return heap_sort(arr)\n"
            )
        )

        supervisor = ResearchSupervisor(
            parent_agent=mock_parent_agent,
            workspace=tmp_workspace,
        )
        history = ExperimentHistory()
        history.add(
            ExperimentResult(
                run_id="r1",
                iteration=0,
                code="def sort_array(arr): pass",
                metrics={"accuracy": 0.5},
                primary_metric=0.5,
                improved=True,
                kept=True,
                elapsed_sec=1.0,
                stdout="baseline",
                stderr="",
            )
        )

        attempts = supervisor._fan_out_attempts(
            mock_llm, code_spec, "def sort_array(arr): pass", history, n=3
        )

        assert len(attempts) == 3
        assert "sorted(arr)" in attempts[0]
        assert "merge_sort" in attempts[1]
        assert "heap_sort" in attempts[2]

    def test_fan_out_pads_with_current_attempt_if_parsing_yields_fewer(
        self, mock_llm, mock_parent_agent, tmp_workspace, code_spec
    ):
        mock_llm.chat.return_value = MagicMock(
            content="=== VARIANT 1 ===\n[hypothesis: only one]\ndef f(): pass\n"
        )

        supervisor = ResearchSupervisor(
            parent_agent=mock_parent_agent,
            workspace=tmp_workspace,
        )
        history = ExperimentHistory()

        attempts = supervisor._fan_out_attempts(
            mock_llm, code_spec, "original", history, n=3
        )

        assert len(attempts) == 3
        assert "f(): pass" in attempts[0]
        assert attempts[1] == "original"
        assert attempts[2] == "original"

    def test_fan_out_fallback_on_llm_error(
        self, mock_llm, mock_parent_agent, tmp_workspace, code_spec
    ):
        mock_llm.chat.side_effect = RuntimeError("API failure")

        supervisor = ResearchSupervisor(
            parent_agent=mock_parent_agent,
            workspace=tmp_workspace,
        )
        history = ExperimentHistory()

        attempts = supervisor._fan_out_attempts(
            mock_llm, code_spec, "original", history, n=2
        )

        assert len(attempts) == 1
        assert attempts[0] == "original"


class TestRunFanOutIteration:
    """_run_fan_out_iteration executes N workers in parallel and returns sorted results."""

    def test_fan_out_runs_n_workers(self, mock_parent_agent, tmp_workspace, code_spec):
        supervisor = ResearchSupervisor(
            parent_agent=mock_parent_agent,
            workspace=tmp_workspace,
        )
        history = ExperimentHistory()

        # Seed a baseline so current_best is known
        history.add(
            ExperimentResult(
                run_id="r1",
                iteration=0,
                code="baseline",
                metrics={"accuracy": 0.5},
                primary_metric=0.5,
                improved=True,
                kept=True,
                elapsed_sec=1.0,
                stdout="ok",
                stderr="",
            )
        )

        def fake_batch(tasks, **kwargs):
            # Return one result per task
            results = []
            for i, _ in enumerate(tasks):
                results.append({
                    "task_index": i,
                    "status": "completed",
                    "summary": f"METRIC: accuracy={0.6 + i * 0.1} STATUS: improved NOTES: ok",
                    "error": None,
                    "duration_seconds": 1.0,
                    "api_calls": 1,
                })
            return {"results": results}

        with patch(
            "agent.research.supervisor._call_delegate_task_batch",
            side_effect=fake_batch,
        ):
            results = supervisor._run_fan_out_iteration(
                spec=code_spec,
                attempts=["attempt0", "attempt1", "attempt2"],
                run_id="test-run",
                iteration=1,
                time_budget_sec=0,
                worker_toolsets=None,
                llm=None,
                history=history,
            )

        assert len(results) == 3
        # All should be in history
        assert len(history.results) == 4  # baseline + 3 fan-out

    def test_fan_out_selects_best_first(self, mock_parent_agent, tmp_workspace, code_spec):
        supervisor = ResearchSupervisor(
            parent_agent=mock_parent_agent,
            workspace=tmp_workspace,
        )
        history = ExperimentHistory()
        history.add(
            ExperimentResult(
                run_id="r1",
                iteration=0,
                code="baseline",
                metrics={"accuracy": 0.5},
                primary_metric=0.5,
                improved=True,
                kept=True,
                elapsed_sec=1.0,
                stdout="ok",
                stderr="",
            )
        )

        def fake_batch(tasks, **kwargs):
            results = []
            for i, _ in enumerate(tasks):
                # accuracy values: 0.55, 0.75, 0.65
                acc = 0.55 if i == 0 else (0.75 if i == 1 else 0.65)
                results.append({
                    "task_index": i,
                    "status": "completed",
                    "summary": f"METRIC: accuracy={acc} STATUS: improved NOTES: ok",
                    "error": None,
                    "duration_seconds": 1.0,
                    "api_calls": 1,
                })
            return {"results": results}

        with patch(
            "agent.research.supervisor._call_delegate_task_batch",
            side_effect=fake_batch,
        ):
            results = supervisor._run_fan_out_iteration(
                spec=code_spec,
                attempts=["a0", "a1", "a2"],
                run_id="test-run",
                iteration=1,
                time_budget_sec=0,
                worker_toolsets=None,
                llm=None,
                history=history,
            )

        # Best first: 0.75, 0.65, 0.55
        assert results[0].primary_metric == pytest.approx(0.75)
        assert results[1].primary_metric == pytest.approx(0.65)
        assert results[2].primary_metric == pytest.approx(0.55)

    def test_fan_out_fallback_on_batch_error(
        self, mock_parent_agent, tmp_workspace, code_spec
    ):
        supervisor = ResearchSupervisor(
            parent_agent=mock_parent_agent,
            workspace=tmp_workspace,
        )
        history = ExperimentHistory()

        with patch(
            "agent.research.supervisor._call_delegate_task_batch",
            side_effect=RuntimeError("batch failed"),
        ):
            results = supervisor._run_fan_out_iteration(
                spec=code_spec,
                attempts=["a0", "a1"],
                run_id="test-run",
                iteration=1,
                time_budget_sec=0,
                worker_toolsets=None,
                llm=None,
                history=history,
            )

        assert len(results) == 2
        assert all(r.error is not None for r in results)
        assert all(r.primary_metric is None for r in results)


class TestFanOutIntegration:
    """Integration tests for the fan-out loop via ResearchSupervisor.run()."""

    def test_run_with_fan_out_parameter(self, mock_llm, mock_parent_agent, tmp_workspace, code_spec):
        """run() accepts fan_out parameter and executes parallel iterations."""
        mock_llm.chat.return_value = MagicMock(
            content=(
                "=== VARIANT 1 ===\n"
                "[hypothesis: A]\n"
                "code A\n"
                "=== VARIANT 2 ===\n"
                "[hypothesis: B]\n"
                "code B\n"
            )
        )

        call_count = {"batch": 0}

        def fake_batch(tasks, **kwargs):
            call_count["batch"] += 1
            results = []
            for i, _ in enumerate(tasks):
                acc = 0.7 if i == 0 else 0.8
                results.append({
                    "task_index": i,
                    "status": "completed",
                    "summary": f"METRIC: accuracy={acc} STATUS: improved NOTES: ok",
                    "error": None,
                    "duration_seconds": 1.0,
                })
            return {"results": results}

        with patch(
            "agent.research.supervisor._call_delegate_task_batch",
            side_effect=fake_batch,
        ), patch.object(ResearchSupervisor, "_observe"), patch.object(
            ResearchSupervisor, "_checkpoint"
        ), patch.object(ResearchSupervisor, "_snapshot"):
            supervisor = ResearchSupervisor(
                parent_agent=mock_parent_agent,
                workspace=tmp_workspace,
            )
            # Patch delegate_fn for baseline ( ExperimentRunner uses it )
            with patch(
                "agent.research.supervisor._call_delegate_task",
                return_value={
                    "results": [{
                        "status": "completed",
                        "summary": "METRIC: accuracy=0.5 STATUS: neutral NOTES: baseline",
                    }]
                },
            ):
                history = supervisor.run(
                    code_spec,
                    initial_attempt="def sort_array(arr): pass",
                    run_id="fan-out-test",
                    max_iterations=1,
                    llm=mock_llm,
                    fan_out=2,
                )

        assert call_count["batch"] == 1
        assert len(history.results) == 3  # baseline + 2 fan-out branches
        assert history.best_result is not None
        assert history.best_result.primary_metric == pytest.approx(0.8)

    def test_run_fan_out_1_is_sequential(self, mock_llm, mock_parent_agent, tmp_workspace, code_spec):
        """fan_out=1 uses the original sequential path (no batch calls)."""
        mock_llm.chat.return_value = MagicMock(content="improved code")

        with patch(
            "agent.research.supervisor._call_delegate_task_batch"
        ) as mock_batch, patch.object(ResearchSupervisor, "_observe"), patch.object(
            ResearchSupervisor, "_checkpoint"
        ), patch.object(ResearchSupervisor, "_snapshot"):
            supervisor = ResearchSupervisor(
                parent_agent=mock_parent_agent,
                workspace=tmp_workspace,
            )
            with patch(
                "agent.research.supervisor._call_delegate_task",
                return_value={
                    "results": [{
                        "status": "completed",
                        "summary": "METRIC: accuracy=0.5 STATUS: neutral NOTES: baseline",
                    }]
                },
            ):
                supervisor.run(
                    code_spec,
                    initial_attempt="def sort_array(arr): pass",
                    run_id="seq-test",
                    max_iterations=1,
                    llm=mock_llm,
                    fan_out=1,
                )

        mock_batch.assert_not_called()
