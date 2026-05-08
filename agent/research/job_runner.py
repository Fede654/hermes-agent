"""agent.research.job_runner — detached process entrypoint for long-running research loops.

Two entrypoints:

  * ``main(spec_path)`` — the **parent** orchestrator. It spawns a fresh
    Python subprocess running ``_child_main`` and monitors it for both
    wall-clock timeout (HRM-94) and heartbeat liveness (HRM-95). On
    expiry it escalates SIGTERM → SIGKILL and writes a terminal status
    (``timeout`` / ``stale``) to ``state.json`` so external watchers
    (research_job_tool, status probes) see the job ended.

  * ``_child_main(spec_path)`` — runs inside the spawned subprocess.
    Builds the parent agent, calls ``run_research``, writes
    ``result.json`` + the final ``state.json``, and refreshes
    ``heartbeat.json`` every 30 s on a daemon thread.

Why a subprocess instead of multiprocessing.Process? Hermes' runtime
holds non-fork-safe state (SQLite connections, logging handlers,
provider HTTP clients, native threads). Forking and continuing in a
child interpreter is brittle. A clean subprocess starting from
``python -m agent.research.job_runner --child <spec.json>`` re-imports
the world fresh and avoids those hazards. The trade-off is that the
parent must communicate with the child via files (state.json,
heartbeat.json, result.json) rather than shared memory.

Usage:
    python -m agent.research.job_runner /path/to/job.json
"""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

# Re-exported so callers (research_job_tool, status probes, tests) have a
# single import surface for "is there something to resume here?". The real
# logic lives in supervisor._detect_resume — see the docstring there for
# the consistency-vs-probe distinction.
from agent.research.supervisor import _detect_resume  # noqa: F401
from agent.research.events import ResearchEvent, emit_event

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tunables — overridable via env vars for tests so the timeout / heartbeat
# loops finish in milliseconds instead of minutes.
# ---------------------------------------------------------------------------

_HEARTBEAT_INTERVAL = float(os.getenv("HERMES_JOB_HEARTBEAT_INTERVAL", "30"))
_STALE_THRESHOLD = float(os.getenv("HERMES_JOB_STALE_THRESHOLD", "90"))
_POLL_INTERVAL = float(os.getenv("HERMES_JOB_POLL_INTERVAL", "1"))
_SIGTERM_GRACE = float(os.getenv("HERMES_JOB_SIGTERM_GRACE", "5"))


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


# ---------------------------------------------------------------------------
# Heartbeat — written by the child, read by the parent and external probes.
# ---------------------------------------------------------------------------

def _write_heartbeat(job_dir: Path) -> None:
    """Write ``<job_dir>/heartbeat.json`` atomically.

    Atomic via tempfile + os.replace so a reader never sees a partial
    write — important because the parent polls this file aggressively.
    """
    hb = job_dir / "heartbeat.json"
    tmp = hb.with_name(hb.name + ".tmp")
    tmp.write_text(json.dumps({"ts": time.time(), "pid": os.getpid()}))
    os.replace(tmp, hb)


def _heartbeat_loop(job_dir: Path, stop_event: threading.Event) -> None:
    """Daemon-thread loop that refreshes heartbeat.json every interval."""
    while not stop_event.is_set():
        try:
            _write_heartbeat(job_dir)
        except Exception as exc:
            logger.warning("heartbeat write failed: %s", exc)
        # Event.wait() returns True if set during the wait — clean exit.
        if stop_event.wait(_HEARTBEAT_INTERVAL):
            return


def _is_heartbeat_stale(job_dir: Path) -> bool:
    """True when heartbeat.json is missing or older than the threshold."""
    hb = job_dir / "heartbeat.json"
    if not hb.exists():
        # Don't immediately treat absence as stale — the child may not
        # have written its first heartbeat yet. Caller distinguishes
        # "never seen" from "gone stale" via age tracking.
        return True
    try:
        data = json.loads(hb.read_text())
        ts = float(data.get("ts", 0))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return True
    return (time.time() - ts) > _STALE_THRESHOLD


# ---------------------------------------------------------------------------
# Process control — SIGTERM with SIGKILL escalation.
# ---------------------------------------------------------------------------

