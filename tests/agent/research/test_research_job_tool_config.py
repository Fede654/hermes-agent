"""Tests for config consistency between research_tool and research_job_tool (HRM-102)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import tools.research_job_tool as rjt
from tools.research_job_tool import research_job


def test_action_start_writes_job_dir_into_spec(tmp_path, monkeypatch):
    """job.json MUST carry ``job_dir`` — job_runner.main/_child_main read it
    unconditionally (``Path(spec["job_dir"])``), so omitting it crashes every
    detached job with KeyError before the runner can do anything.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    # Don't actually spawn a subprocess; return a fake terminal_tool payload.
    monkeypatch.setattr(
        "tools.terminal_tool.terminal_tool",
        lambda **kw: json.dumps({"session_id": "sess-1", "pid": 4242}),
    )
    monkeypatch.setattr(rjt, "_load_config_for_job", lambda: {"model": "test-model"})

    out = json.loads(research_job(action="start", job_id="jobxyz", topic="t",
                                  deliverable="d", metric_key="k"))
    assert out["ok"] is True

    job_dir = tmp_path / "research-jobs" / "jobxyz"
    spec = json.loads((job_dir / "job.json").read_text())
    assert spec["job_dir"] == str(job_dir)
    # The exact access job_runner performs must not raise.
    assert Path(spec["job_dir"]) == job_dir


def test_research_job_accepts_acceptance_criterion():
    """research_job must accept acceptance_criterion without error."""
    # Only verify the function signature accepts the param
    import inspect
    sig = inspect.signature(research_job)
    assert "acceptance_criterion" in sig.parameters


def test_research_job_accepts_timeout_sec():
    """research_job must accept timeout_sec without error."""
    import inspect
    sig = inspect.signature(research_job)
    assert "timeout_sec" in sig.parameters
