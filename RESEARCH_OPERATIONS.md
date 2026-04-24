# Hermes AutoResearch — Operations Guide

> This document captures the operational procedures, anti-patterns, and performance characteristics discovered during the development and validation of the Hermes AutoResearch orchestration layer.

## Launching a Research Job

### Method 1: Detached Runner (Recommended for >2 min tasks)

Create a job spec JSON and launch via `research_job_runner`:

```json
{
  "job_id": "unique-job-id",
  "job_dir": "/home/user/.hermes/research-jobs/unique-job-id",
  "model": "kimi-for-coding",
  "provider": "kimi-coding",
  "topic": "Your research topic here",
  "deliverable": "What the worker must produce",
  "metric_key": "completeness_score",
  "metric_direction": "maximize",
  "task_type": "research",
  "max_iterations": 3,
  "env": {"HERMES_YOLO_MODE": "1"}
}
```

Launch:
```bash
cd /path/to/hermes-agent
source venv/bin/activate
HERMES_YOLO_MODE=1 python -m agent.research_job_runner /path/to/job.json
```

### Method 2: Background Process (Non-blocking)

```bash
HERMES_YOLO_MODE=1 python -m agent.research_job_runner /path/to/job.json &
```

The runner creates a `.runner.lock` file atomically. If the job is already running, it exits with code 2.

### Method 3: Direct Python API (Blocking)

```python
from agent.research_supervisor import ResearchSupervisor, TaskSpec
from pathlib import Path

spec = TaskSpec(
    topic="...",
    deliverable="...",
    metric_key="completeness_score",
    metric_direction="maximize",
    task_type="research",
)

supervisor = ResearchSupervisor(parent_agent=agent)
history = supervisor.run(spec, initial_attempt="", run_id="run-001", max_iterations=3, llm=llm_client)
```

## Monitoring Progress

### Passive Monitoring (Recommended)

Read checkpoint files without polling the process:

```bash
# Quick status
cat ~/.hermes/research-jobs/<job_id>/checkpoint.json

# Full history
cat ~/.hermes/research-jobs/<job_id>/history.json

# Live log
tail -f ~/.hermes/research-jobs/<job_id>/runner.log
```

### File Structure

```
~/.hermes/research-jobs/<job_id>/
├── job.json          # Original spec
├── .runner.lock      # PID lock (prevents duplicate runs)
├── state.json        # {status, pid, started_at}
├── checkpoint.json   # {round, total_rounds, best_metric}
├── history.json      # Full results array + best reference
├── result.json       # Final result (appears on completion)
└── runner.log        # Runner + supervisor logs
```

## Anti-Patterns and Fixes

| Anti-Pattern | Why It Fails | Fix |
|-------------|-------------|-----|
| Foreground run with default timeout (60s) | MCP init takes 30-60s; runner killed before loop starts | Use `timeout=300` minimum, or background launch |
| Active process polling (`ps`, `find`, `tail` in loop) | Wastes iterations, creates noise | Read `checkpoint.json` or `history.json` passively |
| Deleting logs and retrying identically | Same failure repeats, no learning | Change timeout or use background mode |
| Launching same job twice | Double resource usage, conflicting checkpoints | Lock file prevents this; check `.runner.lock` |
| No `terminal` in default toolsets | Worker cannot execute code even if brief says it can | `_DEFAULT_TOOLSETS["research"] = ["web", "terminal", "file"]` |
| XML `<function_calls>` from worker | kimi-coding generates XML instead of JSON tools | Add anti-XML guard to task brief |
| Worker without `HERMES_YOLO_MODE` | Worker stalls waiting for command approval | Set `HERMES_YOLO_MODE=1` in env or job spec |

## Performance Baselines

Measured on kimi-for-coding via kimi-coding provider:

