#!/usr/bin/env python3
"""Detached research job runner.

Entrypoint for long-running research loops that run outside the spawning
agent's lifetime.  Constructs its own AIAgent so delegate_task works,
checkpoints state to durable files, and writes results for later collection.

Usage:
    python -m agent.research_job_runner <job_dir>
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _load_config() -> dict[str, Any]:
    import yaml

    config_path = Path.home() / ".hermes" / "config.yaml"
    if not config_path.exists():
        return {}
    return yaml.safe_load(config_path.read_text()) or {}


def _build_parent_agent(spec: dict[str, Any]) -> Any:
    """Build a minimal AIAgent that satisfies delegate_task requirements."""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from run_agent import AIAgent

    config = _load_config()
    model_cfg = config.get("model", {})
    delegation_cfg = config.get("delegation", {})

    model = spec.get("model") or delegation_cfg.get("model") or model_cfg.get("default", "kimi-for-coding")
    provider = spec.get("provider") or delegation_cfg.get("provider") or model_cfg.get("provider", "kimi-coding")
    base_url = spec.get("base_url") or delegation_cfg.get("base_url") or model_cfg.get("base_url", "https://api.kimi.com/coding/v1")
    api_key = spec.get("api_key") or delegation_cfg.get("api_key") or os.getenv("KIMI_API_KEY", "")

    agent = AIAgent(
        model=model,
        provider=provider,
        base_url=base_url,
        api_key=api_key,
        enabled_toolsets=spec.get("toolsets", ["research", "terminal", "file", "web"]),
        quiet_mode=True,
        platform="cli",
        session_id=f"research-job:{spec['job_id']}",
        skip_context_files=True,
        skip_memory=True,
    )

    # Patch attributes that delegate_task expects
    agent._delegate_depth = 0
    agent.terminal_cwd = os.getcwd()
    agent.cwd = os.getcwd()
    agent._subdirectory_hints = None
    agent._delegate_spinner = None
    agent.tool_progress_callback = lambda *a, **k: None
    agent.providers_allowed = getattr(agent, "providers_allowed", None)
    agent.providers_ignored = getattr(agent, "providers_ignored", None)
    agent.providers_order = getattr(agent, "providers_order", None)
    agent.provider_sort = getattr(agent, "provider_sort", None)

    return agent


def _write_checkpoint(job_dir: Path, **fields: Any) -> None:
    state_path = job_dir / "state.json"
    state = {}
    if state_path.exists():
        try:
            state = json.loads(state_path.read_text())
        except json.JSONDecodeError:
            pass
    state.update(fields)
    state["updated_at"] = time.time()
    state_path.write_text(json.dumps(state, indent=2))


def _write_history(job_dir: Path, history: Any) -> None:
    history_path = job_dir / "history.json"
    try:
        # history is an ExperimentHistory object; serialize what we can
        data: dict[str, Any] = {"results": []}
        best = history.best_result if hasattr(history, "best_result") else None
        if best and hasattr(best, "__dict__"):
            data["best"] = {
                "run_id": getattr(best, "run_id", None),
                "iteration": getattr(best, "iteration", None),
                "primary_metric": getattr(best, "primary_metric", None),
                "metrics": getattr(best, "metrics", {}),
                "improved": getattr(best, "improved", False),
                "elapsed_sec": getattr(best, "elapsed_sec", 0),
            }
        if hasattr(history, "results"):
            for r in history.results:
                if hasattr(r, "__dict__"):
                    data["results"].append({
                        "run_id": getattr(r, "run_id", None),
                        "iteration": getattr(r, "iteration", None),
                        "primary_metric": getattr(r, "primary_metric", None),
                        "metrics": getattr(r, "metrics", {}),
                        "improved": getattr(r, "improved", False),
                        "elapsed_sec": getattr(r, "elapsed_sec", 0),
                        "error": getattr(r, "error", None),
                    })
        history_path.write_text(json.dumps(data, indent=2))
    except Exception as exc:
        logger.warning("Failed to serialize history: %s", exc)


def _try_obsidian_publish(job_dir: Path, report_path: Path) -> bool:
    """Attempt to publish the report to Obsidian via MCP if available."""
    try:
        from tools.mcp_tool import call_mcp_tool
        content = report_path.read_text()
        call_mcp_tool(
            server_name="obsidian",
            tool_name="obsidian_append_content",
            arguments={
                "filepath": f"Research/Jobs/{job_dir.name}.md",
                "content": content,
            },
        )
        return True
    except Exception as exc:
        logger.info("Obsidian publish skipped: %s", exc)
        return False


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: python -m agent.research_job_runner <job_dir>", file=sys.stderr)
        return 1

    job_dir = Path(sys.argv[1])
    spec_path = job_dir / "job.json"
    result_path = job_dir / "result.json"
    report_path = job_dir / "report.md"

    if not spec_path.exists():
        print(f"Job spec not found: {spec_path}", file=sys.stderr)
        return 1

    spec = json.loads(spec_path.read_text())
    job_id = spec["job_id"]

    # Bypass approval prompts for autonomous runs
    os.environ["HERMES_YOLO_MODE"] = "1"

    _write_checkpoint(job_dir, status="initializing", job_id=job_id)

    try:
        agent = _build_parent_agent(spec)
    except Exception as exc:
        logger.exception("Failed to build parent agent")
        _write_checkpoint(job_dir, status="failed", error=f"parent_agent build failed: {exc}")
        return 1

    from tools.research_tool import run_research

    _write_checkpoint(job_dir, status="running", pid=os.getpid())

    def _on_checkpoint(history: Any, result: Any) -> None:
        _write_history(job_dir, history)
        if result and hasattr(result, "__dict__"):
            _write_checkpoint(
                job_dir,
                last_iteration=getattr(result, "iteration", None),
                last_metric=getattr(result, "primary_metric", None),
                last_improved=getattr(result, "improved", False),
            )

    try:
        raw = run_research(
            topic=spec["topic"],
            deliverable=spec["deliverable"],
            metric_key=spec["metric_key"],
            metric_direction=spec.get("metric_direction", "maximize"),
            task_type=spec.get("task_type", "generic"),
            evaluation_mode=spec.get("evaluation_mode", "self_report"),
            evaluation_prompt=spec.get("evaluation_prompt", ""),
            initial_attempt=spec.get("initial_attempt", ""),
            max_iterations=spec.get("max_iterations", 3),
            time_budget_sec=spec.get("time_budget_sec", 0),
            lattice_task_id=spec.get("lattice_task_id"),
            parent_agent=agent,
        )
    except Exception as exc:
        logger.exception("run_research failed")
        _write_checkpoint(job_dir, status="failed", error=str(exc))
        return 1

    result = json.loads(raw)
    result_path.write_text(json.dumps(result, indent=2))

    # Build a local report
    report_lines = [
        f"# Research Job Report — {job_id}",
        "",
        f"- **Status**: {'completed' if 'error' not in result else 'failed'}",
        f"- **Run ID**: {result.get('run_id', 'N/A')}",
        f"- **Iterations**: {result.get('iterations', 'N/A')}",
        f"- **Best Metric**: {result.get('best_metric', 'N/A')}",
        f"- **Metric Key**: {result.get('metric_key', 'N/A')}",
        f"- **Workspace**: {result.get('workspace', 'N/A')}",
        "",
        "## Result",
        "",
        "```json",
        json.dumps(result, indent=2),
        "```",
    ]
    report_path.write_text("\n".join(report_lines))

    _write_checkpoint(
        job_dir,
        status="completed" if "error" not in result else "failed",
        **result,
    )

    # Attempt Obsidian publish (best-effort)
    obsidian_ok = _try_obsidian_publish(job_dir, report_path)
    _write_checkpoint(job_dir, obsidian_published=obsidian_ok)

    return 0 if "error" not in result else 1


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    sys.exit(main())
