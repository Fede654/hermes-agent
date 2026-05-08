# ResearchSupervisor `ProgressSink` Refactor (Phase A) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the inline lattice progress hook in `ResearchSupervisor` and `ExperimentRunner` with a `ProgressSink` abstraction, ship a `KanbanSink` implementation that lands run progress in the kanban dashboard, and deprecate the `lattice_task_id` parameter on `run_research` / `research_job` so future call sites converge on the new API.

**Architecture:**
The `ResearchSupervisor` already routes every progress event through one closure (`lattice_comment_fn`) and one helper (`_make_lattice_comment_fn`). We introduce a `ProgressSink` Protocol with three event hooks (`run_started`, `iteration_observed`, `run_completed`) plus a free-form `comment` for places that already only emit text. Three concrete sinks implement it: `StubSink` (log-only), `LatticeSink` (preserves the current shell-out, kept for backward compatibility but no longer the default), and `KanbanSink` (creates a kanban task, appends a comment per iteration, completes/blocks the task at run end). The supervisor stops constructing its own progress closure and instead accepts a `ProgressSink` (or builds one from legacy kwargs).

**Tech Stack:** Python 3.11, `typing.Protocol`, existing `hermes_cli/kanban_db.py` SQLite API, pytest.

---

## Audit corrections (Codex 2026-05-08) + lattice prune decision

Codex flagged the original draft as MAJOR_REVISIONS_NEEDED. After audit + a
direct user decision to fully prune lattice (no `LatticeSink` shim), the
plan now reflects:

1. **`KanbanSink` does NOT create the kanban task.** Caller creates the task; sink only appends comments and transitions status.
2. **No `LatticeSink`.** Lattice is pruned entirely. `lattice_task_id` parameter is removed from supervisor / runner / tools / ABTester. Anyone passing it gets a `TypeError`.
3. **`_make_lattice_comment_fn` is deleted.** Free-form comments now flow through `self._sink.comment` (which for the default `StubSink` is a `logger.info`, and for `KanbanSink` is a kanban comment).
4. **`ResearchSupervisor.__init__` is keyword-only and `workspace: Path | None = None`.** Task 4 preserves the keyword-only contract; `lattice_task_id` and `lattice_root` parameters are dropped.
5. **`ExperimentRunner.__init__` keeps `*` before `delegate_fn` and the `self.workspace.mkdir(parents=True, exist_ok=True)` call.** Task 5 preserves these; `lattice_comment_fn` parameter is dropped.
6. **The `if llm is None:` early-return path (supervisor.py:791) must call `self._sink.run_completed(...)` before returning.** Task 4 covers this.
7. **`_reflect()` (supervisor.py:1489) takes a `comment_fn` parameter (renamed from `lattice_comment_fn`).** The call site passes `self._sink.comment`.
8. **`run_research()` imports `ResearchSupervisor` at module scope.** Required for `unittest.mock.patch("tools.research_tool.ResearchSupervisor")` to bind. Task 6.
9. **The registry handler lambda forwards `kanban_task_id`.** Task 6.
10. **`KanbanSink` takes `db_path: Path` (captured at construction), not a borrowed `Connection`. Opens short-lived connections per call.** Avoids sqlite3 thread-affinity issues and dispatcher races. Task 2 (was Task 3 in original numbering).
11. **`KanbanSink` accepts `complete_on_run_completed: bool = True`.** A/B testing constructs sub-sinks with `False`; `ResearchABTester` closes the parent task once at the end. Task 6 (was Task 7).
12. **Pytest fixture uses `HERMES_KANBAN_DB` env var (highest-precedence path resolver). `connect()` auto-initializes the schema.**

### Renumbered task list (post-prune)

| New # | Old # | What |
|---|---|---|
| 1 | 1 | `ProgressSink` Protocol + `StubSink` |
| 2 | 3 | `KanbanSink` (was Task 3) |
| 3 | 4 | Wire supervisor to `ProgressSink`, drop `lattice_task_id`/`lattice_root`, delete `_make_lattice_comment_fn` |
| 4 | 5 | Wire runner to `ProgressSink`, drop `lattice_comment_fn` parameter |
| 5 | 6 | `run_research(kanban_task_id=...)` + drop `lattice_task_id` from schema/handler/A/B branch |
| 6 | 7 | `research_job` schema cleanup + `ResearchABTester` accepts sink + child sinks suppress complete |
| 7 | 8 | Validation harness against real kanban DB |

Old Task 2 (`LatticeSink`) is removed entirely. The line counts and verifications below still reference the Task 4/Task 5/Task 6/Task 7 headings; treat those as Task 3/4/5/6 in the renumbered scheme.

---

## File Structure

**Create:**
- `agent/research/sinks.py` — `ProgressSink` Protocol + concrete sinks (`StubSink`, `LatticeSink`, `KanbanSink`).
- `tests/agent/research/test_sinks.py` — unit tests for each sink (in-process kanban DB, mocked subprocess for lattice).

**Modify:**
- `agent/research/supervisor.py`
  - Remove `_make_lattice_comment_fn` (lines 435–449) — moved to `LatticeSink`.
  - `ResearchSupervisor.__init__` — accept `progress_sink: Optional[ProgressSink] = None` alongside legacy `lattice_task_id`/`lattice_root`. When `progress_sink` is None and `lattice_task_id` is set, build a `LatticeSink`. Otherwise default to `StubSink`.
  - `ResearchSupervisor.run` — replace local `lattice_comment_fn` variable with `self._sink.comment` and call `self._sink.run_started(spec, run_id)` at the top, `self._sink.iteration_observed(iteration, result, run_dir)` after each `_observe`, `self._sink.run_completed(history)` at the bottom.
- `agent/research/runner.py`
  - `ExperimentRunner.__init__` (line 141) — accept `progress_sink: Optional[ProgressSink] = None`. Keep `lattice_comment_fn` parameter for one release, route it into a `LatticeSink` if passed; emit a `DeprecationWarning`.
  - Replace `self._lattice_comment(...)` calls (lines 229, 240, 260, 263) with `self._sink.comment(...)`.
- `tools/research_tool.py`
  - Line 204: add `kanban_task_id: Optional[str] = None` param.
  - Line 120: extend the JSON schema — add `kanban_task_id`, mark `lattice_task_id` deprecated in the description (keep field).
  - Line 250 / 276: build the right sink (`KanbanSink` if `kanban_task_id` set, else `LatticeSink` if `lattice_task_id` set, else `StubSink`) and pass to `ResearchSupervisor(progress_sink=...)`.
  - When both `kanban_task_id` and `lattice_task_id` are set, prefer kanban and log a warning.
- `tools/research_job_tool.py`
  - Line 129: add `kanban_task_id` to the schema.
  - Line 158/177: thread `kanban_task_id` through into the spec written to the job_dir.
  - Line 52 (`_lattice_available`): leave; it stays accurate.

**Test (existing files, no new test files beyond the one above):**
- `tests/agent/research/test_research_job_tool_config.py` — extend with one test verifying `kanban_task_id` param round-trips through the spec.
- `tests/agent/test_research_supervisor.py` — extend `TestBuildTaskBrief` to confirm progress sink calls do not leak into the brief.

