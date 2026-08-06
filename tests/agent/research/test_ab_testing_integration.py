"""Integration test for ResearchABTester end-to-end with mocked workers.

Runs a synthetic A/B test comparing sequential vs fan-out strategies
without real LLM calls or subagents.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from agent.research.ab_testing import ResearchABTester, StrategyConfig
from agent.research.runner import ExperimentHistory, ExperimentResult
from agent.research.supervisor import ResearchSupervisor, TaskSpec


def _make_result(
    iteration: int,
    primary_metric: float | None,
    cost_usd: float = 0.05,
    tokens_in: int = 100,
    tokens_out: int = 50,
    improved: bool = False,
    code: str = "",
) -> ExperimentResult:
    return ExperimentResult(
        run_id="run-1",
        iteration=iteration,
        code=code,
        metrics={"accuracy": primary_metric} if primary_metric is not None else {},
        primary_metric=primary_metric,
        improved=improved,
        kept=improved,
        elapsed_sec=1.0,
        stdout="",
        stderr="",
        cost_usd=cost_usd,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
    )


def _make_history(baseline: float, best: float, iterations: int = 2) -> ExperimentHistory:
    results = [_make_result(0, baseline, cost_usd=0.05, tokens_in=100, tokens_out=50)]
    for i in range(1, iterations):
        results.append(
            _make_result(
                i,
                baseline + (best - baseline) * (i / (iterations - 1)),
                cost_usd=0.05,
                tokens_in=100,
                tokens_out=50,
                improved=True,
            )
        )
    hist = ExperimentHistory(baseline_metric=baseline)
    for r in results:
        hist.add(r)
    hist.best_result = max(
        (r for r in results if r.primary_metric is not None),
        key=lambda r: r.primary_metric or float("-inf"),
    )
    return hist


@pytest.mark.integration
def test_ab_test_end_to_end(tmp_path: Path, monkeypatch: Any) -> None:
    """Run a synthetic A/B test: sequential vs fan-out."""

    def _fake_run(
        self: Any,
        spec: Any,
        initial_attempt: str,
        *,
        run_id: str,
        max_iterations: int = 5,
        time_budget_sec: int = 0,
        keep_threshold: float = 0.0,
        llm: Any = None,
        worker_toolsets: Any = None,
        checkpoint_dir: Any = None,
        fan_out: int = 1,
        use_moa: bool = True,
    ) -> ExperimentHistory:
        # Simulate that fan-out achieves slightly better metric
        baseline = 0.5
        best = 0.6 if fan_out == 1 else 0.7
        return _make_history(baseline, best, iterations=max_iterations + 1)

    monkeypatch.setattr(ResearchSupervisor, "run", _fake_run)

    tester = ResearchABTester(
        parent_agent=MagicMock(),
        workspace=tmp_path,
    )

    spec = TaskSpec(
        topic="Synthetic benchmark",
        deliverable="Dummy deliverable",
        metric_key="accuracy",
    )

    strategies = [
        StrategyConfig(name="sequential", fan_out=1, max_iterations=2),
        StrategyConfig(name="fanout2", fan_out=2, use_moa=False, max_iterations=2),
    ]

    summaries = tester.compare(spec, strategies, initial_attempt="", repeats=1)

    assert len(summaries) == 2
    seq, fan = summaries
    assert seq.strategy_name == "sequential"
    assert fan.strategy_name == "fanout2"

    # Fan-out should show better metric in this synthetic scenario
    assert fan.mean_best_metric == pytest.approx(0.7)
    assert seq.mean_best_metric == pytest.approx(0.6)

    # Report should contain both strategies and winner lines
    report = tester.format_report(summaries)
    assert "sequential" in report
    assert "fanout2" in report
    assert "Winner by metric" in report
    assert "Winner by cost" in report

    # JSON should round-trip
    raw = tester.to_json(summaries)
    data = json.loads(raw)
    assert len(data) == 2
    assert data[1]["strategy"] == "fanout2"
    assert data[1]["mean_best_metric"] == pytest.approx(0.7)
