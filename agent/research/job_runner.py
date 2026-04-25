"""agent.research.job_runner — detached process entrypoint for long-running research loops.

Reads a job spec JSON, builds an AIAgent, calls run_research, and writes
durable checkpoint state after every completed round.

Usage:
    python -m agent.research.job_runner /path/to/job.json
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
    """Build an AIAgent from the job spec.

    Thin wrapper around ``agent.factory.build_agent_for_research_job`` —
    construction + post-init patching live there so other detached
    entrypoints can reuse the same logic. See agent/factory.py for the
    "keep in sync with AIAgent" caveat.
    """
    from agent.factory import build_agent_for_research_job
    return build_agent_for_research_job(spec)


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
        print("Usage: python -m agent.research.job_runner <job.json>", file=sys.stderr)
        sys.exit(1)
    sys.exit(main(sys.argv[1]))
