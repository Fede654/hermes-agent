"""ResearchSupervisor — Karpathy inner loop for any task with a measurable deliverable.

Implements the Autogenesis self-evolution loop (Act → Observe → Optimize → Remember)
applied to any task with a measurable deliverable:

  Phase     | Autogenesis concept  | Implementation
  --------- | -------------------- | --------------
  ACT       | Agent produces output | worker via delegate_task
  OBSERVE   | Capture outcome + traces | _observe() → learnings.jsonl
  OPTIMIZE  | Propose next hypothesis | _improve_attempt() (reflection optimizer)
  REMEMBER  | Persist insights for future rounds | learnings.jsonl (HeartbeatMemorySystem schema)

The SEPL (Self Evolution Protocol Layer) materializes as:
  - propose: _improve_attempt() drafts the next attempt
  - evaluate: ExperimentRunner scores and keep/discards
  - commit: kept results update best_result + lineage in ExperimentHistory
  - rollback: discarded results revert attempt_holder to prior best

Supported task types: "code" | "search" | "research" | "generic"
Evaluation modes:     "self_report" | "llm_judge"
"""

from __future__ import annotations

import json
import logging
import re
import time as _time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from agent.research_runner import (
    DelegateSandboxResult,
    ExperimentHistory,
    ExperimentResult,
    ExperimentRunner,
    HermesExperimentConfig,
)
from agent.research_metrics import UniversalMetricParser

logger = logging.getLogger(__name__)

_parser = UniversalMetricParser()


# ---------------------------------------------------------------------------
# TaskSpec — the central abstraction for any measurable task
# ---------------------------------------------------------------------------

@dataclass
class TaskSpec:
    """Describes any task with a measurable deliverable.

    Examples:
        # Code task — metric from test pass rate
        TaskSpec(
            topic="Implement a binary search tree",
            deliverable="Python class with insert/search/delete, measured by test pass rate",
            metric_key="pass_rate",
            task_type="code",
        )

        # Search task — metric from result relevance
        TaskSpec(
            topic="Find papers on attention mechanisms published after 2022",
            deliverable="Ranked list of relevant papers with abstracts",
            metric_key="relevance_score",
            task_type="search",
            evaluation_mode="llm_judge",
            evaluation_prompt="Score 0-1: does this paper list cover attention mechanisms published after 2022?",
        )

        # Research task — metric from synthesis quality
        TaskSpec(
            topic="Summarize the state of diffusion models for video generation",
            deliverable="Technical synthesis covering key methods, benchmarks, and open problems",
            metric_key="completeness_score",
            task_type="research",
            evaluation_mode="llm_judge",
            evaluation_prompt="Score 0-1: does this synthesis cover key methods, benchmarks, and open problems?",
        )

        # Generic task — anything with a self-reported numeric metric
        TaskSpec(
            topic="Optimize hermes session search latency",
            deliverable="Modified session search implementation with measured latency in ms",
            metric_key="latency_ms",
            metric_direction="minimize",
            task_type="generic",
        )
    """

    topic: str
    deliverable: str              # what the worker must produce
    metric_key: str               # how success is measured
    metric_direction: str = "maximize"   # "maximize" or "minimize"
    task_type: str = "generic"    # "code" | "search" | "research" | "generic"
    acceptance_criterion: str = ""       # e.g. "pass_rate >= 0.95" or qualitative
    evaluation_mode: str = "self_report" # "self_report" | "llm_judge"
    evaluation_prompt: str = ""          # for llm_judge: how to score the deliverable
    hypothesis: str = ""          # current iteration hypothesis (updated by supervisor)

    # Worker toolset hints per task type (overridable in ResearchSupervisor.run)
    _DEFAULT_TOOLSETS: dict[str, list[str]] = field(default_factory=lambda: {
        "code":     ["terminal", "file"],
        "search":   ["web", "terminal", "file"],
        "research": ["web", "terminal", "file"],
        "generic":  ["terminal", "file"],
    }, repr=False)

    def default_toolsets(self) -> list[str]:
        return self._DEFAULT_TOOLSETS.get(self.task_type, ["terminal", "file"])


# ---------------------------------------------------------------------------
# Task brief templates — one per task_type
# ---------------------------------------------------------------------------

def _build_task_brief(spec: TaskSpec, *, iteration: int, round_dir: str, time_budget_sec: int) -> str:
    """Generate the task brief for the worker. Domain-aware but structurally identical."""
    builders = {
        "code":     _brief_code,
        "search":   _brief_search,
        "research": _brief_research,
    }
    builder = builders.get(spec.task_type, _brief_generic)
    return builder(spec, iteration=iteration, round_dir=round_dir, time_budget_sec=time_budget_sec)


