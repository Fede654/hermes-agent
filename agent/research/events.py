"""agent.research.events — structured event emission for the research loop.

Events are append-only JSON lines written to ``<job_dir>/events.jsonl``
so that external observers (TUI, dashboard, Lattice hooks) can subscribe to
progress without polling the running process.
"""
from __future__ import annotations

import json
import time
from enum import Enum, auto
from pathlib import Path
from typing import Any


class ResearchEvent(Enum):
    """Canonical event types emitted during a research job lifecycle."""

    JOB_STARTED = auto()
    BASELINE_STARTED = auto()
    BASELINE_COMPLETED = auto()
    ITERATION_STARTED = auto()
    ITERATION_COMPLETED = auto()
    CHECKPOINT_SAVED = auto()
    SNAPSHOT_CREATED = auto()
    BEST_RESULT_UPDATED = auto()
    TIMEOUT_DETECTED = auto()
    STALE_DETECTED = auto()
    JOB_COMPLETED = auto()
    JOB_FAILED = auto()


def emit_event(
    job_dir: Path,
    event: ResearchEvent,
    data: dict[str, Any] | None = None,
) -> None:
    """Append a structured event to ``<job_dir>/events.jsonl``.

    Args:
        job_dir: Research job directory (must exist).
        event: Event type.
        data: Optional extra metadata (iteration, metric, error, etc.).
    """
    events_path = job_dir / "events.jsonl"
    line = json.dumps(
        {
            "ts": time.time(),
            "event": event.name,
            "data": data or {},
        },
        default=str,
    )
    with events_path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")