| Metric | Value | Notes |
|--------|-------|-------|
| MCP init time | ~30-60s | 3 MCP servers (ia-bridge, lattice, obsidian) |
| Init-to-first-checkpoint (simple) | ~30s | smoke-test with minimal topic |
| Init-to-first-checkpoint (complex) | ~300s | Benchmark with multi-step worker |
| Iteration time (research task) | ~290-350s | Includes worker execution + judge |
| Provider resolution (cached) | ~0s | Cache hit after first call |
| Provider resolution (uncached) | ~1-2s | Auth resolution + client build |
| Subdirectory hints (cached) | ~0s | Per-directory cache |
| Subdirectory hints (uncached) | ~50-100ms | Disk read + scan |

## Early Stop Behavior

| Baseline | Early Stop Limit | Min Delta | Rationale |
|----------|-----------------|-----------|-----------|
| < 0.9 (maximize) or > 0.1 (minimize) | 3 iterations | 0.0 | Standard exploration |
| ≥ 0.9 (maximize) or ≤ 0.1 (minimize) | 1 iteration | 0.05 | Aggressive stop for high baselines |

## Recovery Scenarios

### Scenario: Job appears stuck

1. Check `checkpoint.json` — has `round` advanced?
2. Check `runner.log` — are there recent `Omitting temperature` lines?
3. If log is stale >5 min, process may be waiting on API
4. Do NOT delete `.runner.lock` — kill the process instead: `kill $(cat .runner.lock)`

### Scenario: Job crashed

1. Read `runner.log` for traceback
2. Fix the issue (e.g., missing field in job.json)
3. Delete `.runner.lock` if stale
4. Relaunch

### Scenario: Want to resume from checkpoint

Current implementation does not support automatic resume from `checkpoint.json`. To resume:
1. Read `history.json` to find the best artifact
2. Create a new job spec with `initial_attempt` set to the best artifact content
3. Launch as new job

## LLM Judge

The judge runs on **every iteration** when `evaluation_mode="llm_judge"`.

- Skipping iterations risks accepting worker-inflated self-reported scores
- Judge latency: ~5-15s per evaluation (one API call)
- Judge prompt is in `_score_with_llm_judge()` — customizable via `evaluation_prompt` in TaskSpec

## Task Types and Toolsets

| Type | Default Toolsets | Use When |
|------|-----------------|----------|
| `code` | terminal, file | Writing/running Python code |
| `search` | web, terminal, file | Web research, data collection |
| `research` | web, terminal, file | Synthesis, analysis, reporting |
| `generic` | terminal, file | Any custom task |

Override with `worker_toolsets` parameter in `supervisor.run()`.

## Environment Variables

| Variable | Effect |
|----------|--------|
| `HERMES_YOLO_MODE=1` | Skip command approval (required for workers) |
| `DELEGATION_MAX_CONCURRENT_CHILDREN=3` | Parallel workers (default 3) |

## Lattice Integration (Optional)

If you pass `lattice_task_id` when starting a research job, the supervisor will post round-by-round progress comments to that Lattice task. This requires:

1. Lattice initialized at `~/.hermes/org/.lattice/` (run `lattice init` in `~/.hermes/org` if missing)
2. The target task ID must exist in that Lattice database

If Lattice is not available, the research job still runs normally — only the progress comments are skipped. Check `runner.log` for "Lattice comment failed" warnings if you expected comments but don't see them.

### Verifying Lattice availability

```bash
# Quick check
ls ~/.hermes/org/.lattice/ids.json

# If missing, initialize:
cd ~/.hermes/org && lattice init
```

## Git Workflow for AutoResearch Changes

All changes to the autoresearch stack are committed to branch `feat/hermes-autoresearch-upstream`:

```bash
git log --oneline feat/hermes-autoresearch-upstream
```

Key commits:
- `cc929c0a` — Add research_job orchestration for long-running loops
- `4d64d568` — Include terminal in research/search default toolsets
- `0da707a3` — Add Tools Available + anti-XML guard to task briefs
- `f08dc63c` — Correct sandbox messaging and add partial recovery
- `8e68e23d` — Performance optimizations (lock, cache, early stop)
- `61c6e994` — Restore LLM judge on every iteration