def _think_block(spec: TaskSpec, iteration: int) -> str:
    action = "improve" if iteration > 0 else "establish a baseline for"
    return f"""\
## Step 0 — Think Before Acting (Karpathy Principle 1)

Before producing anything, state in your output:

1. **Assumption**: What do you understand the task to be asking for?
2. **Bottleneck** *(iteration {iteration} > 0 only)*: Why is `{spec.metric_key}` at its current value?
   What is the binding constraint?
3. **Hypothesis**: What ONE change will {action} `{spec.metric_key}`?
   If uncertain between approaches, pick the simpler one.
4. **Success criterion**: "`{spec.metric_key}` moves from X toward
   {'higher' if spec.metric_direction == 'maximize' else 'lower'}"

If something is unclear, name what is confusing in your NOTES. Do NOT guess silently.
"""


def _report_block(metric_key: str) -> str:
    return f"""\
## Final Report (required)

Your last line of output must be:

```
METRIC: {metric_key}=<value> STATUS: improved|regressed|neutral NOTES: <one line>
```

- Value must be a real number you measured or computed — never fabricated.
- NOTES must say what you did and what the key result was.
- Also write `results.json` with `{{"{metric_key}": <value>}}` for structured parsing.
"""


def _brief_code(spec: TaskSpec, *, iteration: int, round_dir: str, time_budget_sec: int) -> str:
    action = "Improve" if iteration > 0 else "Establish a baseline for"
    return f"""\
# Task Brief — Code ({action})

## Topic
{spec.topic}

## Deliverable
{spec.deliverable}

{_think_block(spec, iteration)}
## Step 1 — Implement

The current attempt is in `attempt.py` in: `{round_dir}`

{"Do not rewrite unless you have a specific, hypothesis-driven change. Make surgical edits only — every changed line must trace to your hypothesis." if iteration > 0 else "Implement the deliverable in `attempt.py`. Run it to verify."}

{f"Time budget: {time_budget_sec}s. Print `TIME_ESTIMATE: Xs` before your main loop." if time_budget_sec > 0 else "Time budget: unlimited. Work until converged."}
{"Stop before 80% of budget and save partial results." if time_budget_sec > 0 else ""}

## Step 2 — Measure

Compute `{spec.metric_key}` from the code's output.
{"Acceptance criterion: " + spec.acceptance_criterion if spec.acceptance_criterion else ""}

{_report_block(spec.metric_key)}
## Rules
- Do NOT fabricate metric values.
- No abstractions for single-use code. If 5 lines solve it, write 5.
- Do NOT refactor code unrelated to your hypothesis.
- **Package installation:** `pip install` is NOT blocked but may fail if the package isn't available. If you need a library, first check if it's already installed. If not, use `ctypes.CDLL` with system libraries (e.g., `/usr/lib/x86_64-linux-gnu/libgmp.so.10`) or write a pure-Python alternative.
- **You MAY use `python -c` and heredoc scripts** — these are allowed in your environment.
- **Tool format:** When calling tools, use the JSON format provided by the system. Do NOT use XML tags like `<function_calls>`.

## Tools Available

You have access to: `terminal` (shell commands), `file` (read/write), `code_execution` (Python scripts), and `search` (web search).
If a task requires running code, use `terminal()` or `code_execution()` — do NOT assume they are unavailable.
"""


def _brief_search(spec: TaskSpec, *, iteration: int, round_dir: str, time_budget_sec: int) -> str:
    action = "Refine" if iteration > 0 else "Execute"
    return f"""\
# Task Brief — Search ({action})

## Topic
{spec.topic}

## Deliverable
{spec.deliverable}

{_think_block(spec, iteration)}
## Step 1 — Search

{"The previous search strategy is in `attempt.md` in: " + round_dir + ". Revise it based on your hypothesis." if iteration > 0 else "Design and execute a search strategy. Save results to `attempt.md`."}

{f"Time budget: {time_budget_sec}s. Do not make redundant searches — each query must have a hypothesis." if time_budget_sec > 0 else "Time budget: unlimited. Work until converged."}

## Step 2 — Evaluate Results

Score your results for `{spec.metric_key}` on a 0.0–1.0 scale.
{"Evaluate against: " + spec.evaluation_prompt if spec.evaluation_prompt and spec.evaluation_mode == "self_report" else ""}
{"Acceptance criterion: " + spec.acceptance_criterion if spec.acceptance_criterion else ""}

{_report_block(spec.metric_key)}
## Rules
- Do NOT fabricate relevance scores.
- Each search iteration must test exactly one new hypothesis about where better results are.
- Save your full result set to `results.json` with `{{"{spec.metric_key}": <score>}}`.
- **Tool format:** When calling tools, use the JSON format provided by the system. Do NOT use XML tags like `<function_calls>`.

## Tools Available

You have access to: `web_search` (find papers/articles), `browser` (visit pages), `file` (read/write), and `terminal` (shell commands for data processing).
Use these actively — do NOT assume they are unavailable.
"""


