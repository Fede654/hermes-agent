"""Stub tests for HRM-95 — heartbeat / stale detection.

These tests are intentionally RED (failing) until the heartbeat writer
and stale checker are wired in.
"""

import json
import os
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from agent.research.job_runner import main as job_runner_main
from tools.research_tool import check_research_stale


@pytest.fixture
def fake_spec(tmp_path: Path):
    def _make(**overrides):
        base = {
            "job_id": "test-job",
            "job_dir": str(tmp_path),
            "topic": "t",
            "deliverable": "d",
            "metric_key": "m",
        }
        base.update(overrides)
        spec_path = tmp_path / "job.json"
        spec_path.write_text(json.dumps(base))
        return spec_path
    return _make


class TestHeartbeatWriter:
    def test_heartbeat_file_created(self, tmp_path: Path, fake_spec):
        """HRM-95: job_runner must write <job_dir>/heartbeat while running."""
        spec_path = fake_spec()
        with patch("agent.research.job_runner._build_agent") as mock_build, \
             patch("tools.research_tool.run_research") as mock_run:
            mock_build.return_value = MagicMock()
            mock_run.return_value = json.dumps({"result": "ok"})
            job_runner_main(str(spec_path))

        hb_path = tmp_path / "heartbeat"
        # RED — heartbeat writing is not implemented yet
        assert hb_path.exists(), "heartbeat file should exist after job_runner finishes"
        data = json.loads(hb_path.read_text())
        assert "ts" in data
        assert "pid" in data

    def test_heartbeat_updated_during_run(self, tmp_path: Path, fake_spec):
        """HRM-95: heartbeat timestamp must be refreshed during a long run."""
        spec_path = fake_spec()

        def _capture_hb(*args, **kwargs):
            time.sleep(2)
            return json.dumps({"result": "ok"})

        with patch("agent.research.job_runner._build_agent") as mock_build, \
             patch("tools.research_tool.run_research", side_effect=_capture_hb):
            mock_build.return_value = MagicMock()
            job_runner_main(str(spec_path))

        hb_path = tmp_path / "heartbeat"
        # RED — multiple heartbeat updates not implemented
        assert hb_path.exists()
        data = json.loads(hb_path.read_text())
        # If we had started at t=0 and run for 2s, heartbeat should be > t+1
        assert data["ts"] > time.time() - 5


class TestStaleChecker:
    def test_stale_when_no_heartbeat(self, tmp_path: Path):
        """HRM-95: missing heartbeat means stale."""
        # RED — stub currently returns False
        assert check_research_stale(str(tmp_path)) is True

    def test_stale_after_90s(self, tmp_path: Path):
        """HRM-95: heartbeat older than 90s means stale."""
        hb = tmp_path / "heartbeat"
        hb.write_text(json.dumps({"ts": time.time() - 120, "pid": 1}))
        # RED — stub currently returns False
        assert check_research_stale(str(tmp_path)) is True

    def test_not_stale_within_90s(self, tmp_path: Path):
        """HRM-95: recent heartbeat means not stale."""
        hb = tmp_path / "heartbeat"
        hb.write_text(json.dumps({"ts": time.time() - 30, "pid": 1}))
        # This should pass even with the stub (returns False) — but once
        # implemented correctly it must continue to pass.
        assert check_research_stale(str(tmp_path)) is False


class TestResearchJobToolStale:
    def test_status_marks_stale_when_heartbeat_missing(self, tmp_path: Path):
        """HRM-95: _action_status must mark a running job as stale if heartbeat is missing."""
        from tools.research_job_tool import _action_status

        job_id = "stale-job"
        job_dir = tmp_path / "research-jobs" / job_id
        job_dir.mkdir(parents=True)
        state = {
            "job_id": job_id,
            "status": "running",
            "process_session_id": "sess-1",
        }
        (job_dir / "state.json").write_text(json.dumps(state))

        with patch("tools.research_job_tool._job_dir", return_value=job_dir):
            result = _action_status({"job_id": job_id})
            parsed = json.loads(result)
            # RED — stale detection not wired in yet
            assert parsed.get("status") == "stale", (
                f"Expected stale, got {parsed.get('status')}"
            )
