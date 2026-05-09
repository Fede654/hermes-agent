# Phases B + C: `/goal` ↔ research bidirectional + kanban-driven `/specify` triage

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make `/goal` metric-aware so a goal with a parseable acceptance criterion is judged deterministically (no LLM judge). Add a `/specify` triage path so `run_research` can flesh out a vague `topic` via the kanban auxiliary LLM before running.

**Architecture:**

Phase B extends `GoalState` (in `hermes_cli/goals.py`) with three optional fields — `metric_key`, `acceptance_criterion`, `last_metric_value` — and rewires `judge_goal` so when those fields are populated, the verdict is computed by `_parse_acceptance_criterion` instead of the auxiliary LLM. The agent's response is scanned for a `METRIC: <key>=<value>` line (same regex the research supervisor uses) to update `last_metric_value`. Phase C adds a `auto_specify: bool` parameter to `run_research` that calls the kanban triage specifier (`auxiliary.triage_specifier` model) to fill in a missing `deliverable`/`metric_key`/`evaluation_prompt` from a short topic.

**Tech Stack:** Python 3.11, existing `hermes_cli/goals.py` GoalManager / GoalState, existing kanban `triage_specifier` auxiliary client path, `agent.research.supervisor._parse_acceptance_criterion` (already exposed), `agent.research.metrics._FLOAT_RE` (existing METRIC parser).

---

## Phase B — `/goal` becomes metric-aware

### Task B1 — Extend `GoalState` with metric fields

**Files:**
- Modify: `hermes_cli/goals.py` — `GoalState` dataclass + `to_json` / `from_json`
- Modify: `tests/agent/test_goals.py` (or create if missing) — round-trip serialization

**Steps:**

- [ ] Write failing test `test_goal_state_round_trips_metric_fields`:

```python
from hermes_cli.goals import GoalState

def test_goal_state_round_trips_metric_fields():
    s = GoalState(
        goal="Improve pass rate",
        metric_key="pass_rate",
        acceptance_criterion="pass_rate >= 0.9",
        last_metric_value=0.85,
    )
    raw = s.to_json()
    back = GoalState.from_json(raw)
    assert back.metric_key == "pass_rate"
    assert back.acceptance_criterion == "pass_rate >= 0.9"
    assert back.last_metric_value == 0.85

def test_goal_state_metric_fields_default_none():
    s = GoalState(goal="freeform goal")
    assert s.metric_key is None
    assert s.acceptance_criterion is None
    assert s.last_metric_value is None
```

- [ ] Run, expect `TypeError: unexpected keyword 'metric_key'` (or AttributeError).
- [ ] Add to `GoalState`:

```python
    metric_key: Optional[str] = None
    acceptance_criterion: Optional[str] = None
    last_metric_value: Optional[float] = None
```

- [ ] Update `from_json` to read those keys defensively:

```python
            metric_key=data.get("metric_key"),
            acceptance_criterion=data.get("acceptance_criterion"),
            last_metric_value=data.get("last_metric_value"),
```

- [ ] Re-run; expect green.
- [ ] Commit: `feat(goals): metric_key + acceptance_criterion + last_metric_value on GoalState`.

### Task B2 — Deterministic judge when metric is set

**Files:**
- Modify: `hermes_cli/goals.py` — new helper `_metric_verdict(state, response)`; `evaluate_after_turn` consults it before falling through to LLM judge
- Modify: `tests/agent/test_goals.py` — verdict precedence tests

**Steps:**

- [ ] Failing tests:

