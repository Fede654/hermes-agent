"""HRM-96: Atomic workspace snapshots per iteration.

After every completed iteration the supervisor must write
<job_dir>/snapshots/iter-{N}.json containing:
  - iteration
  - messages (full ExperimentHistory results up to iteration N)
  - metrics (last result's metrics dict)
  - files (list of {path, content} for the round directory)

Rollback is a single operation: restore_snapshot(snapshot_path, target_dir)
must rewrite every captured file at its captured relative path inside
target_dir.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agent.research.supervisor import (
    ResearchSupervisor,
    TaskSpec,
    restore_snapshot,
)


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


# ---------------------------------------------------------------------------
# Snapshot capture
# ---------------------------------------------------------------------------

@pytest.mark.integration
class TestSnapshotCapture:
    def test_snapshots_dir_created(
        self, tmp_path: Path, parent_agent: MagicMock, code_spec: TaskSpec
    ):
        workspace = tmp_path / "ws"
        checkpoint_dir = tmp_path / "job"
        checkpoint_dir.mkdir()

        with patch(
            "tools.delegate_tool.delegate_task",
            return_value=_mock_delegate_json(0.5),
        ):
            supervisor = ResearchSupervisor(
                parent_agent=parent_agent, workspace=workspace
            )
            supervisor.run(
                code_spec,
                initial_attempt="x = 1",
                run_id="snap-001",
                max_iterations=0,
                llm=None,
                checkpoint_dir=checkpoint_dir,
            )

        snapshots = checkpoint_dir / "snapshots"
        assert snapshots.is_dir(), "snapshots/ must exist"
        files = sorted(p.name for p in snapshots.iterdir())
        assert files == ["iter-0.json"]

    def test_snapshot_contains_required_fields(
        self, tmp_path: Path, parent_agent: MagicMock, code_spec: TaskSpec
    ):
        workspace = tmp_path / "ws"
        checkpoint_dir = tmp_path / "job"
        checkpoint_dir.mkdir()

        with patch(
            "tools.delegate_tool.delegate_task",
            return_value=_mock_delegate_json(0.6),
        ):
            supervisor = ResearchSupervisor(
                parent_agent=parent_agent, workspace=workspace
            )
            supervisor.run(
                code_spec,
                initial_attempt="x = 1",
                run_id="snap-002",
                max_iterations=0,
                llm=None,
                checkpoint_dir=checkpoint_dir,
            )

        snap = json.loads(
            (checkpoint_dir / "snapshots" / "iter-0.json").read_text()
        )
        assert snap["iteration"] == 0
        assert isinstance(snap["messages"], list)
        assert len(snap["messages"]) == 1  # baseline only
        assert isinstance(snap["metrics"], dict)
        assert snap["metrics"].get("accuracy") == pytest.approx(0.6)
        assert isinstance(snap["files"], list)
        # The supervisor wrote attempt.py + task_brief.md → at least 2 entries
        captured = {entry["path"] for entry in snap["files"]}
        assert any(p.endswith("attempt.py") for p in captured)
        assert any(p.endswith("task_brief.md") for p in captured)

    def test_one_snapshot_per_iteration(
        self, tmp_path: Path, parent_agent: MagicMock, code_spec: TaskSpec
    ):
        workspace = tmp_path / "ws"
        checkpoint_dir = tmp_path / "job"
        checkpoint_dir.mkdir()

        # LLM stub so the loop actually iterates
        llm = MagicMock()
        llm.chat.return_value = type("R", (), {"content": "x = 2"})()

        # Returns ascending metrics so the loop keeps "improving"
        delegates = iter([
            _mock_delegate_json(0.1),
            _mock_delegate_json(0.2),
            _mock_delegate_json(0.3),
        ])

        with patch(
            "tools.delegate_tool.delegate_task",
            side_effect=lambda *a, **kw: next(delegates),
        ):
            supervisor = ResearchSupervisor(
                parent_agent=parent_agent, workspace=workspace
            )
            supervisor.run(
                code_spec,
                initial_attempt="x = 1",
                run_id="snap-003",
                max_iterations=2,
                llm=llm,
                checkpoint_dir=checkpoint_dir,
            )

        snapshots = sorted(
            (checkpoint_dir / "snapshots").iterdir(), key=lambda p: p.name
        )
        names = [p.name for p in snapshots]
        assert names == ["iter-0.json", "iter-1.json", "iter-2.json"]


# ---------------------------------------------------------------------------
# Rollback
# ---------------------------------------------------------------------------

class TestRestoreSnapshot:
    def test_restore_recreates_files(self, tmp_path: Path):
        snapshot = {
            "iteration": 1,
            "messages": [],
            "metrics": {"accuracy": 0.7},
            "files": [
                {"path": "round-x-iter1/attempt.py", "content": "print('hi')\n"},
                {"path": "round-x-iter1/results.json", "content": '{"accuracy": 0.7}'},
            ],
        }
        snap_path = tmp_path / "iter-1.json"
        snap_path.write_text(json.dumps(snapshot))

        target = tmp_path / "restored"
        restore_snapshot(snap_path, target)

        assert (target / "round-x-iter1" / "attempt.py").read_text() == "print('hi')\n"
        assert (target / "round-x-iter1" / "results.json").read_text() == '{"accuracy": 0.7}'

    def test_restore_overwrites_existing(self, tmp_path: Path):
        snap_path = tmp_path / "iter-0.json"
        snap_path.write_text(json.dumps({
            "iteration": 0,
            "messages": [],
            "metrics": {},
            "files": [{"path": "f.txt", "content": "NEW"}],
        }))
        target = tmp_path / "out"
        target.mkdir()
        (target / "f.txt").write_text("OLD")

        restore_snapshot(snap_path, target)

        assert (target / "f.txt").read_text() == "NEW"

    def test_restore_rejects_path_traversal(self, tmp_path: Path):
        snap_path = tmp_path / "iter-0.json"
        snap_path.write_text(json.dumps({
            "iteration": 0,
            "messages": [],
            "metrics": {},
            "files": [{"path": "../../escape.txt", "content": "X"}],
        }))
        target = tmp_path / "out"

        with pytest.raises(ValueError):
            restore_snapshot(snap_path, target)

    def test_restore_rejects_single_dotdot(self, tmp_path: Path):
        snap_path = tmp_path / "iter-0.json"
        snap_path.write_text(json.dumps({
            "iteration": 0,
            "messages": [],
            "metrics": {},
            "files": [{"path": "../escape.txt", "content": "X"}],
        }))
        target = tmp_path / "out"

        with pytest.raises(ValueError):
            restore_snapshot(snap_path, target)

    def test_restore_rejects_symlink_in_parent_path(self, tmp_path: Path):
        target = tmp_path / "out"
        target.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        # A subdirectory of target is replaced with a symlink to outside.
        # Even though the resolved path of "round-x/payload" lands inside
        # /tmp/.../outside (escaping target's tree on resolution), and is
        # therefore caught by layer 1, the symlink check must reject it
        # explicitly with a "traverses a symlink" message — defense in
        # depth in case the symlink target is itself inside target_dir.
        symlinked = target / "round-x"
        symlinked.symlink_to(outside, target_is_directory=True)

        snap_path = tmp_path / "iter-0.json"
        snap_path.write_text(json.dumps({
            "iteration": 0,
            "messages": [],
            "metrics": {},
            "files": [{"path": "round-x/payload.txt", "content": "X"}],
        }))

        with pytest.raises(ValueError):
            restore_snapshot(snap_path, target)

    def test_restore_rejects_symlink_target_inside(self, tmp_path: Path):
        """Symlink that points back inside target_dir is still refused —
        we don't traverse symlinks at all."""
        target = tmp_path / "out"
        target.mkdir()
        real_sub = target / "real"
        real_sub.mkdir()
        symlinked = target / "via-link"
        symlinked.symlink_to(real_sub, target_is_directory=True)

        snap_path = tmp_path / "iter-0.json"
        snap_path.write_text(json.dumps({
            "iteration": 0,
            "messages": [],
            "metrics": {},
            "files": [{"path": "via-link/file.txt", "content": "X"}],
        }))

        with pytest.raises(ValueError):
            restore_snapshot(snap_path, target)

    def test_restore_normal_relative_path_succeeds(self, tmp_path: Path):
        snap_path = tmp_path / "iter-0.json"
        snap_path.write_text(json.dumps({
            "iteration": 0,
            "messages": [],
            "metrics": {},
            "files": [
                {"path": "round-1/sub/dir/attempt.py", "content": "ok\n"},
            ],
        }))
        target = tmp_path / "out"

        restore_snapshot(snap_path, target)

        assert (target / "round-1/sub/dir/attempt.py").read_text() == "ok\n"
