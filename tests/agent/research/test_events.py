"""Tests for agent.research.events (HRM-101)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent.research.events import ResearchEvent, emit_event


def test_event_enum_members():
    assert ResearchEvent.JOB_STARTED.name == "JOB_STARTED"
    assert ResearchEvent.ITERATION_COMPLETED.name == "ITERATION_COMPLETED"


def test_emit_event_appends_jsonl(tmp_path: Path):
    job_dir = tmp_path / "job"
    job_dir.mkdir()

    emit_event(job_dir, ResearchEvent.JOB_STARTED, {"job_id": "j1"})
    emit_event(job_dir, ResearchEvent.ITERATION_COMPLETED, {"iteration": 1})

    events_file = job_dir / "events.jsonl"
    assert events_file.exists()

    lines = events_file.read_text().strip().split("\n")
    assert len(lines) == 2

    e0 = json.loads(lines[0])
    assert e0["event"] == "JOB_STARTED"
    assert e0["data"]["job_id"] == "j1"
    assert "ts" in e0

    e1 = json.loads(lines[1])
    assert e1["event"] == "ITERATION_COMPLETED"
    assert e1["data"]["iteration"] == 1


def test_emit_event_with_none_data(tmp_path: Path):
    job_dir = tmp_path / "job"
    job_dir.mkdir()

    emit_event(job_dir, ResearchEvent.CHECKPOINT_SAVED)
    lines = (job_dir / "events.jsonl").read_text().strip().split("\n")
    assert json.loads(lines[0])["data"] == {}