```python
import pytest
from hermes_cli.goals import GoalState, _extract_metric_value, _metric_verdict

class TestExtractMetricValue:
    def test_simple(self):
        assert _extract_metric_value("METRIC: pass_rate=0.85", "pass_rate") == 0.85

    def test_with_status_suffix(self):
        v = _extract_metric_value("METRIC: pass_rate=0.7 STATUS: improved", "pass_rate")
        assert v == 0.7

    def test_missing_returns_none(self):
        assert _extract_metric_value("no metric here", "pass_rate") is None

    def test_wrong_key_returns_none(self):
        assert _extract_metric_value("METRIC: latency_ms=200", "pass_rate") is None


class TestMetricVerdict:
    def test_done_when_criterion_met(self):
        s = GoalState(
            goal="t", metric_key="pass_rate",
            acceptance_criterion="pass_rate >= 0.9",
        )
        verdict, reason, value = _metric_verdict(s, "METRIC: pass_rate=0.95")
        assert verdict == "done"
        assert "0.95" in reason
        assert value == 0.95

    def test_continue_when_criterion_unmet(self):
        s = GoalState(
            goal="t", metric_key="pass_rate",
            acceptance_criterion="pass_rate >= 0.9",
        )
        verdict, reason, value = _metric_verdict(s, "METRIC: pass_rate=0.5")
        assert verdict == "continue"
        assert value == 0.5

    def test_skipped_when_no_metric_in_response(self):
        s = GoalState(
            goal="t", metric_key="pass_rate",
            acceptance_criterion="pass_rate >= 0.9",
        )
        verdict, reason, value = _metric_verdict(s, "no metric line at all")
        assert verdict == "continue"
        assert value is None

    def test_skipped_when_criterion_unparseable(self):
        s = GoalState(
            goal="t", metric_key="pass_rate",
            acceptance_criterion="looks good to me",
        )
        verdict, reason, value = _metric_verdict(s, "METRIC: pass_rate=1.0")
        assert verdict is None  # falls through to LLM judge
        assert value == 1.0     # but we still recorded the value
```

- [ ] Run, expect ImportError.
- [ ] Implement in `hermes_cli/goals.py`:

```python
import re

_METRIC_LINE_RE = re.compile(
    r"METRIC:\s*(\w[\w.]*)\s*=\s*([-+]?\d+(?:\.\d+)?)"
)


def _extract_metric_value(response: str, metric_key: str) -> Optional[float]:
    """Find the latest 'METRIC: <key>=<value>' line whose key matches.
    Last match wins so the most recent worker emission is authoritative."""
    if not response or not metric_key:
        return None
    last: Optional[float] = None
    for match in _METRIC_LINE_RE.finditer(response):
        if match.group(1) == metric_key:
            try:
                last = float(match.group(2))
            except ValueError:
                continue
    return last


def _metric_verdict(
    state: "GoalState",
    response: str,
) -> Tuple[Optional[str], str, Optional[float]]:
    """Deterministic judge for metric-aware goals.

    Returns (verdict, reason, observed_value):
      - verdict='done' if value crosses the parseable acceptance criterion
      - verdict='continue' if value is observed but below threshold
      - verdict=None if criterion is qualitative — caller falls back to LLM judge
      - observed_value is whatever was parsed (None if no METRIC line)
    """
    from agent.research.supervisor import _parse_acceptance_criterion

    if not state.metric_key or not state.acceptance_criterion:
        return None, "metric_key/acceptance_criterion not set", None

    test = _parse_acceptance_criterion(state.acceptance_criterion)
    if test is None:
        # qualitative criterion → defer to LLM judge but still record value
        value = _extract_metric_value(response, state.metric_key)
        return None, "criterion not parseable; deferring to LLM judge", value

    value = _extract_metric_value(response, state.metric_key)
    if value is None:
        return "continue", f"no {state.metric_key} reported in response", None

    if test(value):
        return "done", f"{state.metric_key}={value} satisfies '{state.acceptance_criterion}'", value
    return "continue", f"{state.metric_key}={value} does not satisfy '{state.acceptance_criterion}'", value
```

