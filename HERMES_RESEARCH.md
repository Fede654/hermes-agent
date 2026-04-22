# Hermes AutoResearch

## What This Is

Hermes AutoResearch is the **Karpathy inner loop** for autonomous ML experimentation inside Hermes. Given a research topic, it runs a baseline experiment, proposes code improvements via LLM, executes them through `delegate_task`, keeps improvements and discards regressions, and records lessons via `EvolutionStore`.

It is **not** a 23-stage pipeline. It is a tight 5-step loop that runs entirely through Hermes infrastructure — no external CLI, no pip install, no git branches.

## Quick Start

```python
from agent.research_runner import ExperimentRunner, HermesExperimentConfig
from pathlib import Path

config = HermesExperimentConfig(
    metric_key="accuracy",
    metric_direction="maximize",
    time_budget_sec=300,
    max_iterations=5,
)

runner = ExperimentRunner(
    config=config,
    workspace=Path("artifacts/hermes-research-001"),
    delegate_fn=your_delegate_fn,       # wraps delegate_task
    lattice_comment_fn=your_comment_fn, # wraps lattice_comment
)

history = runner.run_loop(initial_code, run_id="run-001", llm=your_llm_client)
```

## The 5-Step Karpathy Loop

```
Step 1: HYPOTHESIZE     — Write program.md with experiment plan and metric target
Step 2: PROGRAM         — Generate initial experiment code (or load from disk)
Step 3: DELEGATE        — Spawn worker via delegate_task; worker reads program.md and runs code
Step 4: METRIC          — Parse worker output via UniversalMetricParser (JSON → CSV → stdout)
Step 5: KEEP/DISCARD    — If metric improved: keep (update best), else discard; iterate
```

## Project Structure (Hermes ports)

```
agent/
├── research_runner.py   # ExperimentRunner — the Karpathy loop
├── research_evolution.py# EvolutionStore — JSONL lessons, time-decay weighting
└── research_metrics.py  # UniversalMetricParser — JSON/CSV/stdout metric extraction

skills/autoresearch/
├── a-evolve/            # A-Evolve methodology skill
├── hypothesis-formulation/
├── literature-search/
├── scientific-visualization/
├── scientific-writing/
├── statistical-reporting/
└── domain/              # Domain-specific experiment skills (ML, chemistry, biology)

prompts/
└── autoresearch.yaml    # Prompt blocks: compute_budget, topic_constraint, code_generation

HERMES_RESEARCH.md       # This file — agent bootstrap
RESEARCH_AGENTS.md       # Worker agent contract
```

## Loop State Machine

Hermes uses Lattice task states instead of git branches:

| Loop State | Lattice Status | Meaning |
|-----------|---------------|---------|
| Worker running | `in_progress` | delegate_task active |
| Round complete, metric improved | comment posted | supervisor reads metric |
| Best result kept | (stays in_progress) | loop continues |
| Early stop / done | `done` via `lattice complete` | experiment accepted |
| Discarded round | comment posted | loop continues with next iteration |

## Decision Guide

| Situation | Action |
|-----------|--------|
| Have a clear research topic | Write `program.md`, call `run_loop()` with `llm=` set |
| Want baseline only (no LLM improvement) | Call `run_loop()` with `llm=None` |
| Worker times out | `DelegateSandboxResult.timed_out=True`; runner records error, continues loop |
| 3 consecutive non-improving iterations | Runner stops early, posts Lattice comment |
| Want to persist lessons | Use `EvolutionStore.append_many()` after each round |
| Want to inspect history | `ExperimentRunner.history.to_dict()` or `save_history(path)` |

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

Evolution artifacts go in `skills/autoresearch/evolved/`.
