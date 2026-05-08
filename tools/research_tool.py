"""run_research — iterative self-improving research loop tool.

Exposes ResearchSupervisor as a tool callable by the LLM, following the
same pattern as delegate_task. The LLM calls run_research when a task
benefits from multiple iterations scored against a measurable metric.

Autogenesis AOOR loop: Act → Observe → Optimize → Remember.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tool schema
# ---------------------------------------------------------------------------

RESEARCH_TOOL_SCHEMA = {
    "name": "run_research",
    "description": (
        "Run a self-improving research loop on any task with a measurable deliverable. "
        "Spawns worker subagents iteratively, scores their output against a metric, "
        "and applies LLM-guided hypothesis revision to improve the metric across rounds.\n\n"
        "USE WHEN:\n"
        "- A task requires iterative improvement toward a measurable quality criterion\n"
        "- You need web research, code optimization, or synthesis with self-evaluation\n"
        "- Single-shot delegate_task is not enough — the task benefits from multiple rounds\n\n"
        "NOT FOR:\n"
        "- One-shot tasks (use delegate_task directly)\n"
        "- Tasks with no measurable metric (use delegate_task)\n\n"
        "IMPORTANT: This tool spawns multiple subagents and can run for several minutes. "
        "Inform the user before calling it."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "topic": {
                "type": "string",
                "description": "What to research or accomplish. Be specific.",
            },
            "deliverable": {
                "type": "string",
                "description": (
                    "Concrete output the worker must produce. "
                    "E.g. 'Python class with insert/search/delete', "
                    "'ranked list of papers with abstracts and relevance scores'."
                ),
            },
            "metric_key": {
                "type": "string",
                "description": (
                    "Name of the metric to optimize. "
                    "E.g. 'pass_rate', 'relevance_score', 'completeness_score', 'latency_ms'."
                ),
            },
            "metric_direction": {
                "type": "string",
                "enum": ["maximize", "minimize"],
                "description": "Whether higher or lower metric values are better. Default: maximize.",
            },
            "task_type": {
                "type": "string",
                "enum": ["code", "search", "research", "generic"],
                "description": (
                    "Task domain. Controls worker brief template and default toolsets. "
                    "code=terminal+file, search/research=web+file, generic=terminal+file."
                ),
            },
            "acceptance_criterion": {
                "type": "string",
                "description": (
                    "Optional stopping criterion. When parseable as "
                    "'<metric> <op> <number>' (op: >=, <=, >, <, ==), the loop "
                    "exits as soon as the latest iteration's metric satisfies it. "
                    "Qualitative criteria (free-form text) are passed to the worker "
                    "via the brief but do not auto-terminate the loop. "
                    "E.g. 'pass_rate >= 0.95', 'latency_ms < 200'."
                ),
            },
            "evaluation_mode": {
                "type": "string",
                "enum": ["self_report", "llm_judge"],
                "description": (
                    "How to score worker output. "
                    "self_report: worker emits METRIC line. "
                    "llm_judge: supervisor scores the deliverable externally using evaluation_prompt."
                ),
            },
            "evaluation_prompt": {
                "type": "string",
                "description": (
                    "For llm_judge mode: scoring rubric. "
                    "E.g. 'Score 0-1: does this paper list cover attention mechanisms published after 2022?'"
                ),
            },
            "initial_attempt": {
                "type": "string",
                "description": (
                    "Optional starting deliverable or scaffold. "
                    "For code tasks: skeleton code. For research: initial outline. "
                    "Leave empty to let the worker start from scratch."
                ),
            },
            "max_iterations": {
                "type": "integer",
                "description": "Max improvement iterations after baseline (default: 3). Each spawns a worker.",
            },
            "time_budget_sec": {
                "type": "integer",
                "description": "Time budget per worker invocation in seconds (default: 300).",
            },
            "lattice_task_id": {
                "type": "string",
                "description": "Optional Lattice task ID to receive round-by-round progress comments.",
            },
            "strategies": {
                "type": "array",
                "description": (
                    "Optional A/B test strategies. If provided, runs each strategy "
                    "and returns a comparison table instead of a single run. "
                    "Each item is an object with: name, fan_out (int), use_moa (bool), max_iterations (int)."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "fan_out": {"type": "integer", "default": 1},
                        "use_moa": {"type": "boolean", "default": True},
                        "max_iterations": {"type": "integer", "default": 3},
                    },
                    "required": ["name"],
                },
            },
            "repeats": {
                "type": "integer",
                "description": "Number of repeats per strategy when running A/B tests (default: 1).",
                "default": 1,
            },
            "disable_evolution_overlay": {
                "type": "boolean",
                "description": (
                    "If true, do not prepend cross-run lessons from "
                    "$HERMES_HOME/evolution to the worker brief. Useful for "
                    "isolated tests, CI runs, or first-time tasks where the "
                    "global lesson store would only add noise. Default: false."
                ),
                "default": False,
            },
        },
        "required": ["topic", "deliverable", "metric_key"],
    },
}


# ---------------------------------------------------------------------------
# LLM bridge — wraps auxiliary_client.call_llm to match supervisor's Protocol
# ---------------------------------------------------------------------------

class _LLMBridge:
    """Adapter: auxiliary_client.call_llm → _ChatClient Protocol expected by ResearchSupervisor."""

    def chat(self, messages: list[dict[str, str]], *, system: str | None = None) -> Any:
        from agent.auxiliary_client import call_llm

        full_messages: list[dict[str, str]] = []
        if system:
            full_messages.append({"role": "system", "content": system})
        full_messages.extend(messages)

        try:
            resp = call_llm(messages=full_messages, max_tokens=4096)
            text = resp.choices[0].message.content or ""
        except Exception as exc:
            logger.warning("_LLMBridge.chat failed: %s", exc)
            text = ""

        return SimpleNamespace(content=text)


# ---------------------------------------------------------------------------
# Tool handler
# ---------------------------------------------------------------------------

def run_research(
    topic: str,
    deliverable: str,
    metric_key: str,
    metric_direction: str = "maximize",
    task_type: str = "generic",
    acceptance_criterion: str = "",
    evaluation_mode: str = "self_report",
    evaluation_prompt: str = "",
    initial_attempt: str = "",
    max_iterations: int = 3,
    time_budget_sec: int = 0,
    lattice_task_id: Optional[str] = None,
    parent_agent: Any = None,
    checkpoint_dir: Optional[str] = None,
    timeout_sec: int = 0,
    strategies: Optional[list[dict[str, Any]]] = None,
    repeats: int = 1,
    disable_evolution_overlay: bool = False,
) -> str:
    if parent_agent is None:
        return json.dumps({"error": "run_research requires a parent_agent context."})

    from agent.research.supervisor import ResearchSupervisor, TaskSpec
    from hermes_constants import get_hermes_home

    spec = TaskSpec(
        topic=topic,
        deliverable=deliverable,
        metric_key=metric_key,
        metric_direction=metric_direction,
        task_type=task_type,
        acceptance_criterion=acceptance_criterion,
        evaluation_mode=evaluation_mode,
        evaluation_prompt=evaluation_prompt,
    )

    run_id = hashlib.sha1(f"{topic}:{time.time()}".encode()).hexdigest()[:12]
    workspace = get_hermes_home() / "research-workspace"

    # A/B testing path
    if strategies:
        from agent.research.ab_testing import ResearchABTester, StrategyConfig

        strategy_configs = [
            StrategyConfig(
                name=s.get("name", f"strategy-{i}"),
                fan_out=s.get("fan_out", 1),
                use_moa=s.get("use_moa", True),
                max_iterations=s.get("max_iterations", max_iterations),
                time_budget_sec=time_budget_sec,
            )
            for i, s in enumerate(strategies)
        ]

        tester = ResearchABTester(
            parent_agent=parent_agent,
            workspace=workspace,
            lattice_task_id=lattice_task_id,
            llm=_LLMBridge(),
        )
        try:
            summaries = tester.compare(
                spec,
                strategy_configs,
                initial_attempt=initial_attempt,
                repeats=repeats,
                run_prefix=run_id,
            )
        except Exception as exc:
            logger.exception("A/B test failed for run_id=%s: %s", run_id, exc)
            return json.dumps({"error": str(exc), "run_id": run_id})

        return json.dumps({
            "run_id": run_id,
            "ab_test": True,
            "report": tester.format_report(summaries),
            "json": json.loads(tester.to_json(summaries)),
        }, indent=2)

    # Single-run path
    supervisor = ResearchSupervisor(
        parent_agent=parent_agent,
        workspace=workspace,
        lattice_task_id=lattice_task_id,
    )

    try:
        history = supervisor.run(
            spec,
            initial_attempt=initial_attempt,
            run_id=run_id,
            max_iterations=max_iterations,
            time_budget_sec=time_budget_sec,
            llm=_LLMBridge(),
            checkpoint_dir=Path(checkpoint_dir) if checkpoint_dir else None,
            disable_evolution_overlay=disable_evolution_overlay,
        )
    except Exception as exc:
        logger.exception("run_research failed for run_id=%s: %s", run_id, exc)
        return json.dumps({"error": str(exc), "run_id": run_id})

    best = history.best_result
    best_notes = ""
    if best and best.stdout:
        import re
        m = re.search(r"NOTES:\s*(.+)", best.stdout)
        best_notes = m.group(1).strip() if m else ""

    # Aggregate cost accounting across iterations
    iteration_costs = []
    total_tokens_in = 0
    total_tokens_out = 0
    total_cost_usd = 0.0
    for r in history.results:
        iteration_costs.append({
            "iteration": r.iteration,
            "tokens_in": r.tokens_in,
            "tokens_out": r.tokens_out,
            "cost_usd": round(r.cost_usd, 6),
            "primary_metric": r.primary_metric,
            "improved": r.improved,
            "kept": r.kept,
        })
        total_tokens_in += r.tokens_in
        total_tokens_out += r.tokens_out
        total_cost_usd += r.cost_usd

    return json.dumps({
        "run_id": run_id,
        "iterations": len(history.results),
        "best_metric": best.primary_metric if best else None,
        "metric_key": metric_key,
        "metric_direction": metric_direction,
        "best_notes": best_notes,
        "workspace": str(workspace / run_id),
        "learnings_file": str(workspace / run_id / "learnings.jsonl"),
        "iteration_costs": iteration_costs,
        "total_tokens_in": total_tokens_in,
        "total_tokens_out": total_tokens_out,
        "total_cost_usd": round(total_cost_usd, 6),
        "total_iterations": len(history.results),
    }, indent=2)


def check_research_stale(checkpoint_dir: str, stale_threshold_sec: float = 90.0) -> bool:
    """Return True if the research job at checkpoint_dir has no recent heartbeat.

    The heartbeat file is ``<checkpoint_dir>/heartbeat.json``, written by
    ``agent.research.job_runner._child_main`` every 30 s with the schema
    ``{"ts": <unix>, "pid": <int>}``. A job is stale when:

      * the file is missing, or
      * the file is unreadable / malformed, or
      * ``now - ts`` exceeds ``stale_threshold_sec`` (default 90 s = 3
        missed beats, tolerating one GC pause / slow disk).

    Used by ``tools/research_job_tool._action_status`` to mark dead
    detached jobs and by the parent in job_runner to decide when to kill
    a stuck child.
    """
    hb = Path(checkpoint_dir) / "heartbeat.json"
    if not hb.exists():
        return True
    try:
        data = json.loads(hb.read_text(encoding="utf-8"))
        ts = float(data.get("ts", 0))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return True
    return (time.time() - ts) > stale_threshold_sec


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

from tools.registry import registry, tool_error  # noqa: E402


def _check_research_requirements() -> bool:
    try:
        from agent.research.supervisor import ResearchSupervisor  # noqa: F401
        return True
    except ImportError:
        return False


registry.register(
    name="run_research",
    toolset="research",
    schema=RESEARCH_TOOL_SCHEMA,
    handler=lambda args, **kw: run_research(
        topic=args.get("topic", ""),
        deliverable=args.get("deliverable", ""),
        metric_key=args.get("metric_key", ""),
        metric_direction=args.get("metric_direction", "maximize"),
        task_type=args.get("task_type", "generic"),
        acceptance_criterion=args.get("acceptance_criterion", ""),
        evaluation_mode=args.get("evaluation_mode", "self_report"),
        evaluation_prompt=args.get("evaluation_prompt", ""),
        initial_attempt=args.get("initial_attempt", ""),
        max_iterations=args.get("max_iterations", 3),
        time_budget_sec=args.get("time_budget_sec", 0),
        lattice_task_id=args.get("lattice_task_id"),
        parent_agent=kw.get("parent_agent"),
        checkpoint_dir=args.get("checkpoint_dir"),
        strategies=args.get("strategies"),
        repeats=args.get("repeats", 1),
    ),
    check_fn=_check_research_requirements,
    emoji="🔬",
)