- [ ] Run unit tests, expect green.
- [ ] Wire `_metric_verdict` into `GoalManager.evaluate_after_turn`:
  - Locate `evaluate_after_turn` (around line 461).
  - Before calling `judge_goal(goal_text, response, ...)`, attempt the metric path:
    ```python
    metric_verdict, metric_reason, metric_value = _metric_verdict(self._state, last_response)
    if metric_value is not None:
        self._state.last_metric_value = metric_value
    if metric_verdict is not None:
        # Bypass the LLM judge entirely.
        verdict = metric_verdict
        reason = metric_reason
        parse_failed = False
    else:
        verdict, reason, parse_failed = judge_goal(goal_text, last_response, timeout=...)
    ```
  - Persist `last_metric_value` via the existing `save_goal` call.
- [ ] Add an `evaluate_after_turn` integration test:

```python
def test_evaluate_after_turn_metric_path_done(monkeypatch):
    # No LLM judge involvement at all.
    called = {"judge": False}
    def fake_judge(*a, **kw):
        called["judge"] = True
        return "continue", "should not be called", False
    monkeypatch.setattr("hermes_cli.goals.judge_goal", fake_judge)

    mgr = GoalManager(session_id="test-metric-done")
    mgr.set("Improve pass rate", metric_key="pass_rate",
            acceptance_criterion="pass_rate >= 0.9")
    mgr.evaluate_after_turn("METRIC: pass_rate=0.95 NOTES: t")
    assert mgr.state.status == "done"
    assert mgr.state.last_metric_value == 0.95
    assert called["judge"] is False
```

- [ ] Run; expect green.
- [ ] Commit: `feat(goals): deterministic metric-aware judge bypass`.

### Task B3 — `GoalManager.set()` accepts metric kwargs

**Files:** `hermes_cli/goals.py`, `tests/agent/test_goals.py`

**Steps:**

- [ ] Failing test:

```python
def test_set_with_metric_kwargs_persists(tmp_path):
    mgr = GoalManager(session_id="test-set-metric")
    s = mgr.set(
        "Improve test pass rate",
        metric_key="pass_rate",
        acceptance_criterion="pass_rate >= 0.95",
    )
    assert s.metric_key == "pass_rate"
    assert s.acceptance_criterion == "pass_rate >= 0.95"
```

- [ ] Update `GoalManager.set()` signature:

```python
    def set(
        self,
        goal: str,
        *,
        max_turns: Optional[int] = None,
        metric_key: Optional[str] = None,
        acceptance_criterion: Optional[str] = None,
    ) -> GoalState:
```

Pass them through to the new `GoalState(...)` instantiation inside `set()`.

- [ ] Run; expect green. Commit: `feat(goals): GoalManager.set() accepts metric_key + acceptance_criterion`.

### Task B4 — `/goal` CLI / chat command parses optional metric syntax

**Files:** `gateway/run.py` (search for `def _handle_goal_command` or similar — the dispatcher around line 8610), or `hermes_cli/main.py` if the CLI has its own entry.

**Steps:**

- [ ] Locate the existing parser that turns `/goal <text>` into `GoalManager.set(text)`.
- [ ] Add a small parse step: if the trailing portion of the text matches `<word> <op> <number>` (using `_parse_acceptance_criterion`), split off the criterion. Keep everything before it as the goal text and pass the criterion separately:

```python
def _split_goal_text_and_criterion(raw: str) -> tuple[str, Optional[str], Optional[str]]:
    """Try to split a goal string like 'Improve pass rate, pass_rate >= 0.9'
    into (goal_text, metric_key, criterion). Returns (raw, None, None) when
    no parseable criterion is present at the tail."""
    from agent.research.supervisor import _parse_acceptance_criterion
    # Trailing pattern: "<key> <op> <number>"
    m = re.search(
        r",?\s*(\w[\w.]*)\s*(>=|<=|>|<|==)\s*([-+]?\d+(?:\.\d+)?)\s*$",
        raw,
    )
    if not m:
        return raw, None, None
    criterion = f"{m.group(1)} {m.group(2)} {m.group(3)}"
    if _parse_acceptance_criterion(criterion) is None:
        return raw, None, None
    return raw[: m.start()].rstrip(", \t"), m.group(1), criterion
```

