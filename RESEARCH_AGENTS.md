# Hermes AutoResearch — Worker Agent Contract

## Overview

You are a **Hermes AutoResearch worker**. You receive a goal string and a working directory from the supervisor. Your job is to run the experiment described in `program.md` and report a metric in the required format.

You are NOT responsible for the loop logic (keep/discard, iteration, LLM code improvement). That is handled by the supervisor via `ExperimentRunner`.

## Inputs

| Input | Source | Description |
|-------|--------|-------------|
| Working directory | `delegate_task` argument | Directory containing `program.md` |
| Goal string | `delegate_task` argument | Includes metric key and output format |
| `program.md` | Read from working directory | Experiment plan, code, metric target |

## Your Steps

1. **Read `program.md`** — understand the experiment goal, algorithm, and metric key
2. **Set up experiment files** — write Python code to the working directory if not already present
3. **Run the experiment** — execute the code, collect results
4. **Write `results.json`** if possible (structured output, preferred by `UniversalMetricParser`)
5. **Print metric line** — required for fallback stdout parsing
6. **Report status** — include STATUS word in output

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
METRIC: accuracy=0.923 STATUS: improved NOTES: Adam lr=0.001, 50 epochs, converged at iter 38
```

The metric key must match the key specified in the goal string (e.g., `primary_metric`, `accuracy`, `loss`).

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

## Anti-Patterns

Do NOT:
- Use subprocess, os.system, eval, exec, or shell escapes in experiment code
- Make network calls (experiments must be self-contained)
- Invent or fabricate metric values — measure real outcomes
- Run without a time guard (always implement elapsed-time check near 80% of budget)
- Print non-metric lines as `key: value` (they will be parsed as metrics)
