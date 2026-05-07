"""Stub tests for HRM-94 — real timeout with SIGTERM/SIGKILL.

These tests are intentionally RED (failing) until the timeout logic is
implemented in agent/research/job_runner.py.
"""

import json
import os
import signal
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agent.research.job_runner import main as job_runner_main


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


class TestJobRunnerTimeoutSpec:
    def test_timeout_sec_forwarded_to_run_research(self, tmp_path: Path, fake_spec):
        """HRM-94: timeout_sec from spec must be forwarded to run_research."""
        spec_path = fake_spec(timeout_sec=120)
        with patch("agent.research.job_runner._build_agent") as mock_build, \
             patch("tools.research_tool.run_research") as mock_run:
            mock_build.return_value = MagicMock()
            mock_run.return_value = json.dumps({"result": "ok"})
            rc = job_runner_main(str(spec_path))
            assert rc == 0
            _, kwargs = mock_run.call_args
            assert kwargs.get("timeout_sec") == 120, (
                "timeout_sec must be passed to run_research"
            )

    def test_timeout_writes_state_timeout_on_expiry(self, tmp_path: Path, fake_spec):
        """HRM-94: if the research loop exceeds timeout_sec, state.json must say 'timeout'."""
        spec_path = fake_spec(timeout_sec=1)
        with patch("agent.research.job_runner._build_agent") as mock_build, \
             patch("tools.research_tool.run_research") as mock_run:
            mock_build.return_value = MagicMock()

            def _hang(*args, **kwargs):
                time.sleep(10)
                return json.dumps({"result": "ok"})

            mock_run.side_effect = _hang
            rc = job_runner_main(str(spec_path))

        # RED — currently job_runner does not enforce timeout
        assert rc != 0
        state = json.loads((tmp_path / "state.json").read_text())
        assert state.get("status") == "timeout", (
            f"Expected status='timeout', got {state.get('status')}"
        )

    def test_timeout_sends_sigterm_then_sigkill(self, tmp_path: Path, fake_spec):
        """HRM-94: on timeout, job_runner must SIGTERM then SIGKILL the child process."""
        spec_path = fake_spec(timeout_sec=1)
        with patch("agent.research.job_runner._build_agent") as mock_build, \
             patch("agent.research.job_runner.os.kill") as mock_kill, \
             patch("multiprocessing.Process") as MockProcess, \
             patch("tools.research_tool.run_research"):
            mock_build.return_value = MagicMock()
            proc = MagicMock()
            proc.pid = 9999
            proc.is_alive.return_value = True
            MockProcess.return_value = proc

            job_runner_main(str(spec_path))

        # RED — currently no multiprocessing / kill logic exists
        mock_kill.assert_any_call(9999, signal.SIGTERM)
        mock_kill.assert_any_call(9999, signal.SIGKILL)
