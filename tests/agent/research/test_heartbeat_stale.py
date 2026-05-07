"""HRM-95 — heartbeat / stale detection.

The child subprocess refreshes ``<job_dir>/heartbeat.json`` every
``HERMES_JOB_HEARTBEAT_INTERVAL`` seconds (default 30). The parent
watches that file; if it goes older than ``HERMES_JOB_STALE_THRESHOLD``
(default 90) the parent kills the child and writes
``status="stale"``. External callers can probe staleness via
``tools.research_tool.check_research_stale``.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from agent.research.job_runner import main as job_runner_main
from tools.research_tool import check_research_stale


@pytest.fixture
def fast_runner_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HERMES_JOB_HEARTBEAT_INTERVAL", "0.3")
    monkeypatch.setenv("HERMES_JOB_STALE_THRESHOLD", "1.5")
    monkeypatch.setenv("HERMES_JOB_POLL_INTERVAL", "0.1")
    monkeypatch.setenv("HERMES_JOB_SIGTERM_GRACE", "1")
    import agent.research.job_runner as jr
    jr._HEARTBEAT_INTERVAL = 0.3
    jr._STALE_THRESHOLD = 1.5
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


class TestHeartbeatWriter:
    def test_heartbeat_file_created(self, tmp_path: Path, fake_spec, fast_runner_env):
        """The child writes heartbeat.json before exiting."""
        spec_path = fake_spec(
            _test_mode="sleep",
            _test_sleep_sec=0.5,
        )
        rc = job_runner_main(str(spec_path))
        assert rc == 0
        hb_path = tmp_path / "heartbeat.json"
        assert hb_path.exists()
        data = json.loads(hb_path.read_text())
        assert "ts" in data
        assert "pid" in data

    def test_heartbeat_pid_is_child(self, tmp_path: Path, fake_spec, fast_runner_env):
        """heartbeat.json's pid is the child's, not the parent's."""
        spec_path = fake_spec(
            _test_mode="sleep",
            _test_sleep_sec=0.5,
        )
        job_runner_main(str(spec_path))
        state = json.loads((tmp_path / "state.json").read_text())
        hb = json.loads((tmp_path / "heartbeat.json").read_text())
        assert hb["pid"] == state["child_pid"]


class TestParentStaleDetection:
    def test_stale_child_killed_with_status_stale(
        self, tmp_path: Path, fake_spec, fast_runner_env
    ):
        """If the child stops refreshing heartbeat, parent kills it."""
        spec_path = fake_spec(
            timeout_sec=0,            # no wall-clock timeout
            _test_mode="freeze_heartbeat",
            _test_sleep_sec=30,       # would otherwise sleep forever
        )
        t0 = time.monotonic()
        rc = job_runner_main(str(spec_path))
        elapsed = time.monotonic() - t0

        assert rc != 0
        state = json.loads((tmp_path / "state.json").read_text())
        assert state["status"] == "stale", f"got status={state.get('status')!r}"
        # With threshold=1.5 and grace=1, kill should happen well before 30s.
        assert elapsed < 10, f"stale kill took {elapsed:.1f}s"


class TestStaleChecker:
    def test_stale_when_no_heartbeat(self, tmp_path: Path):
        assert check_research_stale(str(tmp_path)) is True

    def test_stale_after_threshold(self, tmp_path: Path):
        hb = tmp_path / "heartbeat.json"
        hb.write_text(json.dumps({"ts": time.time() - 120, "pid": 1}))
        assert check_research_stale(str(tmp_path)) is True

    def test_not_stale_within_threshold(self, tmp_path: Path):
        hb = tmp_path / "heartbeat.json"
        hb.write_text(json.dumps({"ts": time.time() - 30, "pid": 1}))
        assert check_research_stale(str(tmp_path)) is False

    def test_stale_when_corrupt(self, tmp_path: Path):
        hb = tmp_path / "heartbeat.json"
        hb.write_text("{not json")
        assert check_research_stale(str(tmp_path)) is True

    def test_custom_threshold_honored(self, tmp_path: Path):
        hb = tmp_path / "heartbeat.json"
        hb.write_text(json.dumps({"ts": time.time() - 10, "pid": 1}))
        assert check_research_stale(str(tmp_path), stale_threshold_sec=5.0) is True
        assert check_research_stale(str(tmp_path), stale_threshold_sec=60.0) is False


class TestResearchJobToolStale:
    def test_status_marks_stale_when_heartbeat_missing(self, tmp_path: Path):
        """_action_status flips status to 'stale' when heartbeat is gone."""
        from tools.research_job_tool import _action_status

        job_id = "stale-job"
        job_dir = tmp_path / "research-jobs" / job_id
        job_dir.mkdir(parents=True)
        (job_dir / "state.json").write_text(json.dumps({
            "job_id": job_id,
            "status": "running",
            "process_session_id": "sess-1",
        }))

        with patch("tools.research_job_tool._job_dir", return_value=job_dir):
            result = _action_status({"job_id": job_id})
            parsed = json.loads(result)
            assert parsed.get("status") == "stale"
            assert "no heartbeat" in parsed.get("stale_reason", "").lower()

    def test_status_does_not_mark_stale_when_heartbeat_fresh(self, tmp_path: Path):
        from tools.research_job_tool import _action_status

        job_id = "live-job"
        job_dir = tmp_path / "research-jobs" / job_id
        job_dir.mkdir(parents=True)
        (job_dir / "state.json").write_text(json.dumps({
            "job_id": job_id,
            "status": "running",
        }))
        (job_dir / "heartbeat.json").write_text(json.dumps({
            "ts": time.time(), "pid": 42,
        }))

        with patch("tools.research_job_tool._job_dir", return_value=job_dir):
            result = _action_status({"job_id": job_id})
            parsed = json.loads(result)
            assert parsed.get("status") == "running"
