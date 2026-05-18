"""Unit tests for agent.research.ab_testing (HRM-110).

No integration mark needed — these tests use mocked ExperimentHistory
objects and verify aggregation / reporting logic only.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from agent.research.ab_testing import (
    ResearchABTester,
    StrategyConfig,
    StrategyRun,
    StrategySummary,
)
from agent.research.runner import ExperimentHistory, ExperimentResult


# ---------------------------------------------------------------------------
# Helpers — build minimal ExperimentHistory without running real workers
# ---------------------------------------------------------------------------

def _make_result(
    iteration: int,
    primary_metric: float | None,
    cost_usd: float = 0.0,
    tokens_in: int = 0,
    tokens_out: int = 0,
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


def _make_history(
    baseline_metric: float | None,
    results: list[ExperimentResult],
) -> ExperimentHistory:
    hist = ExperimentHistory(baseline_metric=baseline_metric)
    for r in results:
        hist.add(r)
    # best_result is not auto-updated by add(), so set it manually for tests
    best = max(
        (r for r in results if r.primary_metric is not None),
        key=lambda r: r.primary_metric or float("-inf"),
        default=None,
    )
    hist.best_result = best
    return hist


# ---------------------------------------------------------------------------
# StrategyConfig
# ---------------------------------------------------------------------------

class TestStrategyConfig:
    def test_to_dict(self):
        cfg = StrategyConfig(name="foo", fan_out=3, use_moa=False, max_iterations=5)
        d = cfg.to_dict()
        assert d == {
            "name": "foo",
            "fan_out": 3,
            "use_moa": False,
            "max_iterations": 5,
            "time_budget_sec": 0,
            "keep_threshold": 0.0,
        }


# ---------------------------------------------------------------------------
# StrategyRun
# ---------------------------------------------------------------------------

class TestStrategyRun:
    def test_properties(self):
        hist = _make_history(
            baseline_metric=0.5,
            results=[
                _make_result(0, 0.5, cost_usd=0.1, tokens_in=100, tokens_out=50),
                _make_result(1, 0.7, cost_usd=0.2, tokens_in=200, tokens_out=100, improved=True),
            ],
        )
        run = StrategyRun(
            strategy_name="seq",
            repeat=0,
            history=hist,
            elapsed_sec=10.0,
            workspace=Path("/tmp"),
        )
        assert run.best_metric == 0.7
        assert run.baseline_metric == 0.5
        assert run.total_cost_usd == pytest.approx(0.3)
        assert run.total_tokens_in == 300
        assert run.total_tokens_out == 150
        assert run.iterations_to_converge == 2
        assert run.improvement_rate == pytest.approx((0.7 - 0.5) / 0.5)

    def test_improvement_rate_zero_baseline(self):
        hist = _make_history(
            baseline_metric=0.0,
            results=[
                _make_result(0, 0.0),
                _make_result(1, 0.0),
            ],
        )
        run = StrategyRun(
            strategy_name="seq", repeat=0, history=hist, elapsed_sec=1.0, workspace=Path("/tmp")
        )
        assert run.improvement_rate == 0.0

    def test_improvement_rate_inf(self):
        hist = _make_history(
            baseline_metric=0.0,
            results=[
                _make_result(0, 0.0),
                _make_result(1, 0.5, improved=True),
            ],
        )
        run = StrategyRun(
            strategy_name="seq", repeat=0, history=hist, elapsed_sec=1.0, workspace=Path("/tmp")
        )
        assert run.improvement_rate == float("inf")


# ---------------------------------------------------------------------------
# StrategySummary
# ---------------------------------------------------------------------------

class TestStrategySummary:
    def test_mean_and_std(self):
        runs = [
            StrategyRun(
                strategy_name="s",
                repeat=i,
                history=_make_history(
                    baseline_metric=0.5,
                    results=[_make_result(0, 0.5 + i * 0.1)],
                ),
                elapsed_sec=10.0 + i,
                workspace=Path("/tmp"),
            )
            for i in range(3)
        ]
        summary = StrategySummary(strategy_name="s", runs=runs)
        assert summary.mean_best_metric == pytest.approx((0.5 + 0.6 + 0.7) / 3)
        assert summary.std_best_metric is not None
        assert summary.std_best_metric > 0
        assert summary.mean_elapsed_sec == pytest.approx((10 + 11 + 12) / 3)
        assert summary.mean_iterations == pytest.approx(1.0)

    def test_empty_runs(self):
        summary = StrategySummary(strategy_name="empty")
        assert summary.mean_best_metric is None
        assert summary.std_best_metric is None


# ---------------------------------------------------------------------------
# ResearchABTester formatting
# ---------------------------------------------------------------------------

class TestResearchABTesterFormatting:
    def test_format_report(self):
        summaries = [
            StrategySummary(
                strategy_name="sequential",
                runs=[
                    StrategyRun(
                        strategy_name="sequential",
                        repeat=0,
                        history=_make_history(
                            baseline_metric=0.5,
                            results=[
                                _make_result(0, 0.5, cost_usd=0.1),
                                _make_result(1, 0.6, cost_usd=0.1, improved=True),
                            ],
                        ),
                        elapsed_sec=10.0,
                        workspace=Path("/tmp"),
                    ),
                ],
            ),
            StrategySummary(
                strategy_name="fanout3",
                runs=[
                    StrategyRun(
                        strategy_name="fanout3",
                        repeat=0,
                        history=_make_history(
                            baseline_metric=0.5,
                            results=[
                                _make_result(0, 0.5, cost_usd=0.1),
                                _make_result(1, 0.65, cost_usd=0.2, improved=True),
                            ],
                        ),
                        elapsed_sec=15.0,
                        workspace=Path("/tmp"),
                    ),
                ],
            ),
        ]
        report = ResearchABTester.format_report(summaries)
        assert "A/B Test Report" in report
        assert "sequential" in report
        assert "fanout3" in report
        assert "Winner by metric" in report
        assert "Winner by cost" in report

    def test_to_json(self):
        summaries = [
            StrategySummary(
                strategy_name="seq",
                runs=[
                    StrategyRun(
                        strategy_name="seq",
                        repeat=0,
                        history=_make_history(
                            baseline_metric=0.5,
                            results=[_make_result(0, 0.5)],
                        ),
                        elapsed_sec=5.0,
                        workspace=Path("/tmp"),
                    ),
                ],
            ),
        ]
        raw = ResearchABTester.to_json(summaries)
        data = json.loads(raw)
        assert len(data) == 1
        assert data[0]["strategy"] == "seq"
        assert data[0]["mean_best_metric"] == 0.5
        assert data[0]["repeats"] == 1


# ---------------------------------------------------------------------------
# ResearchABTester.compare mocking
# ---------------------------------------------------------------------------

class TestResearchABTesterCompare:
    def test_compare_runs_each_strategy(self, tmp_path: Path, monkeypatch: Any):
        """Mock supervisor.run so compare() executes without real workers."""
        from agent.research.supervisor import ResearchSupervisor

        call_log: list[tuple[str, int, bool]] = []

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
            call_log.append((run_id, fan_out, use_moa))
            return _make_history(
                baseline_metric=0.5,
                results=[_make_result(0, 0.5 + fan_out * 0.05)],
            )

        monkeypatch.setattr(ResearchSupervisor, "run", _fake_run)

        tester = ResearchABTester(
            parent_agent=object(),
            workspace=tmp_path,
        )
        from agent.research.supervisor import TaskSpec

        spec = TaskSpec(
            topic="test",
            deliverable="test",
            metric_key="accuracy",
        )
        strategies = [
            StrategyConfig(name="seq", fan_out=1),
            StrategyConfig(name="fan3", fan_out=3, use_moa=False),
        ]
        summaries = tester.compare(spec, strategies, repeats=2)

        assert len(summaries) == 2
        assert len(summaries[0].runs) == 2
        assert len(summaries[1].runs) == 2
        # Each run should have been called with correct fan_out / use_moa
        assert any(fan_out == 1 for _, fan_out, _ in call_log)
        assert any(fan_out == 3 for _, fan_out, _ in call_log)
