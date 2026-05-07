"""HRM-100 — Cost accounting per iteration and per job.

Covers:
- DelegateSandboxResult and ExperimentResult accept tokens_in/tokens_out/cost_usd
- ExperimentHistory round-trips through dict with cost fields
- Backward compat: missing cost fields default to 0 / 0.0
- _run_worker extracts token/cost data from delegate_task JSON
- run_research returns iteration_costs + totals in result JSON
- job_runner writes totals into result.json
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from agent.research.runner import (
    DelegateSandboxResult,
    ExperimentHistory,
    ExperimentResult,
    _result_from_dict,
)
from agent.research.supervisor import _call_delegate_task, ResearchSupervisor, TaskSpec


# ---------------------------------------------------------------------------
# DelegateSandboxResult + ExperimentResult fields
# ---------------------------------------------------------------------------

def test_delegate_sandbox_result_defaults():
    r = DelegateSandboxResult(metrics={}, stdout="", stderr="", elapsed_sec=1.0)
    assert r.tokens_in == 0
    assert r.tokens_out == 0
    assert r.cost_usd == 0.0


def test_experiment_result_defaults():
    r = ExperimentResult(
        run_id="r1",
        iteration=0,
        code="code",
        metrics={},
        primary_metric=0.5,
        improved=True,
        kept=True,
        elapsed_sec=1.0,
        stdout="",
        stderr="",
    )
    assert r.tokens_in == 0
    assert r.tokens_out == 0
    assert r.cost_usd == 0.0


def test_experiment_result_with_costs():
    r = ExperimentResult(
        run_id="r1",
        iteration=1,
        code="code",
        metrics={"m": 0.9},
        primary_metric=0.9,
        improved=True,
        kept=True,
        elapsed_sec=2.0,
        stdout="ok",
        stderr="",
        tokens_in=100,
        tokens_out=50,
        cost_usd=0.0015,
    )
    assert r.tokens_in == 100
    assert r.tokens_out == 50
    assert r.cost_usd == 0.0015


# ---------------------------------------------------------------------------
# ExperimentHistory round-trip
# ---------------------------------------------------------------------------

def test_history_to_dict_includes_costs():
    hist = ExperimentHistory()
    hist.add(
        ExperimentResult(
            run_id="r1", iteration=0, code="c", metrics={}, primary_metric=0.5,
            improved=True, kept=True, elapsed_sec=1.0, stdout="", stderr="",
            tokens_in=10, tokens_out=5, cost_usd=0.0001,
        )
    )
    hist.add(
        ExperimentResult(
            run_id="r1", iteration=1, code="c2", metrics={}, primary_metric=0.6,
            improved=True, kept=True, elapsed_sec=1.0, stdout="", stderr="",
            tokens_in=20, tokens_out=10, cost_usd=0.0002,
        )
    )
    d = hist.to_dict()
    results = d["results"]
    assert len(results) == 2
    assert results[0]["tokens_in"] == 10
    assert results[1]["cost_usd"] == 0.0002


def test_history_from_dict_backward_compat():
    """Old checkpoints without cost fields load with zeros."""
    old = {
        "results": [
            {
                "run_id": "r1",
                "iteration": 0,
                "code": "c",
                "metrics": {},
                "primary_metric": 0.5,
                "improved": True,
                "kept": True,
                "elapsed_sec": 1.0,
                "stdout": "",
                "stderr": "",
                "error": None,
            }
        ],
        "best_result": None,
        "baseline_metric": 0.5,
    }
    hist = ExperimentHistory.from_dict(old)
    assert len(hist.results) == 1
    assert hist.results[0].tokens_in == 0
    assert hist.results[0].tokens_out == 0
    assert hist.results[0].cost_usd == 0.0


def test_history_from_dict_with_costs():
    data = {
        "results": [
            {
                "run_id": "r1",
                "iteration": 0,
                "code": "c",
                "metrics": {},
                "primary_metric": 0.5,
                "improved": True,
                "kept": True,
                "elapsed_sec": 1.0,
                "stdout": "",
                "stderr": "",
                "error": None,
                "tokens_in": 42,
                "tokens_out": 7,
                "cost_usd": 0.003,
            }
        ],
        "best_result": None,
        "baseline_metric": 0.5,
    }
    hist = ExperimentHistory.from_dict(data)
    assert hist.results[0].tokens_in == 42
    assert hist.results[0].tokens_out == 7
    assert hist.results[0].cost_usd == 0.003


# ---------------------------------------------------------------------------
# _result_from_dict backward compat
# ---------------------------------------------------------------------------

def test_result_from_dict_missing_cost_fields():
    data = {
        "run_id": "r1",
        "iteration": 0,
        "code": "c",
        "metrics": {},
        "primary_metric": 0.5,
        "improved": True,
        "kept": True,
        "elapsed_sec": 1.0,
        "stdout": "",
        "stderr": "",
        "error": None,
    }
    r = _result_from_dict(data)
    assert r is not None
    assert r.tokens_in == 0
    assert r.tokens_out == 0
    assert r.cost_usd == 0.0


# ---------------------------------------------------------------------------
# _run_worker extracts tokens / cost from delegate_task JSON
# ---------------------------------------------------------------------------

def test_run_worker_extracts_cost_data(monkeypatch):
    """_run_worker should pluck tokens and _child_cost_usd from delegate result."""
    fake_result_json = {
        "results": [
            {
                "status": "completed",
                "summary": "METRIC: pass_rate=0.8 STATUS: improved NOTES: ok",
                "tokens": {"input": 123, "output": 45},
                "_child_cost_usd": 0.005,
            }
        ]
    }

    def fake_call_delegate(goal, context, *, parent_agent, toolsets):
        return fake_result_json

    monkeypatch.setattr(
        "agent.research.supervisor._call_delegate_task", fake_call_delegate
    )

    parent = MagicMock()
    parent.model = "gpt-4o"
    supervisor = ResearchSupervisor(parent_agent=parent)

    spec = TaskSpec(
        topic="test",
        deliverable="test",
        metric_key="pass_rate",
        task_type="generic",
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        result = supervisor._run_worker(
            goal="do it",
            working_dir=tmpdir,
            attempt="code",
            spec=spec,
            time_budget_sec=0,
            iteration=0,
            worker_toolsets=["terminal"],
            llm=None,
        )

    assert result.tokens_in == 123
    assert result.tokens_out == 45
    assert result.cost_usd == 0.005


def test_run_worker_degrades_when_no_cost_data(monkeypatch):
    """If delegate_task omits tokens/cost, defaults must be zero."""
    fake_result_json = {
        "results": [
            {
                "status": "completed",
                "summary": "METRIC: pass_rate=0.8 STATUS: improved NOTES: ok",
            }
        ]
    }

    def fake_call_delegate(goal, context, *, parent_agent, toolsets):
        return fake_result_json

    monkeypatch.setattr(
        "agent.research.supervisor._call_delegate_task", fake_call_delegate
    )

    parent = MagicMock()
    supervisor = ResearchSupervisor(parent_agent=parent)

    spec = TaskSpec(
        topic="test",
        deliverable="test",
        metric_key="pass_rate",
        task_type="generic",
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        result = supervisor._run_worker(
            goal="do it",
            working_dir=tmpdir,
            attempt="code",
            spec=spec,
            time_budget_sec=0,
            iteration=0,
            worker_toolsets=["terminal"],
            llm=None,
        )

    assert result.tokens_in == 0
    assert result.tokens_out == 0
    assert result.cost_usd == 0.0


# ---------------------------------------------------------------------------
# run_research returns totals
# ---------------------------------------------------------------------------

def test_run_research_returns_cost_totals(monkeypatch):
    """run_research JSON must contain iteration_costs and totals."""
    from tools.research_tool import run_research

    hist = ExperimentHistory()
    hist.add(
        ExperimentResult(
            run_id="r1", iteration=0, code="c", metrics={"m": 0.5},
            primary_metric=0.5, improved=True, kept=True, elapsed_sec=1.0,
            stdout="", stderr="", tokens_in=100, tokens_out=50, cost_usd=0.001,
        )
    )
    hist.add(
        ExperimentResult(
            run_id="r1", iteration=1, code="c2", metrics={"m": 0.7},
            primary_metric=0.7, improved=True, kept=True, elapsed_sec=1.0,
            stdout="", stderr="", tokens_in=200, tokens_out=100, cost_usd=0.002,
        )
    )

    fake_supervisor = MagicMock()
    fake_supervisor.run.return_value = hist

    monkeypatch.setattr(
        "agent.research.supervisor.ResearchSupervisor", lambda **kw: fake_supervisor
    )
    monkeypatch.setattr(
        "hermes_constants.get_hermes_home", lambda: Path(tempfile.gettempdir())
    )

    parent = MagicMock()
    raw = run_research(
        topic="t",
        deliverable="d",
        metric_key="m",
        parent_agent=parent,
        max_iterations=2,
    )
    result = json.loads(raw)

    assert result["total_tokens_in"] == 300
    assert result["total_tokens_out"] == 150
    assert result["total_cost_usd"] == 0.003
    assert result["total_iterations"] == 2
    assert "iteration_costs" in result
    assert len(result["iteration_costs"]) == 2
    assert result["iteration_costs"][0]["tokens_in"] == 100
    assert result["iteration_costs"][1]["cost_usd"] == 0.002


def test_run_research_backward_compat_no_cost_data(monkeypatch):
    """If history results have zero costs, totals must still be present."""
    from tools.research_tool import run_research

    hist = ExperimentHistory()
    hist.add(
        ExperimentResult(
            run_id="r1", iteration=0, code="c", metrics={"m": 0.5},
            primary_metric=0.5, improved=True, kept=True, elapsed_sec=1.0,
            stdout="", stderr="",
        )
    )

    fake_supervisor = MagicMock()
    fake_supervisor.run.return_value = hist

    monkeypatch.setattr(
        "agent.research.supervisor.ResearchSupervisor", lambda **kw: fake_supervisor
    )
    monkeypatch.setattr(
        "hermes_constants.get_hermes_home", lambda: Path(tempfile.gettempdir())
    )

    parent = MagicMock()
    raw = run_research(
        topic="t",
        deliverable="d",
        metric_key="m",
        parent_agent=parent,
        max_iterations=1,
    )
    result = json.loads(raw)

    assert result["total_tokens_in"] == 0
    assert result["total_tokens_out"] == 0
    assert result["total_cost_usd"] == 0.0
    assert result["total_iterations"] == 1
    assert len(result["iteration_costs"]) == 1
    assert result["iteration_costs"][0]["cost_usd"] == 0.0


# ---------------------------------------------------------------------------
# job_runner wires totals into result.json
# ---------------------------------------------------------------------------

def test_job_runner_writes_cost_totals(monkeypatch, tmp_path: Path):
    """job_runner main must persist cost totals in result.json."""
    from agent.research import job_runner

    job_dir = tmp_path / "job"
    job_dir.mkdir()

    spec = {
        "job_id": "j1",
        "job_dir": str(job_dir),
        "topic": "t",
        "deliverable": "d",
        "metric_key": "m",
        "max_iterations": 2,
    }
    spec_path = job_dir / "job.json"
    spec_path.write_text(json.dumps(spec))

    fake_result = {
        "run_id": "r1",
        "iterations": 2,
        "best_metric": 0.9,
        "metric_key": "m",
        "metric_direction": "maximize",
        "best_notes": "nice",
        "workspace": "/tmp/ws",
        "learnings_file": "/tmp/ws/l.jsonl",
        "iteration_costs": [
            {"iteration": 0, "tokens_in": 10, "tokens_out": 5, "cost_usd": 0.0001},
            {"iteration": 1, "tokens_in": 20, "tokens_out": 10, "cost_usd": 0.0002},
        ],
        "total_tokens_in": 30,
        "total_tokens_out": 15,
        "total_cost_usd": 0.0003,
        "total_iterations": 2,
    }

    monkeypatch.setattr(
        "agent.research.job_runner._build_agent", lambda spec: MagicMock()
    )

    # With the subprocess model (Fix-3) we can't monkeypatch run_research in the
    # child process. Instead mock _spawn_child to return a completed process
    # and have _child_main write the fake result directly.
    def _fake_child_main(spec_path: str) -> int:
        spec = json.loads(Path(spec_path).read_text())
        job_dir = Path(spec["job_dir"])
        (job_dir / "result.json").write_text(json.dumps(fake_result))
        state_path = job_dir / "state.json"
        state = json.loads(state_path.read_text()) if state_path.exists() else {}
        state.update({"status": "completed", **fake_result})
        state_path.write_text(json.dumps(state, indent=2))
        return 0

    monkeypatch.setattr(
        "agent.research.job_runner._child_main", _fake_child_main
    )

    # Also mock _spawn_child so it calls our fake _child_main inline
    # and returns a mock Popen that looks finished.
    def _fake_spawn_child(spec_path: str):
        rc = _fake_child_main(spec_path)
        mock_proc = MagicMock()
        mock_proc.poll.return_value = rc
        mock_proc.pid = 12345
        return mock_proc

    monkeypatch.setattr(
        "agent.research.job_runner._spawn_child", _fake_spawn_child
    )

    rc = job_runner.main(str(spec_path))
    assert rc == 0

    result_path = job_dir / "result.json"
    assert result_path.exists()
    written = json.loads(result_path.read_text())
    assert written["total_tokens_in"] == 30
    assert written["total_tokens_out"] == 15
    assert written["total_cost_usd"] == 0.0003
    assert written["total_iterations"] == 2
    assert len(written["iteration_costs"]) == 2
