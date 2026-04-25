# Hermes AutoResearch

## What This Is

Hermes AutoResearch is the **Karpathy inner loop** for autonomous experimentation inside Hermes. Given a research topic, it runs a baseline experiment, proposes improvements via LLM, executes them through `delegate_task`, keeps improvements and discards regressions, and records structured learnings.

The architecture is **desacoplada**: long-running research loops execute as independent OS processes with durable checkpoints, so the parent agent does not burn iteration budget or die to timeouts.

## Quick Start

### Running a Research Job (Detached)

```bash
# Create a job spec JSON
python -c '
import json
spec = {
    "job_id": "my-research",
    "job_dir": "/home/user/.hermes/research-jobs/my-research",
    "model": "kimi-for-coding",
    "provider": "kimi-coding",
    "topic": "Analyze WebAssembly adoption in 2025",
    "deliverable": "Ranked list of relevant papers with abstracts",
    "metric_key": "completeness_score",
    "metric_direction": "maximize",
    "task_type": "research",
    "max_iterations": 3,
}
json.dump(spec, open("/home/user/.hermes/research-jobs/my-research/job.json", "w"))
'

# Launch detached runner
source venv/bin/activate
HERMES_YOLO_MODE=1 python -m agent.research.job_runner \
    /home/user/.hermes/research-jobs/my-research/job.json
```

### From Python (Synchronous)

```python
from agent.research.supervisor import ResearchSupervisor, TaskSpec
from pathlib import Path

spec = TaskSpec(
    topic="Analyze WebAssembly adoption in 2025",
    deliverable="Ranked list of relevant papers with abstracts",
    metric_key="completeness_score",
    metric_direction="maximize",
    task_type="research",
)

supervisor = ResearchSupervisor(parent_agent=agent, workspace=Path("research-workspace"))
history = supervisor.run(
    spec,
    initial_attempt="",
    run_id="run-001",
    max_iterations=3,
    llm=agent.llm_client,
)
```

## Architecture

```
Parent Agent / CLI
       │
       ▼
┌─────────────────────────┐
│  research/job_runner.py│  ← Detached OS process
│  (entrypoint)           │
└─────────────────────────┘
       │
       ▼
┌─────────────────────────┐
│   ResearchSupervisor    │  ← Karpathy loop orchestrator
│   • TaskSpec            │
│   • run()               │
│   • _observe()          │
│   • _checkpoint()       │
└─────────────────────────┘
       │
       ▼
┌─────────────────────────┐     ┌─────────────────────────┐
│    delegate_task        │────▶│      Worker Subagent    │
│    (per iteration)      │     │  • Reads task_brief.md  │
└─────────────────────────┘     │  • Writes attempt.md    │
                                │  • Writes results.json  │
                                │  • Reports metric       │
                                └─────────────────────────┘
```

## Project Structure

```
agent/
├── research/job_runner.py     # Detached entrypoint: builds AIAgent, calls run_research
├── research/supervisor.py     # ResearchSupervisor + TaskSpec + task briefs
├── research/runner.py         # ExperimentRunner + ExperimentHistory
├── research/metrics.py        # UniversalMetricParser
└── subdirectory_hints.py     # Progressive context discovery (cached)

tools/
├── research_tool.py          # run_research() public API
└── research_job_tool.py      # start_research_job, research_job_status, collect_research_job

~/.hermes/research-jobs/      # Job specs + checkpoints + logs
~/.hermes/research-workspace/ # Round artifacts (attempt.md, results.json, learnings.jsonl)
```

## The Karpathy Loop

```
Step 1: BASELINE      — Worker receives task brief + attempt file, produces deliverable
Step 2: METRIC        — UniversalMetricParser reads results.json / stdout
Step 3: JUDGE         — LLM judge scores deliverable (if evaluation_mode="llm_judge")
Step 4: OBSERVE       — Structured learning appended to learnings.jsonl
Step 5: CHECKPOINT    — history.json + checkpoint.json written to disk
Step 6: OPTIMIZE      — LLM proposes revised attempt based on history
Step 7: KEEP/DISCARD  — If metric improved: keep, else discard; iterate
```

## Task Types

| Type | Default Toolsets | Deliverable | Attempt File |
|------|-----------------|-------------|--------------|
| `code` | terminal, file | Python code | attempt.py |
| `search` | web, terminal, file | Search results | attempt.md |
| `research` | web, terminal, file | Synthesis | attempt.md |
| `generic` | terminal, file | Any text | attempt.md |

## Worker Contract

The worker receives:
- `task_brief.md` — Full instructions including think block, rules, tools available
- `attempt.py` or `attempt.md` — Current attempt to refine
- Environment variable `HERMES_YOLO_MODE=1` to skip command approval

The worker must produce:
- `results.json` with `{"<metric_key>": <value>}`
- Final line: `METRIC: <key>=<value> STATUS: improved|regressed|neutral NOTES: <one line>`

## Checkpoints and Recovery

After every round, the supervisor writes:

```
~/.hermes/research-jobs/<job_id>/
├── checkpoint.json   # {round, total_rounds, best_metric, updated_at}
├── history.json      # Full results array + best reference
├── runner.log        # Runner + supervisor logs
└── state.json        # {status, pid, started_at}
```

External monitors can read `checkpoint.json` without polling the process.

## Decision Guide

| Situation | Action |
|-----------|--------|
| Long-running research (>5 min) | Use `research/job_runner` detached |
| Quick experiment (<2 min) | Call `run_research()` directly |
| Need baseline only | Set `llm=None` in supervisor |
| Worker times out | `DelegateSandboxResult.timed_out=True`; loop continues |
| 3 consecutive non-improving | Runner stops early (or 1 if high baseline) |
| Want to inspect history | Read `history.json` from checkpoint dir |

## Performance Optimizations

| Optimization | File | Impact |
|-------------|------|--------|
| **Lock file** | `research/job_runner.py` | Prevents duplicate restarts (~16 min saved) |
| **Provider cache** | `auxiliary_client.py` | Caches `resolve_provider_client` (~14 calls → 1) |
| **Subdirectory hints cache** | `subdirectory_hints.py` | Caches hint loads per directory |
| **Aggressive early stop** | `research/supervisor.py` | Baseline ≥0.9 → stop after 1 non-improving iter |
| **LLM judge every iter** | `research/supervisor.py` | Objective scoring on all loops |

## Anti-Patterns

- **DO NOT** run `research/job_runner` in foreground without `timeout >= 300`
- **DO NOT** poll the process with `ps` / `tail` — read `checkpoint.json` instead
- **DO NOT** launch the same job twice — the lock file prevents this
- **DO NOT** delete `.runner.lock` manually — use `kill` on the process

## Metric Reporting (Worker Contract)

Workers must print metrics in one of these formats:

```
# Hermes format (preferred)
METRIC: accuracy=0.923 STATUS: improved NOTES: Adam lr=0.001 beat SGD baseline

# Standard key: value format
accuracy: 0.923
loss: 0.112
```

The `UniversalMetricParser` also reads `results.json` (structured) or `results.csv` if present in the round directory.

## Skills

Hermes AutoResearch skills are in `skills/autoresearch/` and are loaded automatically.
Domain-specific skills (ML, chemistry, biology) are in `skills/autoresearch/domain/`.
