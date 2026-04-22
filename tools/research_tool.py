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
                    "Optional stopping criterion. Loop ends early if met. "
                    "E.g. 'pass_rate >= 0.95', 'relevance_score >= 0.8'."
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
) -> str:
    if parent_agent is None:
        return json.dumps({"error": "run_research requires a parent_agent context."})

    from agent.research_supervisor import ResearchSupervisor, TaskSpec
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

    return json.dumps({
        "run_id": run_id,
        "iterations": len(history.results),
        "best_metric": best.primary_metric if best else None,
        "metric_key": metric_key,
        "metric_direction": metric_direction,
        "best_notes": best_notes,
        "workspace": str(workspace / run_id),
        "learnings_file": str(workspace / run_id / "learnings.jsonl"),
    }, indent=2)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

from tools.registry import registry, tool_error  # noqa: E402


def _check_research_requirements() -> bool:
    try:
        from agent.research_supervisor import ResearchSupervisor  # noqa: F401
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
    ),
    check_fn=_check_research_requirements,
    emoji="🔬",
)
