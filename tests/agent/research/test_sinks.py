"""Tests for ProgressSink Protocol and concrete sink implementations."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agent.research.runner import ExperimentResult
from agent.research.sinks import StubSink
from agent.research.supervisor import TaskSpec


def _spec() -> TaskSpec:
    return TaskSpec(
        topic="t", deliverable="d",
        metric_key="pass_rate", metric_direction="maximize",
    )


def _result(iteration: int = 0, primary_metric: float = 0.5) -> ExperimentResult:
    return ExperimentResult(
        run_id="rid", iteration=iteration, code="",
        metrics={"pass_rate": str(primary_metric)},
        primary_metric=primary_metric,
        improved=True, kept=True,
        elapsed_sec=0.1, stdout="", stderr="", error=None,
    )


class TestStubSink:
    def test_run_started_does_not_raise(self):
        StubSink().run_started(_spec(), "rid")

    def test_iteration_observed_does_not_raise(self, tmp_path: Path):
        StubSink().iteration_observed(0, _result(), tmp_path)

    def test_run_completed_does_not_raise(self):
        history = MagicMock()
        history.results = [_result()]
        history.best_result = _result()
        StubSink().run_completed(history)

    def test_comment_does_not_raise(self):
        StubSink().comment("hello")


import sqlite3

from hermes_cli import kanban_db

from agent.research.sinks import KanbanSink


@pytest.fixture
def kanban_db_path(tmp_path, monkeypatch):
    """Pin a clean kanban.db path. Use HERMES_KANBAN_DB so kanban_db_path()
    resolves through the env override (highest-precedence) and we skip
    the board / current-board state machine entirely. connect() auto-
    initializes the schema, so we don't call init_db().
    """
    db_path = tmp_path / "kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    return db_path


@pytest.fixture
def kanban_conn(kanban_db_path):
    """Helper: an OPEN connection for tests that want to inspect/insert
    directly. Production code (KanbanSink) opens its own short-lived
    connection per call; this fixture is for test setup + assertion only."""
    conn = kanban_db.connect(kanban_db_path)
    yield conn
    conn.close()


class TestKanbanSink:
    def test_existing_task_id_appends_comments(self, kanban_db_path, kanban_conn):
        task_id = kanban_db.create_task(
            kanban_conn, title="parent run", body="research run wrapper",
            created_by="test",
        )
        sink = KanbanSink(task_id=task_id, db_path=kanban_db_path)
        sink.run_started(_spec(), "rid-001")
        sink.iteration_observed(0, _result(0, 0.5), Path("/tmp"))
        sink.iteration_observed(1, _result(1, 0.7), Path("/tmp"))

        comments = kanban_db.list_comments(kanban_conn, task_id)
        assert len(comments) == 3
        assert "rid-001" in comments[0].body
        assert "0.5" in comments[1].body
        assert "0.7" in comments[2].body

    def test_run_completed_completes_task(self, kanban_db_path, kanban_conn):
        task_id = kanban_db.create_task(
            kanban_conn, title="r", body="b", created_by="test",
        )
        sink = KanbanSink(task_id=task_id, db_path=kanban_db_path)
        history = MagicMock()
        history.results = [_result(0, 0.5), _result(1, 0.9)]
        history.best_result = _result(1, 0.9)
        sink.run_completed(history)

        task = kanban_db.get_task(kanban_conn, task_id)
        assert task.status == "done"

    def test_complete_on_run_completed_false_keeps_task_open(
        self, kanban_db_path, kanban_conn,
    ):
        """A/B testing case: per-strategy sub-sinks must NOT close the task."""
        task_id = kanban_db.create_task(
            kanban_conn, title="r", body="b", created_by="test",
        )
        sink = KanbanSink(
            task_id=task_id,
            db_path=kanban_db_path,
            complete_on_run_completed=False,
        )
        history = MagicMock()
        history.results = [_result(0, 0.9)]
        history.best_result = _result(0, 0.9)
        sink.run_completed(history)

        task = kanban_db.get_task(kanban_conn, task_id)
        assert task.status != "done"

    def test_no_task_id_is_log_only(self, kanban_db_path, kanban_conn):
        sink = KanbanSink(task_id=None, db_path=kanban_db_path)
        sink.run_started(_spec(), "rid")
        sink.iteration_observed(0, _result(), Path("/tmp"))
        sink.run_completed(MagicMock(results=[], best_result=None))
        sink.comment("hi")
        # No task should have been created.
        all_tasks = kanban_db.list_tasks(kanban_conn)
        assert len(all_tasks) == 0

    def test_db_error_does_not_raise(
        self, kanban_db_path, kanban_conn, monkeypatch,
    ):
        task_id = kanban_db.create_task(
            kanban_conn, title="r", body="b", created_by="test",
        )

        def boom(*a, **kw):
            raise sqlite3.OperationalError("forced")

        monkeypatch.setattr(kanban_db, "add_comment", boom)
        sink = KanbanSink(task_id=task_id, db_path=kanban_db_path)
        # Must not raise.
        sink.run_started(_spec(), "rid")
        sink.iteration_observed(0, _result(), Path("/tmp"))
