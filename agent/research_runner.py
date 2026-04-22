"""Experiment execution engine — Karpathy edit→run→eval→keep/discard loop for Hermes.

Ported from researchclaw/experiment/runner.py (aiming-lab/AutoResearchClaw, MIT).
Seams replaced:
  - sandbox.run() → delegate_fn (async callable wrapping delegate_task)
  - git branch/commit/discard → Lattice task lifecycle via lattice_comment_fn
  - ExperimentConfig → HermesExperimentConfig (simple dataclass, no researchclaw deps)
"""

from __future__ import annotations

import json
import logging
import re
import time as _time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional, Protocol, cast

logger = logging.getLogger(__name__)


@dataclass
class HermesExperimentConfig:
    """Minimal experiment config — replaces researchclaw ExperimentConfig."""
    metric_key: str = "primary_metric"
    metric_direction: str = "maximize"   # "maximize" or "minimize"
    time_budget_sec: int = 300
    max_iterations: int = 5
    keep_threshold: float = 0.0          # min abs delta to consider "kept"


@dataclass
class DelegateSandboxResult:
    """Adapter wrapping delegate_task JSON result into sandbox-like shape."""
    metrics: dict[str, object]
    stdout: str
    stderr: str
    elapsed_sec: float
    timed_out: bool = False
    returncode: int = 0
    error: Optional[str] = None


@dataclass(frozen=True)
class ExperimentResult:
    run_id: str
    iteration: int
    code: str
    metrics: dict[str, object]
    primary_metric: float | None
    improved: bool
    kept: bool
    elapsed_sec: float
    stdout: str
    stderr: str
    error: str | None = None