- [ ] Add a test in `tests/agent/test_goals.py`:

```python
def test_split_goal_text_and_criterion_with_trailing_metric():
    from hermes_cli.goals import _split_goal_text_and_criterion
    text, key, crit = _split_goal_text_and_criterion(
        "Improve test pass rate, pass_rate >= 0.95"
    )
    assert text == "Improve test pass rate"
    assert key == "pass_rate"
    assert crit == "pass_rate >= 0.95"

def test_split_goal_text_and_criterion_freeform_returns_unchanged():
    from hermes_cli.goals import _split_goal_text_and_criterion
    text, key, crit = _split_goal_text_and_criterion("Just write a haiku")
    assert text == "Just write a haiku"
    assert key is None
    assert crit is None
```

- [ ] Wire the splitter in the existing `/goal` command handler so when a metric tail is detected it calls `mgr.set(text, metric_key=key, acceptance_criterion=crit)`.
- [ ] Run targeted goal tests; expect green.
- [ ] Commit: `feat(gateway): /goal parses optional 'metric op value' tail into criterion`.

### Task B5 — Phase B end-to-end validation script

**Files:** `research-cases/goal-metric-aware/run_validation.py` (untracked, like `research-cases/daemoncraft-classifier/`).

**Steps:**

- [ ] Script that:
  1. Constructs a `GoalManager` with `session_id="phase-b-test"`.
  2. Calls `mgr.set("Improve pass rate", metric_key="pass_rate", acceptance_criterion="pass_rate >= 0.9")`.
  3. Calls `mgr.evaluate_after_turn("METRIC: pass_rate=0.5 NOTES: baseline")` → asserts status active, value 0.5.
  4. Calls `mgr.evaluate_after_turn("METRIC: pass_rate=0.95 NOTES: hit acceptance")` → asserts status done, value 0.95.
  5. Asserts the LLM judge was never called (monkeypatching it to raise).
- [ ] Run; expect all green prints.
- [ ] No commit needed — `research-cases/` is gitignored.

---

## Phase C — `run_research(auto_specify=True)`

### Task C1 — Helper that calls the kanban specifier model

**Files:**
- Create: `agent/research/auto_specify.py`
- Test: `tests/agent/research/test_auto_specify.py`

**Steps:**

- [ ] Failing tests (mock the auxiliary client to return a structured spec):

```python
from unittest.mock import MagicMock, patch
from agent.research.auto_specify import auto_specify_topic

def _fake_aux_response(content: str):
    resp = MagicMock()
    resp.choices = [MagicMock()]
    resp.choices[0].message.content = content
    return resp


def test_auto_specify_returns_structured_spec():
    fake_client = MagicMock()
    fake_client.chat.completions.create.return_value = _fake_aux_response(
        '```json\n{"deliverable": "Python function classify(payload)",'
        '"metric_key": "pass_rate", "metric_direction": "maximize",'
        '"task_type": "code", "evaluation_mode": "self_report"}\n```'
    )
    with patch("agent.research.auto_specify.get_text_auxiliary_client",
               return_value=(fake_client, "test-model")):
        out = auto_specify_topic("classify daemoncraft heartbeat events")
    assert out["deliverable"] == "Python function classify(payload)"
    assert out["metric_key"] == "pass_rate"
    assert out["task_type"] == "code"


def test_auto_specify_returns_none_on_aux_error():
    with patch("agent.research.auto_specify.get_text_auxiliary_client",
               side_effect=RuntimeError("no aux configured")):
        assert auto_specify_topic("vague topic") is None


def test_auto_specify_returns_none_on_unparseable_output():
    fake_client = MagicMock()
    fake_client.chat.completions.create.return_value = _fake_aux_response(
        "I think you should make a thing that scores high"
    )
    with patch("agent.research.auto_specify.get_text_auxiliary_client",
               return_value=(fake_client, "test-model")):
        assert auto_specify_topic("vague") is None
