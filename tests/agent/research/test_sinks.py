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