def _brief_research(spec: TaskSpec, *, iteration: int, round_dir: str, time_budget_sec: int) -> str:
    action = "Deepen" if iteration > 0 else "Produce an initial"
    return f"""\
# Task Brief — Research ({action})

## Topic
{spec.topic}

## Deliverable
{spec.deliverable}

{_think_block(spec, iteration)}
## Step 1 — Investigate and Synthesize

{"The current draft is in `attempt.md` in: " + round_dir + ". Identify its weakest section and address it." if iteration > 0 else "Research the topic. Produce an initial synthesis in `attempt.md`."}

{f"Time budget: {time_budget_sec}s. Focus — do not survey everything; go deep on what your hypothesis identifies as the gap." if time_budget_sec > 0 else "Time budget: unlimited. Work until converged."}

## Step 2 — Self-Evaluate

Rate your synthesis on `{spec.metric_key}` (0.0–1.0).
{"Evaluate against: " + spec.evaluation_prompt if spec.evaluation_prompt and spec.evaluation_mode == "self_report" else ""}
{"Acceptance criterion: " + spec.acceptance_criterion if spec.acceptance_criterion else ""}

{_report_block(spec.metric_key)}
## Rules
- Do NOT fabricate facts, citations, or scores.
- Each iteration must address exactly ONE identified gap — not rewrite everything.
- Save synthesis to `attempt.md` and score to `results.json`.
- **Tool format:** When calling tools, use the JSON format provided by the system. Do NOT use XML tags like `<function_calls>`.

## Tools Available

You have access to: `web_search` (research topics), `browser` (deep reading), `file` (read/write), and `terminal` (data processing).
Use these actively — do NOT assume they are unavailable.
"""


def _brief_generic(spec: TaskSpec, *, iteration: int, round_dir: str, time_budget_sec: int) -> str:
    action = "Improve" if iteration > 0 else "Produce a baseline"
    return f"""\
# Task Brief — {action}

## Topic
{spec.topic}

## Deliverable
{spec.deliverable}

{_think_block(spec, iteration)}
## Step 1 — Produce the Deliverable

{"The previous attempt is in `attempt.md` in: " + round_dir + ". Revise it based on your hypothesis." if iteration > 0 else "Produce the deliverable. Save it to `attempt.md`."}

{f"Time budget: {time_budget_sec}s." if time_budget_sec > 0 else "Time budget: unlimited. Work until converged."}

## Step 2 — Measure

Compute `{spec.metric_key}` as a number from your deliverable.
{"Acceptance criterion: " + spec.acceptance_criterion if spec.acceptance_criterion else ""}

{_report_block(spec.metric_key)}
## Rules
- Do NOT fabricate metric values.
- Minimum effort that moves the metric. No speculative additions.
- Save deliverable to `attempt.md`, score to `results.json`.
- **Tool format:** When calling tools, use the JSON format provided by the system. Do NOT use XML tags like `<function_calls>`.

## Tools Available

You have access to: `terminal` (shell), `file` (read/write), `code_execution` (Python), `web_search`, and `browser`.
Use these actively — do NOT assume they are unavailable.
"""


# ---------------------------------------------------------------------------
# Attempt file name per task type
# ---------------------------------------------------------------------------