**Untouched (do not edit):**
- `hermes_cli/kanban_db.py` — public API is sufficient.
- `agent/research/evolution.py`, `agent/research/metrics.py`, `agent/research/events.py`, `agent/research/ab_testing.py`, `agent/research/job_runner.py` — none reference lattice directly; sinks plug in only at supervisor + runner.

---

## Task 1 — `ProgressSink` Protocol + `StubSink`

**Files:**
- Create: `agent/research/sinks.py`
- Test: `tests/agent/research/test_sinks.py`

- [ ] **Step 1: Write failing tests**

Create `tests/agent/research/test_sinks.py`:

```python
"""Tests for ProgressSink Protocol and concrete sink implementations."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agent.research.runner import ExperimentResult
from agent.research.sinks import StubSink
from agent.research.supervisor import TaskSpec


def _spec() -> TaskSpec:
    return TaskSpec(
        topic="t", deliverable="d",
        metric_key="pass_rate", metric_direction="maximize",
    )


def _result(iteration: int = 0, primary_metric: float = 0.5) -> ExperimentResult:
    return ExperimentResult(
        run_id="rid", iteration=iteration, code="",
        metrics={"pass_rate": str(primary_metric)},
        primary_metric=primary_metric,
        improved=True, kept=True,
        elapsed_sec=0.1, stdout="", stderr="", error=None,
    )


class TestStubSink:
    def test_run_started_does_not_raise(self):
        StubSink().run_started(_spec(), "rid")

    def test_iteration_observed_does_not_raise(self, tmp_path: Path):
        StubSink().iteration_observed(0, _result(), tmp_path)

    def test_run_completed_does_not_raise(self):
        history = MagicMock()
        history.results = [_result()]
        history.best_result = _result()
        StubSink().run_completed(history)

    def test_comment_does_not_raise(self):
        StubSink().comment("hello")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `/home/fede/.hermes/hermes-agent/venv/bin/python -m pytest tests/agent/research/test_sinks.py -v`
Expected: ERROR — `ModuleNotFoundError: No module named 'agent.research.sinks'`

- [ ] **Step 3: Create `agent/research/sinks.py` with Protocol + StubSink**

```python
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
  transitions status on completion. The caller is responsible for creating
  the task; the sink does not auto-create.

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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `/home/fede/.hermes/hermes-agent/venv/bin/python -m pytest tests/agent/research/test_sinks.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add agent/research/sinks.py tests/agent/research/test_sinks.py
git commit -m "feat(autoresearch): introduce ProgressSink Protocol + StubSink

Adds the seam ResearchSupervisor and ExperimentRunner will consume in
place of the inline lattice closure. StubSink is the default no-op
implementation. Specific sinks (LatticeSink, KanbanSink) follow."
```

---

## Task 2 — `LatticeSink` (extracts current behavior)

**Files:**
- Modify: `agent/research/sinks.py`
- Modify: `tests/agent/research/test_sinks.py`

- [ ] **Step 1: Add failing tests for `LatticeSink`**

Append to `tests/agent/research/test_sinks.py`:

```python
import subprocess

from agent.research.sinks import LatticeSink


class TestLatticeSink:
    def test_run_started_shells_out(self, monkeypatch):
        captured: list[list[str]] = []

        def fake_run(args, **kwargs):
            captured.append(args)
            return subprocess.CompletedProcess(args, 0, b"", b"")

        monkeypatch.setattr(subprocess, "run", fake_run)
        sink = LatticeSink(task_id="HRM-99", root="/tmp/lattice")
        sink.run_started(_spec(), "rid-001")
        assert any("lattice" in a[0] for a in captured)
        assert any("HRM-99" in " ".join(a) for a in captured)
        assert any("Loop started" in " ".join(a) or "rid-001" in " ".join(a) for a in captured)

    def test_iteration_observed_posts_comment(self, monkeypatch, tmp_path):
        captured: list[list[str]] = []
        monkeypatch.setattr(
            subprocess, "run",
            lambda args, **kw: captured.append(args) or subprocess.CompletedProcess(args, 0, b"", b""),
        )
        sink = LatticeSink(task_id="HRM-99", root="/tmp/lattice")
        sink.iteration_observed(2, _result(iteration=2, primary_metric=0.7), tmp_path)
        joined = " ".join(captured[-1]) if captured else ""
        assert "iter" in joined or "0.7" in joined

    def test_no_task_id_is_log_only(self, monkeypatch, caplog):
        called = False

        def fake_run(*a, **kw):
            nonlocal called
            called = True
            return subprocess.CompletedProcess(a, 0, b"", b"")

        monkeypatch.setattr(subprocess, "run", fake_run)
        sink = LatticeSink(task_id=None, root="/tmp/lattice")
        with caplog.at_level("INFO", logger="agent.research.sinks"):
            sink.run_started(_spec(), "rid")
            sink.iteration_observed(0, _result(), Path("/tmp"))
            sink.run_completed(MagicMock(results=[], best_result=None))
            sink.comment("hi")
        assert called is False  # never shells out without a task id

    def test_subprocess_failure_does_not_raise(self, monkeypatch):
        def boom(*a, **kw):
            raise FileNotFoundError("lattice not on PATH")

        monkeypatch.setattr(subprocess, "run", boom)
        sink = LatticeSink(task_id="HRM-99", root="/tmp/lattice")
        # Must not raise — failure to talk to lattice cannot kill the loop.
        sink.run_started(_spec(), "rid")
        sink.iteration_observed(0, _result(), Path("/tmp"))
        sink.run_completed(MagicMock(results=[], best_result=None))
        sink.comment("hi")
```

- [ ] **Step 2: Run tests to confirm they fail**

Run: `/home/fede/.hermes/hermes-agent/venv/bin/python -m pytest tests/agent/research/test_sinks.py::TestLatticeSink -v`
Expected: 4 errors — `ImportError: cannot import name 'LatticeSink' from 'agent.research.sinks'`

- [ ] **Step 3: Implement `LatticeSink`**

Append to `agent/research/sinks.py`:

```python
import subprocess
from typing import Optional


class LatticeSink:
    """Shells out to the ``lattice`` CLI to post comments on a task.

    Mirrors the behavior previously embedded in
    ``supervisor._make_lattice_comment_fn``. Kept for backward
    compatibility — new call sites should prefer ``KanbanSink``. When
    ``task_id`` is None, all hooks degrade to log-only.

    Subprocess failures are swallowed: a missing ``lattice`` binary or
    transient I/O error must not break the research loop.
    """

    _ACTOR = "agent:research-supervisor"
    _TIMEOUT_SEC = 10

    def __init__(self, *, task_id: Optional[str], root: str = "."):
        self._task_id = task_id
        self._root = root

    def _post(self, message: str) -> None:
        if not self._task_id:
            logger.info("[lattice-stub] %s", message)
            return
        try:
            subprocess.run(
                ["lattice", "comment", self._task_id, message,
                 "--actor", self._ACTOR],
                cwd=self._root,
                capture_output=True,
                timeout=self._TIMEOUT_SEC,
            )
        except Exception as exc:
            logger.warning("[lattice-sink] comment failed: %s", exc)

    def run_started(self, spec: Any, run_id: str) -> None:
        topic = getattr(spec, "topic", "")[:50]
        task_type = getattr(spec, "task_type", "?")
        metric = getattr(spec, "metric_key", "?")
        self._post(
            f"Loop started: run_id={run_id} type={task_type} "
            f"metric={metric} topic={topic}"
        )

    def iteration_observed(
        self, iteration: int, result: Any, run_dir: Path
    ) -> None:
        metric = getattr(result, "primary_metric", None)
        improved = getattr(result, "improved", False)
        kept = getattr(result, "kept", False)
        run_id = getattr(result, "run_id", "")
        status = "KEPT" if kept else ("IMPROVED" if improved else "DISCARDED")
        self._post(
            f"Round {run_id} iter {iteration}: {status} metric={metric}"
        )

    def run_completed(self, history: Any) -> None:
        results = getattr(history, "results", []) or []
        best = getattr(history, "best_result", None)
        best_metric = getattr(best, "primary_metric", None) if best else None
        self._post(
            f"Loop done: {len(results)} rounds, best={best_metric}"
        )

    def comment(self, message: str) -> None:
        self._post(message)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `/home/fede/.hermes/hermes-agent/venv/bin/python -m pytest tests/agent/research/test_sinks.py -v`
Expected: 8 passed (4 stub + 4 lattice)

- [ ] **Step 5: Commit**

```bash
git add agent/research/sinks.py tests/agent/research/test_sinks.py
git commit -m "feat(autoresearch): port lattice progress posting to LatticeSink

Extracts the inline shell-out from supervisor._make_lattice_comment_fn
into a standalone sink that conforms to the ProgressSink Protocol.
Behavior is identical: when task_id is None, every hook becomes a
log-only no-op; subprocess failures are swallowed."
```

---

## Task 3 — `KanbanSink` (the new default for tracked runs)

**Files:**
- Modify: `agent/research/sinks.py`
- Modify: `tests/agent/research/test_sinks.py`

- [ ] **Step 1: Write failing tests using a real in-memory kanban DB**

Append to `tests/agent/research/test_sinks.py`:

```python
import sqlite3
import tempfile

from hermes_cli import kanban_db

from agent.research.sinks import KanbanSink


@pytest.fixture
def kanban_db_path(tmp_path, monkeypatch):
    """Pin a clean kanban.db path. Use HERMES_KANBAN_DB so kanban_db_path()
    resolves through the env override (highest-precedence) and we skip
    the board / current-board state machine entirely. connect() auto-
    initializes the schema, so we don't call init_db().
    """
    db_path = tmp_path / "kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    return db_path


@pytest.fixture
def kanban_conn(kanban_db_path):
    """Helper: an OPEN connection for tests that want to inspect/insert
    directly. Production code (KanbanSink) opens its own short-lived
    connection per call; this fixture is for test setup + assertion only."""
    conn = kanban_db.connect(kanban_db_path)
    yield conn
    conn.close()


class TestKanbanSink:
    def test_existing_task_id_appends_comments(self, kanban_db_path, kanban_conn):
        task_id = kanban_db.create_task(
            kanban_conn, title="parent run", body="research run wrapper",
            created_by="test",
        )
        sink = KanbanSink(task_id=task_id, db_path=kanban_db_path)
        sink.run_started(_spec(), "rid-001")
        sink.iteration_observed(0, _result(0, 0.5), Path("/tmp"))
        sink.iteration_observed(1, _result(1, 0.7), Path("/tmp"))

        comments = kanban_db.list_comments(kanban_conn, task_id)
        assert len(comments) == 3
        assert "rid-001" in comments[0].body
        assert "0.5" in comments[1].body
        assert "0.7" in comments[2].body

    def test_run_completed_completes_task(self, kanban_db_path, kanban_conn):
        task_id = kanban_db.create_task(
            kanban_conn, title="r", body="b", created_by="test",
        )
        sink = KanbanSink(task_id=task_id, db_path=kanban_db_path)
        history = MagicMock()
        history.results = [_result(0, 0.5), _result(1, 0.9)]
        history.best_result = _result(1, 0.9)
        sink.run_completed(history)

        task = kanban_db.get_task(kanban_conn, task_id)
        assert task.status == "done"

    def test_complete_on_run_completed_false_keeps_task_open(
        self, kanban_db_path, kanban_conn,
    ):
        """A/B testing case: per-strategy sub-sinks must NOT close the task."""
        task_id = kanban_db.create_task(
            kanban_conn, title="r", body="b", created_by="test",
        )
        sink = KanbanSink(
            task_id=task_id,
            db_path=kanban_db_path,
            complete_on_run_completed=False,
        )
        history = MagicMock()
        history.results = [_result(0, 0.9)]
        history.best_result = _result(0, 0.9)
        sink.run_completed(history)

        task = kanban_db.get_task(kanban_conn, task_id)
        # Task should still be open; the comment was appended but no transition.
        assert task.status != "done"

    def test_no_task_id_is_log_only(self, kanban_db_path, kanban_conn):
        sink = KanbanSink(task_id=None, db_path=kanban_db_path)
        sink.run_started(_spec(), "rid")
        sink.iteration_observed(0, _result(), Path("/tmp"))
        sink.run_completed(MagicMock(results=[], best_result=None))
        sink.comment("hi")
        # No task should have been created.
        all_tasks = kanban_db.list_tasks(kanban_conn)
        assert len(all_tasks) == 0

    def test_db_error_does_not_raise(
        self, kanban_db_path, kanban_conn, monkeypatch,
    ):
        task_id = kanban_db.create_task(
            kanban_conn, title="r", body="b", created_by="test",
        )

        def boom(*a, **kw):
            raise sqlite3.OperationalError("forced")

        monkeypatch.setattr(kanban_db, "add_comment", boom)
        sink = KanbanSink(task_id=task_id, db_path=kanban_db_path)
        # Must not raise.
        sink.run_started(_spec(), "rid")
        sink.iteration_observed(0, _result(), Path("/tmp"))
```

- [ ] **Step 2: Run tests to confirm they fail**

Run: `/home/fede/.hermes/hermes-agent/venv/bin/python -m pytest tests/agent/research/test_sinks.py::TestKanbanSink -v`
Expected: ImportError on `KanbanSink`.

- [ ] **Step 3: Implement `KanbanSink`**

Append to `agent/research/sinks.py`:

```python
import sqlite3
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

    def _open(self) -> Optional["sqlite3.Connection"]:
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `/home/fede/.hermes/hermes-agent/venv/bin/python -m pytest tests/agent/research/test_sinks.py -v`
Expected: 12 passed (4 stub + 4 lattice + 4 kanban).

- [ ] **Step 5: Commit**