def _kill_with_escalation(proc: subprocess.Popen) -> None:
    """SIGTERM, wait grace period, SIGKILL if still alive.

    Mirrors the pattern documented in DESIGN-HRM94-95.md but expressed
    against subprocess.Popen rather than multiprocessing.Process.
    """
    if proc.poll() is not None:
        return
    try:
        os.kill(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        proc.wait(timeout=_SIGTERM_GRACE)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.kill(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    try:
        proc.wait(timeout=1)
    except subprocess.TimeoutExpired:
        logger.warning("child %s did not exit after SIGKILL", proc.pid)


# ---------------------------------------------------------------------------
# Child process — the actual research loop runs here.
# ---------------------------------------------------------------------------

def _build_agent(spec: dict[str, Any]) -> Any:
    """Build an AIAgent from the job spec.

    Thin wrapper around ``agent.factory.build_agent_for_research_job`` —
    construction + post-init patching live there so other detached
    entrypoints can reuse the same logic. See agent/factory.py for the
    "keep in sync with AIAgent" caveat.
    """
    from agent.factory import build_agent_for_research_job
    return build_agent_for_research_job(spec)


def _child_main(spec_path: str) -> int:
    """Body of the spawned subprocess: build agent, run research, write state.

    The parent owns timeout / heartbeat-stale detection; this function
    only owns its own heartbeat thread and the actual call to
    ``run_research``. State writes here are read by the parent (and by
    external watchers) for the *successful* completion path; on timeout
    or kill the parent overwrites status itself.
    """
    spec = json.loads(Path(spec_path).read_text())
    job_dir = Path(spec["job_dir"])
    job_dir.mkdir(parents=True, exist_ok=True)

    _setup_logging(job_dir)

    stop_event = threading.Event()
    # Synchronous initial heartbeat — the parent's polling loop starts
    # before the daemon thread fires, and we don't want a phantom
    # "stale" within the first second of life.
    _write_heartbeat(job_dir)
    hb_thread = threading.Thread(
        target=_heartbeat_loop, args=(job_dir, stop_event), daemon=True
    )
    hb_thread.start()

    try:
        # Test hooks — exercised by tests/agent/research/test_*_timeout
        # and test_heartbeat_stale. Production specs never set these.
        test_mode = spec.get("_test_mode")
        if test_mode == "sleep":
            time.sleep(float(spec.get("_test_sleep_sec", 5)))
            _write_state(job_dir, status="completed")
            return 0
        if test_mode == "freeze_heartbeat":
            # Stop refreshing the heartbeat, then sleep — used to trigger
            # the parent's stale-detection path without killing the child
            # ourselves.
            stop_event.set()
            time.sleep(float(spec.get("_test_sleep_sec", 60)))
            return 0

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
                kanban_task_id=spec.get("kanban_task_id"),
                parent_agent=agent,
                checkpoint_dir=str(job_dir),
                timeout_sec=spec.get("timeout_sec", 0),
            )

            result = json.loads(raw)
            (job_dir / "result.json").write_text(json.dumps(result, indent=2))

            status = "completed" if "error" not in result else "failed"
            _write_state(job_dir, status=status, **result)
            logger.info("Job %s finished: %s", spec["job_id"], status)
            return 0 if status == "completed" else 1

        except Exception as exc:
            logger.exception("Job %s failed", spec["job_id"])
            _write_state(job_dir, status="failed", error=str(exc))
            return 1
    finally:
        stop_event.set()


# ---------------------------------------------------------------------------
# Parent process — spawns the child, watches the clock and the heartbeat.
# ---------------------------------------------------------------------------

def _spawn_child(spec_path: str) -> subprocess.Popen:
    """Spawn the child subprocess that actually runs the research loop.

    Uses ``sys.executable -m agent.research.job_runner --child <spec>``
    so the child re-imports the module from a clean interpreter — no
    forked SQLite handles, no carried-over logging state.
    """
    return subprocess.Popen(
        [sys.executable, "-m", "agent.research.job_runner", "--child", spec_path],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def main(spec_path: str) -> int:
    """Parent orchestrator: spawn child, monitor, finalize state.

    Returns 0 on clean child exit, 1 on timeout / stale / failure, 2 if
    another instance of the same job already holds the lock.
    """
    spec = json.loads(Path(spec_path).read_text())
    job_dir = Path(spec["job_dir"])
    job_dir.mkdir(parents=True, exist_ok=True)

    # Lock to prevent concurrent runs of the same job.
    lock_path = job_dir / ".runner.lock"
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        with os.fdopen(fd, "w") as f:
            f.write(str(os.getpid()))
    except FileExistsError:
        print(
            f"ERROR: Job {spec['job_id']} is already running (lock file exists). Exiting.",
            file=sys.stderr,
        )
        return 2

    proc: subprocess.Popen | None = None
    try:
        _setup_logging(job_dir)
        logger.info("Job %s starting (parent pid=%d)", spec["job_id"], os.getpid())
        emit_event(job_dir, ResearchEvent.JOB_STARTED, {"job_id": spec["job_id"], "parent_pid": os.getpid()})

        timeout_sec = int(spec.get("timeout_sec", 0) or 0)

        _write_state(
            job_dir,
            job_id=spec["job_id"],
            status="running",
            pid=os.getpid(),
            started_at=time.time(),
            spec_path=spec_path,
            timeout_sec=timeout_sec,
        )

        proc = _spawn_child(spec_path)
        _write_state(job_dir, child_pid=proc.pid)
        logger.info("Spawned child pid=%d for job %s", proc.pid, spec["job_id"])

        deadline = time.monotonic() + timeout_sec if timeout_sec > 0 else None
        # Allow the child a grace window to write its first heartbeat
        # before we start treating "missing heartbeat" as a kill signal.
        first_seen_at: float | None = None
        startup_grace = max(_STALE_THRESHOLD, _HEARTBEAT_INTERVAL * 2)
        spawn_time = time.monotonic()

        while True:
            rc = proc.poll()
            if rc is not None:
                logger.info("Child %s exited rc=%d", proc.pid, rc)
                if rc != 0:
                    # Child should have written state.json itself; only
                    # overwrite if it didn't manage to set a terminal
                    # status (e.g. crashed before the finally block).
                    state = _read_state(job_dir)
                    if state.get("status") in (None, "running"):
                        _write_state(
                            job_dir,
                            status="failed",
                            error=f"child exited rc={rc}",
                        )
                return rc

            now = time.monotonic()

            if deadline is not None and now > deadline:
                logger.warning(
                    "Timeout: child %s exceeded %ds, killing", proc.pid, timeout_sec
                )
                _kill_with_escalation(proc)
                emit_event(job_dir, ResearchEvent.TIMEOUT_DETECTED, {"timeout_sec": timeout_sec})
                _write_state(
                    job_dir,
                    status="timeout",
                    error=f"Timed out after {timeout_sec}s",
                )
                return 1

            hb_path = job_dir / "heartbeat.json"
            if hb_path.exists():
                if first_seen_at is None:
                    first_seen_at = now
                if _is_heartbeat_stale(job_dir):
                    logger.warning(
                        "Heartbeat stale: child %s, killing", proc.pid
                    )
                    _kill_with_escalation(proc)
                    _write_state(
                        job_dir,
                        status="stale",
                        error="No heartbeat update for >threshold",
                    )
                    return 1
            elif (now - spawn_time) > startup_grace:
                logger.warning(
                    "Child %s never wrote heartbeat within %ss, killing",
                    proc.pid, startup_grace,
                )
                _kill_with_escalation(proc)
                emit_event(job_dir, ResearchEvent.STALE_DETECTED, {"reason": "no_initial_heartbeat"})
                _write_state(
                    job_dir,
                    status="stale",
                    error="Child never wrote initial heartbeat",
                )
                return 1

            time.sleep(_POLL_INTERVAL)

    finally:
        if proc is not None and proc.poll() is None:
            _kill_with_escalation(proc)
        try:
            lock_path.unlink(missing_ok=True)
        except Exception:
            pass


def _read_state(job_dir: Path) -> dict[str, Any]:
    sp = job_dir / "state.json"
    if not sp.exists():
        return {}
    try:
        return json.loads(sp.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


# ---------------------------------------------------------------------------
# CLI dispatch
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "--child":
        sys.exit(_child_main(sys.argv[2]))
    if len(sys.argv) < 2:
        print("Usage: python -m agent.research.job_runner <job.json>", file=sys.stderr)
        sys.exit(1)
    sys.exit(main(sys.argv[1]))
