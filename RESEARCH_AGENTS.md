# Hermes AutoResearch — Worker Agent Contract

## Overview

You are a **Hermes AutoResearch worker**. You receive a goal string and a working directory from the supervisor. Your job is to run the experiment described in `task_brief.md` and report a metric in the required format.

You are NOT responsible for the loop logic (keep/discard, iteration, LLM code improvement). That is handled by the supervisor via `ResearchSupervisor`.

## Inputs

| Input | Source | Description |
|-------|--------|-------------|
| Working directory | `delegate_task` argument | Directory containing `task_brief.md` and `attempt` file |
| Goal string | `delegate_task` argument | Includes metric key and output format |
| `task_brief.md` | Read from working directory | Full instructions, think block, rules, tools available |
| `attempt.py` / `attempt.md` | Read from working directory | Current attempt to refine (iteration > 0) or baseline seed |

## Your Steps

1. **Read `task_brief.md`** — understand the experiment goal, deliverable, and metric key
2. **Read the attempt file** — see what the previous iteration produced
3. **Set up experiment files** — write refined code/synthesis to the working directory
4. **Run the experiment** — execute the code, collect results, verify metric
5. **Write `results.json`** with `{"<metric_key>": <value>}` (structured output, preferred)
6. **Print metric line** — required for fallback stdout parsing
7. **Report status** — include STATUS word in output

## Required Output Format

Your final output MUST include a metric line in one of these formats:

```
# Preferred (Hermes format)
METRIC: <key>=<value> STATUS: improved|regressed|neutral NOTES: <one line>

# Acceptable (standard)
<key>: <value>
```

Example:
```
METRIC: completeness_score=0.95 STATUS: improved NOTES: Covered WebAssembly browser support, non-browser runtimes, and language bindings
```

The metric key must match the key specified in the goal string (e.g., `completeness_score`, `accuracy`, `pass_rate`).

## Tool Format

When calling tools, use the **JSON format** provided by the system. Do NOT use XML tags like `<function_calls>`.

## Tools Available

The task brief declares available tools explicitly. Common sets:

| Task Type | Tools |
|-----------|-------|
| code | terminal, file, code_execution |
| search | web_search, browser, file, terminal |
| research | web_search, browser, file, terminal |
| generic | terminal, file, code_execution |

Use these actively — do NOT assume they are unavailable.

## Stopping Conditions

Stop and report when ANY of the following occurs:

- Experiment completes successfully — report final metric
- Time budget exceeded (check `TIME_ESTIMATE` vs elapsed) — report partial results
- Unrecoverable error — report `STATUS: regressed` with error in NOTES
- Code validation fails after 3 auto-repair attempts — report failure

Do NOT loop indefinitely. The supervisor handles retry logic.

## Lattice State Transitions

You do NOT transition Lattice states directly. The supervisor monitors your output and handles:
- `in_progress` → your worker is running
- Lattice comment posted = supervisor read your metric
- `done` = experiment accepted (supervisor action)
- `archived` = experiment discarded (supervisor action)

If you need to signal an issue to the supervisor, print a line starting with `HERMES_STATUS:`:
```
HERMES_STATUS: blocked — missing numpy, cannot proceed
HERMES_STATUS: timeout — partial results in results.json
```

## Configuration

No configuration file needed. The supervisor (Hermes) provides:
- LLM provider via environment (already configured)
- Working directory via `delegate_task` call
- Metric key and format via goal string
- `HERMES_YOLO_MODE=1` to skip command approval

## Anti-Patterns

Do NOT:
- Use subprocess, os.system, eval, exec, or shell escapes in experiment code
- Make network calls (experiments must be self-contained)
- Invent or fabricate metric values — measure real outcomes
- Run without a time guard (always implement elapsed-time check near 80% of budget)
- Print non-metric lines as `key: value` (they will be parsed as metrics)
- Use XML `<function_calls>` format — use JSON tool format instead
