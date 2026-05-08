"""Progress sinks for the autoresearch loop.

A ``ProgressSink`` is the seam through which ``ResearchSupervisor`` and
``ExperimentRunner`` report run progress to an external tracker. The
supervisor consumes a ``ProgressSink`` only — it does not know about
lattice, kanban, or any other backend.

Built-in implementations:

* :class:`StubSink` — log-only (default when no tracker is wired).
* :class:`LatticeSink` — shells out to the ``lattice`` CLI.
  Deprecated: kept for backward compatibility only, will be removed.
* :class:`KanbanSink` — appends comments to an EXISTING kanban task and
  transitions status on completion. The caller is responsible for
  creating the task; the sink does not auto-create.

Sinks must never raise: a misbehaving tracker must not break the loop.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Protocol

logger = logging.getLogger(__name__)


class ProgressSink(Protocol):
    """The contract implemented by every progress sink.

    Hooks are invoked by ResearchSupervisor / ExperimentRunner. All hooks
    must be best-effort and never raise — failures are swallowed and logged.
    """

    def run_started(self, spec: Any, run_id: str) -> None:
        """Called once at the start of a run, before iteration 0."""
        ...

    def iteration_observed(
        self, iteration: int, result: Any, run_dir: Path
    ) -> None:
        """Called after _observe for each completed iteration."""
        ...

    def run_completed(self, history: Any) -> None:
        """Called once at the end of a run with the final ExperimentHistory."""
        ...

    def comment(self, message: str) -> None:
        """Free-form progress comment. Used by call sites that already only
        emit text and don't have a structured event."""
        ...


class StubSink:
    """Log-only sink. The default when no tracker is configured.

    Every hook drops a ``logger.info`` line at "[sink-stub]". Never raises.
    """

    def run_started(self, spec: Any, run_id: str) -> None:
        topic = getattr(spec, "topic", "")[:60]
        logger.info("[sink-stub] run_started run_id=%s topic=%s", run_id, topic)

    def iteration_observed(
        self, iteration: int, result: Any, run_dir: Path
    ) -> None:
        metric = getattr(result, "primary_metric", None)
        improved = getattr(result, "improved", False)
        logger.info(
            "[sink-stub] iter=%d metric=%s improved=%s",
            iteration, metric, improved,
        )

    def run_completed(self, history: Any) -> None:
        results = getattr(history, "results", []) or []
        best = getattr(history, "best_result", None)
        best_metric = getattr(best, "primary_metric", None) if best else None
        logger.info(
            "[sink-stub] run_completed iters=%d best=%s",
            len(results), best_metric,
        )

    def comment(self, message: str) -> None:
        logger.info("[sink-stub] %s", message)