```

- [ ] Implement `agent/research/auto_specify.py`:

```python
"""Auto-specify: flesh out a vague research topic via the kanban triage
auxiliary LLM. Used by run_research(..., auto_specify=True) so callers can
pass a one-line topic and get back a structured TaskSpec scaffold."""
from __future__ import annotations

import json
import logging
import re
from typing import Optional

logger = logging.getLogger(__name__)

_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)

_SPECIFY_SYSTEM = (
    "You are a research-task specifier. Given a short, possibly vague topic, "
    "produce a JSON object with these keys: deliverable, metric_key, "
    "metric_direction (maximize|minimize), task_type (code|search|research|"
    "generic), evaluation_mode (self_report|llm_judge), evaluation_prompt "
    "(only when evaluation_mode is llm_judge). Output ONLY the JSON object — "
    "no commentary, no fences."
)

_SPECIFY_USER_TEMPLATE = "Topic: {topic}\n\nProduce the JSON spec."


def get_text_auxiliary_client(role: str):
    """Indirection seam — patched in tests."""
    from agent.auxiliary_client import get_text_auxiliary_client as _impl
    return _impl(role)


def _extract_json_blob(raw: str) -> Optional[dict]:
    if not raw:
        return None
    stripped = _FENCE_RE.sub("", raw.strip())
    first = stripped.find("{")
    last = stripped.rfind("}")
    if first == -1 or last == -1 or last <= first:
        return None
    try:
        val = json.loads(stripped[first : last + 1])
    except (ValueError, json.JSONDecodeError):
        return None
    return val if isinstance(val, dict) else None


def auto_specify_topic(topic: str) -> Optional[dict]:
    """Return a dict with TaskSpec scaffolding fields, or None on any failure.

    Reuses the kanban triage_specifier auxiliary LLM role.
    """
    if not topic or not topic.strip():
        return None

    try:
        client, model = get_text_auxiliary_client("triage_specifier")
    except Exception as exc:
        logger.debug("auto_specify: aux client unavailable: %s", exc)
        return None
    if client is None or not model:
        return None

    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": _SPECIFY_SYSTEM},
                {"role": "user", "content": _SPECIFY_USER_TEMPLATE.format(topic=topic)},
            ],
            temperature=0,
        )
    except Exception as exc:
        logger.debug("auto_specify: aux call failed: %s", exc)
        return None

    try:
        content = resp.choices[0].message.content or ""
    except Exception:
        return None

    return _extract_json_blob(content)
```

- [ ] Run; expect green.
- [ ] Commit: `feat(autoresearch): auto_specify helper for vague topics`.

### Task C2 — Wire `auto_specify` into `run_research`

**Files:**
- Modify: `tools/research_tool.py` — schema + signature + body
- Test: `tests/agent/research/test_run_research_auto_specify.py`

**Steps:**

- [ ] Failing test:

```python
"""run_research(auto_specify=True) fills missing fields from a vague topic."""
from unittest.mock import MagicMock, patch
import json

def test_auto_specify_fills_missing_fields():
    fake_spec = {
        "deliverable": "Python classify(payload) function",
        "metric_key": "pass_rate",
        "metric_direction": "maximize",
        "task_type": "code",
        "evaluation_mode": "self_report",
    }

    captured = {}

    def fake_supervisor(**kwargs):
        sup = MagicMock()
        sup.run.return_value = MagicMock(
            results=[], best_result=None,
        )
        captured["sup"] = sup
        return sup

    with patch("agent.research.auto_specify.auto_specify_topic", return_value=fake_spec), \
         patch("tools.research_tool.ResearchSupervisor", side_effect=fake_supervisor):
        from tools.research_tool import run_research
        out = run_research(
            topic="classify daemoncraft heartbeat events",
            deliverable="",
            metric_key="",
            parent_agent=MagicMock(),
            auto_specify=True,
            disable_evolution_overlay=True,
        )
        body = json.loads(out)
        # The supervisor must have been built and run.
        assert captured["sup"].run.called
        # The first positional arg of run() is the TaskSpec; it should now
        # have deliverable + metric_key from auto_specify, not the empty input.
        spec_arg = captured["sup"].run.call_args.args[0]
        assert spec_arg.deliverable == "Python classify(payload) function"
        assert spec_arg.metric_key == "pass_rate"