_ATTEMPT_FILENAME: dict[str, str] = {
    "code":     "attempt.py",
    "search":   "attempt.md",
    "research": "attempt.md",
    "generic":  "attempt.md",
}


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
    """Karpathy loop for any task with a measurable deliverable.

    Args:
        parent_agent: Live AIAgent instance (required for delegate_task).
        workspace: Root directory for round artefacts.
        lattice_task_id: Lattice task to post round comments to (optional).
        lattice_root: Directory containing .lattice/.
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
        spec: TaskSpec,
        initial_attempt: str,
        *,
        run_id: str,
        max_iterations: int = 5,
        time_budget_sec: int = 0,
        keep_threshold: float = 0.0,
        llm: Any = None,
        worker_toolsets: list[str] | None = None,
        checkpoint_dir: Path | None = None,
    ) -> ExperimentHistory:
        """Run the Karpathy loop for any TaskSpec.

        Args:
            spec: Task description — topic, deliverable, metric, task type.
            initial_attempt: Starting deliverable (code string, search query,
                research outline, or any text the worker can iterate on).
            run_id: Unique identifier for this run.
            max_iterations: Max improvement iterations (not counting baseline).
            time_budget_sec: Time budget per worker invocation (seconds).
            keep_threshold: Min absolute metric delta to count as kept.
            llm: LLM client for improvement proposals (None = baseline only).
            worker_toolsets: Override default toolsets for workers.

        Returns:
            ExperimentHistory with all round results and the best result.
        """
        config = HermesExperimentConfig(
            metric_key=spec.metric_key,
            metric_direction=spec.metric_direction,
            time_budget_sec=time_budget_sec,
            max_iterations=max_iterations,
            keep_threshold=keep_threshold,
        )

        lattice_comment_fn = _make_lattice_comment_fn(
            self._lattice_task_id, self._lattice_root
        )

        toolsets = worker_toolsets or spec.default_toolsets()
        attempt_holder: list[str] = [initial_attempt]

        def delegate_fn(goal: str, working_dir: str) -> DelegateSandboxResult:
            return self._run_worker(
                goal=goal,
                working_dir=working_dir,
                attempt=attempt_holder[0],
                spec=spec,
                time_budget_sec=time_budget_sec,
                iteration=_extract_iteration(working_dir),
                worker_toolsets=toolsets,
                llm=llm,
            )

        runner = ExperimentRunner(
            config=config,
            workspace=self._workspace / run_id,
            delegate_fn=delegate_fn,
            lattice_comment_fn=lattice_comment_fn,
        )

        run_dir = self._workspace / run_id
        lattice_comment_fn(
            f"Loop started: run_id={run_id} type={spec.task_type} "
            f"metric={spec.metric_key} topic={spec.topic[:50]}"
        )

        # --- ACT (baseline) ---
        baseline = runner.run_experiment(initial_attempt, run_id=run_id, iteration=0)
        # Read on-disk artifact — worker may have modified the seed during baseline
        baseline_artifact = self._read_artifact(spec, run_dir, baseline) or initial_attempt
        attempt_holder[0] = baseline_artifact
        best_artifact_holder: list[str] = [baseline_artifact]
        # --- OBSERVE ---
        self._observe(baseline, spec, run_dir)
        self._checkpoint(runner.history, checkpoint_dir, round=0)

        if llm is None:
            lattice_comment_fn(f"Baseline only. best={runner.history.baseline_metric}")
            return runner.history

        # Determine early-stop parameters based on baseline quality
        baseline_metric = runner.history.baseline_metric
        is_high_baseline = False
        min_delta = 0.0
        if baseline_metric is not None:
            if spec.metric_direction == "maximize" and baseline_metric >= 0.9:
                is_high_baseline = True
                min_delta = 0.05
            elif spec.metric_direction == "minimize" and baseline_metric <= 0.1:
                is_high_baseline = True
                min_delta = 0.05

        early_stop_limit = 1 if is_high_baseline else 3
        if is_high_baseline:
            logger.info(
                "High baseline detected (%s=%.4f). Using aggressive early stop: "
                "limit=%d, min_delta=%.2f",
                spec.metric_key, baseline_metric, early_stop_limit, min_delta,
            )

        # Autogenesis AOOR improvement loop
        no_improvement = 0
        for iteration in range(1, max_iterations + 1):
            # OPTIMIZE — propose revised attempt (SEPL: propose)
            next_attempt = self._improve_attempt(llm, spec, attempt_holder[0], runner.history)
            attempt_holder[0] = next_attempt

            # ACT — worker executes the attempt
            result = runner.run_experiment(next_attempt, run_id=run_id, iteration=iteration)

            # Read on-disk artifact — worker may have refined it beyond the seed
            actual_artifact = self._read_artifact(spec, run_dir, result) or next_attempt
            attempt_holder[0] = actual_artifact

            # OBSERVE + REMEMBER — extract and persist structured learning
            self._observe(result, spec, run_dir)
            self._checkpoint(runner.history, checkpoint_dir, round=iteration)

            # SEPL: evaluate → keep/discard (handled by ExperimentRunner)
            # SEPL: rollback — restore best on-disk artifact, not the seed string
            # For high baselines, require min_delta for improvement to count
            improved = result.improved
            if improved and is_high_baseline and baseline_metric is not None:
                current_metric = result.primary_metric
                if current_metric is not None:
                    delta = abs(current_metric - baseline_metric)
                    if delta < min_delta:
                        improved = False
                        logger.info(
                            "Improvement below min_delta (%.4f < %.2f), treating as non-improving",
                            delta, min_delta,
                        )

            if improved:
                no_improvement = 0
                best_artifact_holder[0] = actual_artifact
            else:
                no_improvement += 1
                attempt_holder[0] = best_artifact_holder[0]  # rollback to best artifact

            if no_improvement >= early_stop_limit:
                logger.info(
                    "Early stop: %d non-improving iterations for %s (limit=%d)",
                    no_improvement, run_id, early_stop_limit,
                )
                # SEPL: reflection optimizer — synthesize before giving up
                self._reflect(runner.history, spec, llm, lattice_comment_fn, run_dir)
                break

        best = runner.history.best_result
        # Partial recovery: if the last iteration failed but we have prior results,
        # report as partial success instead of total failure
        last_result = runner.history.results[-1] if runner.history.results else None
        if last_result and last_result.primary_metric is None and best:
            lattice_comment_fn(
                f"Loop done (PARTIAL): {len(runner.history.results)} rounds, "
                f"best={best.primary_metric}. Last iteration failed but prior best preserved."
            )
        else:
            lattice_comment_fn(
                f"Loop done: {len(runner.history.results)} rounds, "
                f"best={best.primary_metric if best else None}"
            )
        return runner.history

    # ------------------------------------------------------------------
    # Worker execution
    # ------------------------------------------------------------------

    def _run_worker(
        self,
        *,
        goal: str,
        working_dir: str,
        attempt: str,
        spec: TaskSpec,
        time_budget_sec: int,
        iteration: int,
        worker_toolsets: list[str] | None,
        llm: Any,
    ) -> DelegateSandboxResult:
        """Write task brief + attempt file, spawn delegate_task, parse result."""
        t0 = _time.monotonic()
        wd = Path(working_dir)
        wd.mkdir(parents=True, exist_ok=True)

        # Write the attempt in the appropriate format
        attempt_filename = _ATTEMPT_FILENAME.get(spec.task_type, "attempt.md")
        (wd / attempt_filename).write_text(attempt, encoding="utf-8")

        # Write the task brief
        brief = _build_task_brief(
            spec,
            iteration=iteration,
            round_dir=working_dir,
            time_budget_sec=time_budget_sec,
        )
        (wd / "task_brief.md").write_text(brief, encoding="utf-8")

        context = (
            f"Working directory: {working_dir}\n"
            f"Topic: {spec.topic}\n"
            f"Task type: {spec.task_type}\n"
            f"Metric: {spec.metric_key} ({spec.metric_direction})\n"
            f"Read task_brief.md for full instructions."
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

        # Parse metrics from structured files first, stdout fallback
        parsed = _parser.parse(wd, stdout=summary)
        metrics: dict[str, object] = dict(parsed.to_flat_metrics())

        # LLM judge override: score the deliverable externally
        # Optimization: only run judge on baseline (iter 0) and every 2nd iteration
        # to reduce API calls and latency.
        if (
            spec.evaluation_mode == "llm_judge"
            and llm is not None
            and summary
            and (iteration == 0 or iteration % 2 == 0)
        ):
            judge_score = self._score_with_llm_judge(summary, spec, llm)
            if judge_score is not None:
                metrics[spec.metric_key] = judge_score
                logger.info(
                    "LLM judge scored %s=%.4f for %s iter %d",
                    spec.metric_key, judge_score, working_dir, iteration,
                )

        completed = status == "completed"
        error: str | None = None if completed else (first.get("error") or f"Worker status: {status}")

        return DelegateSandboxResult(
            metrics=metrics,
            stdout=summary,
            stderr="",
            elapsed_sec=elapsed,
            timed_out=False,
            returncode=0 if completed else 1,
            error=error,
        )

    # ------------------------------------------------------------------
    # Autogenesis: Observe + Remember (HeartbeatMemorySystem schema)
    # ------------------------------------------------------------------

    def _read_artifact(self, spec: TaskSpec, run_dir: Path, result: ExperimentResult) -> str | None:
        """Read the actual on-disk artifact produced by the worker.

        Workers may modify attempt.py / attempt.md beyond the seed string passed in.
        This ensures rollback restores the real artifact, not the seed text.
        """
        round_dir = run_dir / f"round-{result.run_id}-iter{result.iteration}"
        attempt_filename = _ATTEMPT_FILENAME.get(spec.task_type, "attempt.md")
        artifact_file = round_dir / attempt_filename
        try:
            return artifact_file.read_text(encoding="utf-8") if artifact_file.exists() else None
        except OSError:
            return None

    @staticmethod
    def _insight_from_json(round_dir: Path, metric_key: str) -> str:
        """Extract a human-readable insight from results.json (structured source)."""
        results_json = round_dir / "results.json"
        if not results_json.exists():
            return ""
        try:
            data = json.loads(results_json.read_text(encoding="utf-8"))
            for field in ("notes", "summary", "insight", "description"):
                val = data.get(field)
                if isinstance(val, str) and val.strip():
                    return val.strip()[:200]
            val = data.get(metric_key)
            if val is not None:
                return f"{metric_key}={val}"
        except (json.JSONDecodeError, OSError):
            pass
        return ""

    def _observe(
        self,
        result: ExperimentResult,
        spec: TaskSpec,
        run_dir: Path,
    ) -> None:
        """Extract a structured learning from a completed round and append to learnings.jsonl.

        Schema mirrors Autogenesis HeartbeatMemorySystem:
          type       — "improvement" | "regression" | "failure"
          key        — metric name being optimized
          insight    — one-line summary of what happened and why
          confidence — metric value (0.0 if unavailable)
          source     — "iter-N" for lineage tracing

        Insight extraction priority:
          1. results.json (structured, most reliable)
          2. NOTES: field from METRIC line in stdout
          3. Raw stdout excerpt (last resort)
        """
        if result.primary_metric is not None:
            entry_type = "improvement" if result.improved else "regression"
        else:
            entry_type = "failure"

        round_dir = run_dir / f"round-{result.run_id}-iter{result.iteration}"

        # 1. Structured source: results.json
        insight_text = self._insight_from_json(round_dir, spec.metric_key)

        # 2. NOTES: field from the worker's METRIC line
        if not insight_text and result.stdout:
            m = re.search(r"NOTES:\s*(.+)", result.stdout)
            if m:
                insight_text = m.group(1).strip()

        # 3. Raw stdout excerpt
        if not insight_text and result.stdout:
            insight_text = result.stdout[:200].replace("\n", " ")

        # 4. Error fallback
        if not insight_text and result.error:
            insight_text = result.error[:200]

        # Normalize confidence to 0-1 scale regardless of metric direction
        raw_metric = result.primary_metric
        if raw_metric is not None:
            # For minimize metrics, invert so higher confidence = better result
            if spec.metric_direction == "minimize":
                # Use inverse with a small epsilon to avoid div by zero
                confidence = round(1.0 / (1.0 + abs(raw_metric)), 6)
            else:
                confidence = round(min(abs(raw_metric), 1.0), 6)
        else:
            confidence = 0.0

        entry = {
            "type": entry_type,
            "key": spec.metric_key,
            "insight": insight_text or "no output",
            "confidence": confidence,
            "source": f"iter-{result.iteration}",
        }

        run_dir.mkdir(parents=True, exist_ok=True)
        learnings_file = run_dir / "learnings.jsonl"
        with learnings_file.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")

        logger.debug(
            "[observe] iter=%d type=%s %s=%.4f insight=%s",
            result.iteration, entry_type, spec.metric_key,
            entry["confidence"], insight_text[:80],
        )

    def _checkpoint(
        self,
        history: ExperimentHistory,
        checkpoint_dir: Path | None,
        round: int,
    ) -> None:
        """Serialize experiment history to a durable checkpoint directory.

        Called after baseline and every completed iteration so that external
        monitors (e.g. research_job_tool) can read progress without polling
        the running process.
        """
        if checkpoint_dir is None:
            return

        checkpoint_dir.mkdir(parents=True, exist_ok=True)

        # Serialize results
        results = []
        for r in history.results:
            results.append({
                "run_id": r.run_id,
                "iteration": r.iteration,
                "metrics": r.metrics,
                "primary_metric": r.primary_metric,
                "improved": r.improved,
                "kept": r.kept,
                "elapsed_sec": r.elapsed_sec,
                "error": r.error,
            })

        best = None
        if history.best_result:
            br = history.best_result
            best = {
                "run_id": br.run_id,
                "iteration": br.iteration,
                "primary_metric": br.primary_metric,
                "metrics": br.metrics,
            }

        checkpoint_dir.joinpath("history.json").write_text(
            json.dumps({"results": results, "best": best}, indent=2)
        )

        # Lightweight status file for quick polling
        checkpoint_dir.joinpath("checkpoint.json").write_text(
            json.dumps({
                "round": round,
                "total_rounds": len(history.results),
                "best_metric": history.best_result.primary_metric if history.best_result else None,
                "updated_at": _time.time(),
            }, indent=2)
        )

        logger.debug("[checkpoint] round=%d dir=%s", round, checkpoint_dir)

    # ------------------------------------------------------------------
    # Autogenesis: Reflect (SEPL reflection optimizer on early stop)
    # ------------------------------------------------------------------

    def _reflect(
        self,
        history: "ExperimentHistory",
        spec: TaskSpec,
        llm: Any,
        lattice_comment_fn: "Callable[[str], None]",
        run_dir: Path,
    ) -> None:
        """Synthesis pass after 3 non-improving iterations.

        Reads learnings.jsonl, asks the LLM to diagnose why the metric stalled,
        and posts the diagnosis to Lattice. This is the SEPL reflection optimizer:
        instead of iterating blindly, we re-examine whether the hypothesis was wrong.
        """
        learnings_file = run_dir / "learnings.jsonl"
        learnings: list[dict[str, Any]] = []
        if learnings_file.exists():
            for line in learnings_file.read_text(encoding="utf-8").splitlines():
                try:
                    learnings.append(json.loads(line))
                except json.JSONDecodeError:
                    pass

        best = history.best_result
        best_metric = best.primary_metric if best else None

        if not llm:
            lattice_comment_fn(
                f"[reflect] Early stop after 3 non-improving rounds. "
                f"Best {spec.metric_key}={best_metric}. "
                f"llm=None — reflection skipped. Pass an LLM client to enable diagnosis."
            )
            return

        if not learnings:
            lattice_comment_fn(
                f"[reflect] Early stop after 3 non-improving rounds. "
                f"Best {spec.metric_key}={best_metric}. "
                f"No learnings in learnings.jsonl — re-examine hypothesis manually."
            )
            return

        learnings_summary = "\n".join(
            f"- iter {e['source']}: {e['type']} | {e['key']}={e['confidence']} | {e['insight']}"
            for e in learnings
        )

        prompt = (
            f"A research loop ran {len(learnings)} iterations on the following task:\n\n"
            f"Topic: {spec.topic}\n"
            f"Deliverable: {spec.deliverable}\n"
            f"Metric: {spec.metric_key} ({spec.metric_direction})\n"
            f"Best achieved: {best_metric}\n\n"
            f"Round-by-round observations:\n{learnings_summary}\n\n"
            "The loop stopped because 3 consecutive iterations did not improve the metric.\n\n"
            "Diagnose:\n"
            "1. Why did the metric stall? What is the fundamental bottleneck?\n"
            "2. Was the hypothesis wrong — or was the approach right but the budget too small?\n"
            "3. What ONE different approach would you try next if given another budget?\n\n"
            "Be specific and concise. This diagnosis will be posted to the task tracker."
        )

        try:
            response = llm.chat(
                [{"role": "user", "content": prompt}],
                system=(
                    "You are an expert research diagnostician. "
                    "Identify root causes, not symptoms. Be concrete and actionable."
                ),
            )
            diagnosis = getattr(response, "content", "").strip()[:1000]
        except Exception as exc:
            logger.warning("Reflection LLM call failed: %s", exc)
            diagnosis = f"LLM reflection failed: {exc}"

        lattice_comment_fn(
            f"[reflect] Early stop after {len(learnings)} rounds. "
            f"Best {spec.metric_key}={best_metric}.\n\n"
            f"Diagnosis:\n{diagnosis}"
        )

        # Persist the reflection as a special learning entry
        reflection_entry = {
            "type": "reflection",
            "key": spec.metric_key,
            "insight": diagnosis[:500],
            "confidence": best_metric or 0.0,
            "source": "reflect-final",
        }
        with learnings_file.open("a", encoding="utf-8") as f:
            f.write(json.dumps(reflection_entry) + "\n")

    # ------------------------------------------------------------------
    # Autogenesis: Optimize — improvement proposal (Karpathy principles)
    # ------------------------------------------------------------------

    def _improve_attempt(
        self,
        llm: Any,
        spec: TaskSpec,
        current_attempt: str,
        history: ExperimentHistory,
    ) -> str:
        """Propose a revised attempt using domain-aware Karpathy prompting."""
        last = history.results[-1] if history.results else None
        best = history.best_result
        last_metric = last.primary_metric if last else None
        best_metric = best.primary_metric if best else None
        last_stdout = last.stdout if last else ""

        _DOMAIN_VERB = {
            "code":     "Revise the code",
            "search":   "Revise your search strategy, queries, or result ranking",
            "research": "Deepen or reframe your research synthesis",
            "generic":  "Revise your approach",
        }
        domain_verb = _DOMAIN_VERB.get(spec.task_type, "Revise your approach")

        _DOMAIN_HINT = {
            "code":     "Make surgical edits — every changed line must trace to your hypothesis. "
                        "No refactoring of unrelated sections.",
            "search":   "Test exactly ONE new query strategy or source. "
                        "Don't repeat what didn't work.",
            "research": "Address exactly ONE identified gap (missing source, weak argument, "
                        "uncovered angle). Don't rewrite everything.",
            "generic":  "Change only what your hypothesis requires. "
                        "Minimum viable revision.",
        }
        domain_hint = _DOMAIN_HINT.get(spec.task_type, "")

        prompt = (
            f"Task: {spec.topic}\n"
            f"Deliverable: {spec.deliverable}\n"
            f"Metric: {spec.metric_key} ({spec.metric_direction})\n"
            f"Last score: {last_metric}\n"
            f"Best score: {best_metric}\n"
            f"Last worker output (excerpt):\n{last_stdout[:800]}\n\n"
            "---\n\n"
            "Current attempt:\n"
            f"{current_attempt}\n\n"
            "---\n\n"
            "## Think Before Revising (Karpathy Principle 1)\n\n"
            "State:\n"
            f"1. WHY is `{spec.metric_key}` at {last_metric}? What is the binding bottleneck?\n"
            "2. Your ONE hypothesis for what change will move it.\n"
            f"3. Success criterion: `{spec.metric_key}` moves from {last_metric} toward "
            f"{'higher' if spec.metric_direction == 'maximize' else 'lower'}.\n\n"
            "## Simplicity First\n\n"
            "If the revision can be 5 lines, make it 5 lines — not 50.\n"
            "No speculative additions. No features that don't serve the metric.\n\n"
            f"## Your Task\n\n{domain_verb}. {domain_hint}\n\n"
            "Return ONLY the revised attempt. "
            "Include a brief comment at the top stating your hypothesis and what you changed."
        )

        system = (
            f"You are a {spec.task_type} improvement specialist. "
            "Apply the Karpathy loop: think first, make surgical changes, verify the metric moves. "
            "Surface your reasoning. Never guess silently."
        )

        try:
            response = llm.chat([{"role": "user", "content": prompt}], system=system)
        except Exception as exc:
            logger.exception("Improvement call failed: %s", exc)
            return current_attempt

        candidate = getattr(response, "content", "")
        if not isinstance(candidate, str) or not candidate.strip():
            logger.warning("LLM returned empty attempt; keeping current")
            return current_attempt

        # For code tasks, extract from code fence if present
        if spec.task_type == "code":
            from agent.research_runner import ExperimentRunner
            extracted = ExperimentRunner._extract_python_code(candidate)
            return extracted if extracted.strip() else candidate.strip()

        return candidate.strip()

    # ------------------------------------------------------------------
    # LLM judge evaluator
    # ------------------------------------------------------------------

    def _score_with_llm_judge(
        self,
        deliverable: str,
        spec: TaskSpec,
        llm: Any,
    ) -> float | None:
        """Score a deliverable using an LLM judge. Returns 0.0–1.0 or None."""
        eval_prompt = spec.evaluation_prompt or (
            f"Score the following deliverable for the task '{spec.topic}' "
            f"on a scale of 0.0 to 1.0, where 1.0 = perfect. "
            f"Return ONLY a decimal number, nothing else."
        )
        prompt = f"{eval_prompt}\n\nDeliverable:\n{deliverable[:4000]}\n\nScore (0.0–1.0):"
        try:
            response = llm.chat(
                [{"role": "user", "content": prompt}],
                system="You are an objective evaluator. Return only a decimal number between 0.0 and 1.0.",
            )
            content = (getattr(response, "content", "") or "").strip()
            tokens = content.split()
            if not tokens:
                logger.warning("LLM judge returned empty response")
                return None
            raw = tokens[0].rstrip(".,")
            return max(0.0, min(1.0, float(raw)))
        except Exception as exc:
            logger.warning("LLM judge scoring failed: %s", exc)
            return None


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _extract_iteration(working_dir: str) -> int:
    try:
        return int(working_dir.rsplit("iter", 1)[-1])
    except (ValueError, IndexError):
        return 0
