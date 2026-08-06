"""HRM-93: Durable checkpoint + resume for the research loop.

The job_runner-driven research loop must:
  1. Write checkpoint.json atomically at the end of each iteration.
  2. Read checkpoint.json (and history.json) on startup and resume from the
     last completed iteration instead of restarting from baseline.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from agent.research.runner import (
    ExperimentHistory,
    ExperimentResult,
)
from agent.research.job_runner import _detect_resume
from agent.research.supervisor import (
    ResearchSupervisor,
    TaskSpec,
    _atomic_write_text,
    _load_checkpoint,
)


# ---------------------------------------------------------------------------
# Helpers (mirrors test_research_supervisor.py fixtures)
# ---------------------------------------------------------------------------

def _mock_delegate_json(metric_value: float, metric_key: str = "accuracy") -> str:
    summary = (
        f"Done.\n"
        f"METRIC: {metric_key}={metric_value} STATUS: improved NOTES: mock\n"
    )
    return json.dumps({
        "results": [{
            "task_index": 0,
            "status": "completed",
            "summary": summary,
            "api_calls": 1,
            "duration_seconds": 0.1,
            "exit_reason": "completed",
            "tokens": {"input": 1, "output": 1},
            "tool_trace": [],
        }],
        "total_duration_seconds": 0.1,
    })


@pytest.fixture()
def parent_agent() -> MagicMock:
    a = MagicMock()
    a.model = "claude-sonnet-4-6"
    a.base_url = "https://example"
    a.api_key = "k"
    a.provider = "anthropic"
    a.api_mode = "anthropic_messages"
    a.providers_allowed = None
    a.providers_ignored = None
    a.providers_order = None
    a.provider_sort = None
    a.enabled_toolsets = ["terminal", "file"]
    a._delegate_depth = 0
    a._active_children = []
    a._active_children_lock = None
    return a


@pytest.fixture()
def code_spec() -> TaskSpec:
    return TaskSpec(
        topic="t",
        deliverable="d",
        metric_key="accuracy",
        metric_direction="maximize",
        task_type="code",
    )


def _make_result(iteration: int, metric: float, run_id: str = "rid") -> ExperimentResult:
    return ExperimentResult(
        run_id=run_id,
        iteration=iteration,
        code=f"# iter {iteration}",
        metrics={"accuracy": metric},
        primary_metric=metric,
        improved=True,
        kept=True,
        elapsed_sec=0.1,
        stdout=f"METRIC: accuracy={metric}",
        stderr="",
        error=None,
    )


def _write_snapshot_stub(checkpoint_dir: Path, iteration: int) -> None:
    """Write a minimal snapshots/iter-{N}.json so _load_checkpoint's
    consistency check accepts the resume."""
    snap_dir = checkpoint_dir / "snapshots"
    snap_dir.mkdir(parents=True, exist_ok=True)
    (snap_dir / f"iter-{iteration}.json").write_text(json.dumps({
        "iteration": iteration,
        "messages": [],
        "metrics": {},
        "files": [],
    }))


# ---------------------------------------------------------------------------
# Atomic write
# ---------------------------------------------------------------------------

class TestAtomicWrite:
    def test_writes_file_with_final_content(self, tmp_path: Path):
        target = tmp_path / "x.json"
        _atomic_write_text(target, '{"a": 1}')
        assert target.read_text() == '{"a": 1}'

    def test_no_tmp_left_behind(self, tmp_path: Path):
        target = tmp_path / "x.json"
        _atomic_write_text(target, '{"a": 1}')
        leftovers = [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")]
        assert leftovers == [], f"unexpected .tmp leftovers: {leftovers}"

    def test_overwrite_existing(self, tmp_path: Path):
        target = tmp_path / "x.json"
        target.write_text("OLD")
        _atomic_write_text(target, "NEW")
        assert target.read_text() == "NEW"


# ---------------------------------------------------------------------------
# Checkpoint loading
# ---------------------------------------------------------------------------

class TestLoadCheckpoint:
    def test_returns_none_when_missing(self, tmp_path: Path):
        assert _load_checkpoint(tmp_path) is None

    def test_returns_none_when_only_checkpoint_present(self, tmp_path: Path):
        # checkpoint.json without history.json is unusable for resume
        (tmp_path / "checkpoint.json").write_text(json.dumps({"round": 1}))
        assert _load_checkpoint(tmp_path) is None

    def test_loads_history_and_iteration(self, tmp_path: Path):
        history = ExperimentHistory()
        history.add(_make_result(0, 0.5))
        history.add(_make_result(1, 0.7))
        history.best_result = history.results[1]

        (tmp_path / "history.json").write_text(json.dumps(history.to_dict()))
        (tmp_path / "checkpoint.json").write_text(json.dumps({
            "round": 1,
            "total_rounds": 2,
            "best_metric": 0.7,
            "updated_at": 0,
        }))
        _write_snapshot_stub(tmp_path, 1)

        loaded = _load_checkpoint(tmp_path)
        assert loaded is not None
        loaded_history, current_iteration = loaded
        assert current_iteration == 1
        assert len(loaded_history.results) == 2
        assert loaded_history.best_result is not None
        assert loaded_history.best_result.primary_metric == pytest.approx(0.7)


class TestLoadCheckpointConsistency:
    """Fix-2: cross-file consistency between checkpoint.json, history.json,
    and snapshots/iter-{N}.json. A crash mid-write must not produce a
    silent resume that skips a round."""

    def test_returns_none_when_history_shorter_than_round(self, tmp_path: Path):
        # checkpoint claims round=2 (3 iterations completed) but history
        # only contains 1 result. Resuming would silently skip rounds.
        history = ExperimentHistory()
        history.add(_make_result(0, 0.5))
        (tmp_path / "history.json").write_text(json.dumps(history.to_dict()))
        (tmp_path / "checkpoint.json").write_text(json.dumps({
            "round": 2, "total_rounds": 3, "best_metric": 0.5, "updated_at": 0,
        }))
        _write_snapshot_stub(tmp_path, 2)

        assert _load_checkpoint(tmp_path) is None

    def test_returns_none_when_history_longer_than_round(self, tmp_path: Path):
        # Symmetric mismatch: history has 3 results but checkpoint says
        # round=1. Either checkpoint.json or history.json was clobbered.
        history = ExperimentHistory()
        for i in range(3):
            history.add(_make_result(i, 0.1 * (i + 1)))
        (tmp_path / "history.json").write_text(json.dumps(history.to_dict()))
        (tmp_path / "checkpoint.json").write_text(json.dumps({
            "round": 1, "total_rounds": 3, "best_metric": 0.3, "updated_at": 0,
        }))
        _write_snapshot_stub(tmp_path, 1)

        assert _load_checkpoint(tmp_path) is None

    def test_returns_none_when_snapshot_missing(self, tmp_path: Path):
        history = ExperimentHistory()
        history.add(_make_result(0, 0.4))
        history.add(_make_result(1, 0.6))
        history.add(_make_result(2, 0.8))
        (tmp_path / "history.json").write_text(json.dumps(history.to_dict()))
        (tmp_path / "checkpoint.json").write_text(json.dumps({
            "round": 2, "total_rounds": 3, "best_metric": 0.8, "updated_at": 0,
        }))
        # Note: no _write_snapshot_stub call → iter-2.json is absent.
        assert _load_checkpoint(tmp_path) is None

    def test_returns_state_when_all_consistent(self, tmp_path: Path):
        history = ExperimentHistory()
        history.add(_make_result(0, 0.4))
        history.add(_make_result(1, 0.6))
        history.add(_make_result(2, 0.8))
        history.best_result = history.results[-1]
        (tmp_path / "history.json").write_text(json.dumps(history.to_dict()))
        (tmp_path / "checkpoint.json").write_text(json.dumps({
            "round": 2, "total_rounds": 3, "best_metric": 0.8, "updated_at": 0,
        }))
        _write_snapshot_stub(tmp_path, 2)

        loaded = _load_checkpoint(tmp_path)
        assert loaded is not None
        loaded_history, current_iteration = loaded
        assert current_iteration == 2
        assert len(loaded_history.results) == 3


# ---------------------------------------------------------------------------
# End-to-end resume (mocked delegate)
# ---------------------------------------------------------------------------

@pytest.mark.integration
class TestResumeFromCheckpoint:
    def test_baseline_skipped_when_checkpoint_exists(
        self, tmp_path: Path, parent_agent: MagicMock, code_spec: TaskSpec
    ):
        """If checkpoint says iteration 0 (baseline) is done, supervisor must
        not call delegate_task again for the baseline."""
        workspace = tmp_path / "ws"
        checkpoint_dir = tmp_path / "job"
        checkpoint_dir.mkdir()

        # Pre-seed checkpoint at iteration=0 (baseline complete)
        history = ExperimentHistory()
        history.add(_make_result(0, 0.42, run_id="resume-001"))
        history.best_result = history.results[0]
        (checkpoint_dir / "history.json").write_text(json.dumps(history.to_dict()))
        (checkpoint_dir / "checkpoint.json").write_text(json.dumps({
            "round": 0, "total_rounds": 1, "best_metric": 0.42, "updated_at": 0,
        }))
        _write_snapshot_stub(checkpoint_dir, 0)

        with patch(
            "tools.delegate_tool.delegate_task",
            return_value=_mock_delegate_json(0.99),
        ) as mock_delegate:
            supervisor = ResearchSupervisor(
                parent_agent=parent_agent, workspace=workspace
            )
            new_history = supervisor.run(
                code_spec,
                initial_attempt="x = 1",
                run_id="resume-001",
                max_iterations=0,        # baseline only
                llm=None,
                checkpoint_dir=checkpoint_dir,
            )

        # Baseline was skipped → delegate_task should NOT have been invoked
        assert mock_delegate.call_count == 0
        # Pre-existing baseline preserved
        assert len(new_history.results) == 1
        assert new_history.results[0].primary_metric == pytest.approx(0.42)

    def test_iteration_loop_resumes_after_completed_round(
        self, tmp_path: Path, parent_agent: MagicMock, code_spec: TaskSpec
    ):
        """If checkpoint says round=2 is done, the next call should run only
        the remaining iterations (3..max)."""
        workspace = tmp_path / "ws"
        checkpoint_dir = tmp_path / "job"
        checkpoint_dir.mkdir()

        history = ExperimentHistory()
        for i in range(3):  # iterations 0, 1, 2
            history.add(_make_result(i, 0.1 * (i + 1), run_id="resume-002"))
        history.best_result = history.results[-1]
        (checkpoint_dir / "history.json").write_text(json.dumps(history.to_dict()))
        (checkpoint_dir / "checkpoint.json").write_text(json.dumps({
            "round": 2, "total_rounds": 3, "best_metric": 0.3, "updated_at": 0,
        }))
        _write_snapshot_stub(checkpoint_dir, 2)

        # Stub the LLM so iterations 3..5 run
        llm = MagicMock()
        llm.chat.return_value = type("R", (), {"content": "x = 2"})()

        with patch(
            "tools.delegate_tool.delegate_task",
            return_value=_mock_delegate_json(0.5),
        ) as mock_delegate:
            supervisor = ResearchSupervisor(
                parent_agent=parent_agent, workspace=workspace
            )
            new_history = supervisor.run(
                code_spec,
                initial_attempt="x = 1",
                run_id="resume-002",
                max_iterations=5,
                llm=llm,
                checkpoint_dir=checkpoint_dir,
            )

        # Only iterations 3, 4, 5 should run → at most 3 delegate calls.
        # Early-stop may cut this short; assert it didn't redo earlier rounds.
        assert mock_delegate.call_count <= 3
        # The new history must contain the original 3 results plus the new ones.
        assert len(new_history.results) >= 3
        # The first three iterations are exactly the pre-seeded ones.
        for i in range(3):
            assert new_history.results[i].iteration == i
            assert new_history.results[i].primary_metric == pytest.approx(0.1 * (i + 1))

    def test_checkpoint_written_atomically_each_iteration(
        self, tmp_path: Path, parent_agent: MagicMock, code_spec: TaskSpec
    ):
        """After each iteration checkpoint.json must contain the current
        round number and history.json must round-trip via from_dict."""
        workspace = tmp_path / "ws"
        checkpoint_dir = tmp_path / "job"
        checkpoint_dir.mkdir()

        with patch(
            "tools.delegate_tool.delegate_task",
            return_value=_mock_delegate_json(0.7),
        ):
            supervisor = ResearchSupervisor(
                parent_agent=parent_agent, workspace=workspace
            )
            supervisor.run(
                code_spec,
                initial_attempt="x = 1",
                run_id="cp-001",
                max_iterations=0,
                llm=None,
                checkpoint_dir=checkpoint_dir,
            )

        cp = json.loads((checkpoint_dir / "checkpoint.json").read_text())
        assert cp["round"] == 0
        history_data = json.loads((checkpoint_dir / "history.json").read_text())
        # Full-fidelity history → round-trips through from_dict
        rebuilt = ExperimentHistory.from_dict(history_data)
        assert len(rebuilt.results) == 1
        assert rebuilt.best_result is not None


# ---------------------------------------------------------------------------
# job_runner resume detection
# ---------------------------------------------------------------------------

class TestDetectResume:
    def test_returns_none_when_no_checkpoint(self, tmp_path: Path):
        assert _detect_resume(tmp_path) is None

    def test_surfaces_round_and_best_metric(self, tmp_path: Path):
        (tmp_path / "checkpoint.json").write_text(json.dumps({
            "round": 4, "total_rounds": 5, "best_metric": 0.92, "updated_at": 0,
        }))
        info = _detect_resume(tmp_path)
        assert info is not None
        assert info["resumed_from_round"] == 4
        assert info["resumed_total_rounds"] == 5
        assert info["resumed_best_metric"] == pytest.approx(0.92)

    def test_returns_none_on_corrupt_checkpoint(self, tmp_path: Path):
        (tmp_path / "checkpoint.json").write_text("{not json")
        assert _detect_resume(tmp_path) is None
