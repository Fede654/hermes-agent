"""research_job_runner — detached process entrypoint for long-running research loops.

Reads a job spec JSON, builds an AIAgent, calls run_research, and writes
durable checkpoint state after every completed round.

Usage:
    python -m agent.research_job_runner /path/to/job.json
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


def _setup_logging(job_dir: Path) -> None:
    log_path = job_dir / "runner.log"
    handler = logging.FileHandler(log_path, mode="a")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    root.addHandler(handler)


def _write_state(job_dir: Path, **fields: Any) -> None:
    state_path = job_dir / "state.json"
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    state.update(fields)
    state["updated_at"] = time.time()
    state_path.write_text(json.dumps(state, indent=2))


def _build_agent(spec: dict[str, Any]) -> Any:
    """Build an AIAgent from the job spec."""
    from run_agent import AIAgent

    agent = AIAgent(
        model=spec["model"],
        provider=spec.get("provider"),
        base_url=spec.get("base_url"),
        api_key=spec.get("api_key"),
        api_mode=spec.get("api_mode"),
        enabled_toolsets=spec.get("toolsets", ["research", "terminal", "file"]),
        quiet_mode=True,
        platform="cli",
        session_id=f"research-job:{spec['job_id']}",
        # Detached research jobs run under a curated profile (typically the
        # researcher scaffold). Inherit SOUL.md/MEMORY.md so the parent agent
        # gets the same context an interactive `researcher chat` would.
        # The job spec may override via "skip_context_files"/"skip_memory" keys.
        skip_context_files=spec.get("skip_context_files", False),
        skip_memory=spec.get("skip_memory", False),
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


def main(spec_path: str) -> int:
    spec = json.loads(Path(spec_path).read_text())
    job_dir = Path(spec["job_dir"])
    job_dir.mkdir(parents=True, exist_ok=True)

    # --- Lock file to prevent multiple instances of the same job ---
    lock_path = job_dir / ".runner.lock"
    try:
        # Use O_EXCL to atomically create the lock file; fail if it exists
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        with os.fdopen(fd, "w") as f:
            f.write(str(os.getpid()))
    except FileExistsError:
        print(f"ERROR: Job {spec['job_id']} is already running (lock file exists). Exiting.", file=sys.stderr)
        return 2

    try:
        _setup_logging(job_dir)
        logger.info("Job %s starting", spec["job_id"])

        _write_state(
            job_dir,
            job_id=spec["job_id"],
            status="running",
            pid=os.getpid(),
            started_at=time.time(),
            spec_path=spec_path,
        )

        try:
            agent = _build_agent(spec)
        except Exception as exc:
            logger.exception("Failed to build parent agent")
            _write_state(job_dir, status="failed", error=f"parent_agent build failed: {exc}")
            return 1

        from tools.research_tool import run_research

        try:
            logger.info("Calling run_research with checkpoint_dir=%s", job_dir)
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
                checkpoint_dir=str(job_dir),
            )

            result = json.loads(raw)
            result_path = job_dir / "result.json"
            result_path.write_text(json.dumps(result, indent=2))

            status = "completed" if "error" not in result else "failed"
            _write_state(job_dir, status=status, **result)
            logger.info("Job %s finished: %s", spec["job_id"], status)
            return 0 if status == "completed" else 1

        except Exception as exc:
            logger.exception("Job %s failed", spec["job_id"])
            _write_state(job_dir, status="failed", error=str(exc))
            return 1
    finally:
        try:
            lock_path.unlink(missing_ok=True)
        except Exception:
            pass


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python -m agent.research_job_runner <job.json>", file=sys.stderr)
        sys.exit(1)
    sys.exit(main(sys.argv[1]))
