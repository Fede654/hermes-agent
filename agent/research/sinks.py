"""Progress sinks for the autoresearch loop.

A ``ProgressSink`` is the seam through which ``ResearchSupervisor`` and
``ExperimentRunner`` report run progress to an external tracker. The
supervisor consumes a ``ProgressSink`` only — it does not know about
lattice, kanban, or any other backend.

Built-in implementations:

* :class:`StubSink` — log-only (default when no tracker is wired).
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


from typing import Optional


class KanbanSink:
    """Posts run progress to an EXISTING kanban task.

    On ``run_started`` and ``iteration_observed`` it appends a comment
    to the configured task. On ``run_completed`` it (optionally)
    transitions the task to ``done``. When ``task_id`` is None, every
    hook is log-only and no DB connection is opened.

    Connection lifecycle: the sink stores ``db_path`` (captured at
    construction so we don't re-resolve "current board" on every call)
    and opens a fresh short-lived sqlite3.Connection per write inside a
    try/finally. This avoids sqlite3 thread-affinity issues if the loop
    fans out across threads, and lets the dispatcher hold its own
    long-lived connection without contending. WAL mode keeps reads
    non-blocking and ``write_txn()`` (BEGIN IMMEDIATE) inside
    ``add_comment`` / ``complete_task`` keeps writes serialized.

    ``complete_on_run_completed`` controls whether ``run_completed`` calls
    ``complete_task``. A/B testing constructs per-strategy sub-sinks with
    ``complete_on_run_completed=False`` so the task stays open until the
    tester layer closes it once at the end.
    """

    _ACTOR = "agent:research-supervisor"

    def __init__(
        self,
        *,
        task_id: Optional[str],
        db_path: Optional[Path] = None,
        complete_on_run_completed: bool = True,
    ):
        self._task_id = task_id
        self._db_path = db_path  # captured at construction; do not re-resolve
        self._complete_on_run_completed = complete_on_run_completed

    def _open(self) -> Optional[Any]:
        """Open a fresh short-lived connection. Returns None when no task_id."""
        if not self._task_id:
            return None
        try:
            from hermes_cli import kanban_db
            # If db_path is None, fall through to kanban_db_path()'s env /
            # current-board resolution. Caller should usually pin db_path.
            path = self._db_path or kanban_db.kanban_db_path()
            return kanban_db.connect(path)
        except Exception as exc:
            logger.warning("[kanban-sink] connect failed: %s", exc)
            return None

    def _comment(self, message: str) -> None:
        if not self._task_id:
            logger.info("[kanban-stub] %s", message)
            return
        conn = self._open()
        if conn is None:
            return
        try:
            from hermes_cli import kanban_db
            kanban_db.add_comment(
                conn, self._task_id, self._ACTOR, message,
            )
        except Exception as exc:
            logger.warning("[kanban-sink] add_comment failed: %s", exc)
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def run_started(self, spec: Any, run_id: str) -> None:
        topic = getattr(spec, "topic", "")[:80]
        task_type = getattr(spec, "task_type", "?")
        metric = getattr(spec, "metric_key", "?")
        self._comment(
            f"Loop started: run_id={run_id} type={task_type} "
            f"metric={metric}\nTopic: {topic}"
        )

    def iteration_observed(
        self, iteration: int, result: Any, run_dir: Path
    ) -> None:
        metric = getattr(result, "primary_metric", None)
        improved = getattr(result, "improved", False)
        kept = getattr(result, "kept", False)
        status = "KEPT" if kept else ("IMPROVED" if improved else "DISCARDED")
        self._comment(
            f"Iteration {iteration}: {status} metric={metric}"
        )

    def run_completed(self, history: Any) -> None:
        results = getattr(history, "results", []) or []
        best = getattr(history, "best_result", None)
        best_metric = getattr(best, "primary_metric", None) if best else None
        self._comment(
            f"Loop done: {len(results)} rounds, best={best_metric}"
        )
        if not self._task_id or not self._complete_on_run_completed:
            return
        conn = self._open()
        if conn is None:
            return
        try:
            from hermes_cli import kanban_db
            kanban_db.complete_task(
                conn, self._task_id,
                result=str(best_metric) if best_metric is not None else None,
                summary=f"{len(results)} rounds, best={best_metric}",
            )
        except Exception as exc:
            logger.warning("[kanban-sink] complete_task failed: %s", exc)
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def comment(self, message: str) -> None:
        self._comment(message)