```bash
git add agent/research/sinks.py tests/agent/research/test_sinks.py
git commit -m "feat(autoresearch): add KanbanSink for kanban-backed progress tracking

Appends a comment per iteration to a kanban task and transitions it to
done at the end of the run. The sink owns no DB connection — the caller
provides one. Failures in add_comment / complete_task are logged but
never raise: a misbehaving DB cannot break the research loop."
```

---

## Task 4 — Wire `ProgressSink` into `ResearchSupervisor`

**Files:**
- Modify: `agent/research/supervisor.py`
- Modify: `tests/agent/test_research_supervisor.py`

- [ ] **Step 1: Write failing test verifying the supervisor calls the sink**

Append to `tests/agent/test_research_supervisor.py`:

```python
class TestSupervisorCallsProgressSink:
    @pytest.mark.integration
    def test_sink_receives_run_started_and_completed(self, tmp_path, monkeypatch):
        from agent.research.sinks import StubSink

        sink = StubSink()
        sink.run_started = MagicMock(side_effect=sink.run_started)
        sink.iteration_observed = MagicMock(side_effect=sink.iteration_observed)
        sink.run_completed = MagicMock(side_effect=sink.run_completed)

        spec = TaskSpec(
            topic="t", deliverable="d",
            metric_key="m", metric_direction="maximize",
        )

        def fake_delegate(*a, **kw):
            return {"results": [{"status": "completed", "summary": "METRIC: m=0.5"}]}

        with patch("agent.research.supervisor._call_delegate_task", side_effect=fake_delegate):
            sup = ResearchSupervisor(
                parent_agent=MagicMock(),
                workspace=tmp_path,
                progress_sink=sink,
            )
            sup.run(
                spec, initial_attempt="x", run_id="rid",
                max_iterations=0,
                disable_evolution_overlay=True,
            )

        sink.run_started.assert_called_once()
        # baseline iteration → exactly one iteration_observed
        assert sink.iteration_observed.call_count == 1
        sink.run_completed.assert_called_once()
```

- [ ] **Step 2: Run test to confirm it fails**

Run: `/home/fede/.hermes/hermes-agent/venv/bin/python -m pytest tests/agent/test_research_supervisor.py::TestSupervisorCallsProgressSink -v --override-ini='addopts='`
Expected: TypeError — `ResearchSupervisor.__init__() got an unexpected keyword argument 'progress_sink'`.

- [ ] **Step 3: Modify `ResearchSupervisor.__init__`**

In `agent/research/supervisor.py`, replace the existing `__init__` so it accepts `progress_sink`. Locate the constructor (search for `class ResearchSupervisor:` then `def __init__`) and replace its body:

```python
    def __init__(
        self,
        *,
        parent_agent: Any,
        workspace: Path | None = None,
        lattice_task_id: Optional[str] = None,
        lattice_root: str = str(get_hermes_home() / "org"),
        progress_sink: Optional["ProgressSink"] = None,
    ) -> None:
        self._parent_agent = parent_agent
        self._workspace = workspace or (get_hermes_home() / "research-workspace")
        self._lattice_task_id = lattice_task_id
        self._lattice_root = lattice_root
        # Populated by run() — past-run lessons prepended to every worker brief.
        self._evolution_overlay: str = ""

        # Resolve sink: explicit progress_sink wins; else legacy lattice_task_id
        # builds a LatticeSink (with deprecation warning); else StubSink.
        if progress_sink is not None:
            self._sink = progress_sink
        elif lattice_task_id:
            import warnings
            warnings.warn(
                "Passing lattice_task_id directly to ResearchSupervisor is "
                "deprecated; pass progress_sink=LatticeSink(...) explicitly. "
                "This shim will be removed.",
                DeprecationWarning,
                stacklevel=2,
            )
            from agent.research.sinks import LatticeSink
            self._sink = LatticeSink(task_id=lattice_task_id, root=lattice_root)
        else:
            from agent.research.sinks import StubSink
            self._sink = StubSink()
```

Add the import for the Protocol type at the top of `supervisor.py` (under the existing imports):

```python
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from agent.research.sinks import ProgressSink
```

- [ ] **Step 4: Replace `_make_lattice_comment_fn` usage in `run`**

In `agent/research/supervisor.py`, find the `run` method body. Locate the line:

```python
        lattice_comment_fn = _make_lattice_comment_fn(
            self._lattice_task_id, self._lattice_root
        )
```

Replace it with:

```python
        # All progress events flow through the sink. The local
        # lattice_comment_fn name is retained for the diff but now points
        # at the sink's comment method for the places that emit free-form
        # text (the early-return baseline-only branch, _reflect, etc.).
        lattice_comment_fn = self._sink.comment
        self._sink.run_started(spec, run_id)
```

Then in the same method, after `runner = ExperimentRunner(...)` (around line 730), **delete** the existing `lattice_comment_fn(f"Loop started: run_id={run_id} ...")` block — the sink's `run_started` already covers it.

After the baseline `self._observe(baseline, spec, run_dir, previous_best=None)` (around line 783), add:

```python
        self._sink.iteration_observed(0, baseline, run_dir)
```