@dataclass
class ExperimentHistory:
    results: list[ExperimentResult] = field(default_factory=list)
    best_result: ExperimentResult | None = None
    baseline_metric: float | None = None

    def add(self, result: ExperimentResult) -> None:
        self.results.append(result)
        if self.baseline_metric is None and result.primary_metric is not None:
            self.baseline_metric = result.primary_metric

    def to_dict(self) -> dict[str, object]:
        return {
            "results": [asdict(result) for result in self.results],
            "best_result": asdict(self.best_result) if self.best_result else None,
            "baseline_metric": self.baseline_metric,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> ExperimentHistory:
        results: list[ExperimentResult] = []
        raw_results = data.get("results")
        if isinstance(raw_results, list):
            for item in cast(list[object], raw_results):
                if isinstance(item, dict):
                    item_map = cast(dict[object, object], item)
                    normalized_item: dict[str, object] = {}
                    for key, value in item_map.items():
                        normalized_item[str(key)] = value
                    parsed = _result_from_dict(normalized_item)
                    if parsed is not None:
                        results.append(parsed)
        best_raw = data.get("best_result")
        best_result = (
            _result_from_dict(
                {
                    str(key): value
                    for key, value in cast(dict[object, object], best_raw).items()
                }
            )
            if isinstance(best_raw, dict)
            else None
        )
        baseline_metric_raw = data.get("baseline_metric")
        baseline_metric = (
            float(baseline_metric_raw)
            if isinstance(baseline_metric_raw, (int, float))
            else None
        )
        return cls(
            results=results, best_result=best_result, baseline_metric=baseline_metric
        )


class _ChatResponse(Protocol):
    content: str


class _ChatClient(Protocol):
    def chat(
        self, messages: list[dict[str, str]], *, system: str | None = None
    ) -> _ChatResponse: ...


class ExperimentRunner:
    """Karpathy inner loop: baseline → iterate → improve/discard, wired to Hermes delegate_task.

    Args:
        config: HermesExperimentConfig with metric_key, direction, budget, iterations.
        workspace: Directory for round artefacts.
        delegate_fn: Callable(goal: str, working_dir: str) -> DelegateSandboxResult.
            Wraps delegate_task; caller is responsible for spawning the worker.
        lattice_comment_fn: Optional callable(msg: str) -> None for posting round summaries.
    """

    def __init__(
        self,
        config: "HermesExperimentConfig",
        workspace: Path,
        *,
        delegate_fn: Callable[[str, str], "DelegateSandboxResult"],
        lattice_comment_fn: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.config: HermesExperimentConfig = config
        self.workspace: Path = workspace
        self.workspace.mkdir(parents=True, exist_ok=True)
        self._delegate_fn = delegate_fn
        self._lattice_comment = lattice_comment_fn or (lambda msg: None)
        self.history: ExperimentHistory = ExperimentHistory()

    def run_experiment(
        self, code: str, *, run_id: str, iteration: int = 0
    ) -> ExperimentResult:
        """Run one experiment round via delegate_task and score the result."""
        t0 = _time.monotonic()
        round_dir = str(self.workspace / f"round-{run_id}-iter{iteration}")
        goal = (
            f"You are a Hermes research worker. Read program.md in {round_dir} "
            f"and run the experiment. Report your result as:\n"
            f"METRIC: {self.config.metric_key}=<value> STATUS: improved|regressed|neutral "
            f"NOTES: <one line summary>"
        )

        try:
            sandbox_result = self._delegate_fn(goal, round_dir)
        except Exception as exc:
            elapsed = _time.monotonic() - t0
            logger.exception("delegate_fn failed for %s iter %d: %s", run_id, iteration, exc)
            sandbox_result = DelegateSandboxResult(
                metrics={}, stdout="", stderr=str(exc), elapsed_sec=elapsed,
                timed_out=False, returncode=1, error=str(exc),
            )

        primary_metric = self._to_float(
            sandbox_result.metrics.get(self.config.metric_key)
        )
        current_best = (
            self.history.best_result.primary_metric
            if self.history.best_result
            else None
        )

        improved = False
        kept = False

        if primary_metric is not None:
            if current_best is None:
                improved = True
                kept = True
            elif self._is_improvement(primary_metric, current_best):
                improved = True
                kept = abs(primary_metric - current_best) > self.config.keep_threshold

        error: str | None = sandbox_result.error
        if not error and sandbox_result.timed_out:
            error = f"Timed out after {self.config.time_budget_sec}s"
        elif not error and sandbox_result.returncode != 0:
            error = sandbox_result.stderr.strip() or f"Process exited with {sandbox_result.returncode}"

        result = ExperimentResult(
            run_id=run_id,
            iteration=iteration,
            code=code,
            metrics=sandbox_result.metrics,
            primary_metric=primary_metric,
            improved=improved,
            kept=kept,
            elapsed_sec=sandbox_result.elapsed_sec,
            stdout=sandbox_result.stdout,
            stderr=sandbox_result.stderr,
            error=error,
        )

        if kept:
            self.history.best_result = result

        self.history.add(result)

        # Post Lattice comment summarising this round
        status_word = "KEPT" if kept else ("IMPROVED" if improved else "DISCARDED")
        self._lattice_comment(
            f"Round {run_id} iter {iteration}: {status_word} "
            f"{self.config.metric_key}={primary_metric} "
            f"(best={current_best})"
        )
        return result

    def run_loop(
        self, initial_code: str, *, run_id: str, llm: "_ChatClient | None" = None
    ) -> ExperimentHistory:
        """Karpathy inner loop: baseline → iterate → keep/discard."""
        self._lattice_comment(f"Research loop started: run_id={run_id}")
        current_code = initial_code
        baseline = self.run_experiment(current_code, run_id=run_id, iteration=0)

        if llm is None:
            return self.history

        no_improvement_count = 0
        for iteration in range(1, self.config.max_iterations + 1):
            next_code = self._improve_code(llm, current_code, self.history)
            result = self.run_experiment(next_code, run_id=run_id, iteration=iteration)
            current_code = next_code

            if result.improved:
                no_improvement_count = 0
            else:
                no_improvement_count += 1

            if no_improvement_count >= 3:
                logger.info("Stopping early: 3 non-improving iterations for %s", run_id)
                self._lattice_comment(f"Early stop after {iteration} iterations (3 non-improving)")
                break

        self._lattice_comment(
            f"Research loop done: {len(self.history.results)} rounds, "
            f"best={self.history.best_result.primary_metric if self.history.best_result else None}"
        )
        return self.history

    def _improve_code(
        self, llm: _ChatClient, current_code: str, history: ExperimentHistory
    ) -> str:
        direction = self.config.metric_direction
        last_result = history.results[-1] if history.results else None
        last_metrics = last_result.metrics if last_result else {}
        best_metrics = history.best_result.metrics if history.best_result else {}
        last_metric = last_result.primary_metric if last_result else None
        best_metric = (
            history.best_result.primary_metric if history.best_result else None
        )

        prompt = (
            "Improve the experiment code to optimize the primary metric.\n\n"
            f"Metric key: {self.config.metric_key}\n"
            f"Direction: {direction}\n"
            f"Last primary metric: {last_metric}\n"
            f"Best primary metric: {best_metric}\n"
            f"Last metrics JSON: {json.dumps(last_metrics, ensure_ascii=True)}\n"
            f"Best metrics JSON: {json.dumps(best_metrics, ensure_ascii=True)}\n\n"
            "Current code:\n"
            "```python\n"
            f"{current_code}\n"
            "```\n\n"
            "Return only the updated Python code."
        )

        try:
            response = llm.chat(
                [{"role": "user", "content": prompt}],
                system="You are an expert ML experimentation assistant.",
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Code improvement call failed: %s", exc)
            return current_code

        candidate = getattr(response, "content", "")
        if not isinstance(candidate, str) or not candidate.strip():
            logger.warning("LLM returned empty code; keeping current version")
            return current_code

        extracted = self._extract_python_code(candidate)
        return extracted if extracted.strip() else current_code

    def save_history(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_text(
            json.dumps(self.history.to_dict(), indent=2), encoding="utf-8"
        )

    def _is_improvement(self, new_value: float, best_value: float) -> bool:
        if self.config.metric_direction == "maximize":
            return new_value > best_value
        return new_value < best_value

    @staticmethod
    def _to_float(value: object) -> float | None:
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value)
            except ValueError:
                return None
        return None

    @staticmethod
    def _extract_python_code(content: str) -> str:
        match = re.search(r"```(?:python)?\s*(.*?)\s*```", content, flags=re.DOTALL)
        if match is None:
            return content.strip()
        return match.group(1).strip()


def _result_from_dict(data: dict[str, object]) -> ExperimentResult | None:
    run_id = data.get("run_id")
    iteration = data.get("iteration")
    code = data.get("code")
    metrics = data.get("metrics")
    primary_metric = data.get("primary_metric")
    improved = data.get("improved")
    kept = data.get("kept")
    elapsed_sec = data.get("elapsed_sec")
    stdout = data.get("stdout")
    stderr = data.get("stderr")
    error = data.get("error")

    if not isinstance(run_id, str) or not isinstance(iteration, int):
        return None
    if not isinstance(code, str) or not isinstance(metrics, dict):
        return None
    if primary_metric is not None and not isinstance(primary_metric, (int, float)):
        return None
    if not isinstance(improved, bool) or not isinstance(kept, bool):
        return None
    if not isinstance(elapsed_sec, (int, float)):
        return None
    if not isinstance(stdout, str) or not isinstance(stderr, str):
        return None
    if error is not None and not isinstance(error, str):
        return None

    typed_metrics: dict[str, object] = {}
    for key, value in cast(dict[object, object], metrics).items():
        typed_metrics[str(key)] = value
    return ExperimentResult(
        run_id=run_id,
        iteration=iteration,
        code=code,
        metrics=typed_metrics,
        primary_metric=float(primary_metric)
        if isinstance(primary_metric, (int, float))
        else None,
        improved=improved,
        kept=kept,
        elapsed_sec=float(elapsed_sec),
        stdout=stdout,
        stderr=stderr,
        error=error,
    )