```

- [ ] Add `auto_specify: bool = False` to `run_research` signature.
- [ ] Add to schema:

```python
            "auto_specify": {
                "type": "boolean",
                "description": (
                    "When true and deliverable/metric_key are empty, call "
                    "the kanban triage specifier auxiliary LLM to flesh "
                    "out the TaskSpec from the topic alone. Default: false."
                ),
                "default": False,
            },
```

- [ ] Add forwarding in registry handler.
- [ ] In `run_research` body, before `spec = TaskSpec(...)`:

```python
    if auto_specify and (not deliverable or not metric_key):
        from agent.research.auto_specify import auto_specify_topic
        scaffold = auto_specify_topic(topic)
        if scaffold:
            deliverable = deliverable or scaffold.get("deliverable", "")
            metric_key = metric_key or scaffold.get("metric_key", "")
            metric_direction = metric_direction or scaffold.get("metric_direction", "maximize")
            task_type = task_type if task_type != "generic" else scaffold.get("task_type", "generic")
            evaluation_mode = evaluation_mode or scaffold.get("evaluation_mode", "self_report")
            evaluation_prompt = evaluation_prompt or scaffold.get("evaluation_prompt", "")
        else:
            logger.warning("auto_specify failed to flesh out topic %r; running with original args", topic)
```

- [ ] Run; expect green.
- [ ] Commit: `feat(autoresearch): run_research(auto_specify=True) for vague topics`.

### Task C3 — Phase C end-to-end validation script

**Files:** `research-cases/auto-specify/run_validation.py` (untracked).

- [ ] Script with a real (or stubbed) auxiliary client that calls `auto_specify_topic("classify daemoncraft heartbeat events")` and prints the structured spec.
- [ ] Run; expect printed dict containing `metric_key`, `deliverable`, etc.

---

## Self-Review

**Spec coverage**

- GoalState gains 3 metric fields → B1 ✓
- Deterministic judge when metric is set, falls through otherwise → B2 ✓
- GoalManager.set() accepts metric kwargs → B3 ✓
- /goal command parses metric tail → B4 ✓
- Phase B validation harness → B5 ✓
- auto_specify helper using triage_specifier aux LLM role → C1 ✓
- run_research wires auto_specify when fields missing → C2 ✓
- Phase C validation harness → C3 ✓

**Out of scope (explicit)**

- `/goal` automatically spawning `run_research` (full bidirectional Level 3) — would require gateway-side wiring. Deferred.
- Kanban task auto-creation when /goal has metric — also deferred (caller still creates the task).
- `/specify` directly editing the TaskSpec on a kanban task — deferred; auto_specify path is callable-only for now.

**Risk areas**

- B2 changes goal-loop behavior on every turn. The metric path must not regress freeform goals: when `metric_key` is None, `_metric_verdict` returns `(None, ..., None)` and the LLM-judge path runs unchanged. The new tests lock that contract.
- B4 modifies a user-facing command parser. The splitter must NOT trigger on freeform text that happens to contain `>` or `<` (e.g. "make X better than Y"). The splitter only runs `_parse_acceptance_criterion` on the matched tail, and that parser is strict (`<word> <op> <number>` only).
- C2 changes `run_research` semantics — silent field-filling. To make this safe, auto_specify is opt-in (`auto_specify=False` default) and only fills empty fields; it never overrides explicit caller values.
