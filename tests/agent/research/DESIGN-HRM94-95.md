# Design Document: HRM-94 & HRM-95

## Scope
- **Do NOT modify** `agent/research/supervisor.py` (owned by another worker).
- Touch only:
  - `agent/research/job_runner.py`
  - `agent/research/runner.py` (optional, for per-iteration timeout)
  - `tools/research_tool.py` (stale checker)
  - `tools/research_job_tool.py` (status action wiring)

---

## HRM-94 — Real Timeout with SIGTERM/SIGKILL

### Problem
`job_runner.py` currently runs the full `run_research` loop in the same Python
process. If a sub-agent or API call hangs, the job never terminates and
`state.json` never reflects a timeout.

### Proposed Solution
1. `job_runner.py` reads `timeout_sec` from the job spec (default 0 = unlimited).
2. When `timeout_sec > 0`, wrap the call to `run_research` in a
   `multiprocessing.Process` so the parent can monitor wall-clock time.
3. The parent waits `timeout_sec` for the child to finish.
4. On expiry:
   - `os.kill(child_pid, signal.SIGTERM)`
   - Wait 5 s (`child.join(timeout=5)`)
   - If still alive: `os.kill(child_pid, signal.SIGKILL)` + `child.join(timeout=1)`
   - Write `state.json` with `status="timeout"` and `error="Timed out after Xs"`.
5. If the child finishes normally, the parent reads `result.json` / `state.json`
   already written by the child and returns its exit code.

### Why multiprocessing?
- Python threads cannot be forcefully killed.
- `delegate_task` hangs inside API calls or subprocess tools; only a real OS
  signal can break a stuck syscall.
- Using a child process keeps the parent alive long enough to write the
  "timeout" state.

### Where to change
- `agent/research/job_runner.py`
  - Extract the body of `main()` (from agent build onward) into a
    `_run_research_child(spec_path)` helper that can be the `target` of
    `multiprocessing.Process`.
  - Add signal-safe cleanup in the child (`signal.SIGTERM` handler that writes
    state and exits gracefully).
  - Parent loop: `proc.start()` → `proc.join(timeout=timeout_sec)` → kill
    escalation if still alive.
- `tools/research_tool.py`
  - Add `timeout_sec: int = 0` to `run_research()` signature so the spec field
    can flow through without breaking existing callers.

### Tests (RED stubs)
- `tests/agent/research/test_job_runner_timeout.py`
  - `test_timeout_sec_forwarded_to_run_research`
  - `test_timeout_writes_state_timeout_on_expiry`
  - `test_timeout_sends_sigterm_then_sigkill`

---

## HRM-95 — Heartbeat / Stale Detection

### Problem
Detached jobs have no liveness probe. If the `job_runner` process dies
(OOM, `kill -9`, host reboot), `state.json` stays `"running"` forever.

### Proposed Solution
1. **Heartbeat writer** (`agent/research/job_runner.py`)
   - After writing `status="running"`, start a daemon thread that writes
     `<job_dir>/heartbeat` every 30 s.
   - File format: `{"ts": <unix_timestamp>, "pid": <os.getpid()>}`.
   - Stop the thread in the `finally` block before exiting.

2. **Stale checker** (`tools/research_tool.py`)
   - `check_research_stale(checkpoint_dir: str, stale_threshold_sec=90.0) -> bool`
   - Reads `heartbeat`, returns `True` if missing or `now - ts > threshold`.

3. **Status wiring** (`tools/research_job_tool.py`)
   - `_action_status()` calls `check_research_stale(str(job_dir))` when the
     current status is `"queued"` or `"running"`.
   - If stale, overwrite `state.json` with `status="stale"` and
     `stale_reason="no heartbeat for >90s"`.

### Why 30 s / 90 s?
- Same heartbeat cadence already used by `tools/delegate_tool.py`
  (`_HEARTBEAT_INTERVAL = 30`).
- 90 s = 3 missed heartbeats — tolerates one GC pause or slow disk write.

### Where to change
- `agent/research/job_runner.py`
  - Add `_write_heartbeat(job_dir)` and `_heartbeat_loop(job_dir, stop_event)`.
  - Start/stop the daemon thread around the research loop.
- `tools/research_tool.py`
  - Add `check_research_stale()` function.
- `tools/research_job_tool.py`
  - Import and call `check_research_stale` inside `_action_status`.

### Tests (RED stubs)
- `tests/agent/research/test_heartbeat_stale.py`
  - `test_heartbeat_file_created`
  - `test_heartbeat_updated_during_run`
  - `test_stale_when_no_heartbeat`
  - `test_stale_after_90s`
  - `test_not_stale_within_90s`
  - `test_status_marks_stale_when_heartbeat_missing`

---

## Open Questions
1. Should `runner.py` (`ExperimentRunner`) also enforce a per-iteration
   `time_budget_sec` timeout on `_delegate_fn`?  This is separate from the
   global HRM-94 timeout but could be added later without conflicting.
2. Should stale jobs be auto-resumable?  Out of scope for these tickets —
   resume logic lives in `research_job_tool.py` and already handles
   `"interrupted"` / `"failed"`; we may need to add `"stale"` to the allow-list.
