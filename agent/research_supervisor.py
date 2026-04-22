"""ResearchSupervisor — Karpathy inner loop wired to Hermes delegate_task + Lattice.

Orchestrates the 5-step research loop:
  1. HYPOTHESIZE  — caller provides topic, hypothesis, initial code
  2. PROGRAM      — supervisor writes program.md + main.py into round directory
  3. DELEGATE     — spawns a research worker via delegate_task
  4. METRIC       — UniversalMetricParser extracts metric from worker output
  5. KEEP/DISCARD — ExperimentRunner keeps improvements, discards regressions
"""

from __future__ import annotations

import json
import logging
import time as _time
from pathlib import Path
from typing import Any, Callable, Optional

from agent.research_runner import (
    DelegateSandboxResult,
    ExperimentHistory,
    ExperimentRunner,
    HermesExperimentConfig,
)
from agent.research_metrics import UniversalMetricParser

logger = logging.getLogger(__name__)

_parser = UniversalMetricParser()

# ---------------------------------------------------------------------------
# program.md template
# ---------------------------------------------------------------------------

def _build_program_md(
    *,
    topic: str,
    hypothesis: str,
    metric_key: str,
    metric_direction: str,
    time_budget_sec: int,
    iteration: int,
    round_dir: str,
) -> str:
    """Generate program.md for the research worker to read."""
    return f"""\
# Hermes Research Experiment

## Topic
{topic}

## Hypothesis (iteration {iteration})
{hypothesis}

## Objective
Optimize **{metric_key}** ({metric_direction}).

Time budget: {time_budget_sec} seconds.

## Instructions

1. Read and run `main.py` in this directory: `{round_dir}`
2. Collect the primary metric `{metric_key}`.
3. Write results to `results.json` if possible (structured output).
4. Print a metric line as your **final output**:

```
METRIC: {metric_key}=<value> STATUS: improved|regressed|neutral NOTES: <one line>
```

## Time Guard

Implement a time guard: check `time.monotonic()` periodically.
Stop gracefully before 80% of the {time_budget_sec}s budget and save all results so far.
Print `TIME_ESTIMATE: Xs` before your main loop.

## Anti-patterns

- Do NOT invent or fabricate metric values.
- Do NOT print other `key: value` lines unless they are real metrics.
- Do NOT make network calls; keep the experiment self-contained.
"""


# ---------------------------------------------------------------------------
# delegate_task bridge
# ---------------------------------------------------------------------------

def _call_delegate_task(
    goal: str,
    context: str,
    *,
    parent_agent: Any,
    toolsets: list[str] | None = None,
) -> dict[str, Any]:
    """Call delegate_task and return the parsed JSON result dict."""
    from tools.delegate_tool import delegate_task

    raw = delegate_task(
        goal=goal,
        context=context,
        toolsets=toolsets or ["terminal", "file"],
        parent_agent=parent_agent,
    )
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {"results": [{"status": "failed", "summary": raw or "", "error": "JSON parse failed"}]}


# ---------------------------------------------------------------------------
# Lattice comment bridge
# ---------------------------------------------------------------------------

def _make_lattice_comment_fn(
    lattice_task_id: Optional[str],
    lattice_root: str,
) -> Callable[[str], None]:
    """Return a function that posts a comment to a Lattice task."""
    if not lattice_task_id:
        return lambda msg: logger.info("[lattice-stub] %s", msg)

    def _comment(msg: str) -> None:
        try:
            import subprocess
            subprocess.run(
                ["lattice", "comment", lattice_task_id, msg, "--actor", "agent:research-supervisor"],
                cwd=lattice_root,
                capture_output=True,
                timeout=10,
            )
        except Exception as exc:
            logger.warning("Lattice comment failed: %s", exc)

    return _comment


# ---------------------------------------------------------------------------
# ResearchSupervisor
# ---------------------------------------------------------------------------