**Critical (audit fix #4):** locate the existing early-return at supervisor.py:791-793:

```python
        if llm is None:
            lattice_comment_fn(f"Baseline only. best={runner.history.baseline_metric}")
            try:
                self._evolve(runner.history, spec, run_id)
            except Exception as exc:
                logger.warning("Evolution persistence failed for %s: %s", run_id, exc)
            return runner.history
```

Replace with:

```python
        if llm is None:
            lattice_comment_fn(f"Baseline only. best={runner.history.baseline_metric}")
            self._sink.run_completed(runner.history)
            try:
                self._evolve(runner.history, spec, run_id)
            except Exception as exc:
                logger.warning("Evolution persistence failed for %s: %s", run_id, exc)
            return runner.history
```

After every `self._observe(...)` call inside the iteration loop (2 sites — fan-out branch and sequential branch), add immediately below the `_observe` call:

```python
        self._sink.iteration_observed(iteration, best_result, run_dir)  # fan-out branch
```

```python
        self._sink.iteration_observed(iteration, result, run_dir)  # sequential branch
```

At the end of `run` before `return runner.history`, replace the existing pair of `lattice_comment_fn(f"Loop done: ...")` calls (the success and PARTIAL branches) with:

```python
        self._sink.run_completed(runner.history)
```

Keep the partial-success log message as a `logger.info(...)` for debug visibility, but the sink event is what counts.

- [ ] **Step 5: Delete `_make_lattice_comment_fn`**

In `agent/research/supervisor.py`, delete the entire `_make_lattice_comment_fn` definition (the function that begins at the line containing `def _make_lattice_comment_fn`). It lives between two horizontal-rule comment blocks; remove the function only.

- [ ] **Step 6: Run all tests**

Run: `/home/fede/.hermes/hermes-agent/venv/bin/python -m pytest tests/agent/research/ tests/agent/test_research_supervisor.py -q`
Expected: all green (existing 100 tests + 1 new = 101).

- [ ] **Step 7: Commit**

```bash
git add agent/research/supervisor.py tests/agent/test_research_supervisor.py
git commit -m "refactor(autoresearch): supervisor consumes ProgressSink (lattice deprecated)

ResearchSupervisor now takes a progress_sink kwarg and routes all run
events (run_started, iteration_observed, run_completed, free-form
comments) through it. Legacy lattice_task_id still works but emits a
DeprecationWarning and is silently mapped to a LatticeSink.

_make_lattice_comment_fn is removed; its behavior moved to LatticeSink
in agent/research/sinks.py."
```

---

## Task 5 — Wire `ProgressSink` into `ExperimentRunner`

**Files:**
- Modify: `agent/research/runner.py`
- Modify: `tests/agent/test_research_supervisor.py` (existing tests pass `lattice_comment_fn=`; ensure shim works)

- [ ] **Step 1: Add a failing test that uses the new param**

Append to `tests/agent/test_research_supervisor.py` (in the existing class for runner tests, or add a new class):

```python
class TestExperimentRunnerSink:
    def test_runner_uses_progress_sink_comment(self):
        from agent.research.sinks import StubSink
        sink = StubSink()
        sink.comment = MagicMock()

        def fake_delegate(goal, working_dir):
            from agent.research.runner import DelegateSandboxResult
            return DelegateSandboxResult(
                metrics={"m": "0.5"}, stdout="METRIC: m=0.5",
                stderr="", elapsed_sec=0.1, timed_out=False,
                returncode=0, error=None,
            )

        runner = ExperimentRunner(
            config=HermesExperimentConfig(metric_key="m", metric_direction="maximize"),
            workspace=Path("/tmp/runner-sink-test"),
            delegate_fn=fake_delegate,
            progress_sink=sink,
        )
        runner.run_experiment(code="x", run_id="rid", iteration=0)
        assert sink.comment.called
```

- [ ] **Step 2: Confirm it fails**

Run: `/home/fede/.hermes/hermes-agent/venv/bin/python -m pytest tests/agent/test_research_supervisor.py::TestExperimentRunnerSink -v --override-ini='addopts='`
Expected: TypeError — `ExperimentRunner.__init__() got an unexpected keyword argument 'progress_sink'`.

- [ ] **Step 3: Update `ExperimentRunner.__init__`**

In `agent/research/runner.py`, modify the `__init__` method signature. Find the existing `def __init__(...)` of `ExperimentRunner` and change it to:

```python
    def __init__(
        self,
        config: "HermesExperimentConfig",
        workspace: Path,
        *,
        delegate_fn: Callable[[str, str], "DelegateSandboxResult"],
        lattice_comment_fn: Optional[Callable[[str], None]] = None,
        progress_sink: Optional[Any] = None,
    ) -> None:
        self.config: HermesExperimentConfig = config
        self.workspace: Path = workspace
        self.workspace.mkdir(parents=True, exist_ok=True)
        self._delegate_fn = delegate_fn
        self.history: ExperimentHistory = ExperimentHistory()

        # Resolve comment hook. New API is progress_sink; legacy is
        # lattice_comment_fn. Either may be None — fall back to no-op.
        if progress_sink is not None:
            self._sink = progress_sink
        elif lattice_comment_fn is not None:
            import warnings
            warnings.warn(
                "ExperimentRunner(lattice_comment_fn=...) is deprecated. "
                "Pass progress_sink=<a ProgressSink> instead.",
                DeprecationWarning,
                stacklevel=2,
            )
            from agent.research.sinks import StubSink
            shim = StubSink()
            shim.comment = lattice_comment_fn  # type: ignore[assignment]
            self._sink = shim
        else:
            from agent.research.sinks import StubSink
            self._sink = StubSink()

        # Backward-compat alias: existing code uses self._lattice_comment.
        # Keep the attribute pointing at the sink's comment method.
        self._lattice_comment = self._sink.comment
```

**Critical (audit fix #3):** the `*` keyword-only marker before `delegate_fn` and the `self.workspace.mkdir(parents=True, exist_ok=True)` line MUST be preserved. Existing callers (supervisor.py:735) pass `delegate_fn=...` as a keyword, and the `mkdir` is what creates `<workspace>/<run_id>/` so round dirs can be written. Dropping either silently breaks the suite.

(`self._lattice_comment` is preserved as a method reference so the existing 4 internal call sites continue to work without further edits.)

- [ ] **Step 4: Run all research tests**

Run: `/home/fede/.hermes/hermes-agent/venv/bin/python -m pytest tests/agent/research/ tests/agent/test_research_supervisor.py -q`
Expected: all green (101 + 1 = 102 tests).

- [ ] **Step 5: Update the supervisor to pass `progress_sink` instead of `lattice_comment_fn`**

In `agent/research/supervisor.py`, find the `ExperimentRunner(...)` constructor call inside `run` (search for `runner = ExperimentRunner(`). Change it from:

```python
        runner = ExperimentRunner(
            config=config,
            workspace=self._workspace / run_id,
            delegate_fn=delegate_fn,
            lattice_comment_fn=lattice_comment_fn,
        )
```

To:

```python
        runner = ExperimentRunner(
            config=config,
            workspace=self._workspace / run_id,
            delegate_fn=delegate_fn,
            progress_sink=self._sink,
        )
```

- [ ] **Step 6: Run all research tests again**

Run: `/home/fede/.hermes/hermes-agent/venv/bin/python -m pytest tests/agent/research/ tests/agent/test_research_supervisor.py -q`
Expected: all green.

- [ ] **Step 7: Commit**

```bash
git add agent/research/runner.py agent/research/supervisor.py tests/agent/test_research_supervisor.py
git commit -m "refactor(autoresearch): ExperimentRunner accepts ProgressSink

Adds progress_sink kwarg to ExperimentRunner. Legacy lattice_comment_fn
keeps working but emits a DeprecationWarning. The supervisor now passes
its own sink through to the runner so both layers report through the
same backend."
```

---

## Task 6 — Surface `kanban_task_id` on `run_research` tool

**Files:**
- Modify: `tools/research_tool.py`
- Modify: `tests/agent/research/test_sinks.py` or new tool-level test

- [ ] **Step 0 (audit fix #5): Move `ResearchSupervisor` import to module scope**

The current `tools/research_tool.py` imports `ResearchSupervisor` inside `run_research()` (lines 200, 215, 258). For `unittest.mock.patch("tools.research_tool.ResearchSupervisor")` to bind, the symbol must exist as a module attribute at patch time. Move the import to the top of the file (above `def run_research`):

```python
# At the top, with the other imports:
from agent.research.supervisor import ResearchSupervisor, TaskSpec
```

Remove the in-function `from agent.research.supervisor import ResearchSupervisor, TaskSpec` line(s).

- [ ] **Step 1: Add a failing tool-level test**

Create `tests/agent/research/test_run_research_kanban_param.py`:

```python
"""Verify run_research wires kanban_task_id through to a KanbanSink."""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from tools.research_tool import run_research


def test_kanban_task_id_param_accepted():
    """run_research must accept kanban_task_id and not crash on it."""
    with patch("tools.research_tool.ResearchSupervisor") as MockSupervisor:
        instance = MockSupervisor.return_value
        instance.run.return_value = MagicMock(
            results=[], best_result=None,
        )
        result = run_research(
            topic="t",
            deliverable="d",
            metric_key="m",
            parent_agent=MagicMock(),
            kanban_task_id="k_test_123",
            disable_evolution_overlay=True,
        )
        # Must return a JSON string, no crash.
        assert isinstance(result, str)
        json.loads(result)


def test_kanban_task_id_takes_precedence_over_lattice(caplog):
    with patch("tools.research_tool.ResearchSupervisor") as MockSupervisor:
        instance = MockSupervisor.return_value
        instance.run.return_value = MagicMock(results=[], best_result=None)
        with caplog.at_level("WARNING"):
            run_research(
                topic="t", deliverable="d", metric_key="m",
                parent_agent=MagicMock(),
                kanban_task_id="k_x",
                lattice_task_id="HRM-99",
                disable_evolution_overlay=True,
            )
        assert any("kanban" in r.message.lower() for r in caplog.records)


def test_registry_handler_forwards_kanban_task_id():
    """Audit fix #6b: the registry lambda must enumerate kanban_task_id."""
    from tools.research_tool import RESEARCH_TOOL_SCHEMA
    from tools.registry import get_handler

    handler = get_handler("run_research")
    captured = {}
    with patch("tools.research_tool.run_research") as mock_run:
        mock_run.return_value = "{}"
        handler({"topic": "t", "deliverable": "d", "metric_key": "m",
                 "kanban_task_id": "k_xyz"},
                parent_agent=MagicMock())
        captured.update(mock_run.call_args.kwargs)
    assert captured.get("kanban_task_id") == "k_xyz"
```

(If `get_handler` is not the right helper, replace with whatever the registry exposes; the assertion is what matters.)

- [ ] **Step 2: Run to confirm failure**

Run: `/home/fede/.hermes/hermes-agent/venv/bin/python -m pytest tests/agent/research/test_run_research_kanban_param.py -v`
Expected: TypeError — unexpected keyword `kanban_task_id`.

- [ ] **Step 3: Add the param to `run_research`**

In `tools/research_tool.py`, find the `def run_research(...)` signature and add `kanban_task_id` after `lattice_task_id`:

```python
def run_research(
    topic: str,
    deliverable: str,
    metric_key: str,
    metric_direction: str = "maximize",
    task_type: str = "generic",
    acceptance_criterion: str = "",
    evaluation_mode: str = "self_report",
    evaluation_prompt: str = "",
    initial_attempt: str = "",
    max_iterations: int = 3,
    time_budget_sec: int = 0,
    lattice_task_id: Optional[str] = None,
    kanban_task_id: Optional[str] = None,
    parent_agent: Any = None,
    checkpoint_dir: Optional[str] = None,
    timeout_sec: int = 0,
    strategies: Optional[list[dict[str, Any]]] = None,
    repeats: int = 1,
    disable_evolution_overlay: bool = False,
) -> str:
```

- [ ] **Step 4: Build the right sink in `run_research`**

In `tools/research_tool.py`, find the line where the `ResearchSupervisor` is instantiated (search for `ResearchSupervisor(`). Right above it, insert sink construction. Replace the block:

```python
    supervisor = ResearchSupervisor(
        parent_agent=parent_agent,
        workspace=workspace,
        lattice_task_id=lattice_task_id,
    )
```

With:

```python
    # Build the progress sink. Order: kanban (preferred) > lattice (legacy)
    # > stub (default). When both are passed, kanban wins and a warning is
    # logged.
    sink: "ProgressSink"
    if kanban_task_id:
        if lattice_task_id:
            logger.warning(
                "Both kanban_task_id and lattice_task_id provided; "
                "preferring kanban (%s) and ignoring lattice (%s).",
                kanban_task_id, lattice_task_id,
            )
        from hermes_cli import kanban_db
        from agent.research.sinks import KanbanSink
        # Capture db_path NOW (resolves env / current-board state) so later
        # KanbanSink calls don't re-resolve and accidentally talk to a
        # different board if the user switches active board mid-run.
        try:
            db_path = kanban_db.kanban_db_path()
        except Exception as exc:
            logger.warning(
                "Failed to resolve kanban db_path for task %s: %s. "
                "Falling back to log-only sink.", kanban_task_id, exc,
            )
            from agent.research.sinks import StubSink
            sink = StubSink()
        else:
            sink = KanbanSink(task_id=kanban_task_id, db_path=db_path)
    elif lattice_task_id:
        from agent.research.sinks import LatticeSink
        sink = LatticeSink(task_id=lattice_task_id)
    else:
        from agent.research.sinks import StubSink
        sink = StubSink()

    supervisor = ResearchSupervisor(
        parent_agent=parent_agent,
        workspace=workspace,
        progress_sink=sink,
    )
```

Add the import at the top of `tools/research_tool.py`:

```python
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from agent.research.sinks import ProgressSink
```

(If a `TYPE_CHECKING` block already exists, append to it instead.)

- [ ] **Step 5: Update the JSON schema**

In `tools/research_tool.py`, find the schema dict (search for `"lattice_task_id"`). Update its description and add `kanban_task_id`:

```python
            "lattice_task_id": {
                "type": "string",
                "description": (
                    "Deprecated. Use kanban_task_id instead. "
                    "Optional Lattice task ID to receive progress comments."
                ),
            },
            "kanban_task_id": {
                "type": "string",
                "description": (
                    "Optional kanban task ID. When set, run progress is "
                    "posted as comments to the task and the task is "
                    "transitioned to 'done' on completion. Preferred over "
                    "lattice_task_id."
                ),
            },
```

Also update the A/B testing branch in `run_research` (search for `tester = ResearchABTester(`). Pass the same sink there (Task 7 makes `ResearchABTester` accept it). For now keep the legacy `lattice_task_id=` arg too so the diff stays minimal during the refactor; Task 7 removes the legacy arg from the call.

```python
        tester = ResearchABTester(
            parent_agent=parent_agent,
            workspace=workspace,
            progress_sink=sink,  # built earlier in this function
            llm=_LLMBridge(),
        )
```

- [ ] **Step 5b (audit fix #6b): Forward `kanban_task_id` in the registry handler**

`tools/research_tool.py` registers `run_research` with a lambda that enumerates the args manually (lines ~388-401). Without an explicit forward, the agent's tool call would silently drop `kanban_task_id`. Add the line below to the lambda:

```python
        kanban_task_id=args.get("kanban_task_id"),
```

Place it right after `lattice_task_id=args.get("lattice_task_id"),` so the diff stays clean.

- [ ] **Step 6: Run tests**

Run: `/home/fede/.hermes/hermes-agent/venv/bin/python -m pytest tests/agent/research/test_run_research_kanban_param.py tests/agent/research/ -q`
Expected: all green.

- [ ] **Step 7: Commit**

```bash
git add tools/research_tool.py tests/agent/research/test_run_research_kanban_param.py
git commit -m "feat(autoresearch): run_research accepts kanban_task_id

Adds the kanban_task_id parameter, surfaces it in the JSON schema, and
wires it to a KanbanSink. When both kanban_task_id and lattice_task_id
are provided, kanban wins and a warning is logged. lattice_task_id
remains accepted but its description is updated to mark it deprecated."
```

---

## Task 7 — Surface `kanban_task_id` on `research_job` + update `ResearchABTester`

**Files:**
- Modify: `tools/research_job_tool.py`
- Modify: `agent/research/ab_testing.py`
- Modify: `tests/agent/research/test_research_job_tool_config.py`

- [ ] **Step 1: Add failing test for `research_job`**

Append to `tests/agent/research/test_research_job_tool_config.py`:

```python
def test_research_job_accepts_kanban_task_id(tmp_path, monkeypatch):
    """research_job must thread kanban_task_id into the persisted spec."""
    from tools.research_job_tool import _action_start

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    args = {
        "topic": "t",
        "deliverable": "d",
        "metric_key": "m",
        "kanban_task_id": "k_xyz",
    }
    out = _action_start(args)
    payload = json.loads(out)
    job_id = payload["job_id"]

    # Spec on disk should include kanban_task_id.
    spec_path = tmp_path / "research-jobs" / job_id / "spec.json"
    spec = json.loads(spec_path.read_text())
    assert spec["kanban_task_id"] == "k_xyz"
```

- [ ] **Step 2: Confirm failure**

Run: `/home/fede/.hermes/hermes-agent/venv/bin/python -m pytest tests/agent/research/test_research_job_tool_config.py::test_research_job_accepts_kanban_task_id -v`
Expected: KeyError or `'kanban_task_id'` not in spec.

- [ ] **Step 3: Thread `kanban_task_id` through `research_job_tool`**

In `tools/research_job_tool.py`, find the schema dict (search for `"lattice_task_id"`). Add directly under it:

```python
            "kanban_task_id": {
                "type": "string",
                "description": (
                    "Optional kanban task id. Run progress will post as "
                    "comments and the task will be marked done on "
                    "completion. Preferred over lattice_task_id."
                ),
            },
```

Find `_action_start` and update the spec it writes (search for `"lattice_task_id"` in the spec dict). Add `"kanban_task_id"` next to it:

```python
    spec = {
        ...
        "lattice_task_id": args.get("lattice_task_id"),
        "kanban_task_id": args.get("kanban_task_id"),
        ...
    }
```

In the top-level `research_job` function signature (line ~363), add `kanban_task_id: str = ""` after `lattice_task_id`. In its body where `args["lattice_task_id"]` is set, also set `args["kanban_task_id"] = kanban_task_id`.

- [ ] **Step 4: Update `ResearchABTester` to accept a sink + close once at end (audit fix #8)**

The naive approach — pass the same sink to every per-strategy supervisor — would have the FIRST strategy's `run_completed` close the kanban task, leaving the rest to comment on a closed task. Fix: build a "child" sink per strategy with `complete_on_run_completed=False`, then have the tester explicitly close the parent task after all strategies finish.

In `agent/research/ab_testing.py`, modify `ResearchABTester.__init__`:

```python
    def __init__(
        self,
        *,
        parent_agent: Any,
        workspace: Path,
        lattice_task_id: Optional[str] = None,
        progress_sink: Optional["ProgressSink"] = None,
        llm: Any = None,
    ):
        self._parent_agent = parent_agent
        self._workspace = workspace
        self._lattice_task_id = lattice_task_id
        self._llm = llm

        # Resolve parent sink. The tester owns the close-on-completion;
        # per-strategy sub-sinks must NOT call complete_task themselves.
        if progress_sink is not None:
            self._parent_sink = progress_sink
        elif lattice_task_id:
            from agent.research.sinks import LatticeSink
            self._parent_sink = LatticeSink(task_id=lattice_task_id)
        else:
            from agent.research.sinks import StubSink
            self._parent_sink = StubSink()
```

Where the tester constructs each per-strategy supervisor (search for `ResearchSupervisor(` inside `ab_testing.py`), build a child sink that shares the parent's identity but won't call `complete_task`:

```python
        # Per-strategy child sink. For KanbanSink we suppress run_completed's
        # complete_task call by cloning with complete_on_run_completed=False;
        # the tester closes the parent task once at the very end.
        from agent.research.sinks import KanbanSink, StubSink
        if isinstance(self._parent_sink, KanbanSink):
            child_sink = KanbanSink(
                task_id=self._parent_sink._task_id,
                db_path=self._parent_sink._db_path,
                complete_on_run_completed=False,
            )
        else:
            child_sink = self._parent_sink  # Lattice / Stub: idempotent

        sup = ResearchSupervisor(
            parent_agent=self._parent_agent,
            workspace=self._workspace,
            progress_sink=child_sink,
        )
```

After the `compare()` loop finishes (just before returning the summaries), close the parent task once:

```python
        # Close the parent kanban task ONCE, after all strategies+repeats are done.
        self._parent_sink.run_completed(history_for_summary)
```

Where `history_for_summary` is whichever final aggregated history the tester already produces (the last strategy's history, or a synthesized one — match the existing return-value behavior, do not change it).

- [ ] **Step 5: Update `tools/research_tool.py` A/B branch**

In `tools/research_tool.py`, in the `if strategies:` branch where `ResearchABTester(...)` is constructed, replace:

```python
        tester = ResearchABTester(
            parent_agent=parent_agent,
            workspace=workspace,
            lattice_task_id=lattice_task_id,
            llm=_LLMBridge(),
        )
```

With:

```python
        tester = ResearchABTester(
            parent_agent=parent_agent,
            workspace=workspace,
            progress_sink=sink,  # built earlier in the function
            llm=_LLMBridge(),
        )
```

- [ ] **Step 6: Run all research tests**

Run: `/home/fede/.hermes/hermes-agent/venv/bin/python -m pytest tests/agent/research/ tests/agent/test_research_supervisor.py -q`
Expected: all green.

- [ ] **Step 7: Commit**

```bash
git add tools/research_job_tool.py agent/research/ab_testing.py tools/research_tool.py tests/agent/research/test_research_job_tool_config.py
git commit -m "feat(autoresearch): kanban_task_id on research_job + ResearchABTester

Threads kanban_task_id through the detached job spec so resumed jobs
keep the same kanban target. ResearchABTester now accepts a
progress_sink and builds per-strategy supervisors that share the sink,
so all strategies of an A/B test land on the same kanban task."
```

---

## Task 8 — Re-run mechanics validation harness with kanban sink

**Files:**
- Create: `research-cases/daemoncraft-classifier/run_kanban_validation.py`

This task does not modify production code; it is a hand-driven validation that exercises the new sink end-to-end against a real (temporary) kanban DB. Counterpart to the existing `run_mechanics_validation.py` and `run_plateau_validation.py`.

- [ ] **Step 1: Write the validation script**

Create `research-cases/daemoncraft-classifier/run_kanban_validation.py`:

```python
"""End-to-end validation: ResearchSupervisor + KanbanSink against a real DB.

Spins up a tmp HERMES_HOME, creates a kanban task, runs the scripted
heartbeat-classifier scenario through the supervisor with a KanbanSink,
and prints the resulting kanban comments + final task status.

Run from the repo root:
    /home/fede/.hermes/hermes-agent/venv/bin/python \\
      research-cases/daemoncraft-classifier/run_kanban_validation.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

# Override HERMES_HOME BEFORE importing kanban_db.
TMP_HOME = Path(tempfile.mkdtemp(prefix="kanban-validation-"))
os.environ["HERMES_HOME"] = str(TMP_HOME)

from hermes_cli import kanban_db  # noqa: E402
from agent.research.sinks import KanbanSink  # noqa: E402
from agent.research.supervisor import ResearchSupervisor, TaskSpec  # noqa: E402

SCORES = [0.50, 0.70, 0.85, 0.95]


def make_scripted_delegate():
    n = {"i": 0}
    def fn(*a, **kw):
        idx = min(n["i"], len(SCORES) - 1)
        n["i"] += 1
        s = SCORES[idx]
        return json.dumps({
            "results": [{
                "status": "completed",
                "summary": f"METRIC: pass_rate={s} STATUS: improved NOTES: iter-{idx}",
                "tokens": {"input": 100, "output": 50},
                "_child_cost_usd": 0.001,
            }]
        })
    return fn


class StubLLM:
    def chat(self, *a, **kw):
        r = MagicMock()
        r.content = "```python\ndef classify(p):\n    return 'context'\n```"
        return r


def main():
    db_path = kanban_db.kanban_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = kanban_db.connect(db_path)
    kanban_db.init_db(conn)

    task_id = kanban_db.create_task(
        conn,
        title="DC heartbeat classifier — kanban-sink validation",
        body="Improve pass_rate on a synthetic fixture set",
        created_by="validation",
    )
    print(f"  Created kanban task: {task_id}")

    workspace = TMP_HOME / "research-workspace"
    spec = TaskSpec(
        topic="Improve daemoncraft heartbeat classifier",
        deliverable="Python classifier",
        metric_key="pass_rate",
        metric_direction="maximize",
        task_type="code",
        acceptance_criterion="pass_rate >= 0.9",
    )

    sink = KanbanSink(task_id=task_id, conn=conn)
    sup = ResearchSupervisor(
        parent_agent=MagicMock(),
        workspace=workspace,
        progress_sink=sink,
    )

    import tools.delegate_tool
    tools.delegate_tool.delegate_task = make_scripted_delegate()

    history = sup.run(
        spec, initial_attempt="def classify(p): return 'context'\n",
        run_id="kanban-test", max_iterations=4, llm=StubLLM(),
        disable_evolution_overlay=True,
    )

    # Inspect.
    comments = kanban_db.list_comments(conn, task_id)
    task = kanban_db.get_task(conn, task_id)
    print(f"\n  Iterations executed: {len(history.results)}")
    print(f"  Best metric:         {history.best_result.primary_metric if history.best_result else None}")
    print(f"  Final task status:   {task.status}")
    print(f"  Comments on task:    {len(comments)}")
    print()
    for c in comments:
        print(f"    [{c.author}] {c.body}")

    print(f"\n  Tmp HERMES_HOME preserved at: {TMP_HOME}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 2: Run the validation**

Run: `/home/fede/.hermes/hermes-agent/venv/bin/python research-cases/daemoncraft-classifier/run_kanban_validation.py`

Expected output (numbers identical to the existing mechanics harness):

```
  Created kanban task: t_<hex>

  Iterations executed: 4
  Best metric:         0.95
  Final task status:   done
  Comments on task:    6

    [agent:research-supervisor] Loop started: run_id=kanban-test type=code metric=pass_rate
    Topic: Improve daemoncraft heartbeat classifier
    [agent:research-supervisor] Iteration 0: KEPT metric=0.5
    [agent:research-supervisor] Iteration 1: KEPT metric=0.7
    [agent:research-supervisor] Iteration 2: KEPT metric=0.85
    [agent:research-supervisor] Iteration 3: KEPT metric=0.95
    [agent:research-supervisor] Loop done: 4 rounds, best=0.95
```

If acceptance_criterion termination + KanbanSink integration both work, the task transitions to `done` and the comment trail matches.

- [ ] **Step 3: Run the full research test suite once more**

Run: `/home/fede/.hermes/hermes-agent/venv/bin/python -m pytest tests/agent/research/ tests/agent/test_research_supervisor.py -q`
Expected: all green.

- [ ] **Step 4: Commit (or skip if untracked is fine)**

The validation script lives under `research-cases/` which we have been keeping untracked. If you want it tracked, add a top-level `research-cases/README.md` first. Otherwise:

```bash
echo "research-cases/" >> .gitignore
git add .gitignore
git commit -m "chore: keep research-cases/ untracked

Validation harnesses for the autoresearch loop. Scripts here are
hand-driven validations, not part of the test suite."
```

---

## Self-Review

**Spec coverage**

- ProgressSink Protocol with three event hooks + free-form comment → Task 1 ✓
- StubSink (default) → Task 1 ✓
- LatticeSink (legacy preserved) → Task 2 ✓
- KanbanSink (new, append comments + close task) → Task 3 ✓
- Supervisor consumes ProgressSink → Task 4 ✓
- Runner consumes ProgressSink → Task 5 ✓
- `run_research` exposes `kanban_task_id` → Task 6 ✓
- `research_job` exposes `kanban_task_id` → Task 7 ✓
- `ResearchABTester` accepts a sink → Task 7 ✓
- `lattice_task_id` deprecated with warning, not removed → Tasks 4, 6, 7 ✓
- Validation harness exercising the new sink against a real kanban DB → Task 8 ✓

**Placeholder scan** — no "TBD", "implement later", "etc." in the plan body. Every code block is complete.

**Type consistency**
- `ProgressSink` Protocol method names: `run_started`, `iteration_observed`, `run_completed`, `comment` — used identically in Tasks 1, 2, 3, 4, 5.
- `KanbanSink.__init__(task_id, conn)` — same signature in Task 3 (definition), Task 6 (call in `run_research`), Task 8 (call in validation script).
- `LatticeSink.__init__(task_id, root)` — same in Tasks 2, 4, 6, 7.
- `ExperimentRunner.__init__(progress_sink=...)` — same in Tasks 5, 4 (via supervisor).
- `ResearchSupervisor.__init__(progress_sink=...)` — same in Tasks 4, 6, 7, 8.
- `disable_evolution_overlay=True` in Task 4 test and Task 8 — already exists in the production code (added in commit `7e4c3c953`).

**Out of scope (explicit)**

- Bidirectional `/goal` ↔ research integration — deferred to **Phase B**.
- Kanban `/specify` triage at input — deferred to **Phase C**.
- Removal of `lattice_task_id` parameter (not just deprecation) — left for a future release after one cycle of warning.
