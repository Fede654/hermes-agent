"""HRM-94 — parent-side timeout enforcement.

The job_runner now spawns a fresh Python subprocess for the actual
research loop. The parent watches wall-clock time and signals the child
on expiry: SIGTERM, then SIGKILL after a 5 s grace. On timeout it
overwrites ``state.json`` with ``status="timeout"``.

Tests use the ``_test_mode`` hook in the spec so we don't need to mock
``run_research`` across a process boundary — the child interprets the
hook and just sleeps. Tunables (poll interval, SIGTERM grace) are
shrunk via env vars to keep the suite fast.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from agent.research.job_runner import main as job_runner_main


@pytest.fixture
def fast_runner_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Shrink job_runner timeouts so the suite finishes in seconds.

    These env vars are read at import time by job_runner — the child
    subprocess is a fresh interpreter that will read them from os.environ
    we propagate via subprocess.Popen's default env inheritance.
    """
    monkeypatch.setenv("HERMES_JOB_HEARTBEAT_INTERVAL", "0.3")
    monkeypatch.setenv("HERMES_JOB_STALE_THRESHOLD", "60")  # large — we want timeout, not stale
    monkeypatch.setenv("HERMES_JOB_POLL_INTERVAL", "0.1")
    monkeypatch.setenv("HERMES_JOB_SIGTERM_GRACE", "1")
    # Module-level constants in the parent process were captured at
    # import time. Rebind them so the running parent uses the test
    # values too.
    import agent.research.job_runner as jr
    jr._HEARTBEAT_INTERVAL = 0.3
    jr._STALE_THRESHOLD = 60
    jr._POLL_INTERVAL = 0.1
    jr._SIGTERM_GRACE = 1


@pytest.fixture
def fake_spec(tmp_path: Path):
    def _make(**overrides):
        base = {
            "job_id": "test-job",
            "job_dir": str(tmp_path),
            "topic": "t",
            "deliverable": "d",
            "metric_key": "m",
            "timeout_sec": 0,
        }
        base.update(overrides)
        spec_path = tmp_path / "job.json"
        spec_path.write_text(json.dumps(base))
        return spec_path
    return _make


class TestJobRunnerTimeout:
    def test_completes_within_timeout(self, tmp_path: Path, fake_spec, fast_runner_env):
        """Child finishes before timeout → parent returns 0, status=completed."""
        spec_path = fake_spec(
            timeout_sec=10,
            _test_mode="sleep",
            _test_sleep_sec=0.5,
        )
        rc = job_runner_main(str(spec_path))
        assert rc == 0
        state = json.loads((tmp_path / "state.json").read_text())
        assert state["status"] == "completed"

    def test_timeout_writes_state_timeout(self, tmp_path: Path, fake_spec, fast_runner_env):
        """Child runs longer than timeout → parent kills it, status=timeout."""
        spec_path = fake_spec(
            timeout_sec=2,
            _test_mode="sleep",
            _test_sleep_sec=30,
        )
        t0 = time.monotonic()
        rc = job_runner_main(str(spec_path))
        elapsed = time.monotonic() - t0

        assert rc != 0
        state = json.loads((tmp_path / "state.json").read_text())
        assert state.get("status") == "timeout", f"got status={state.get('status')!r}"
        assert "Timed out" in state.get("error", "")
        # Should kill within ~timeout + grace, not run the full 30 s sleep.
        assert elapsed < 10, f"timeout took {elapsed:.1f}s — kill escalation broken?"

    def test_child_pid_recorded_in_state(self, tmp_path: Path, fake_spec, fast_runner_env):
        spec_path = fake_spec(
            timeout_sec=10,
            _test_mode="sleep",
            _test_sleep_sec=0.5,
        )
        job_runner_main(str(spec_path))
        state = json.loads((tmp_path / "state.json").read_text())
        assert isinstance(state.get("child_pid"), int)
        assert state["child_pid"] != os.getpid()

    def test_no_timeout_when_zero(self, tmp_path: Path, fake_spec, fast_runner_env):
        """timeout_sec=0 disables the wall-clock guard."""
        spec_path = fake_spec(
            timeout_sec=0,
            _test_mode="sleep",
            _test_sleep_sec=0.3,
        )
        rc = job_runner_main(str(spec_path))
        assert rc == 0
        state = json.loads((tmp_path / "state.json").read_text())
        assert state["status"] == "completed"