class ResearchSupervisor:
    """Orchestrates the Hermes Karpathy research loop.

    Args:
        parent_agent: The live AIAgent instance (required for delegate_task).
        workspace: Root directory for round artefacts.
        lattice_task_id: Lattice task ID to post round comments to (optional).
        lattice_root: Path to the project directory containing .lattice/.
    """

    def __init__(
        self,
        *,
        parent_agent: Any,
        workspace: Path | None = None,
        lattice_task_id: Optional[str] = None,
        lattice_root: str = "/home/fede/.hermes/org",
    ) -> None:
        self._parent_agent = parent_agent
        self._workspace = workspace or (Path.home() / ".hermes" / "research-workspace")
        self._lattice_task_id = lattice_task_id
        self._lattice_root = lattice_root

    def run(
        self,
        topic: str,
        hypothesis: str,
        initial_code: str,
        *,
        run_id: str,
        metric_key: str = "primary_metric",
        metric_direction: str = "maximize",
        max_iterations: int = 5,
        time_budget_sec: int = 300,
        keep_threshold: float = 0.0,
        llm: Any = None,
        worker_toolsets: list[str] | None = None,
    ) -> ExperimentHistory:
        """Run the full Karpathy research loop.

        Args:
            topic: Research topic description.
            hypothesis: Initial hypothesis to test.
            initial_code: Python code string for the baseline experiment.
            run_id: Unique identifier for this research run.
            metric_key: Metric name to optimize (e.g. "accuracy", "loss").
            metric_direction: "maximize" or "minimize".
            max_iterations: Maximum code improvement iterations.
            time_budget_sec: Time budget per worker invocation (seconds).
            keep_threshold: Min absolute metric delta to count as "kept".
            llm: LLM client for code improvement (None = baseline only).
            worker_toolsets: Toolsets for research workers (default: ["terminal", "file"]).

        Returns:
            ExperimentHistory with all round results and the best result.
        """
        config = HermesExperimentConfig(
            metric_key=metric_key,
            metric_direction=metric_direction,
            time_budget_sec=time_budget_sec,
            max_iterations=max_iterations,
            keep_threshold=keep_threshold,
        )

        lattice_comment_fn = _make_lattice_comment_fn(
            self._lattice_task_id, self._lattice_root
        )

        # Mutable ref so the delegate_fn always writes the current code
        code_holder: list[str] = [initial_code]

        def delegate_fn(goal: str, working_dir: str) -> DelegateSandboxResult:
            return self._run_worker(
                goal=goal,
                working_dir=working_dir,
                code=code_holder[0],
                topic=topic,
                hypothesis=hypothesis,
                metric_key=metric_key,
                metric_direction=metric_direction,
                time_budget_sec=time_budget_sec,
                iteration=_extract_iteration(working_dir),
                worker_toolsets=worker_toolsets,
            )

        runner = ExperimentRunner(
            config=config,
            workspace=self._workspace / run_id,
            delegate_fn=delegate_fn,
            lattice_comment_fn=lattice_comment_fn,
        )

        lattice_comment_fn(f"Research loop started: run_id={run_id} topic={topic[:60]}")

        # Baseline
        runner.run_experiment(initial_code, run_id=run_id, iteration=0)

        if llm is None:
            lattice_comment_fn(f"Baseline only (no LLM). Best={runner.history.baseline_metric}")
            return runner.history

        # Improvement loop
        no_improvement = 0
        for iteration in range(1, max_iterations + 1):
            next_code = runner._improve_code(llm, code_holder[0], runner.history)
            code_holder[0] = next_code  # update before run_experiment calls delegate_fn
            result = runner.run_experiment(next_code, run_id=run_id, iteration=iteration)

            if result.improved:
                no_improvement = 0
            else:
                no_improvement += 1

            if no_improvement >= 3:
                logger.info("Early stop: 3 non-improving iterations for %s", run_id)
                lattice_comment_fn(
                    f"Early stop after {iteration} iterations (3 non-improving)"
                )
                break

        best = runner.history.best_result
        lattice_comment_fn(
            f"Research loop done: {len(runner.history.results)} rounds, "
            f"best={best.primary_metric if best else None}"
        )
        return runner.history

    def _run_worker(
        self,
        *,
        goal: str,
        working_dir: str,
        code: str,
        topic: str,
        hypothesis: str,
        metric_key: str,
        metric_direction: str,
        time_budget_sec: int,
        iteration: int,
        worker_toolsets: list[str] | None,
    ) -> DelegateSandboxResult:
        """Write program.md + main.py, spawn delegate_task, parse result."""
        t0 = _time.monotonic()
        wd = Path(working_dir)
        wd.mkdir(parents=True, exist_ok=True)

        # Write experiment files
        (wd / "main.py").write_text(code, encoding="utf-8")
        program_md = _build_program_md(
            topic=topic,
            hypothesis=hypothesis,
            metric_key=metric_key,
            metric_direction=metric_direction,
            time_budget_sec=time_budget_sec,
            iteration=iteration,
            round_dir=working_dir,
        )
        (wd / "program.md").write_text(program_md, encoding="utf-8")

        context = (
            f"Working directory: {working_dir}\n"
            f"Topic: {topic}\n"
            f"Metric key: {metric_key}\n"
            f"Read program.md for full instructions, then run main.py."
        )

        result = _call_delegate_task(
            goal,
            context,
            parent_agent=self._parent_agent,
            toolsets=worker_toolsets or ["terminal", "file"],
        )

        elapsed = _time.monotonic() - t0
        first = result.get("results", [{}])[0] if result.get("results") else {}
        summary = first.get("summary") or ""
        status = first.get("status", "failed")

        # Parse metrics: JSON/CSV files first, then stdout fallback
        parsed = _parser.parse(wd, stdout=summary)
        metrics: dict[str, object] = {k: v for k, v in parsed.to_flat_metrics().items()}

        completed = status == "completed"
        error: str | None = None
        if not completed:
            error = first.get("error") or f"Worker status: {status}"

        return DelegateSandboxResult(
            metrics=metrics,
            stdout=summary,
            stderr="",
            elapsed_sec=elapsed,
            timed_out=False,
            returncode=0 if completed else 1,
            error=error,
        )


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _extract_iteration(working_dir: str) -> int:
    """Parse iteration number from round directory name (round-<id>-iter<N>)."""
    try:
        return int(working_dir.rsplit("iter", 1)[-1])
    except (ValueError, IndexError):
        return 0
