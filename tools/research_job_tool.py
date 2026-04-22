"""research_job — long-running research job orchestration tool.

Provides start/status/resume for research loops that run as detached OS
processes, avoiding iteration-budget and timeout problems of the spawning
agent.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tool schema
# ---------------------------------------------------------------------------

RESEARCH_JOB_TOOL_SCHEMA = {
    "name": "research_job",
    "description": (
        "Start, monitor, or resume a long-running research job that runs as a detached "
        "OS process.  Use this when a research task needs multiple iterations and may take "
        "10+ minutes, to avoid burning the spawning agent's iteration budget or hitting "
        "foreground timeouts.\n\n"
        "USE WHEN:\n"
        "- A research loop needs >3 iterations or >5 minutes total\n"
        "- You want the loop to survive even if the spawning agent restarts\n"
        "- You need checkpoint/resume for reliability\n\n"
        "NOT FOR:\n"
        "- One-shot tasks (use run_research directly)\n"
        "- Tasks that finish in <60 seconds (use delegate_task)\n\n"
        "IMPORTANT: Jobs run in the background.  You must poll or wait for completion."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["start", "status", "resume", "list"],
                "description": "Action to perform: start a new job, check status, resume a paused job, or list active jobs.",
            },
            "spec": {
                "type": "object",
                "description": (
                    "For action='start': the research spec.  Must contain at least "
                    "topic, deliverable, metric_key.  Optional: metric_direction, "
                    "task_type, evaluation_mode, evaluation_prompt, max_iterations, "
                    "time_budget_sec, lattice_task_id, toolsets."
                ),
            },
            "job_id": {
                "type": "string",
                "description": "For action='status' or 'resume': the job ID returned by start.",
            },
        },
        "required": ["action"],
    },
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _job_dir(job_id: str) -> Path:
    return get_hermes_home() / "research-jobs" / job_id


def _venv_python() -> str:
    hermes_root = Path(__file__).resolve().parent.parent
    venv_python = hermes_root / "venv" / "bin" / "python"
    if venv_python.exists():
        return str(venv_python)
    return sys.executable


def _write_job_spec(job_dir: Path, spec: dict[str, Any]) -> None:
    job_dir.mkdir(parents=True, exist_ok=True)
    spec_path = job_dir / "job.json"
    spec_path.write_text(json.dumps(spec, indent=2))


def _write_state(job_dir: Path, state: dict[str, Any]) -> None:
    state_path = job_dir / "state.json"
    state["updated_at"] = time.time()
    state_path.write_text(json.dumps(state, indent=2))


def _read_state(job_dir: Path) -> dict[str, Any]:
    state_path = job_dir / "state.json"
    if not state_path.exists():
        return {}
    try:
        return json.loads(state_path.read_text())
    except json.JSONDecodeError:
        return {}


def _spawn_runner(job_dir: Path, spec: dict[str, Any]) -> subprocess.Popen:
    """Launch the detached runner process."""
    python = _venv_python()
    runner_module = "agent.research_job_runner"
    env = {**os.environ, "HERMES_YOLO_MODE": "1"}

    stdout_log = job_dir / "runner.stdout.log"
    stderr_log = job_dir / "runner.stderr.log"
    stdout_f = stdout_log.open("a")
    stderr_f = stderr_log.open("a")

    proc = subprocess.Popen(
        [python, "-m", runner_module, str(job_dir)],
        stdout=stdout_f,
        stderr=stderr_f,
        env=env,
        start_new_session=True,  # detach from parent terminal
    )
    return proc


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------

def _action_start(spec: dict[str, Any]) -> dict[str, Any]:
    job_id = spec.get("job_id") or secrets.token_hex(8)
    job_dir = _job_dir(job_id)

    full_spec = {
        **spec,
        "job_id": job_id,
        "job_dir": str(job_dir),
    }
    _write_job_spec(job_dir, full_spec)

    state = {
        "job_id": job_id,
        "status": "queued",
        "action": "start",
    }
    _write_state(job_dir, state)

    proc = _spawn_runner(job_dir, full_spec)

    state["status"] = "running"
    state["pid"] = proc.pid
    _write_state(job_dir, state)

    logger.info("Research job started: %s (pid=%d)", job_id, proc.pid)
    return {
        "job_id": job_id,
        "status": "running",
        "pid": proc.pid,
        "job_dir": str(job_dir),
        "workspace": str(get_hermes_home() / "research-workspace"),
    }


def _action_status(job_id: str) -> dict[str, Any]:
    job_dir = _job_dir(job_id)
    if not job_dir.exists():
        return {"error": f"Job {job_id} not found"}

    state = _read_state(job_dir)
    spec_path = job_dir / "job.json"
    result_path = job_dir / "result.json"
    report_path = job_dir / "report.md"

    # If state says running, verify the process is still alive
    if state.get("status") == "running":
        pid = state.get("pid")
        if pid and isinstance(pid, int):
            try:
                os.kill(pid, 0)
            except OSError:
                state["status"] = "interrupted"
                _write_state(job_dir, state)

    out: dict[str, Any] = {
        "job_id": job_id,
        "status": state.get("status", "unknown"),
        "state": state,
    }

    if result_path.exists():
        try:
            out["result"] = json.loads(result_path.read_text())
        except json.JSONDecodeError:
            pass

    if report_path.exists():
        out["report_path"] = str(report_path)

    if spec_path.exists():
        try:
            out["spec"] = json.loads(spec_path.read_text())
        except json.JSONDecodeError:
            pass

    return out


def _action_resume(job_id: str) -> dict[str, Any]:
    job_dir = _job_dir(job_id)
    if not job_dir.exists():
        return {"error": f"Job {job_id} not found"}

    state = _read_state(job_dir)
    if state.get("status") not in ("interrupted", "failed"):
        return {"error": f"Job {job_id} cannot be resumed from status '{state.get('status')}'"}

    spec_path = job_dir / "job.json"
    if not spec_path.exists():
        return {"error": f"Job spec missing for {job_id}"}

    spec = json.loads(spec_path.read_text())

    # Mark resume attempt
    state["status"] = "resuming"
    state["resumed_at"] = time.time()
    _write_state(job_dir, state)

    proc = _spawn_runner(job_dir, spec)

    state["status"] = "running"
    state["pid"] = proc.pid
    _write_state(job_dir, state)

    logger.info("Research job resumed: %s (pid=%d)", job_id, proc.pid)
    return {
        "job_id": job_id,
        "status": "running",
        "pid": proc.pid,
        "job_dir": str(job_dir),
    }


def _action_list() -> dict[str, Any]:
    jobs_dir = get_hermes_home() / "research-jobs"
    if not jobs_dir.exists():
        return {"jobs": []}

    jobs: list[dict[str, Any]] = []
    for entry in sorted(jobs_dir.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if not entry.is_dir():
            continue
        state = _read_state(entry)
        jobs.append({
            "job_id": entry.name,
            "status": state.get("status", "unknown"),
            "updated_at": state.get("updated_at"),
        })
    return {"jobs": jobs[:20]}


# ---------------------------------------------------------------------------
# Tool handler
# ---------------------------------------------------------------------------

def handle_research_job(args: dict[str, Any]) -> str:
    action = args.get("action", "")
    try:
        if action == "start":
            result = _action_start(args.get("spec", {}))
        elif action == "status":
            result = _action_status(args.get("job_id", ""))
        elif action == "resume":
            result = _action_resume(args.get("job_id", ""))
        elif action == "list":
            result = _action_list()
        else:
            result = {"error": f"Unknown action: {action}"}
    except Exception as exc:
        logger.exception("research_job action=%s failed", action)
        result = {"error": str(exc)}

    return json.dumps(result, indent=2)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

from tools.registry import registry


def _check_research_job_requirements() -> bool:
    try:
        from tools.research_tool import run_research  # noqa: F401
        return True
    except ImportError:
        return False


registry.register(
    name="research_job",
    toolset="research",
    schema=RESEARCH_JOB_TOOL_SCHEMA,
    handler=lambda args, **kw: handle_research_job(args),
    check_fn=_check_research_job_requirements,
    emoji="📚",
)
