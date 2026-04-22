"""research_job_tool — orchestrate long-running research jobs as detached OS processes.

Provides start, status, collect, and resume operations for research loops
that outlive a single agent turn.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import shlex
from pathlib import Path
from typing import Any, Optional

from tools.registry import registry, tool_error

logger = logging.getLogger(__name__)


def _job_dir(job_id: str) -> Path:
    from hermes_constants import get_hermes_home
    return get_hermes_home() / "research-jobs" / job_id


def _write_job_spec(job_id: str, spec: dict[str, Any]) -> Path:
    jd = _job_dir(job_id)
    jd.mkdir(parents=True, exist_ok=True)
    spec_path = jd / "job.json"
    spec_path.write_text(json.dumps(spec, indent=2))
    return spec_path


def _load_config_for_job() -> dict[str, Any]:
    """Read Hermes config to extract model/provider/base_url for the runner."""
    import yaml
    config_path = Path.home() / ".hermes" / "config.yaml"
    if not config_path.exists():
        return {}
    cfg = yaml.safe_load(config_path.read_text())
    model_cfg = cfg.get("model", {})
    delegation_cfg = cfg.get("delegation", {})
    return {
        "model": delegation_cfg.get("model") or model_cfg.get("default", "kimi-for-coding"),
        "provider": delegation_cfg.get("provider") or model_cfg.get("provider", "kimi-coding"),
        "base_url": delegation_cfg.get("base_url") or model_cfg.get("base_url", "https://api.kimi.com/coding/v1"),
        "api_key": os.getenv("KIMI_API_KEY", ""),
    }


# ---------------------------------------------------------------------------
# Tool schema
# ---------------------------------------------------------------------------

RESEARCH_JOB_SCHEMA = {
    "name": "research_job",
    "description": (
        "Start, monitor, or resume a long-running research job as a detached OS process. "
        "Use this instead of run_research when the loop may take longer than a single "
        "agent turn (e.g. >5 minutes). Jobs are durable: state is checkpointed to disk "
        "after every round, and can be resumed if the process crashes.\n\n"
        "USE WHEN:\n"
        "- A research task needs multiple iterations and may take 10+ minutes\n"
        "- You cannot afford to keep a foreground agent alive as a watcher\n\n"
        "NOT FOR:\n"
        "- One-shot tasks (use delegate_task directly)\n"
        "- Tasks that fit in a single agent turn (use run_research)\n\n"
        "IMPORTANT: This tool spawns a background process. Poll status with "
        "`research_job_status` or wait for the process completion notification."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["start", "status", "collect", "resume"],
                "description": "Operation to perform on the research job.",
            },
            "job_id": {
                "type": "string",
                "description": "Job identifier. Required for status, collect, resume. Generated on start if omitted.",
            },
            "topic": {
                "type": "string",
                "description": "What to research. Required for start.",
            },
            "deliverable": {
                "type": "string",
                "description": "Concrete output the worker must produce. Required for start.",
            },
            "metric_key": {
                "type": "string",
                "description": "Name of the metric to optimize. Required for start.",
            },
            "metric_direction": {
                "type": "string",
                "enum": ["maximize", "minimize"],
                "description": "Whether higher or lower metric values are better. Default: maximize.",
            },
            "task_type": {
                "type": "string",
                "enum": ["code", "search", "research", "generic"],
                "description": "Task domain. Default: generic.",
            },
            "evaluation_mode": {
                "type": "string",
                "enum": ["self_report", "llm_judge"],
                "description": "How to score worker output. Default: self_report.",
            },
            "evaluation_prompt": {
                "type": "string",
                "description": "For llm_judge mode: scoring rubric.",
            },
            "max_iterations": {
                "type": "integer",
                "description": "Max improvement iterations after baseline. Default: 3.",
            },
            "time_budget_sec": {
                "type": "integer",
                "description": "Time budget per worker invocation in seconds. Default: 0 (unlimited).",
            },
            "lattice_task_id": {
                "type": "string",
                "description": "Optional Lattice task ID for round-by-round progress comments.",
            },
            "initial_attempt": {
                "type": "string",
                "description": "Optional starting scaffold for the worker.",
            },
        },
        "required": ["action"],
    },
}


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------

def _action_start(args: dict[str, Any]) -> str:
    job_id = args.get("job_id") or secrets.token_hex(8)
    cfg = _load_config_for_job()

    spec = {
        "job_id": job_id,
        "topic": args.get("topic", ""),
        "deliverable": args.get("deliverable", ""),
        "metric_key": args.get("metric_key", ""),
        "metric_direction": args.get("metric_direction", "maximize"),
        "task_type": args.get("task_type", "generic"),
        "evaluation_mode": args.get("evaluation_mode", "self_report"),
        "evaluation_prompt": args.get("evaluation_prompt", ""),
        "max_iterations": args.get("max_iterations", 3),
        "time_budget_sec": args.get("time_budget_sec", 0),
        "lattice_task_id": args.get("lattice_task_id"),
        "initial_attempt": args.get("initial_attempt", ""),
        "model": cfg.get("model"),
        "provider": cfg.get("provider"),
        "base_url": cfg.get("base_url"),
        "api_key": cfg.get("api_key"),
        "toolsets": ["research", "terminal", "file", "web"],
    }

    spec_path = _write_job_spec(job_id, spec)
    job_dir = _job_dir(job_id)

    hermes_root = Path("/home/fede/.hermes/hermes-agent")
    cmd = (
        f"cd {shlex.quote(str(hermes_root))} && "
        f"source venv/bin/activate && "
        f"HERMES_YOLO_MODE=1 python -m agent.research_job_runner {shlex.quote(str(spec_path))}"
    )

    # Spawn via terminal_tool in background
    from tools.terminal_tool import terminal
    raw = terminal(
        command=cmd,
        background=True,
        notify_on_complete=True,
        workdir=str(hermes_root),
    )
    proc = json.loads(raw) if isinstance(raw, str) else raw

    state = {
        "job_id": job_id,
        "status": "queued",
        "process_session_id": proc.get("session_id"),
        "pid": proc.get("pid"),
        "job_dir": str(job_dir),
        "spec_path": str(spec_path),
    }
    (job_dir / "state.json").write_text(json.dumps(state, indent=2))

    return json.dumps({
        "ok": True,
        "job_id": job_id,
        "status": "queued",
        "message": f"Research job {job_id} queued. Poll with research_job_status or wait for completion notification.",
        "job_dir": str(job_dir),
        "process_session_id": proc.get("session_id"),
    }, indent=2)


def _action_status(args: dict[str, Any]) -> str:
    job_id = args.get("job_id", "")
    if not job_id:
        return tool_error("job_id is required for status")

    job_dir = _job_dir(job_id)
    state_path = job_dir / "state.json"
    if not state_path.exists():
        return json.dumps({"ok": False, "error": f"Job {job_id} not found"}, indent=2)

    state = json.loads(state_path.read_text())

    # If still running, also poll the background process
    if state.get("status") in ("queued", "running"):
        session_id = state.get("process_session_id")
        if session_id:
            try:
                from tools.process_registry import process
                proc_info = process(action="poll", session_id=session_id)
                state["process_alive"] = proc_info.get("status") == "running"
                state["process_uptime_seconds"] = proc_info.get("uptime_seconds")
            except Exception:
                state["process_alive"] = False

    # Include latest metric if available
    history_path = job_dir / "history.json"
    if history_path.exists():
        try:
            history = json.loads(history_path.read_text())
            best = history.get("best")
            if best:
                state["best_metric"] = best.get("primary_metric")
                state["best_iteration"] = best.get("iteration")
        except Exception:
            pass

    return json.dumps({"ok": True, **state}, indent=2)


def _action_collect(args: dict[str, Any]) -> str:
    job_id = args.get("job_id", "")
    if not job_id:
        return tool_error("job_id is required for collect")

    job_dir = _job_dir(job_id)
    result_path = job_dir / "result.json"
    state_path = job_dir / "state.json"

    if not result_path.exists():
        status = "unknown"
        if state_path.exists():
            status = json.loads(state_path.read_text()).get("status", "unknown")
        return json.dumps({
            "ok": False,
            "error": f"Result not ready. Job status: {status}",
            "job_id": job_id,
        }, indent=2)

    result = json.loads(result_path.read_text())
    return json.dumps({"ok": True, "job_id": job_id, **result}, indent=2)


def _action_resume(args: dict[str, Any]) -> str:
    job_id = args.get("job_id", "")
    if not job_id:
        return tool_error("job_id is required for resume")

    job_dir = _job_dir(job_id)
    state_path = job_dir / "state.json"
    spec_path = job_dir / "job.json"
    history_path = job_dir / "history.json"

    if not state_path.exists() or not spec_path.exists():
        return json.dumps({"ok": False, "error": f"Job {job_id} not found"}, indent=2)

    state = json.loads(state_path.read_text())
    if state.get("status") not in ("interrupted", "failed"):
        return json.dumps({
            "ok": False,
            "error": f"Cannot resume job in status '{state.get('status')}'. Only interrupted or failed jobs can be resumed."
        }, indent=2)

    # Mark as resuming and re-launch
    state["status"] = "resuming"
    state_path.write_text(json.dumps(state, indent=2))

    hermes_root = Path("/home/fede/.hermes/hermes-agent")
    cmd = (
        f"cd {shlex.quote(str(hermes_root))} && "
        f"source venv/bin/activate && "
        f"HERMES_YOLO_MODE=1 python -m agent.research_job_runner {shlex.quote(str(spec_path))}"
    )

    from tools.terminal_tool import terminal
    raw = terminal(
        command=cmd,
        background=True,
        notify_on_complete=True,
        workdir=str(hermes_root),
    )
    proc = json.loads(raw) if isinstance(raw, str) else raw

    state["status"] = "queued"
    state["process_session_id"] = proc.get("session_id")
    state["pid"] = proc.get("pid")
    state["resumed_at"] = time.time()
    state_path.write_text(json.dumps(state, indent=2))

    return json.dumps({
        "ok": True,
        "job_id": job_id,
        "status": "queued",
        "message": f"Research job {job_id} resumed.",
        "process_session_id": proc.get("session_id"),
    }, indent=2)


# ---------------------------------------------------------------------------
# Tool handler
# ---------------------------------------------------------------------------

def research_job(
    action: str,
    job_id: str = "",
    topic: str = "",
    deliverable: str = "",
    metric_key: str = "",
    metric_direction: str = "maximize",
    task_type: str = "generic",
    evaluation_mode: str = "self_report",
    evaluation_prompt: str = "",
    max_iterations: int = 3,
    time_budget_sec: int = 0,
    lattice_task_id: str = "",
    initial_attempt: str = "",
    **_: Any,
) -> str:
    if action == "start":
        if not topic or not deliverable or not metric_key:
            return tool_error("topic, deliverable, and metric_key are required for start")
        return _action_start(locals())
    elif action == "status":
        return _action_status(locals())
    elif action == "collect":
        return _action_collect(locals())
    elif action == "resume":
        return _action_resume(locals())
    else:
        return tool_error(f"Unknown action: {action}")


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

registry.register(
    name="research_job",
    toolset="research",
    schema=RESEARCH_JOB_SCHEMA,
    handler=lambda args, **kw: research_job(**args),
    emoji="📋",
)
