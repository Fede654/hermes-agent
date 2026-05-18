"""A/B testing framework for research strategies (HRM-110).

Compares different research strategies on the same TaskSpec:
  - sequential (baseline, fan_out=1)
  - fan-out N without MOA
  - fan-out N with MOA aggregation
  - human baseline (no iterations)

Usage::

    from agent.research.ab_testing import ResearchABTester, StrategyConfig
    from agent.research.supervisor import TaskSpec

    tester = ResearchABTester(parent_agent=agent, workspace=Path("/tmp/ab"))
    strategies = [
        StrategyConfig(name="sequential", fan_out=1, max_iterations=3),
        StrategyConfig(name="fanout3", fan_out=3, use_moa=False, max_iterations=3),
        StrategyConfig(name="fanout3_moa", fan_out=3, use_moa=True, max_iterations=3),
    ]
    results = tester.compare(spec, strategies, initial_attempt="", repeats=1)
    print(tester.format_report(results))
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

from agent.research.supervisor import ResearchSupervisor, TaskSpec
from agent.research.runner import ExperimentHistory, ExperimentResult
from agent.research.metrics import UniversalMetricParser

logger = logging.getLogger(__name__)


@dataclass
class StrategyConfig:
    """Configuration for a single strategy in an A/B test."""

    name: str
    fan_out: int = 1
    use_moa: bool = True
    max_iterations: int = 3
    time_budget_sec: int = 0
    keep_threshold: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class StrategyRun:
    """Result of one execution of a strategy."""

    strategy_name: str
    repeat: int
    history: ExperimentHistory
    elapsed_sec: float
    workspace: Path

    @property
    def best_metric(self) -> float | None:
        return self.history.best_result.primary_metric if self.history.best_result else None

    @property
    def baseline_metric(self) -> float | None:
        return self.history.baseline_metric

    @property
    def total_cost_usd(self) -> float:
        return sum(
            (r.cost_usd or 0.0)
            for r in self.history.results
        )

    @property
    def total_tokens_in(self) -> int:
        return sum(
            (r.tokens_in or 0)
            for r in self.history.results
        )

    @property
    def total_tokens_out(self) -> int:
        return sum(
            (r.tokens_out or 0)
            for r in self.history.results
        )

    @property
    def iterations_to_converge(self) -> int:
        return len(self.history.results)

    @property
    def improvement_rate(self) -> float | None:
        if self.baseline_metric is None or self.best_metric is None:
            return None
        if self.baseline_metric == 0:
            return float("inf") if self.best_metric != 0 else 0.0
        return (self.best_metric - self.baseline_metric) / abs(self.baseline_metric)


@dataclass
class StrategySummary:
    """Aggregated statistics across repeats for one strategy."""

    strategy_name: str
    runs: list[StrategyRun] = field(default_factory=list)

    @property
    def best_metrics(self) -> list[float]:
        return [r.best_metric for r in self.runs if r.best_metric is not None]

    @property
    def mean_best_metric(self) -> float | None:
        vals = self.best_metrics
        return sum(vals) / len(vals) if vals else None

    @property
    def std_best_metric(self) -> float | None:
        vals = self.best_metrics
        if len(vals) < 2:
            return 0.0 if vals else None
        mean = sum(vals) / len(vals)
        variance = sum((x - mean) ** 2 for x in vals) / (len(vals) - 1)
        return variance ** 0.5

    @property
    def mean_cost_usd(self) -> float | None:
        vals = [r.total_cost_usd for r in self.runs]
        return sum(vals) / len(vals) if vals else None

    @property
    def mean_elapsed_sec(self) -> float | None:
        vals = [r.elapsed_sec for r in self.runs]
        return sum(vals) / len(vals) if vals else None

    @property
    def mean_iterations(self) -> float | None:
        vals = [r.iterations_to_converge for r in self.runs]
        return sum(vals) / len(vals) if vals else None

    @property
    def mean_improvement_rate(self) -> float | None:
        vals = [r.improvement_rate for r in self.runs if r.improvement_rate is not None]
        return sum(vals) / len(vals) if vals else None

    @property
    def mean_tokens_in(self) -> float | None:
        vals = [r.total_tokens_in for r in self.runs]
        return sum(vals) / len(vals) if vals else None

    @property
    def mean_tokens_out(self) -> float | None:
        vals = [r.total_tokens_out for r in self.runs]
        return sum(vals) / len(vals) if vals else None


class ResearchABTester:
    """Orchestrates A/B tests between research strategies."""

    def __init__(
        self,
        parent_agent: Any,
        workspace: Path,
        *,
        progress_sink: Optional[Any] = None,
        llm: Any = None,
    ) -> None:
        self.parent_agent = parent_agent
        self.workspace = workspace
        self.llm = llm
        self._ab_test_dir = workspace / "ab-tests"
        self._ab_test_dir.mkdir(parents=True, exist_ok=True)

        # Resolve parent sink. The tester owns the close-on-completion;
        # per-strategy sub-sinks must NOT call complete_task themselves
        # (otherwise the first strategy closes the parent kanban task and
        # subsequent strategies comment on a closed task).
        if progress_sink is None:
            from agent.research.sinks import StubSink
            self._parent_sink = StubSink()
        else:
            self._parent_sink = progress_sink

    def _make_supervisor(self) -> ResearchSupervisor:
        # Per-strategy child sink: same identity as the parent sink, but
        # with run_completed-driven task completion suppressed. We only
        # know how to do this for KanbanSink; other sinks are passed
        # through unchanged (Stub is idempotent).
        from agent.research.sinks import KanbanSink
        if isinstance(self._parent_sink, KanbanSink):
            child_sink = KanbanSink(
                task_id=self._parent_sink._task_id,
                db_path=self._parent_sink._db_path,
                complete_on_run_completed=False,
            )
        else:
            child_sink = self._parent_sink

        return ResearchSupervisor(
            parent_agent=self.parent_agent,
            workspace=self.workspace,
            progress_sink=child_sink,
        )

    def _run_single(
        self,
        spec: TaskSpec,
        strategy: StrategyConfig,
        initial_attempt: str,
        run_id: str,
    ) -> StrategyRun:
        """Execute one strategy configuration once."""
        strategy_workspace = self._ab_test_dir / run_id / strategy.name
        strategy_workspace.mkdir(parents=True, exist_ok=True)

        supervisor = self._make_supervisor()
        start = time.monotonic()
        history = supervisor.run(
            spec,
            initial_attempt=initial_attempt,
            run_id=run_id,
            max_iterations=strategy.max_iterations,
            time_budget_sec=strategy.time_budget_sec,
            keep_threshold=strategy.keep_threshold,
            llm=self.llm,
            checkpoint_dir=strategy_workspace / "checkpoints",
            fan_out=strategy.fan_out,
            use_moa=strategy.use_moa,
        )
        elapsed = time.monotonic() - start

        return StrategyRun(
            strategy_name=strategy.name,
            repeat=0,
            history=history,
            elapsed_sec=elapsed,
            workspace=strategy_workspace,
        )

    def compare(
        self,
        spec: TaskSpec,
        strategies: list[StrategyConfig],
        initial_attempt: str = "",
        *,
        repeats: int = 1,
        run_prefix: str = "ab",
    ) -> list[StrategySummary]:
        """Run each strategy ``repeats`` times and return aggregated summaries.

        Args:
            spec: The TaskSpec to test (same for all strategies).
            strategies: List of StrategyConfig to compare.
            initial_attempt: Starting seed.
            repeats: How many times to run each strategy (for variance estimation).
            run_prefix: Prefix for run IDs.

        Returns:
            One StrategySummary per strategy, ordered by input list.
        """
        summaries: list[StrategySummary] = []
        for strategy in strategies:
            summary = StrategySummary(strategy_name=strategy.name)
            for repeat in range(repeats):
                run_id = f"{run_prefix}-{strategy.name}-r{repeat}"
                logger.info(
                    "A/B test: running strategy=%s repeat=%d run_id=%s",
                    strategy.name, repeat, run_id,
                )
                run = self._run_single(spec, strategy, initial_attempt, run_id)
                run.repeat = repeat
                summary.runs.append(run)
            summaries.append(summary)

        # Close the parent kanban task ONCE, after all strategies+repeats
        # are done. Sub-sinks ran with complete_on_run_completed=False so
        # the task stayed open through every strategy's run_completed.
        # We pass the LAST strategy's last run history as the "summary
        # history"; the sink's run_completed only consumes results count
        # and best_metric for the closing comment, both of which are
        # representative enough for the close.
        last_history = (
            summaries[-1].runs[-1].history
            if summaries and summaries[-1].runs
            else None
        )
        if last_history is not None:
            self._parent_sink.run_completed(last_history)
        return summaries

    @staticmethod
    def format_report(summaries: list[StrategySummary]) -> str:
        """Return a human-readable comparison table."""
        lines: list[str] = [
            "# A/B Test Report: Research Strategies",
            "",
            "| Strategy | Best Metric ± std | Improvement | Cost USD | Time (s) | Iterations | Tokens In | Tokens Out |",
            "|----------|-------------------|-------------|----------|----------|------------|-----------|------------|",
        ]
        for s in summaries:
            best = f"{s.mean_best_metric:.4f} ± {s.std_best_metric:.4f}" if s.mean_best_metric is not None else "N/A"
            impr = f"{s.mean_improvement_rate:.2%}" if s.mean_improvement_rate is not None else "N/A"
            cost = f"${s.mean_cost_usd:.4f}" if s.mean_cost_usd is not None else "N/A"
            secs = f"{s.mean_elapsed_sec:.1f}" if s.mean_elapsed_sec is not None else "N/A"
            iters = f"{s.mean_iterations:.1f}" if s.mean_iterations is not None else "N/A"
            tin = f"{s.mean_tokens_in:,.0f}" if s.mean_tokens_in is not None else "N/A"
            tout = f"{s.mean_tokens_out:,.0f}" if s.mean_tokens_out is not None else "N/A"
            lines.append(
                f"| {s.strategy_name:8} | {best:17} | {impr:11} | {cost:8} | {secs:8} | {iters:10} | {tin:9} | {tout:10} |"
            )
        lines.append("")
        # Winner by metric
        by_metric = [(s.mean_best_metric or float("-inf"), s.strategy_name) for s in summaries]
        winner_metric = max(by_metric, key=lambda x: x[0])
        lines.append(f"**Winner by metric**: {winner_metric[1]} ({winner_metric[0]:.4f})")
        # Winner by cost
        by_cost = [(s.mean_cost_usd or float("inf"), s.strategy_name) for s in summaries]
        winner_cost = min(by_cost, key=lambda x: x[0])
        lines.append(f"**Winner by cost**: {winner_cost[1]} (${winner_cost[0]:.4f})")
        # Winner by time
        by_time = [(s.mean_elapsed_sec or float("inf"), s.strategy_name) for s in summaries]
        winner_time = min(by_time, key=lambda x: x[0])
        lines.append(f"**Winner by time**: {winner_time[1]} ({winner_time[0]:.1f}s)")
        lines.append("")
        return "\n".join(lines)

    @staticmethod
    def to_json(summaries: list[StrategySummary]) -> str:
        """Return a JSON-serializable report."""
        data = []
        for s in summaries:
            data.append({
                "strategy": s.strategy_name,
                "repeats": len(s.runs),
                "mean_best_metric": s.mean_best_metric,
                "std_best_metric": s.std_best_metric,
                "mean_improvement_rate": s.mean_improvement_rate,
                "mean_cost_usd": s.mean_cost_usd,
                "mean_elapsed_sec": s.mean_elapsed_sec,
                "mean_iterations": s.mean_iterations,
                "mean_tokens_in": s.mean_tokens_in,
                "mean_tokens_out": s.mean_tokens_out,
                "runs": [
                    {
                        "repeat": r.repeat,
                        "best_metric": r.best_metric,
                        "baseline_metric": r.baseline_metric,
                        "total_cost_usd": r.total_cost_usd,
                        "elapsed_sec": r.elapsed_sec,
                        "iterations": r.iterations_to_converge,
                        "improvement_rate": r.improvement_rate,
                    }
                    for r in s.runs
                ],
            })
        return json.dumps(data, indent=2)
